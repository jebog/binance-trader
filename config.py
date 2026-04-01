# ═══════════════════════════════════════════════════════════════
#  config.py — All scanner settings in one place
#
#  Credentials are loaded from the .env file (see .env.example).
#  All other settings can be edited directly here.
# ═══════════════════════════════════════════════════════════════

import os

from dotenv import load_dotenv

load_dotenv()

# ── Binance API credentials ───────────────────────────────────
BINANCE_API_KEY    = os.environ["BINANCE_API_KEY"]
BINANCE_SECRET_KEY = os.environ["BINANCE_SECRET_KEY"]

# ── Telegram ──────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = int(os.environ["TELEGRAM_CHAT_ID"])

# ── Webhook (optional — leave empty to disable) ───────────────
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")

# ── Trading pairs & capital ───────────────────────────────────
PAIRS   = ["ETHUSDC", "ADAUSDC", "DOGEUSDC", "BNBUSDC", "SOLUSDC", "XRPUSDC"]
CAPITAL = 200.0   # USDC per trade

# ── Risk management ───────────────────────────────────────────
MAX_POSITIONS    = 2     # max concurrent open positions
SL_COOLDOWN_H    = 4     # hours to block a pair after a stop-loss hit
MAX_DRAWDOWN_PCT = 0.15  # halt new orders if portfolio drops >15% from peak
DIGEST_HOUR      = 8    # local hour (0–23) to send morning digest

# ── SL / TP ───────────────────────────────────────────────────
STOP_LOSS      = 0.03   # 3%   — fixed fallback when ATR unavailable
TAKE_PROFIT    = 0.075  # 7.5% — fixed fallback
TRAILING_DELTA = 150    # basis points for trailing stop (150 = 1.5%); 0 = fixed SL
ATR_SL_MULT    = 1.5    # SL = ATR × 1.5
ATR_TP_MULT    = 3.5    # TP = ATR × 3.5  → ~2.33 R/R
ATR_SL_MIN     = 0.02   # SL floor  (never tighter than 2%)
ATR_SL_MAX     = 0.06   # SL ceiling (never wider than 6%)

# ── RSI divergence filter (T2-2) ─────────────────────────────────────────────
DIVERGENCE_ENABLED     = True
DIVERGENCE_LOOKBACK    = 20     # candles to scan for swing lows
DIVERGENCE_SWING_DEPTH = 0.005  # swing low must be ≥ 0.5% below both neighbors

# ── 15m entry refinement (T4-2) ──────────────────────────────────────────────
ENTRY_REFINE_ENABLED     = True
ENTRY_REFINE_15M_RSI_MAX = 45    # skip order if 15m RSI > this (momentum peaked on shorter TF)
ENTRY_REFINE_15M_LIMIT   = 50    # candles to fetch: 14 seed + 35 Wilder steps → good convergence

# ── BTC dominance filter (T2-3) ───────────────────────────────────────────────
BTC_DOM_ENABLED        = True
BTC_DOM_CACHE_H        = 1       # cache lifetime hours (CoinGecko free tier: ~50 req/min)
BTC_DOM_RISE_THRESHOLD = 0.005   # 0.5% scan-over-scan rise = "rising dominance"

# ── Partial take-profit (T2-4) ────────────────────────────────────────────────
PARTIAL_TP_ENABLED   = True
PARTIAL_TP1_ATR_MULT = 1.0    # TP1 at entry × (1 + 1.0 × ATR%) — halfway to full TP
PARTIAL_TP1_QTY_PCT  = 0.50   # fraction of position closed at TP1

# ── Split entry (T2-1) ────────────────────────────────────────────────────────
SPLIT_ENTRY_ENABLED  = True
SPLIT_ENTRY_ATR_MULT = 1.0    # second entry triggers at first_fill × (1 - 1 × ATR%)
SPLIT_ENTRY_TTL_H    = 48     # expire pending entry after 48h (Telegram notice sent)

# ── Trade timeout (T3-2) ─────────────────────────────────────────────────────
TRADE_TIMEOUT_ENABLED = True
TRADE_TIMEOUT_H       = 72     # force-exit any position open longer than 72h

# ── Break-even stop (T3-1) ────────────────────────────────────────────────────
BREAKEVEN_ENABLED  = True
BREAKEVEN_ATR_MULT = 1.0    # trigger: price ≥ entry × (1 + BREAKEVEN_ATR_MULT × atr_pct)

# ── Volatility-adjusted capital sizing (T3-4) ─────────────────────────────────
VOL_SIZING_ENABLED = True
TARGET_RISK_PCT    = 0.015  # target 1.5% portfolio risk per trade (= 1×ATR as SL floor)
VOL_SIZING_MIN     = 0.25   # floor: never below 25% of CAPITAL
VOL_SIZING_MAX     = 1.00   # ceiling: never above 100% of CAPITAL

# ── Scanner internals ─────────────────────────────────────────
INTERVAL    = "1h"   # candle timeframe
KLINE_LIMIT = 100    # candles per fetch (Wilder RSI needs ≥ 2×period to converge)
