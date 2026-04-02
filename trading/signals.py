from __future__ import annotations

from typing import Any, Optional

from config import (
    ATR_SL_MAX,
    ATR_SL_MIN,
    ATR_SL_MULT,
    ATR_TP_MULT,
    DIVERGENCE_ENABLED,
    DIVERGENCE_LOOKBACK,
    DIVERGENCE_SWING_DEPTH,
    ENTRY_REFINE_15M_LIMIT,
    ENTRY_REFINE_15M_RSI_MAX,
    ENTRY_REFINE_ENABLED,
    INTERVAL,
    KLINE_LIMIT,
    STOP_LOSS,
    TAKE_PROFIT,
)
from trading.http_client import get
from trading.indicators import calc_atr, calc_rsi, calc_sma, detect_bullish_divergence


# ── Signal logic ─────────────────────────────────────────────────────────────
def analyze(symbol: str, context: dict[str, Any]) -> dict[str, Any]:
    klines = get("/api/v3/klines", {"symbol": symbol, "interval": INTERVAL, "limit": KLINE_LIMIT})
    closed = klines[:-1]
    closes = [float(k[4]) for k in closed]
    vols   = [float(k[5]) for k in closed]

    price     = closes[-1]
    rsi       = calc_rsi(closes)

    # ── RSI divergence series (T2-2) ─────────────────────────────────────────
    rsi_series: Optional[list[float]] = None
    div_result: Optional[bool] = None
    if DIVERGENCE_ENABLED:
        lb  = DIVERGENCE_LOOKBACK + 14 + 28
        win = closes[-lb:]
        rsi_series = [calc_rsi(win[:i]) for i in range(14, len(win) + 1)]
        rsi_series = rsi_series[-DIVERGENCE_LOOKBACK:]

    sma20     = calc_sma(closes, 20)
    above_sma = (sma20 is not None) and (price > sma20)
    avg_vol   = sum(vols[:-1]) / (len(vols) - 1) if len(vols) > 1 else 0
    vol_surge = avg_vol > 0 and vols[-1] > avg_vol * 1.3
    momentum_up = closes[-1] > closes[-5]

    # ── Daily trend filter (multi-timeframe) ─────────────────────────────────
    daily_bullish = True
    daily_rsi_val = None
    try:
        d_klines  = get("/api/v3/klines", {"symbol": symbol, "interval": "1d", "limit": 30})
        d_closed  = d_klines[:-1]
        d_closes  = [float(k[4]) for k in d_closed]
        d_rsi     = calc_rsi(d_closes)
        d_sma20   = calc_sma(d_closes, 20)
        daily_rsi_val  = round(d_rsi, 1)
        d_above_sma    = (d_sma20 is not None) and (d_closes[-1] > d_sma20)
        daily_bullish  = d_rsi > 45 and d_above_sma
        daily_neutral  = d_rsi >= 30 and not daily_bullish
        daily_bearish  = not daily_bullish and not daily_neutral
    except Exception:
        daily_neutral  = False
        daily_bearish  = False

    fg             = context["fg_value"]
    btc_above      = context["btc_above_sma"]
    btc_dom_rising = context.get("btc_dom_rising", False)

    # ── RSI divergence gate (T2-2) ────────────────────────────────────────────
    divergence_ok = True
    if DIVERGENCE_ENABLED and rsi_series and len(rsi_series) >= 4:
        div_result = detect_bullish_divergence(
            closes, rsi_series, DIVERGENCE_LOOKBACK, DIVERGENCE_SWING_DEPTH,
        )
        if div_result is False:
            divergence_ok = False

    # ── Signal tiers (1h thresholds) ─────────────────────────────────────────
    extreme_signal = rsi < 25
    extreme_quality = extreme_signal and above_sma and fg < 40

    strong_signal = rsi < 32 and above_sma and fg < 75 and not daily_bearish and divergence_ok

    moderate_signal = (
        rsi < 40 and above_sma and vol_surge and momentum_up
        and fg < 60
        and btc_above
        and daily_bullish
        and divergence_ok
        and not btc_dom_rising
    )

    ticker     = get("/api/v3/ticker/24hr", {"symbol": symbol})
    change_pct = float(ticker["priceChangePercent"])

    if extreme_signal:
        strength = "EXTREME"
    elif strong_signal:
        strength = "STRONG"
    elif moderate_signal:
        strength = "MODERATE"
    else:
        strength = "NONE"

    return {
        "symbol":           symbol,
        "price":            price,
        "rsi":              round(rsi, 1),
        "daily_rsi":        daily_rsi_val,
        "sma20":            round(sma20, 6) if sma20 is not None else None,
        "above_sma":        above_sma,
        "vol_surge":        vol_surge,
        "momentum":         momentum_up,
        "change24h":        change_pct,
        "buy_signal":       extreme_signal or strong_signal or moderate_signal,
        "signal_strength":  strength,
        "extreme_quality":  extreme_quality,
        "divergence":       div_result,
        "btc_dom_rising":   btc_dom_rising,
        "closed_klines":    closed,
    }


def _estimate_sl_tp_pct(s: dict[str, Any]) -> tuple[float, float]:
    """Estimate SL/TP % for pre-order display -- mirrors place_buy_order ATR logic."""
    if ATR_SL_MULT > 0 and s.get("closed_klines"):
        atr = calc_atr(s["closed_klines"])
        if atr is not None:
            atr_pct = atr / s["price"]
            sl_pct = min(max(atr_pct * ATR_SL_MULT, ATR_SL_MIN), ATR_SL_MAX)
            tp_pct = sl_pct * (ATR_TP_MULT / ATR_SL_MULT)
            return sl_pct, tp_pct
    return STOP_LOSS, TAKE_PROFIT


def _get_15m_rsi(symbol: str) -> Optional[float]:
    """Fetch latest 15m RSI for a symbol. Returns None on failure (fail-open)."""
    try:
        klines = get("/api/v3/klines", {"symbol": symbol, "interval": "15m",
                                        "limit": ENTRY_REFINE_15M_LIMIT})
        closes = [float(k[4]) for k in klines]
        return calc_rsi(closes)
    except Exception as e:
        print(f"  \u26a0 15m RSI fetch failed for {symbol}: {e} \u2014 fail-open")
        return None


def _check_15m_rsi_gate(symbol: str) -> Optional[float]:
    """Return the blocking RSI value if entry should be deferred, else None."""
    if not ENTRY_REFINE_ENABLED:
        return None
    rsi_15m = _get_15m_rsi(symbol)
    if rsi_15m is not None and rsi_15m > ENTRY_REFINE_15M_RSI_MAX:
        return rsi_15m
    return None
