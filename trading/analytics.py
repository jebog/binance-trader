from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from config import (
    ATR_SL_MULT,
    CAPITAL,
    PAIR_SCORE_LOOKBACK,
    PAIR_SCORE_MIN_TRADES,
    TARGET_RISK_PCT,
    VOL_SIZING_ENABLED,
    VOL_SIZING_MAX,
    VOL_SIZING_MIN,
)
from trading.notify import send_telegram
from trading.signals import _estimate_sl_tp_pct


def _fg_regime(value: int) -> str:
    """Map a Fear & Greed value (0-100) to a named regime bucket."""
    if value < 20:
        return "extreme_fear"
    elif value < 30:
        return "fear"
    elif value < 50:
        return "neutral"
    elif value < 75:
        return "greed"
    else:
        return "extreme_greed"


def _check_fg_regime_change(fg_value: int, fg_class: str, old_regime: str) -> str:
    """Fire a Telegram alert if F&G has crossed into a new regime. Returns new regime."""
    new_regime = _fg_regime(fg_value)
    if new_regime == old_regime:
        return new_regime

    messages: dict[str, str] = {
        "extreme_fear": f"\U0001f534 *F&G: Extreme Fear* (`{fg_value}`)\nMODERATE signals are now *blocked*.",
        "fear":         f"\U0001f7e1 *F&G: Fear* (`{fg_value}`)\nEntered Fear zone (20\u201329).",
        "neutral":      f"\U0001f7e2 *F&G: Neutral* (`{fg_value}`)\nF&G recovering past the Fear zone.",
        "greed":        f"\u26a1 *F&G: Greed* (`{fg_value}`)\nMarket turning greedy \u2014 tighten risk.",
        "extreme_greed": f"\U0001f6a8 *F&G: Extreme Greed* (`{fg_value}`)\nConsider reducing exposure.",
    }
    msg = messages.get(new_regime, f"F&G regime changed to {new_regime} ({fg_value})")
    send_telegram(msg)
    print(f"  \U0001f4e1 F&G regime change: {old_regime} \u2192 {new_regime} ({fg_value} {fg_class})")
    return new_regime


def _escape_md(text: Any) -> str:
    """Escape Telegram Markdown special characters in arbitrary strings."""
    for ch in ("*", "_", "`", "[", "]"):
        text = str(text).replace(ch, "\\" + ch)
    return text


def _calc_capital(s: dict[str, Any], context: dict[str, Any]) -> float:
    """Central capital-sizing rule -- single source of truth."""
    if VOL_SIZING_ENABLED and ATR_SL_MULT > 0:
        sl_pct, _ = _estimate_sl_tp_pct(s)
        atr_pct = sl_pct / ATR_SL_MULT
        if atr_pct > 0:
            raw   = CAPITAL * TARGET_RISK_PCT / atr_pct
            sized = max(CAPITAL * VOL_SIZING_MIN, min(CAPITAL * VOL_SIZING_MAX, raw))
            if s["signal_strength"] == "EXTREME":
                sized = min(sized, CAPITAL * 0.5)
            return round(sized, 2)
    if s["signal_strength"] == "EXTREME":
        return CAPITAL / 2
    if s["signal_strength"] == "STRONG" and context["btc_rsi"] < 35:
        return CAPITAL / 2
    return CAPITAL


def _safe_fromisoformat(ts: str) -> datetime:
    try:
        return datetime.fromisoformat(ts)
    except Exception:
        return datetime.min


