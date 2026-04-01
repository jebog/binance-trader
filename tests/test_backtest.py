"""
Unit tests for backtest.py simulation logic.

All tests use synthetic klines (no network calls).
Kline format: [open_time, open, high, low, close, vol]  (Binance format)
  k[2]=high, k[3]=low, k[4]=close, k[5]=vol

Price design — 100-candle declining window:
  prices[k] = 100 - k * 0.5  →  klines[99][4] = 50.5  (entry price)
  ATR ≈ 0.61  →  atr_pct_raw ≈ 0.0122  →  sl_pct = 0.02 (clamped to ATR_SL_MIN)
  sl_price   ≈ 49.49   (entry * 0.98)
  tp_price   ≈ 52.86   (entry * (1 + 0.02 * ATR_TP_MULT/ATR_SL_MULT))
  be_trigger ≈ 51.11   (entry * (1 + 1 × atr_pct_raw))   — uses raw ATR%, NOT sl_pct
  tp1_price  ≈ 51.17   (entry * (1 + 1 × sl_pct/ATR_SL_MULT)) — uses clamped sl_pct
  stage1_trig≈ 51.42   (entry * (1 + 1.5 × atr_pct_raw))

Key implementation note: progressive trailing updates sl_price on the same candle it fires.
Tests that want clean TP/SL/TIMEOUT outcomes patch BREAKEVEN_ENABLED=False and
PROGRESSIVE_TRAILING_ENABLED=False to eliminate that interaction.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from backtest import backtest_symbol, compute_stats

# ── Kline builder helpers ──────────────────────────────────────────────────────

ENTRY_PRICE = 50.5   # klines[99][4] with the standard declining window
SPREAD      = 0.002  # ±0.2% default spread


def _kline(price: float, high: float | None = None, low: float | None = None,
           vol: float = 1.0) -> list:
    """Binance kline: [open_time, open, high, low, close, vol].
    backtest.py reads k[2]=high, k[3]=low, k[4]=close, k[5]=vol.
    """
    h = high if high is not None else price * (1 + SPREAD)
    lo = low if low is not None else price * (1 - SPREAD)
    return [0, price, h, lo, price, vol]


def _declining_window() -> list[list]:
    """100 declining candles — RSI will be < 25 (EXTREME signal).
    Last close = 50.5 (= ENTRY_PRICE).
    """
    return [_kline(100.0 - i * 0.5) for i in range(100)]


def _run(post_signal: list[list]) -> list[dict[str, Any]]:
    """Run backtest with the standard window + post-signal candles.

    The loop in backtest_symbol fires the signal at i=100 (open_trade set,
    entry at klines[99]).  The FIRST check candle is klines[101] (i=101).
    A neutral filler at klines[100] is needed so the range reaches i=100
    and the check can happen at i=101.
    """
    filler = _kline(ENTRY_PRICE)                         # klines[100]: triggers signal detection
    klines = _declining_window() + [filler] + post_signal
    return backtest_symbol("TESTUSDC", klines)


# ── Trade outcome tests (be + trailing patched out for isolation) ──────────────

_no_be = patch("backtest.BREAKEVEN_ENABLED", False)
_no_tr = patch("backtest.PROGRESSIVE_TRAILING_ENABLED", False)


def test_tp_hit():
    """High of first check candle clears TP — outcome TP."""
    tp_kline = _kline(ENTRY_PRICE, high=ENTRY_PRICE * 1.12, low=ENTRY_PRICE * 1.001)
    with _no_be, _no_tr:
        trades = _run([tp_kline])
    assert len(trades) == 1
    t = trades[0]
    assert t["outcome"] == "TP"
    assert t["pnl_pct"] > 0


def test_sl_hit():
    """Low of first check candle falls below SL — outcome SL."""
    sl_kline = _kline(ENTRY_PRICE, high=ENTRY_PRICE * 1.001, low=ENTRY_PRICE * 0.95)
    with _no_be, _no_tr:
        trades = _run([sl_kline])
    assert len(trades) == 1
    t = trades[0]
    assert t["outcome"] == "SL"
    assert t["pnl_pct"] < 0


def test_timeout():
    """72 neutral candles with no TP/SL hit → TIMEOUT at market.
    71 post-signal candles used so range(100,172) ends exactly at i=171 (held=72)
    without leaving a trailing iteration that could fire a second signal.
    """
    neutral = _kline(ENTRY_PRICE)
    with _no_be, _no_tr:
        trades = _run([neutral] * 71)
    assert len(trades) == 1
    t = trades[0]
    assert t["outcome"] == "TIMEOUT"
    assert t["candles_held"] == 72


def test_end_of_data_force_close():
    """Trade still open when klines run out → force-closed as TIMEOUT."""
    neutral = _kline(ENTRY_PRICE)
    with _no_be, _no_tr:
        trades = _run([neutral] * 5)
    assert len(trades) == 1
    t = trades[0]
    assert t["outcome"] == "TIMEOUT"
    assert t["exit_price"] == ENTRY_PRICE


def test_partial_tp1_credited():
    """TP1 fills on candle N, full TP on candle N+1 — P&L is weighted average."""
    # Candle 0 (i=101): high above tp1_price (~51.17) but below tp2 (~52.86)
    tp1_candle = _kline(ENTRY_PRICE, high=ENTRY_PRICE * 1.017, low=ENTRY_PRICE * 1.001)
    # Candle 1 (i=102): high above tp2
    tp2_candle = _kline(ENTRY_PRICE, high=ENTRY_PRICE * 1.08, low=ENTRY_PRICE * 1.001)
    with _no_be, _no_tr:
        trades = _run([tp1_candle, tp2_candle])
    assert len(trades) == 1
    t = trades[0]
    assert t["outcome"] == "TP"
    assert t["partial_tp1_hit"] is True
    assert t["pnl_pct"] > 0


def test_same_candle_tp1_no_credit():
    """TP1 and full TP fill on the same candle — TP1 credit is NOT applied (intra-candle
    order undefined; same-candle guard prevents optimistic crediting)."""
    # Single candle with high well above tp2 — tp1 and TP2 both hit on candle i=101
    one_candle = _kline(ENTRY_PRICE, high=ENTRY_PRICE * 1.12, low=ENTRY_PRICE * 1.001)
    with _no_be, _no_tr:
        trades = _run([one_candle])
    assert len(trades) == 1
    t = trades[0]
    assert t["outcome"] == "TP"
    assert t["partial_tp1_hit"] is True
    # tp1_candle_idx == exit candle idx → NOT credited → pnl = simple full-position P&L
    tp1_candle_idx = t.get("tp1_candle_idx")
    exit_idx = t["exit_candle_idx"]
    assert tp1_candle_idx == exit_idx   # same-candle guard in play


# ── Break-even and progressive trailing (real config values) ──────────────────
# Actual values from the declining-window trade (atr_pct_raw ≈ 0.01216):
#   sl_pct      = 0.02    (clamped to ATR_SL_MIN; raw 0.01216 * 1.5 < 0.02)
#   be_trigger  ≈ 51.11   (entry * (1 + 1.0 × atr_pct_raw))
#   stage1_trig ≈ 51.42   (entry * (1 + 1.5 × atr_pct_raw))
#   stage2_trig ≈ 51.73   (entry * (1 + 2.0 × atr_pct_raw))
#   stage1 trailing_sl = peak * 0.99  (100 bps)

def test_breakeven_arms_on_trigger():
    """Once price reaches be_trigger, breakeven_moved is set and sl persisted."""
    # high=51.31 > be_trigger(~51.11); low safely above entry so SL does not fire
    be_candle   = _kline(ENTRY_PRICE, high=ENTRY_PRICE * 1.016, low=ENTRY_PRICE * 1.003)
    # Drop candle: now sl=entry=50.5; low drops below entry → SL fires
    drop_candle = _kline(ENTRY_PRICE, high=ENTRY_PRICE * 1.001, low=ENTRY_PRICE * 0.997)
    trades = _run([be_candle, drop_candle])
    assert len(trades) == 1
    t = trades[0]
    assert t["breakeven_moved"] is True
    assert t["outcome"] == "SL"


def test_breakeven_save():
    """Break-even floor catches a falling trade at entry — exit at or above entry."""
    be_candle   = _kline(ENTRY_PRICE, high=ENTRY_PRICE * 1.016, low=ENTRY_PRICE * 1.003)
    drop_candle = _kline(ENTRY_PRICE, high=ENTRY_PRICE * 1.001, low=ENTRY_PRICE * 0.997)
    trades = _run([be_candle, drop_candle])
    assert len(trades) == 1
    t = trades[0]
    assert t["outcome"] == "SL"
    assert t["breakeven_moved"] is True
    assert t["exit_price"] >= t["entry"]


def test_breakeven_not_set_before_trigger():
    """SL fires before be_trigger reached — breakeven_moved stays False."""
    sl_kline = _kline(ENTRY_PRICE, high=ENTRY_PRICE * 1.001, low=ENTRY_PRICE * 0.95)
    trades = _run([sl_kline])
    assert len(trades) == 1
    assert trades[0]["breakeven_moved"] is False


def test_progressive_trailing_tightens_sl():
    """Trailing tightens at stage-1 milestone; exit SL is above entry (profitable)."""
    # Candle 0 (i=101): be arms (high 51.31 > be_trig 51.11); stage1_trig=51.42 not yet hit
    #   low=51.16 > entry(50.5) → no SL on this candle
    be_candle   = _kline(ENTRY_PRICE, high=ENTRY_PRICE * 1.016, low=ENTRY_PRICE * 1.013)

    # Candle 1 (i=102): stage-1 trigger hit (high 51.51 > stage1 51.42); not stage2 (51.73)
    #   peak=51.51, trailing_sl=51.51*0.99=50.995; low=51.31 > 50.995 → no SL
    stg1_candle = _kline(ENTRY_PRICE, high=ENTRY_PRICE * 1.020, low=ENTRY_PRICE * 1.016)

    # Candle 2 (i=103): peak rises; trailing_sl tightens above low → SL
    #   high=51.61 → peak=51.61, trailing_sl=51.61*0.99=51.094; stage2_trig=51.73 not hit
    #   low=51.00 < trailing_sl(51.094) → SL fires; exit_price=51.094 > entry(50.5)
    exit_candle = _kline(ENTRY_PRICE, high=ENTRY_PRICE * 1.022, low=ENTRY_PRICE * 1.010)

    trades = _run([be_candle, stg1_candle, exit_candle])
    assert len(trades) == 1
    t = trades[0]
    assert t["outcome"] == "SL"
    assert t["breakeven_moved"] is True
    assert t["trailing_stage"] == 1
    assert t["exit_price"] > t["entry"]


# ── compute_stats tests ────────────────────────────────────────────────────────

def _trade(outcome: str, pnl: float, entry: float = 50.0, exit_price: float | None = None,
           breakeven_moved: bool = False) -> dict[str, Any]:
    ep = exit_price if exit_price is not None else entry * (1 + pnl / 100)
    return {
        "outcome": outcome, "pnl_pct": pnl, "pnl_usdc": pnl,
        "entry": entry, "exit_price": ep, "breakeven_moved": breakeven_moved,
    }


def test_compute_stats_empty():
    s = compute_stats([])
    assert s["n"] == 0
    assert s["breakeven_saves"] == 0


def test_compute_stats_counts():
    trades = [
        _trade("TP",      5.0),
        _trade("SL",     -3.0),
        _trade("TIMEOUT", 0.5),
    ]
    s = compute_stats(trades)
    assert s["n"]        == 3
    assert s["wins"]     == 1
    assert s["losses"]   == 1
    assert s["timeouts"] == 1


def test_compute_stats_win_rate():
    import pytest
    trades = [_trade("TP", 5.0), _trade("TP", 3.0), _trade("SL", -2.0)]
    s = compute_stats(trades)
    assert s["win_rate"] == pytest.approx(66.7, abs=0.1)


def test_compute_stats_net_and_expectancy():
    import pytest
    trades = [_trade("TP", 10.0), _trade("TP", 5.0), _trade("SL", -3.0)]
    s = compute_stats(trades)
    assert s["net_pct"]    == pytest.approx(12.0)
    assert s["expectancy"] == pytest.approx(4.0)


def test_compute_stats_breakeven_saves_counted():
    """SL exits at or above entry with breakeven_moved=True count as breakeven_saves."""
    trades = [
        _trade("SL",  0.0, entry=50.0, exit_price=50.0,  breakeven_moved=True),   # flat — save
        _trade("SL",  0.5, entry=50.0, exit_price=50.25, breakeven_moved=True),   # slight profit — save
        _trade("SL", -2.0, entry=50.0, exit_price=49.0,  breakeven_moved=False),  # real loss
        _trade("SL", -1.0, entry=50.0, exit_price=49.5,  breakeven_moved=True),   # be moved but still lost
    ]
    s = compute_stats(trades)
    assert s["breakeven_saves"] == 2


def test_compute_stats_breakeven_save_requires_be_moved():
    """A flat SL exit without breakeven_moved does NOT count as a save."""
    trades = [_trade("SL", 0.0, entry=50.0, exit_price=50.0, breakeven_moved=False)]
    s = compute_stats(trades)
    assert s["breakeven_saves"] == 0
