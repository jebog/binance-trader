from __future__ import annotations

from typing import Any, Optional

from config import DIVERGENCE_LOOKBACK, DIVERGENCE_SWING_DEPTH


# ── Indicators ───────────────────────────────────────────────────────────────
def calc_rsi(closes: list[float], period: int = 14) -> float:
    """Wilder's EMA RSI -- matches TradingView/Binance standard."""
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    if len(gains) < period:
        return 50.0
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    return 100 - (100 / (1 + avg_gain / avg_loss))


def calc_atr(klines: list[list[Any]], period: int = 14) -> Optional[float]:
    """Wilder's ATR -- uses high/low/prev_close from raw klines."""
    trs = []
    for i in range(1, len(klines)):
        high       = float(klines[i][2])
        low        = float(klines[i][3])
        prev_close = float(klines[i-1][4])
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    if len(trs) < period:
        return None
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


def calc_sma(closes: list[float], period: int = 20) -> Optional[float]:
    if len(closes) < period:
        return None
    return sum(closes[-period:]) / period


def detect_bullish_divergence(
    closes: list[float],
    rsi_series: list[float],
    lookback: int = DIVERGENCE_LOOKBACK,
    swing_depth: float = DIVERGENCE_SWING_DEPTH,
) -> Optional[bool]:
    """Detect bullish RSI divergence in the last `lookback` candles.

    Returns:
      True  -- price lower low + RSI higher low (classic bullish divergence)
      False -- price lower low + RSI lower low  (confirmed weakness)
      None  -- ambiguous
    """
    win_c = closes[-lookback:]
    win_r = rsi_series[-lookback:]
    n = len(win_c)

    swings: list[int] = []
    for i in range(1, n - 1):
        c_prev, c_curr, c_next = win_c[i - 1], win_c[i], win_c[i + 1]
        if (c_curr < c_prev and c_curr < c_next
                and (c_prev - c_curr) / c_prev >= swing_depth
                and (c_next - c_curr) / c_next >= swing_depth):
            swings.append(i)

    if len(swings) < 2:
        return None

    i1, i2 = swings[-2], swings[-1]
    price_lower = win_c[i2] < win_c[i1]
    rsi_higher  = win_r[i2] > win_r[i1]

    if price_lower and rsi_higher:
        return True
    if price_lower and not rsi_higher:
        return False
    return None
