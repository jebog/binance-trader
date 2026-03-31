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
MAX_POSITIONS = 2   # max concurrent open positions
SL_COOLDOWN_H = 4   # hours to block a pair after a stop-loss hit

# ── SL / TP ───────────────────────────────────────────────────
STOP_LOSS      = 0.03   # 3%   — fixed fallback when ATR unavailable
TAKE_PROFIT    = 0.075  # 7.5% — fixed fallback
TRAILING_DELTA = 150    # basis points for trailing stop (150 = 1.5%); 0 = fixed SL
ATR_SL_MULT    = 1.5    # SL = ATR × 1.5
ATR_TP_MULT    = 3.5    # TP = ATR × 3.5  → ~2.33 R/R
ATR_SL_MIN     = 0.02   # SL floor  (never tighter than 2%)
ATR_SL_MAX     = 0.06   # SL ceiling (never wider than 6%)

# ── Scanner internals ─────────────────────────────────────────
INTERVAL    = "1h"   # candle timeframe
KLINE_LIMIT = 100    # candles per fetch (Wilder RSI needs ≥ 2×period to converge)
