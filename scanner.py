#!/usr/bin/env python3
"""
Binance Trading Scanner
Config: ETH/ADA/DOGE/BNB (USDC pairs) | $200/trade | SL -3% | TP +7.5%

This module is a thin facade — all logic lives in the trading/ package.
It re-exports every public name so that tui.py, backtest.py, and tests
continue to work with ``import scanner`` / ``from scanner import ...``.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime
from typing import Any, Optional

# ── Logger (must be first — sets up SCANNER_DIR, LOG_FILE, etc.) ─────────────
from trading.logger import LOG_FILE, SCANNER_DIR, STATE_FILE, logger  # noqa: F401

# ── TeeLogger (guarded — only active in CLI mode) ────────────────────────────
class TeeLogger:
    """Write to both stdout and the log file (append)."""
    def __init__(self):
        self._log = open(LOG_FILE, "a", buffering=1)
        self._stdout = sys.__stdout__
    def write(self, msg: str) -> None:
        self._stdout.write(msg)
        self._log.write(msg)
    def flush(self) -> None:
        self._stdout.flush()
        self._log.flush()

if __name__ == "__main__":
    sys.stdout = TeeLogger()

# ── Config imports (kept here for backward compat — tests read scanner.PAIRS) ─
from config import (  # noqa: E402, F401
    BINANCE_API_KEY    as API_KEY,
    BINANCE_SECRET_KEY as SECRET_KEY,
    TELEGRAM_TOKEN,
    TELEGRAM_CHAT_ID,
    WEBHOOK_URL,
    PAIRS, CAPITAL,
    MAX_POSITIONS, SL_COOLDOWN_H, MAX_DRAWDOWN_PCT, DIGEST_HOUR,
    ENTRY_REFINE_ENABLED, ENTRY_REFINE_15M_RSI_MAX, ENTRY_REFINE_15M_LIMIT,
    PAIR_SCORE_ENABLED, PAIR_SCORE_MIN_TRADES, PAIR_SCORE_LOOKBACK,
    DIVERGENCE_ENABLED, DIVERGENCE_LOOKBACK, DIVERGENCE_SWING_DEPTH,
    BTC_DOM_ENABLED, BTC_DOM_CACHE_H, BTC_DOM_RISE_THRESHOLD,
    PARTIAL_TP_ENABLED, PARTIAL_TP1_ATR_MULT, PARTIAL_TP1_QTY_PCT,
    SPLIT_ENTRY_ENABLED, SPLIT_ENTRY_ATR_MULT, SPLIT_ENTRY_TTL_H,
    TRADE_TIMEOUT_ENABLED, TRADE_TIMEOUT_H,
    BREAKEVEN_ENABLED, BREAKEVEN_ATR_MULT,
    PROGRESSIVE_TRAILING_ENABLED, PROGRESSIVE_TRAILING_STAGES,
    VOL_SIZING_ENABLED, TARGET_RISK_PCT, VOL_SIZING_MIN, VOL_SIZING_MAX,
    STOP_LOSS, TAKE_PROFIT,
    TRAILING_DELTA,
    ATR_SL_MULT, ATR_TP_MULT, ATR_SL_MIN, ATR_SL_MAX,
    INTERVAL, KLINE_LIMIT,
    DB_FILE,
)

# ══════════════════════════════════════════════════════════════════════════════
#  Re-exports from trading/ sub-modules
#
#  Every name that was previously defined in this file is re-exported here
#  so that ``import scanner; scanner.foo()`` and ``from scanner import foo``
#  both continue to work unchanged.
# ══════════════════════════════════════════════════════════════════════════════

# ── trading.notify ───────────────────────────────────────────────────────────
from trading.notify import (  # noqa: E402, F401
    send_telegram,
    send_telegram_sync,
    telegram_get_updates,
    wait_telegram_confirm,
    call_webhook,
    notify_mac,
    markup_escape,
)

# ── trading.db ───────────────────────────────────────────────────────────────
from trading.db import (  # noqa: E402, F401
    _DB_SCHEMA,
    _TRADE_COLS,
    db_connect,
    db_init,
    get_kv,
    set_kv,
    _row_to_trade,
    get_open_trades,
    get_all_trades,
    get_closed_trades,
    insert_trade,
    update_trade_fields,
    load_cooldowns,
    save_cooldown,
    load_pending_second_entries,
    save_pending_second_entry,
    clear_pending_second_entry,
    load_sent_signals,
    save_sent_signal,
    get_fg_cache,
    set_fg_cache,
    get_btc_dom_cache,
    set_btc_dom_cache,
    save_portfolio,
    db_get_portfolio,
    save_scan_results,
    get_scan_results,
    append_scan_history,
    get_scan_history,
    get_state_dict,
    migrate_from_json,
    save_state,
)

# ── trading.http_client ──────────────────────────────────────────────────────
from trading.http_client import (  # noqa: E402, F401
    BASE_URL,
    get,
    signed_get,
    signed_post,
    signed_delete,
)

# ── trading.indicators ───────────────────────────────────────────────────────
from trading.indicators import (  # noqa: E402, F401
    calc_rsi,
    calc_atr,
    calc_sma,
    detect_bullish_divergence,
)

# ── trading.market_data ──────────────────────────────────────────────────────
from trading.market_data import (  # noqa: E402, F401
    COINGECKO_GLOBAL,
    get_fear_greed,
    get_btc_context,
    get_btc_dominance,
    _is_btc_dom_rising,
    has_open_position,
    get_open_positions,
    get_portfolio,
)

# ── trading.signals ──────────────────────────────────────────────────────────
from trading.signals import (  # noqa: E402, F401
    analyze,
    _estimate_sl_tp_pct,
    _get_15m_rsi,
    _check_15m_rsi_gate,
)

# ── trading.orders ───────────────────────────────────────────────────────────
from trading.orders import (  # noqa: E402, F401
    _load_cooldowns,
    _order_fill_price,
    _save_cooldown,
    _load_pending_second_entries,
    _save_pending_second_entry,
    _clear_pending_second_entry,
    _place_split_second_entry,
    place_buy_order,
)

# ── trading.positions ────────────────────────────────────────────────────────
from trading.positions import (  # noqa: E402, F401
    _check_breakeven,
    _check_progressive_trailing,
    _handle_trade_timeout,
    _handle_partial_tp1,
    _check_sl_outcomes,
)

# ── trading.analytics ────────────────────────────────────────────────────────
from trading.analytics import (  # noqa: E402, F401
    _fg_regime,
    _check_fg_regime_change,
    _escape_md,
    _calc_capital,
    _safe_fromisoformat,
    _compute_perf_stats,
    _send_daily_digest,
    _pair_score,
)

# ── trading.dashboard ────────────────────────────────────────────────────────
from trading.dashboard import generate_dashboard  # noqa: E402, F401

# ── trading.scan_helpers ─────────────────────────────────────────────────────
from trading.scan_helpers import (  # noqa: E402, F401
    acquire_scan_lock,
    apply_correlation_cap,
    build_market_context,
    release_scan_lock,
    run_position_management,
    run_split_entry_checks,
)

# ── trading.dca / trading.staking ────────────────────────────────────────────
from trading.dca import (  # noqa: E402, F401
    get_dca_reserve,
    get_dca_stats,
    initialize_dca_reserve,
    next_dca_time,
    place_dca_buy,
    run_dca_check,
    should_run_dca,
)
from trading.staking import (  # noqa: E402, F401
    get_beth_balance,
    get_staking_stats,
    get_total_eth,
    stake_eth,
)


# ══════════════════════════════════════════════════════════════════════════════
#  scan() and _scan_body() — the main entry points
# ══════════════════════════════════════════════════════════════════════════════

def scan() -> None:
    print(f"\n--- {datetime.now().strftime('%a. %d %b %Y %H:%M:%S')} ---")
    print(f"\n{'='*55}")
    print(f"  TRADING SCANNER — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Pairs: {', '.join(PAIRS)} | Capital: ${CAPITAL}/trade")
    print(f"  SL: -{STOP_LOSS*100:.0f}% | TP: +{TAKE_PROFIT*100:.0f}%")
    print(f"{'='*55}")

    _scan_conn = db_connect()
    db_init(_scan_conn)
    try:
        _scan_body(_scan_conn)
    finally:
        try:
            release_scan_lock(_scan_conn)
        except Exception:
            pass
        _scan_conn.close()
    print(f"\n{'='*55}\n")


def _scan_body(_scan_conn: sqlite3.Connection) -> None:
    """Inner scan logic — separated so scan() can wrap it in try/finally."""
    sent_signals: dict[str, str] = load_sent_signals(_scan_conn)

    _check_sl_outcomes(_scan_conn)

    try:
        _stuck = _scan_conn.execute(
            "SELECT symbol, status FROM trades WHERE status IN ('timeout_sell_failed', 'no_oco')"
        ).fetchall()
        if _stuck:
            _stuck_list = ", ".join(f"{r[0]}({r[1]})" for r in _stuck)
            print(f"  \u26a0 Stuck positions requiring manual intervention: {_stuck_list}")
    except Exception:
        pass

    run_split_entry_checks(_scan_conn)
    run_position_management(_scan_conn)

    # ── DCA accumulation check (cheap when not scheduled) ──────────────────────
    try:
        from trading.dca import run_dca_check
        run_dca_check(_scan_conn)
    except Exception as _dca_e:
        print(f"  \u26a0 DCA check failed: {_dca_e}")

    # ── Acquire scan lock (prevents cron + TUI double-ordering) ────────────────
    if not acquire_scan_lock(_scan_conn, caller="scanner"):
        print("  ⏸ Scan lock held by another process — skipping signal detection")
        return

    context = build_market_context()
    fg_value   = context["fg_value"]
    fg_class   = context["fg_class"]
    fg_fresh   = context["fg_fresh"]
    btc_dom    = context["btc_dom"]
    try:
        old_fg_regime: str = get_kv(_scan_conn, "fg_regime") or _fg_regime(fg_value)
    except Exception:
        old_fg_regime = _fg_regime(fg_value)
    _arrow = "\u2191" if context["btc_dom_rising"] else ""
    dom_str = f"{btc_dom:.1f}%{_arrow}" if btc_dom is not None else "n/a"
    print(f"  F&G: {fg_value} ({fg_class})  |  BTC: ${context['btc_price']:,.0f}  RSI:{context['btc_rsi']}  "
          f"SMA:{'above' if context['btc_above_sma'] else 'below'}  |  BTC.D:{dom_str}")
    if BTC_DOM_ENABLED and btc_dom is not None:
        try:
            set_kv(_scan_conn, "btc_dom_prev", str(btc_dom))
        except Exception as e:
            print(f"  \u26a0 Could not persist btc_dom_prev: {e}")

    if fg_fresh:
        new_fg_regime = _check_fg_regime_change(fg_value, fg_class, old_fg_regime)
    else:
        new_fg_regime = old_fg_regime

    portfolio = get_portfolio()
    if portfolio:
        total = portfolio["total_usdc"]
        asset_str = "  ".join(
            f"{a['asset']}:{a['qty']:.4f}(${a['value_usdc']:.0f})"
            for a in portfolio["assets"]
        )
        print(f"  Portfolio: ${total:,.2f} USDC total  |  {asset_str}")
    _sep = "\u2500" * 55
    print(_sep)

    signals = []
    all_results = []
    cooldowns = _load_cooldowns()
    _open_pos   = get_open_positions()
    open_count  = len(_open_pos)
    _pnl_vals     = [p["pnl"] for p in _open_pos if p.get("pnl") is not None]
    open_pnl_usdc = sum(_pnl_vals) if _pnl_vals else None

    candidates = []
    for symbol in PAIRS:
        try:
            result = analyze(symbol, context)
            all_results.append(result)
            icon = "\U0001f7e2" if result["buy_signal"] else "\u26aa"
            print(f"\n  {icon} {symbol:<12} ${result['price']:<12.6f} RSI:{result['rsi']:<6} "
                  f"24h:{result['change24h']:+.2f}%  Signal:{result['signal_strength']}")
            print(f"     SMA20:{'above' if result['above_sma'] else 'below'} | "
                  f"Vol surge:{'yes' if result['vol_surge'] else 'no'} | "
                  f"Momentum:{'up' if result['momentum'] else 'flat/down'}")
            if result["buy_signal"]:
                candidates.append(result)
        except Exception as e:
            print(f"  \u2717 {symbol}: Error \u2014 {e}")

    candidates, dropped, cap_reason = apply_correlation_cap(candidates, _scan_conn)
    if dropped:
        print(f"\n  \u26a0 Correlation cap \u2014 keeping {candidates[0]['symbol']} ({cap_reason}), "
              f"dropping: {', '.join(dropped)}")

    try:
        _peak_str = get_kv(_scan_conn, "peak_portfolio_usdc")
        peak_usdc: float = float(_peak_str) if _peak_str else 0.0
    except Exception:
        peak_usdc = 0.0
    current_usdc = portfolio["total_usdc"] if portfolio else None
    cb_alert_ts: Optional[str] = None
    if peak_usdc and current_usdc:
        drawdown_pct = (peak_usdc - current_usdc) / peak_usdc
        if drawdown_pct >= MAX_DRAWDOWN_PCT:
            try:
                cb_last = get_kv(_scan_conn, "cb_alert_sent_at") or ""
            except Exception:
                cb_last = ""
            cb_cooldown_expired = (
                not cb_last
                or (datetime.now() - datetime.fromisoformat(cb_last)).total_seconds() >= 4 * 3600
            )
            if cb_cooldown_expired:
                cb_msg = (
                    f"\U0001f6d1 *Circuit breaker triggered*\n"
                    f"Drawdown: `{drawdown_pct*100:.1f}%` from peak\n"
                    f"Peak: `${peak_usdc:,.0f}` \u2192 Now: `${current_usdc:,.0f}`\n"
                    f"New orders halted until portfolio recovers."
                )
                send_telegram(cb_msg)
                cb_alert_ts = datetime.now().isoformat()
            print(f"  \U0001f6d1 CIRCUIT BREAKER: {drawdown_pct*100:.1f}% drawdown \u2014 no orders placed")
            candidates = []

    for result in candidates:
        symbol = result["symbol"]
        if open_count >= MAX_POSITIONS:
            print(f"     \u23f8 {symbol} \u2014 skipped (max positions {MAX_POSITIONS})")
        elif symbol in cooldowns:
            print(f"     \u23f8 {symbol} \u2014 skipped (SL cooldown until {cooldowns[symbol][:16]})")
        elif has_open_position(symbol):
            print(f"     \u23f8 {symbol} \u2014 skipped (open position exists)")
        else:
            signals.append(result)

    save_state(all_results, [{"symbol": s["symbol"], "price": s["price"], "rsi": s["rsi"],
                               "signal_strength": s["signal_strength"]} for s in signals],
               portfolio=portfolio, fg_regime=new_fg_regime, open_pnl=open_pnl_usdc,
               cb_alert_sent_at=cb_alert_ts, conn=_scan_conn)

    if all_results:
        perf_line = ""
        try:
            closed = get_closed_trades(_scan_conn)
            if closed:
                wins  = sum(1 for t in closed if t.get("status") == "tp_hit")
                total = len(closed)
                perf_line = f"\n\U0001f4ca Trades: `{wins}W/{total-wins}L` ({wins/total*100:.0f}% WR)"
        except Exception:
            pass

        icons = {"EXTREME": "\U0001f534", "STRONG": "\U0001f7e0", "MODERATE": "\U0001f7e1", "NONE": "\u26aa"}
        btc_trend = "\u2191" if context["btc_above_sma"] else "\u2193"
        lines = [
            f"\U0001f4ca *Scan {datetime.now().strftime('%H:%M')}*\n"
            f"F&G: `{context['fg_value']}` {context['fg_class']}  |  "
            f"BTC `${context['btc_price']:,.0f}` RSI:`{context['btc_rsi']}` {btc_trend}\n"
        ]
        for r in all_results:
            icon  = icons.get(r["signal_strength"], "\u26aa")
            pair  = r["symbol"].replace("USDC", "")
            lines.append(
                f"{icon} `{pair:<5}` ${r['price']:<10.4f} RSI:`{r['rsi']:<5}` 24h:`{r['change24h']:+.2f}%`"
                + (f"  *{r['signal_strength']}*" if r["signal_strength"] != "NONE" else "")
            )
        if _open_pos:
            lines.append("\n\U0001f4c8 *Positions*")
            for p in _open_pos:
                pair    = p["symbol"].replace("USDC", "")
                pnl_str = (f"{p['pnl_pct']:+.2f}%  `{'%.2f' % p['pnl']}$`"
                           if p["pnl"] is not None else "n/a")
                entry_s = f"${p['entry']:.4f}" if p["entry"] else "?"
                cur_s   = f"${p['current']:.4f}" if p["current"] else "?"
                tp_s    = f"${p['tp']:.2f}" if p["tp"] else "?"
                sl_s    = f"${p['sl']:.2f}" if p["sl"] else "?"
                lines.append(
                    f"`{pair}` {p['qty'] or '?'} \u00b7 {entry_s}\u2192{cur_s} {pnl_str}\n"
                    f"  TP:{tp_s}  SL:{sl_s}"
                )
        if perf_line:
            lines.append(perf_line)
        send_telegram("\n".join(lines))

    if signals:
        SIGNAL_DEDUP_H = 2
        for s in signals:
            capital = _calc_capital(s, context)
            sl_pct, tp_pct = _estimate_sl_tp_pct(s)
            dedup_key = f"{s['symbol']}:{s['signal_strength']}"
            last_sent = sent_signals.get(dedup_key)
            if last_sent:
                age_h = (datetime.now() - datetime.fromisoformat(last_sent)).total_seconds() / 3600
                if age_h < SIGNAL_DEDUP_H:
                    print(f"     \u23ed {s['symbol']} \u2014 alert already sent {age_h:.1f}h ago, skipping Telegram")
                    continue
            msg = (
                f"\U0001f4e1 *{s['signal_strength']} BUY SIGNAL*\n"
                f"Pair: `{s['symbol']}`\n"
                f"Entry: `${s['price']:.4f}` | RSI: `{s['rsi']}`\n"
                f"TP: `${s['price'] * (1 + tp_pct):.4f}` (+{tp_pct*100:.1f}%)  "
                f"SL: `${s['price'] * (1 - sl_pct):.4f}` (-{sl_pct*100:.1f}%)\n"
                f"Cost: `${capital} USDC`"
            )
            send_telegram(msg)
            _sent_ts = datetime.now().isoformat()
            sent_signals[dedup_key] = _sent_ts
            try:
                save_sent_signal(_scan_conn, dedup_key, _sent_ts)
            except Exception:
                pass

    if signals:
        cron_mode = os.environ.get("SCANNER_CRON", "") == "1"
        symbols_str = ", ".join(s["symbol"] for s in signals)
        notify_mac("Trading Scanner", f"Signal found: {symbols_str} \u2014 open terminal to confirm"
                   if cron_mode else f"Signal found: {symbols_str}")
        for s in signals:
            call_webhook({
                "symbol":          s["symbol"],
                "price":           s["price"],
                "rsi":             s["rsi"],
                "signal_strength": s["signal_strength"],
                "tp":              round(s["price"] * (1 + TAKE_PROFIT), 6),
                "sl":              round(s["price"] * (1 - STOP_LOSS), 6),
                "capital":         CAPITAL,
            })

    _hr = "\u2500" * 55
    print(f"\n{_hr}")
    if not signals:
        print("  No buy signals found. Check again in 30 minutes.")
    else:
        print(f"  {len(signals)} signal(s) found!")
        for s in signals:
            capital = _calc_capital(s, context)
            sl_pct, tp_pct = _estimate_sl_tp_pct(s)
            print(f"\n  \u25ba {s['symbol']} \u2014 {s['signal_strength']} BUY SIGNAL")
            print(f"    Entry: ~${s['price']:.6f}")
            print(f"    TP:    ~${s['price'] * (1 + tp_pct):.6f} (+{tp_pct*100:.1f}%)")
            print(f"    SL:    ~${s['price'] * (1 - sl_pct):.6f} (-{sl_pct*100:.1f}%)")
            _half = "  (half-size \u2014 downtrend dip)" if s["signal_strength"] == "EXTREME" else ""
            print(f"    Cost:  ${capital} USDC{_half}")

        cron_mode = os.environ.get("SCANNER_CRON", "") == "1"
        new_trades = []

        def _place_and_arm(s: dict[str, Any]) -> Optional[dict[str, Any]]:
            """Place buy order and arm split-entry pending leg if applicable."""
            capital = _calc_capital(s, context)
            _, _, trade = place_buy_order(s["symbol"], capital, s["price"], s.get("closed_klines"))
            trade["signal_strength"] = s.get("signal_strength", "UNKNOWN")
            if trade.get("status") == "open" and trade.get("tp"):
                send_telegram(
                    f"\u2705 *Order placed*\n"
                    f"`{s['symbol']}` {trade['qty']} units @ `${trade['entry']:.4f}`\n"
                    f"TP `${trade['tp']:.4f}` \u00b7 SL `${trade['sl']:.4f}`\n"
                    f"OCO #{trade['oco_id']}"
                )
            if (SPLIT_ENTRY_ENABLED
                    and s["signal_strength"] == "EXTREME"
                    and s.get("extreme_quality")
                    and trade.get("status") == "open"):
                atr_pct = trade.get("sl_pct", STOP_LOSS) / ATR_SL_MULT if ATR_SL_MULT > 0 else STOP_LOSS
                pending_data = {
                    "first_fill":    trade["entry"],
                    "first_qty":     trade["qty"],
                    "first_oco_id":  trade["oco_id"],
                    "atr_pct":       atr_pct,
                    "sl_pct":        trade.get("sl_pct", STOP_LOSS),
                    "tp_pct":        trade.get("tp_pct", TAKE_PROFIT),
                    "capital_half":  capital,
                    "time":          datetime.now().isoformat(),
                }
                _save_pending_second_entry(s["symbol"], pending_data, _scan_conn)
                trigger_price = trade["entry"] * (1 - atr_pct * SPLIT_ENTRY_ATR_MULT)
                send_telegram(
                    f"\U0001f3af *Split entry armed* \u2014 `{s['symbol']}`\n"
                    f"Second leg triggers at `${trigger_price:.4f}` "
                    f"({SPLIT_ENTRY_ATR_MULT}\u00d7 ATR below entry). "
                    f"TTL: {SPLIT_ENTRY_TTL_H}h."
                )
            return trade

        if cron_mode:
            print("  [CRON MODE] Waiting for Telegram confirmation...")
            for s in signals:
                if wait_telegram_confirm(s["symbol"], timeout=120):
                    blocked_rsi = _check_15m_rsi_gate(s["symbol"])
                    if blocked_rsi is not None:
                        print(f"  \u23e9 {s['symbol']} 15m RSI {blocked_rsi:.1f} > {ENTRY_REFINE_15M_RSI_MAX} \u2014 deferred")
                        send_telegram(
                            f"\u23e9 *Entry deferred* \u2014 `{s['symbol']}`\n"
                            f"15m RSI `{blocked_rsi:.1f}` > `{ENTRY_REFINE_15M_RSI_MAX}` \u2014 wait for 15m pullback"
                        )
                        continue
                    try:
                        trade = _place_and_arm(s)
                        new_trades.append(trade)
                    except Exception as e:
                        print(f"  \u2717 Order failed for {s['symbol']}: {e}")
                        send_telegram(f"\u274c Order failed for `{s['symbol']}`: {_escape_md(e)}")
        else:
            confirm = input("\n  Type CONFIRM to place order(s), or SKIP to skip: ").strip()
            if confirm.upper() == "CONFIRM":
                for s in signals:
                    blocked_rsi = _check_15m_rsi_gate(s["symbol"])
                    if blocked_rsi is not None:
                        print(f"  \u23e9 {s['symbol']} 15m RSI {blocked_rsi:.1f} > {ENTRY_REFINE_15M_RSI_MAX} \u2014 deferred")
                        continue
                    try:
                        trade = _place_and_arm(s)
                        new_trades.append(trade)
                    except Exception as e:
                        print(f"  \u2717 Order failed for {s['symbol']}: {e}")
                        send_telegram(f"\u274c Order failed for `{s['symbol']}`: {_escape_md(e)}")
            else:
                print("  Skipped. Run again or wait for next scan.")
        if new_trades:
            save_state(all_results, [{"symbol": s["symbol"], "price": s["price"],
                                      "rsi": s["rsi"], "signal_strength": s["signal_strength"]}
                                     for s in signals], new_trades,
                                     conn=_scan_conn)
    # ── Generate dashboard ────────────────────────────────────────────────────
    try:
        generate_dashboard(get_state_dict(_scan_conn))
    except Exception as e:
        print(f"  \u26a0 Dashboard generation failed: {e}")

    # ── Daily digest (8am, once per calendar day) ─────────────────────────────
    try:
        now = datetime.now()
        last_digest = get_kv(_scan_conn, "last_digest_date") or ""
        if now.hour == DIGEST_HOUR and str(now.date()) != last_digest:
            _send_daily_digest(get_state_dict(_scan_conn))
            set_kv(_scan_conn, "last_digest_date", str(now.date()))
    except Exception as e:
        print(f"  \u26a0 Daily digest failed: {e}")

    # ── Health sentinel ───────────────────────────────────────────────────────
    set_kv(_scan_conn, "last_scan_ok", datetime.now().isoformat())


def get_health(conn: Optional[sqlite3.Connection] = None) -> dict[str, Any]:
    """Return scanner health status."""
    _own = conn is None
    if _own:
        conn = db_connect()
        db_init(conn)
    last_ok = get_kv(conn, "last_scan_ok") or ""
    last_scan = get_kv(conn, "last_scan") or ""
    stuck = conn.execute(
        "SELECT COUNT(*) FROM trades WHERE status IN ('timeout_sell_failed', 'no_oco')"
    ).fetchone()[0]
    if _own:
        conn.close()
    age_s = (datetime.now() - datetime.fromisoformat(last_ok)).total_seconds() if last_ok else None
    return {
        "last_scan_ok": last_ok,
        "last_scan": last_scan,
        "age_seconds": round(age_s, 1) if age_s is not None else None,
        "healthy": age_s is not None and age_s < 3600,
        "stuck_positions": stuck,
    }


if __name__ == "__main__":
    scan()
