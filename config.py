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

# ── Cron mode (launchd) ──────────────────────────────────────
# Set CRON_ENABLED=true in .env to enable the launchd cron job.
# When false, run_scanner.sh exits immediately (no-op).
# The TUI has full feature parity — cron is optional.
CRON_ENABLED = os.getenv("CRON_ENABLED", "false").lower() == "true"

# ── Trading pairs & capital ───────────────────────────────────
PAIRS   = ["ETHUSDC", "ADAUSDC", "DOGEUSDC", "BNBUSDC", "SOLUSDC", "XRPUSDC"]
CAPITAL = 200.0   # USDC per trade

# ── Risk management ───────────────────────────────────────────
MAX_POSITIONS    = 2     # max concurrent open positions
SL_COOLDOWN_H    = 8     # hours to block a pair after a stop-loss hit (was 4)
MAX_DRAWDOWN_PCT = 0.15  # halt new orders if portfolio drops >15% from peak
DIGEST_HOUR      = 8    # local hour (0–23) to send morning digest

# ── SL / TP ───────────────────────────────────────────────────
STOP_LOSS      = 0.03   # 3%   — fixed fallback when ATR unavailable
TAKE_PROFIT    = 0.05   # 5%   — fixed fallback (was 7.5% — too ambitious)
TRAILING_DELTA = 150    # basis points for trailing stop (150 = 1.5%); 0 = fixed SL
ATR_SL_MULT    = 1.5    # SL = ATR × 1.5
ATR_TP_MULT    = 2.5    # TP = ATR × 2.5  → ~1.67 R/R (was 3.5 — TP never hit)
ATR_SL_MIN     = 0.02   # SL floor  (never tighter than 2%)
ATR_SL_MAX     = 0.04   # SL ceiling (never wider than 4%, was 6%)

# ── RSI divergence filter (T2-2) ─────────────────────────────────────────────
DIVERGENCE_ENABLED     = True
DIVERGENCE_LOOKBACK    = 20     # candles to scan for swing lows
DIVERGENCE_SWING_DEPTH = 0.005  # swing low must be ≥ 0.5% below both neighbors

# ── 15m entry refinement (T4-2) ──────────────────────────────────────────────
ENTRY_REFINE_ENABLED     = True
ENTRY_REFINE_15M_RSI_MAX = 45    # skip order if 15m RSI > this (momentum peaked on shorter TF)
ENTRY_REFINE_15M_LIMIT   = 50    # candles to fetch: 14 seed + 35 Wilder steps → good convergence

# ── Dynamic pair scoring (T4-3) ──────────────────────────────────────────────
PAIR_SCORE_ENABLED    = True
PAIR_SCORE_MIN_TRADES = 3     # minimum closed trades per symbol to use score (else neutral 0.5)
PAIR_SCORE_LOOKBACK   = 20    # last N closed trades per symbol to consider

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
BREAKEVEN_ATR_MULT = 0.7    # trigger: price ≥ entry × (1 + 0.7 × atr_pct) — arm earlier (was 1.0)

# ── Progressive trailing stop (T4-4) ──────────────────────────────────────────
PROGRESSIVE_TRAILING_ENABLED = True
# After break-even arms, tighten trailing delta at each ATR milestone.
# Format: list of (atr_mult_trigger, new_trailing_delta_bps).
# Stages apply in order; each fires exactly once (guarded by trailing_stage index).
# WARNING: Do not reorder or remove stages while open trades are active.
# Active trades track progress via an integer stage index; reordering changes
# which trigger/bps pair applies next, potentially tightening the stop prematurely.
PROGRESSIVE_TRAILING_STAGES: list[tuple[float, int]] = [
    (1.0, 120),   # at +1.0×ATR: tighten to 120bps (1.2%) — was 1.5×ATR, 100bps
    (1.5,  80),   # at +1.5×ATR: tighten to  80bps (0.8%) — was 2.0×ATR, 75bps
    (2.0,  50),   # at +2.0×ATR: tighten to  50bps (0.5%) — was 2.5×ATR, 50bps
]

# ── Volatility-adjusted capital sizing (T3-4) ─────────────────────────────────
VOL_SIZING_ENABLED = True
TARGET_RISK_PCT    = 0.015  # target 1.5% portfolio risk per trade (= 1×ATR as SL floor)
VOL_SIZING_MIN     = 0.25   # floor: never below 25% of CAPITAL
VOL_SIZING_MAX     = 1.00   # ceiling: never above 100% of CAPITAL

# ── ETH Accumulation + DCA ────────────────────────────────────
# Long-term accumulation layer targeting 1 ETH, runs independently of scanner.
# Funded from USDC balance, isolated via DCA_RESERVED_USDC kv sentinel so the
# scanner never spends into the DCA reserve.
# Set DCA_ENABLED=true in .env to activate.
DCA_ENABLED       = os.getenv("DCA_ENABLED", "false").lower() == "true"
DCA_TARGET_ASSET  = os.getenv("DCA_TARGET_ASSET", "ETH")
DCA_TARGET_PAIR   = os.getenv("DCA_TARGET_PAIR", "ETHUSDC")
DCA_TARGET_QTY    = float(os.getenv("DCA_TARGET_QTY", "1.0"))     # accumulation goal
DCA_AMOUNT_USDC   = float(os.getenv("DCA_AMOUNT_USDC", "40.0"))   # per weekly buy
DCA_DAY_OF_WEEK   = int(os.getenv("DCA_DAY_OF_WEEK", "3"))        # 0=Mon, 3=Thu, 6=Sun
DCA_HOUR          = int(os.getenv("DCA_HOUR", "10"))              # 0-23 local time
DCA_RESERVE_MULT  = int(os.getenv("DCA_RESERVE_MULT", "10"))      # reserve N weeks upfront
DCA_MIN_SCANNER_USDC = float(os.getenv("DCA_MIN_SCANNER_USDC", "200.0"))  # scanner floor

# ── ETH Staking ───────────────────────────────────────────────
# Auto-stake accumulated ETH on Binance Flexible ETH Staking (no lockup).
# BETH token represents staked ETH 1:1 and earns ~2.5-3% APY.
STAKING_ENABLED    = os.getenv("STAKING_ENABLED", "false").lower() == "true"
STAKING_ASSET      = os.getenv("STAKING_ASSET", "BETH")
STAKING_AUTO_STAKE = os.getenv("STAKING_AUTO_STAKE", "true").lower() == "true"

# ── Persistence ───────────────────────────────────────────────
DB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.db")

# ── Scanner internals ─────────────────────────────────────────
INTERVAL    = "1h"   # candle timeframe
KLINE_LIMIT = 100    # candles per fetch (Wilder RSI needs ≥ 2×period to converge)
