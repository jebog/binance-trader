#!/usr/bin/env python3
"""
Standalone backtest script — no dependencies beyond stdlib + scanner.py.

Strategy:  RSI/SMA/Vol/Momentum signals (EXTREME/STRONG/MODERATE tiers)
           mirroring scanner.py logic exactly.
Filters:   RSI divergence filter (T2-2) applied when DIVERGENCE_ENABLED.
           No Fear & Greed or BTC dominance filter (not available historically).
Execution: Partial TP at TP1 (T2-4) when PARTIAL_TP_ENABLED.
           Volatility-adjusted capital sizing (T3-4) when VOL_SIZING_ENABLED.
           Break-even stop (T3-1) when BREAKEVEN_ENABLED.
           Progressive trailing tightening (T4-4) when PROGRESSIVE_TRAILING_ENABLED.
Data:      Binance public klines API, 1h, 1000 candles (~41 days) per pair.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

# ── Config ────────────────────────────────────────────────────────────────────
from config import (
    ATR_SL_MAX,
    ATR_SL_MIN,
    ATR_SL_MULT,
    ATR_TP_MULT,
    BREAKEVEN_ATR_MULT,
    BREAKEVEN_ENABLED,
    CAPITAL,
    DIVERGENCE_ENABLED,
    DIVERGENCE_LOOKBACK,
    DIVERGENCE_SWING_DEPTH,
    PAIRS,
    PARTIAL_TP1_ATR_MULT,
    PARTIAL_TP1_QTY_PCT,
    PARTIAL_TP_ENABLED,
    PROGRESSIVE_TRAILING_ENABLED,
    PROGRESSIVE_TRAILING_STAGES,
    STOP_LOSS,
    TAKE_PROFIT,
    TARGET_RISK_PCT,
    TRAILING_DELTA,
    VOL_SIZING_ENABLED,
    VOL_SIZING_MAX,
    VOL_SIZING_MIN,
)

# ── RSI divergence helper (imported from scanner.py) ─────────────────────────
# scanner.py has a TeeLogger guard (`if __name__ == "__main__"`) so importing
# it here is safe — no side effects on import.
from scanner import detect_bullish_divergence

INTERVAL     = "1h"
KLINE_LIMIT  = 1000   # max per Binance request (backtest fetches more than scanner)
WINDOW       = 100    # rolling look-back window (matches scanner KLINE_LIMIT)

MAX_HOLD_CANDLES = 72        # 3 days — close at market if neither TP nor SL hit
TRAIN_FRAC       = 0.7       # 70% in-sample (≈700 candles), 30% out-of-sample (≈300)

RESULTS_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backtest_results.json")

# ── Indicator functions (copied verbatim from scanner.py) ─────────────────────

def calc_rsi(closes: list[float], period: int = 14) -> float:
    """Wilder's EMA RSI — matches TradingView/Binance standard."""
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    if len(gains) < period:
        return 50.0
    # Seed: simple average for the first period
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    # Wilder's smoothing for the remaining values
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    return 100 - (100 / (1 + avg_gain / avg_loss))


def calc_atr(klines: list[list[Any]], period: int = 14) -> Optional[float]:
    """Wilder's ATR — uses high/low/prev_close from raw klines."""
    trs = []
    for i in range(1, len(klines)):
        high       = float(klines[i][2])
        low        = float(klines[i][3])
        prev_close = float(klines[i-1][4])
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    if len(trs) < period:
        return None
    # Seed
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


def calc_sma(closes: list[float], period: int = 20) -> Optional[float]:
    if len(closes) < period:
        return None
    return sum(closes[-period:]) / period


# ── Data fetching ─────────────────────────────────────────────────────────────

