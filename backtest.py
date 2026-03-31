#!/usr/bin/env python3
"""
Standalone backtest script — no dependencies beyond stdlib.

Strategy:  RSI/SMA/Vol/Momentum signals (EXTREME/STRONG/MODERATE tiers)
           mirroring scanner.py logic exactly.
Filters:   No Fear & Greed or BTC context filter applied
           (not available historically without extra API calls).
Data:      Binance public klines API, 1h, 1000 candles (~41 days) per pair.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
import urllib.parse
import urllib.error
from typing import Any, Optional

# ── Config ────────────────────────────────────────────────────────────────────
from config import (
    PAIRS,
    STOP_LOSS, TAKE_PROFIT,
    ATR_SL_MULT, ATR_TP_MULT, ATR_SL_MIN, ATR_SL_MAX,
)

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


def calc_atr(klines: list[list], period: int = 14) -> Optional[float]:
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

def fetch_klines(symbol: str, interval: str = INTERVAL, limit: int = KLINE_LIMIT) -> list[list]:
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


# ── Signal logic (mirrors scanner.py analyze(), no F&G / BTC filter) ──────────

def compute_signal(window_klines: list[list]) -> tuple[str, float, bool, bool, bool, Optional[float], float]:
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

def backtest_symbol(symbol: str, klines: list[list]) -> list[dict[str, Any]]:
    """
    Rolling-window simulation over all klines for one symbol.
    Returns list of trade dicts.
    """
    trades = []
    open_trade = None   # at most one open trade at a time per symbol

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
            tp_hit   = high >= tp_price
            sl_hit   = low  <= sl_price
            held     = j - open_trade["entry_candle_idx"]

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

            entry       = open_trade["entry"]
            pnl_pct     = (exit_price - entry) / entry * 100
            open_trade["outcome"]       = outcome
            open_trade["exit_price"]    = exit_price
            open_trade["exit_candle_idx"] = j
            open_trade["candles_held"]  = held
            open_trade["pnl_pct"]       = round(pnl_pct, 4)
            trades.append(open_trade)
            open_trade = None
            continue

        # Compute signal on the closed window
        signal, rsi, above_sma, vol_surge, momentum_up, atr, price = compute_signal(window)

        if signal == "NONE":
            continue

        # Entry at close of candle[i-1] (last candle in window = klines[i-1])
        entry_candle_idx = i - 1
        entry_price      = float(klines[entry_candle_idx][4])

        # ATR-based SL/TP
        if atr is not None and entry_price > 0:
            atr_pct  = atr / entry_price
            sl_pct   = max(ATR_SL_MIN, min(ATR_SL_MAX, atr_pct * ATR_SL_MULT))
            tp_pct   = sl_pct * (ATR_TP_MULT / ATR_SL_MULT)
        else:
            sl_pct = STOP_LOSS
            tp_pct = TAKE_PROFIT

        sl_price = entry_price * (1 - sl_pct)
        tp_price = entry_price * (1 + tp_pct)

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
        }

    # If a trade is still open at end of data, force-close at last candle
    if open_trade is not None:
        last_idx   = len(klines) - 1
        exit_price = float(klines[last_idx][4])
        held       = last_idx - open_trade["entry_candle_idx"]
        pnl_pct    = (exit_price - open_trade["entry"]) / open_trade["entry"] * 100
        open_trade["outcome"]         = "TIMEOUT"
        open_trade["exit_price"]      = exit_price
        open_trade["exit_candle_idx"] = last_idx
        open_trade["candles_held"]    = held
        open_trade["pnl_pct"]         = round(pnl_pct, 4)
        trades.append(open_trade)

    return trades


# ── Stats helpers ──────────────────────────────────────────────────────────────

def compute_stats(trades: list[dict[str, Any]]) -> dict[str, Any]:
    if not trades:
        return {
            "n": 0, "wins": 0, "losses": 0, "timeouts": 0,
            "win_rate": 0.0, "avg_tp_pct": 0.0, "avg_sl_pct": 0.0,
            "net_pct": 0.0, "expectancy": 0.0,
        }
    wins     = [t for t in trades if t["outcome"] == "TP"]
    losses   = [t for t in trades if t["outcome"] == "SL"]
    timeouts = [t for t in trades if t["outcome"] == "TIMEOUT"]

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

    return {
        "n":         n,
        "wins":      nw,
        "losses":    nl,
        "timeouts":  nt,
        "win_rate":  round(wr, 1),
        "avg_tp_pct": round(avg_win, 2),
        "avg_sl_pct": round(avg_loss, 2),
        "avg_to_pct": round(avg_to, 2),
        "net_pct":   round(net, 2),
        "expectancy": round(exp, 2),
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def _print_stats_row(label: str, s: dict[str, Any], trades: list[dict[str, Any]], suffix: str = "") -> None:
    from collections import Counter
    c   = Counter(t["signal"] for t in trades)
    sig = " ".join(f"{k[0]}{k[1:3].lower()}:{c[k]}" for k in ["EXTREME","STRONG","MODERATE"] if c[k]) or "—"
    to_note = f"  TO:{s['timeouts']}" if s["timeouts"] else ""
    print(
        f"  {label:<12}  {s['n']:>2} trades  {s['wins']}W/{s['losses']}L{to_note:<6}"
        f"  WR:{s['win_rate']:.1f}%  AvgTP:{s['avg_tp_pct']:+.1f}%  "
        f"AvgSL:{s['avg_sl_pct']:+.1f}%  Net:{s['net_pct']:+.1f}%{suffix}  [{sig}]"
    )


def main() -> None:
    from collections import Counter
    print()
    print("══════════════════════════════════════════════════════════════")
    print("  BACKTEST — 1h · 1000 candles · ~41 days")
    print(f"  Walk-forward split: {int(TRAIN_FRAC*100)}% train / {100-int(TRAIN_FRAC*100)}% test")
    print("  Signal filters: RSI/SMA/Vol/Momentum")
    print("  NOTE: No F&G or BTC trend filter (not available historically)")
    print("  SL/TP: ATR-based, SL=ATR×1.5 clamped [2%,6%], TP=SL×(3.5/1.5)")
    print("  Max hold: 72 candles (3 days) → TIMEOUT at market")
    print("══════════════════════════════════════════════════════════════")
    print()

    train_trades_all = []
    test_trades_all  = []
    symbol_data      = {}   # {symbol: {train_stats, test_stats, train_trades, test_trades}}

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
            "interval":       INTERVAL,
            "kline_limit":    KLINE_LIMIT,
            "window":         WINDOW,
            "train_frac":     TRAIN_FRAC,
            "max_hold_h":     MAX_HOLD_CANDLES,
            "atr_sl_mult":    ATR_SL_MULT,
            "atr_tp_mult":    ATR_TP_MULT,
            "atr_sl_min":     ATR_SL_MIN,
            "atr_sl_max":     ATR_SL_MAX,
            "note":           "No F&G or BTC trend filter applied (backtest limitation)",
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