def _compute_perf_stats(trades: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute rolling 30-day performance stats from closed trades."""
    cutoff = datetime.now() - timedelta(days=30)
    closed = [
        t for t in trades
        if t.get("status") in ("tp_hit", "sl_hit", "timeout")
        and t.get("exit_time")
        and t.get("pnl_pct") is not None
        and _safe_fromisoformat(t["exit_time"]) >= cutoff
    ]
    if not closed:
        return {}

    pnls = [t["pnl_pct"] for t in closed]
    wins   = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]

    mean_pnl = sum(pnls) / len(pnls)
    variance = (sum((p - mean_pnl) ** 2 for p in pnls) / (len(pnls) - 1)
                if len(pnls) > 1 else 0.0)
    std_pnl  = variance ** 0.5
    sharpe = (mean_pnl / std_pnl) if std_pnl > 0 else 0.0

    gross_profit = sum(wins)   if wins   else 0.0
    gross_loss   = abs(sum(losses)) if losses else 0.0
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float("inf")

    max_consec = cur = 0
    for p in pnls:
        if p < 0:
            cur += 1
            max_consec = max(max_consec, cur)
        else:
            cur = 0

    tier_stats: dict[str, dict[str, int]] = {}
    for t in closed:
        tier = t.get("signal_strength", "UNKNOWN")
        tier_stats.setdefault(tier, {"wins": 0, "total": 0})
        tier_stats[tier]["total"] += 1
        if t.get("pnl_pct", 0) > 0:
            tier_stats[tier]["wins"] += 1

    return {
        "count":             len(closed),
        "win_rate":          len(wins) / len(closed),
        "sharpe":            sharpe,
        "profit_factor":     profit_factor,
        "max_consec_losses": max_consec,
        "tier_stats":        tier_stats,
    }


def _send_daily_digest(state: dict[str, Any]) -> None:
    """Send an 8am morning digest summarising the last 7 days of trading."""
    now       = datetime.now()
    cutoff    = now - timedelta(days=7)
    trades    = state.get("trades") or []
    portfolio = state.get("portfolio")
    fg_cache  = state.get("fg_cache") or {}
    fg_val    = fg_cache.get("value")
    fg_str    = f"\n*Fear & Greed:* `{fg_val}`" if fg_val is not None else ""

    window = []
    for t in trades:
        if t.get("status") not in ("tp_hit", "sl_hit"):
            continue
        try:
            ts = datetime.fromisoformat(t.get("exit_time") or t.get("time", ""))
            if ts >= cutoff:
                window.append(t)
        except Exception:
            pass

    wins   = [t for t in window if t.get("status") == "tp_hit"]
    losses = [t for t in window if t.get("status") == "sl_hit"]
    net_usdc = sum(
        (t.get("pnl_pct") or 0) / 100 * (t.get("capital") or CAPITAL)
        for t in window
    )
    win_usdc  = sum((t.get("pnl_pct") or 0) / 100 * (t.get("capital") or CAPITAL) for t in wins)
    loss_usdc = sum((t.get("pnl_pct") or 0) / 100 * (t.get("capital") or CAPITAL) for t in losses)
    deployed  = sum(t.get("capital") or CAPITAL for t in window) or CAPITAL
    net_pct   = net_usdc / deployed * 100 if deployed else 0.0

    open_trades = [t for t in trades if t.get("status") == "open"]
    open_lines  = []
    for t in open_trades:
        sym   = t.get("symbol", "?")
        entry = t.get("entry", 0)
        try:
            held_h = (now - datetime.fromisoformat(t["time"])).total_seconds() / 3600
            held_s = f"{held_h:.0f}h" if held_h < 24 else f"{held_h/24:.1f}d"
        except Exception:
            held_s = "?"
        open_lines.append(f"  `{sym}`  entry `${entry:.4f}`  held `{held_s}`")

    portfolio_line = (
        f"\n*Portfolio:* `${portfolio['total_usdc']:,.0f} USDC`"
        if portfolio else ""
    )
    trades_section = (
        f"\n*Last 7 days \u2014 {len(window)} trade(s):*\n"
        f"  \u2705 TP: {len(wins)}  \u2192  `+${win_usdc:,.2f}`\n"
        f"  \u274c SL: {len(losses)}  \u2192  `${loss_usdc:,.2f}`\n"
        f"  Net: `{'+'if net_usdc>=0 else ''}{net_usdc:,.2f} ({net_pct:+.1f}% on deployed capital)`"
    ) if window else "\n*Last 7 days:* No closed trades"
    open_section = (
        f"\n*Open positions ({len(open_trades)}):*\n" + "\n".join(open_lines)
    ) if open_trades else "\n*Open positions:* None"

    perf = _compute_perf_stats(trades)
    if perf:
        pf_str = f"{perf['profit_factor']:.2f}" if perf["profit_factor"] != float("inf") else "\u221e"
        tier_lines = "\n  ".join(
            f"{tier}: {v['wins']}/{v['total']} ({v['wins']/v['total']*100:.0f}%)"
            for tier, v in sorted(perf["tier_stats"].items())
            if v["total"] > 0
        )
        perf_section = (
            f"\n\n\U0001f4c8 *30-day stats ({perf['count']} trades)*\n"
            f"  Win rate: `{perf['win_rate']*100:.1f}%` | P.Factor: `{pf_str}` | IR: `{perf['sharpe']:.2f}`\n"
            f"  Max consec losses: `{perf['max_consec_losses']}`"
            + (f"\n  {tier_lines}" if tier_lines else "")
        )
    else:
        perf_section = ""

    msg = (
        f"\U0001f4ca *Morning Digest \u2014 {now.strftime('%a %b %-d')}*"
        f"{portfolio_line}"
        f"{fg_str}"
        f"{trades_section}"
        f"{open_section}"
        f"{perf_section}"
        f"\n\n_Next scan in ~30 min_"
    )
    send_telegram(msg)
    print("  \U0001f4ca Morning digest sent")


# ── Dynamic pair scoring (T4-3) ──────────────────────────────────────────────
def _pair_score(symbol: str, trades: list[dict[str, Any]]) -> float:
    """Composite score = win_rate x profit_factor from last PAIR_SCORE_LOOKBACK closed trades."""
    closed = [
        t for t in trades
        if t.get("symbol") == symbol
        and t.get("status") in ("tp_hit", "sl_hit", "timeout")
        and t.get("pnl_pct") is not None
    ][-PAIR_SCORE_LOOKBACK:]
    if len(closed) < PAIR_SCORE_MIN_TRADES:
        return 0.5
    wins   = [t["pnl_pct"] for t in closed if t["pnl_pct"] > 0]
    losses = [t["pnl_pct"] for t in closed if t["pnl_pct"] < 0]
    win_rate     = len(wins) / len(closed)
    if not losses:
        return win_rate
    gross_profit  = sum(wins) if wins else 0.0
    profit_factor = gross_profit / abs(sum(losses))
    return win_rate * profit_factor