def fetch_klines(symbol: str, interval: str = INTERVAL, limit: int = KLINE_LIMIT) -> list[list[Any]]:
    """Fetch klines from Binance public API (no auth required)."""
    url = "https://api.binance.com/api/v3/klines?" + urllib.parse.urlencode({
        "symbol":   symbol,
        "interval": interval,
        "limit":    limit,
    })
    req = urllib.request.Request(url, headers={"User-Agent": "backtest/1.0"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            raise RuntimeError(f"HTTP {e.code} fetching {symbol}: {body}") from None
        except Exception as e:
            if attempt == 2:
                raise
            print(f"  Retry {attempt+1}/3 for {symbol}: {e}")
            time.sleep(2)
    raise RuntimeError(f"fetch_klines: all retries exhausted for {symbol}")


# ── Signal logic (mirrors scanner.py analyze(), no F&G / BTC filter) ──────────

def compute_signal(window_klines: list[list[Any]]) -> tuple[str, float, bool, bool, bool, Optional[float], float]:
    """
    Given a list of WINDOW closed klines, compute the signal tier.
    Returns: (signal_str, rsi, above_sma, vol_surge, momentum_up, atr, price)
    """
    closes  = [float(k[4]) for k in window_klines]
    vols    = [float(k[5]) for k in window_klines]
    price   = closes[-1]

    rsi      = calc_rsi(closes)
    sma20    = calc_sma(closes, 20)
    above_sma = (sma20 is not None) and (price > sma20)

    # Vol surge: last vol vs avg of previous candles (same as scanner: vols[:-1])
    avg_vol   = sum(vols[:-1]) / (len(vols) - 1) if len(vols) > 1 else 0
    vol_surge = (vols[-1] > avg_vol * 1.3) if avg_vol > 0 else False

    # Momentum: close[-1] > close[-5]
    momentum_up = closes[-1] > closes[-5] if len(closes) >= 5 else False

    # ── Signal tiers ────────────────────────────────────────────────────────
    extreme_signal = rsi < 25
    # STRONG: no F&G filter in backtest (omitted) → fg<75 always passes
    strong_signal  = rsi < 32 and above_sma
    # MODERATE: no F&G or BTC filter in backtest
    moderate_signal = rsi < 40 and above_sma and vol_surge and momentum_up

    if extreme_signal:
        signal = "EXTREME"
    elif strong_signal:
        signal = "STRONG"
    elif moderate_signal:
        signal = "MODERATE"
    else:
        signal = "NONE"

    atr = calc_atr(window_klines, period=14)
    return signal, rsi, above_sma, vol_surge, momentum_up, atr, price

# ── Per-symbol backtest ────────────────────────────────────────────────────────

def backtest_symbol(symbol: str, klines: list[list[Any]]) -> list[dict[str, Any]]:
    """
    Rolling-window simulation over all klines for one symbol.
    Returns list of trade dicts.
    """
    trades = []
    open_trade: Optional[dict[str, Any]] = None   # at most one open trade at a time per symbol

    for i in range(WINDOW, len(klines)):
        window = klines[i - WINDOW : i]   # 100 closed candles, index i-1 is last closed

        # Skip if trade already open
        if open_trade is not None:
            # Check if this candle exits the open trade
            j = i  # current candle index
            high  = float(klines[j][2])
            low   = float(klines[j][3])
            close = float(klines[j][4])

            sl_price = open_trade["sl"]
            tp_price = open_trade["tp"]
            held     = j - open_trade["entry_candle_idx"]

            # T3-3: partial TP1 tracking — mark hit if candle high reaches TP1
            tp1_price = open_trade.get("tp1_price")
            if PARTIAL_TP_ENABLED and tp1_price and not open_trade.get("partial_tp1_hit"):
                if high >= tp1_price:
                    open_trade["partial_tp1_hit"]    = True
                    open_trade["tp1_exit_price"]     = tp1_price
                    open_trade["tp1_candle_idx"]     = i   # track candle for same-candle guard

            # T3-1: break-even stop — move SL to entry once price reaches trigger
            _atr_pct_raw = open_trade.get("atr_pct_raw") or 0.0
            if BREAKEVEN_ENABLED and not open_trade.get("breakeven_moved") and _atr_pct_raw:
                be_trigger = open_trade["entry"] * (1 + BREAKEVEN_ATR_MULT * _atr_pct_raw)
                if high >= be_trigger:
                    open_trade["breakeven_moved"] = True
                    open_trade["peak_high_be"]    = high
                    if open_trade["entry"] > sl_price:
                        sl_price = open_trade["entry"]
                        open_trade["sl"] = sl_price  # persist — next candle reads open_trade["sl"]

            # T4-4: progressive trailing — tighten delta at successive ATR milestones
            if (PROGRESSIVE_TRAILING_ENABLED and TRAILING_DELTA > 0
                    and open_trade.get("breakeven_moved") and _atr_pct_raw):
                _peak = max(open_trade.get("peak_high_be") or open_trade["entry"], high)
                open_trade["peak_high_be"] = _peak
                _stage = open_trade.get("trailing_stage", 0)
                for _idx, (_atr_mult, _new_bps) in enumerate(PROGRESSIVE_TRAILING_STAGES):
                    if _stage <= _idx and _peak >= open_trade["entry"] * (1 + _atr_mult * _atr_pct_raw):
                        _stage = _idx + 1
                open_trade["trailing_stage"] = _stage
                if _stage > 0:
                    _, _current_bps = PROGRESSIVE_TRAILING_STAGES[_stage - 1]
                    _trailing_sl = _peak * (1 - _current_bps / 10000)
                    if _trailing_sl > sl_price:
                        sl_price = _trailing_sl
                        open_trade["sl"] = sl_price  # persist — next candle reads open_trade["sl"]

            tp_hit = high >= tp_price
            sl_hit = low  <= sl_price

            if tp_hit and sl_hit:
                outcome    = "SL"
                exit_price = sl_price
            elif tp_hit:
                outcome    = "TP"
                exit_price = tp_price
            elif sl_hit:
                outcome    = "SL"
                exit_price = sl_price
            elif held >= MAX_HOLD_CANDLES:
                outcome    = "TIMEOUT"
                exit_price = close
            else:
                continue  # trade still open

            entry = open_trade["entry"]
            # T3-3: weighted P&L when TP1 was hit on a PREVIOUS candle.
            # Do NOT credit TP1 if it hit on the same candle as the final exit —
            # intra-candle execution order is undefined; crediting would be optimistic.
            tp1_credited = (
                PARTIAL_TP_ENABLED
                and open_trade.get("partial_tp1_hit")
                and open_trade.get("tp1_candle_idx") != i
            )
            if tp1_credited:
                tp1_ep        = open_trade["tp1_exit_price"]
                tp1_pnl       = (tp1_ep - entry) / entry * 100
                final_leg_pnl = (exit_price - entry) / entry * 100
                pnl_pct       = tp1_pnl * PARTIAL_TP1_QTY_PCT + final_leg_pnl * (1 - PARTIAL_TP1_QTY_PCT)
            else:
                pnl_pct = (exit_price - entry) / entry * 100
            capital     = open_trade.get("capital", CAPITAL)
            pnl_usdc    = capital * pnl_pct / 100
            open_trade["outcome"]           = outcome
            open_trade["exit_price"]        = exit_price
            open_trade["exit_candle_idx"]   = j
            open_trade["candles_held"]      = held
            open_trade["pnl_pct"]           = round(pnl_pct, 4)
            open_trade["pnl_usdc"]          = round(pnl_usdc, 4)
            trades.append(open_trade)
            open_trade = None
            continue

        # Compute signal on the closed window
        signal, rsi, above_sma, vol_surge, momentum_up, atr, price = compute_signal(window)

        if signal == "NONE":
            continue

        # T3-3: RSI divergence filter — mirrors scanner.py analyze() logic
        # Blocks STRONG/MODERATE when price makes lower low + RSI lower low.
        # EXTREME bypasses (catches capitulation bottoms regardless of divergence).
        if DIVERGENCE_ENABLED and signal in ("STRONG", "MODERATE"):
            closes = [float(k[4]) for k in window]
            lb_needed = DIVERGENCE_LOOKBACK + 14 + 28  # +28 Wilder steps (matches scanner)
            div_closes = closes[-lb_needed:]
            rsi_series = [calc_rsi(div_closes[:j]) for j in range(14, len(div_closes) + 1)]
            rsi_series = rsi_series[-DIVERGENCE_LOOKBACK:]
            if len(rsi_series) >= 4:
                div = detect_bullish_divergence(
                    div_closes, rsi_series, DIVERGENCE_LOOKBACK, DIVERGENCE_SWING_DEPTH
                )
                if div is False:
                    continue  # confirmed weakness — skip signal

        # Entry at close of candle[i-1] (last candle in window = klines[i-1])
        entry_candle_idx = i - 1
        entry_price      = float(klines[entry_candle_idx][4])

        # ATR-based SL/TP — mirrors place_buy_order() exactly
        if atr is not None and entry_price > 0:
            atr_pct_raw = atr / entry_price
            sl_pct      = max(ATR_SL_MIN, min(ATR_SL_MAX, atr_pct_raw * ATR_SL_MULT))
            tp_pct      = sl_pct * (ATR_TP_MULT / ATR_SL_MULT)
        else:
            atr_pct_raw = 0.0
            sl_pct      = STOP_LOSS
            tp_pct      = TAKE_PROFIT

        sl_price = entry_price * (1 - sl_pct)
        tp_price = entry_price * (1 + tp_pct)

        # T3-3: partial TP1 price — mirrors place_buy_order() PARTIAL_TP_ENABLED block
        tp1_price: Optional[float] = None
        if PARTIAL_TP_ENABLED and ATR_SL_MULT > 0:
            atr_pct_for_tp1 = sl_pct / ATR_SL_MULT  # use clamped sl_pct, same as live scanner
            tp1_pct   = atr_pct_for_tp1 * PARTIAL_TP1_ATR_MULT
            tp1_price = entry_price * (1 + tp1_pct)
            if tp1_price >= tp_price:
                tp1_price = None  # TP1 must be below TP2

        # T3-3: volatility-adjusted capital — mirrors _calc_capital() formula
        if VOL_SIZING_ENABLED and ATR_SL_MULT > 0 and sl_pct > 0:
            atr_for_sizing = sl_pct / ATR_SL_MULT
            raw_capital = CAPITAL * TARGET_RISK_PCT / atr_for_sizing
            capital     = max(CAPITAL * VOL_SIZING_MIN, min(CAPITAL * VOL_SIZING_MAX, raw_capital))
        else:
            capital = float(CAPITAL)

        open_trade = {
            "symbol":           symbol,
            "signal":           signal,
            "rsi":              round(rsi, 2),
            "entry":            entry_price,
            "sl":               sl_price,
            "tp":               tp_price,
            "sl_pct":           round(sl_pct * 100, 3),
            "tp_pct":           round(tp_pct * 100, 3),
            "entry_candle_idx": entry_candle_idx,
            "atr":              round(atr, 8) if atr else None,
            "atr_pct_raw":      atr_pct_raw,
            "capital":          round(capital, 2),
            "tp1_price":        tp1_price,
            "partial_tp1_hit":  False,
            "breakeven_moved":  False,
            "trailing_stage":   0,
            "peak_high_be":     None,
        }

    # If a trade is still open at end of data, force-close at last candle
    if open_trade is not None:
        last_idx   = len(klines) - 1
        exit_price = float(klines[last_idx][4])
        held       = last_idx - open_trade["entry_candle_idx"]
        entry        = open_trade["entry"]
        tp1_credited = (
            PARTIAL_TP_ENABLED
            and open_trade.get("partial_tp1_hit")
            and open_trade.get("tp1_candle_idx") != last_idx
        )
        if tp1_credited:
            tp1_ep        = open_trade["tp1_exit_price"]
            tp1_pnl       = (tp1_ep - entry) / entry * 100
            final_leg_pnl = (exit_price - entry) / entry * 100
            pnl_pct       = tp1_pnl * PARTIAL_TP1_QTY_PCT + final_leg_pnl * (1 - PARTIAL_TP1_QTY_PCT)
        else:
            pnl_pct = (exit_price - entry) / entry * 100
        capital  = open_trade.get("capital", CAPITAL)
        pnl_usdc = capital * pnl_pct / 100
        open_trade["outcome"]         = "TIMEOUT"
        open_trade["exit_price"]      = exit_price
        open_trade["exit_candle_idx"] = last_idx
        open_trade["candles_held"]    = held
        open_trade["pnl_pct"]         = round(pnl_pct, 4)
        open_trade["pnl_usdc"]        = round(pnl_usdc, 4)
        trades.append(open_trade)

    return trades


# ── Stats helpers ──────────────────────────────────────────────────────────────

def compute_stats(trades: list[dict[str, Any]]) -> dict[str, Any]:
    if not trades:
        return {
            "n": 0, "wins": 0, "losses": 0, "timeouts": 0, "breakeven_saves": 0,
            "win_rate": 0.0, "avg_tp_pct": 0.0, "avg_sl_pct": 0.0,
            "avg_to_pct": 0.0, "net_pct": 0.0, "expectancy": 0.0,
            "net_usdc": 0.0, "expectancy_usdc": 0.0,
        }
    wins     = [t for t in trades if t["outcome"] == "TP"]
    losses   = [t for t in trades if t["outcome"] == "SL"]
    timeouts = [t for t in trades if t["outcome"] == "TIMEOUT"]
    # Breakeven saves: SL exits where break-even floor caught the trade at or above entry
    be_saves = [t for t in losses if t.get("breakeven_moved") and t["exit_price"] >= t["entry"]]

    n        = len(trades)
    nw       = len(wins)
    nl       = len(losses)
    nt       = len(timeouts)
    wr       = nw / n * 100

    avg_win  = sum(t["pnl_pct"] for t in wins)     / nw if nw else 0.0
    avg_loss = sum(t["pnl_pct"] for t in losses)   / nl if nl else 0.0
    avg_to   = sum(t["pnl_pct"] for t in timeouts) / nt if nt else 0.0
    net      = sum(t["pnl_pct"] for t in trades)
    exp      = net / n
    # Dollar-weighted P&L (available when VOL_SIZING_ENABLED)
    net_usdc = sum(t.get("pnl_usdc", 0.0) for t in trades)
    exp_usdc = net_usdc / n

    return {
        "n":               n,
        "wins":            nw,
        "losses":          nl,
        "timeouts":        nt,
        "breakeven_saves": len(be_saves),
        "win_rate":        round(wr, 1),
        "avg_tp_pct":      round(avg_win, 2),
        "avg_sl_pct":      round(avg_loss, 2),
        "avg_to_pct":      round(avg_to, 2),
        "net_pct":         round(net, 2),
        "expectancy":      round(exp, 2),
        "net_usdc":        round(net_usdc, 2),
        "expectancy_usdc": round(exp_usdc, 2),
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def _print_stats_row(label: str, s: dict[str, Any], trades: list[dict[str, Any]], suffix: str = "") -> None:
    from collections import Counter
    c   = Counter(t["signal"] for t in trades)
    sig = " ".join(f"{k[0]}{k[1:3].lower()}:{c[k]}" for k in ["EXTREME","STRONG","MODERATE"] if c[k]) or "—"
    to_note = f"  TO:{s['timeouts']}" if s["timeouts"] else ""
    be_note = f"  BE:{s['breakeven_saves']}" if s.get("breakeven_saves") else ""
    print(
        f"  {label:<12}  {s['n']:>2} trades  {s['wins']}W/{s['losses']}L{to_note}{be_note:<8}"
        f"  WR:{s['win_rate']:.1f}%  AvgTP:{s['avg_tp_pct']:+.1f}%  "
        f"AvgSL:{s['avg_sl_pct']:+.1f}%  Net:{s['net_pct']:+.1f}%{suffix}  [{sig}]"
    )


def main() -> None:
    from collections import Counter
    print()
    partial_note = f"Partial TP1={PARTIAL_TP1_ATR_MULT}×ATR ({int(PARTIAL_TP1_QTY_PCT*100)}% qty)" if PARTIAL_TP_ENABLED else "off"
    div_note     = f"Divergence filter (lookback={DIVERGENCE_LOOKBACK})" if DIVERGENCE_ENABLED else "off"
    vol_note     = f"Vol sizing (target_risk={TARGET_RISK_PCT*100:.1f}%)" if VOL_SIZING_ENABLED else "off"
    print("══════════════════════════════════════════════════════════════")
    print("  BACKTEST — 1h · 1000 candles · ~41 days")
    print(f"  Walk-forward split: {int(TRAIN_FRAC*100)}% train / {100-int(TRAIN_FRAC*100)}% test")
    print("  Signal filters: RSI/SMA/Vol/Momentum")
    print(f"  {div_note}")
    print(f"  Execution: {partial_note}")
    print(f"  Sizing:    {vol_note}")
    print("  NOTE: No F&G or BTC dominance filter (not available historically)")
    print("  SL/TP: ATR-based, SL=ATR×1.5 clamped [2%,6%], TP=SL×(3.5/1.5)")
    print("  Max hold: 72 candles (3 days) → TIMEOUT at market")
    print("══════════════════════════════════════════════════════════════")
    print()

    train_trades_all = []
    test_trades_all  = []
    symbol_data: dict[str, dict[str, Any]] = {}   # {symbol: {train_stats, test_stats, train_trades, test_trades}}

    for symbol in PAIRS:
        print(f"  Fetching {symbol} ...", end=" ", flush=True)
        try:
            klines = fetch_klines(symbol)
        except Exception as e:
            print(f"ERROR: {e}")
            continue
        n_total = len(klines)
        split   = int(n_total * TRAIN_FRAC)  # ≈700

        # Test window includes a WINDOW-length warm-up from the end of train
        # so backtest_symbol can generate a signal on the first test candle.
        train_klines = klines[:split]
        test_klines  = klines[split - WINDOW:]   # warm-up overlap + test candles

        train_trades = backtest_symbol(symbol, train_klines)
        test_trades  = backtest_symbol(symbol, test_klines)
        # Strip warm-up trades (entries before klines[split]) from test results
        test_trades  = [t for t in test_trades if t["entry_candle_idx"] >= WINDOW]

        print(f"{n_total} candles  (train:{split}  test:{n_total-split})")

        train_trades_all.extend(train_trades)
        test_trades_all.extend(test_trades)
        symbol_data[symbol] = {
            "train_stats":  compute_stats(train_trades),
            "test_stats":   compute_stats(test_trades),
            "train_trades": train_trades,
            "test_trades":  test_trades,
        }

    print()
    print("  " + "─" * 76)
    print(f"  {'Symbol':<12}  {'Trades':>2}         {'W/L':<8}  {'WR':>6}  {'AvgTP':>8}  {'AvgSL':>8}  {'Net':>8}")
    print("  " + "─" * 76)

    for symbol in PAIRS:
        if symbol not in symbol_data:
            continue
        d     = symbol_data[symbol]
        short = symbol.replace("USDC", "")
        _print_stats_row(f"{short} TRAIN", d["train_stats"], d["train_trades"])
        _print_stats_row(f"{short} TEST ",  d["test_stats"],  d["test_trades"],
                         suffix=("  ✓" if d["test_stats"]["win_rate"] >= d["train_stats"]["win_rate"] * 0.8
                                  else "  ⚠ degraded"))
        print()

    # ── Overall summary ───────────────────────────────────────────────────────
    overall_train = compute_stats(train_trades_all)
    overall_test  = compute_stats(test_trades_all)
    print("  " + "─" * 76)
    _print_stats_row("TOTAL TRAIN", overall_train, train_trades_all)
    _print_stats_row("TOTAL TEST",  overall_test,  test_trades_all)
    print()

    # ── Signal distribution ───────────────────────────────────────────────────
    all_test_trades = test_trades_all
    sc = Counter(t["signal"] for t in all_test_trades)
    if all_test_trades:
        print("  Test signal distribution:")
        for sig in ["EXTREME", "STRONG", "MODERATE"]:
            cnt = sc.get(sig, 0)
            if cnt:
                sig_t   = [t for t in all_test_trades if t["signal"] == sig]
                sig_s   = compute_stats(sig_t)
                print(f"    {sig:<10}  {cnt} trades  WR:{sig_s['win_rate']}%  "
                      f"Exp:{sig_s['expectancy']:+.2f}%/trade  Net:{sig_s['net_pct']:+.1f}%")
        print()

    # ── Write JSON results ────────────────────────────────────────────────────
    results = {
        "meta": {
            "interval":              INTERVAL,
            "kline_limit":           KLINE_LIMIT,
            "window":                WINDOW,
            "train_frac":            TRAIN_FRAC,
            "max_hold_h":            MAX_HOLD_CANDLES,
            "atr_sl_mult":           ATR_SL_MULT,
            "atr_tp_mult":           ATR_TP_MULT,
            "atr_sl_min":            ATR_SL_MIN,
            "atr_sl_max":            ATR_SL_MAX,
            "partial_tp_enabled":    PARTIAL_TP_ENABLED,
            "partial_tp1_atr_mult":  PARTIAL_TP1_ATR_MULT,
            "partial_tp1_qty_pct":   PARTIAL_TP1_QTY_PCT,
            "vol_sizing_enabled":    VOL_SIZING_ENABLED,
            "target_risk_pct":       TARGET_RISK_PCT,
            "divergence_enabled":             DIVERGENCE_ENABLED,
            "breakeven_enabled":              BREAKEVEN_ENABLED,
            "progressive_trailing_enabled":   PROGRESSIVE_TRAILING_ENABLED,
            "note":                           "No F&G or BTC dominance filter (not available historically)",
        },
        "overall_train": overall_train,
        "overall_test":  overall_test,
        "by_symbol": {
            sym: {
                "train": {"stats": d["train_stats"], "trades": d["train_trades"]},
                "test":  {"stats": d["test_stats"],  "trades": d["test_trades"]},
            }
            for sym, d in symbol_data.items()
        },
    }

    try:
        with open(RESULTS_JSON, "w") as f:
            json.dump(results, f, indent=2)
        print(f"  Results written to {RESULTS_JSON}")
    except Exception as e:
        print(f"  Warning: could not write JSON results: {e}")

    print()


if __name__ == "__main__":
    main()
