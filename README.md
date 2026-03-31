# Binance Trading Scanner

Personal macOS algorithmic scanner that monitors 6 spot pairs every 10 minutes, detects multi-tier RSI + SMA + volume + sentiment buy signals, and places confirmed market orders with automatic ATR-based OCO exit brackets.

---

## Table of Contents

1. [Quick Start](#quick-start)
2. [Project Structure](#project-structure)
3. [Running Modes](#running-modes)
4. [TUI App](#tui-app)
5. [Strategy](#strategy)
6. [Configuration Reference](#configuration-reference)
7. [Order Flow](#order-flow)
8. [Backtest](#backtest)
9. [Dashboard](#dashboard)
10. [Telegram Integration](#telegram-integration)
11. [launchd Setup (macOS)](#launchd-setup-macos)
12. [Credentials](#credentials)
13. [Troubleshooting](#troubleshooting)

---

## Quick Start

```bash
# 1 — Clone and install dependencies
git clone https://github.com/your-username/trading-scanner.git
cd trading-scanner
pip3 install -r requirements.txt

# 2 — Set your credentials
cp .env.example .env
# Edit .env and fill in BINANCE_API_KEY, BINANCE_SECRET_KEY, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID

# 3a — Interactive scan (manual confirm prompt)
python3 scanner.py

# 3b — Real-time TUI dashboard + scan (recommended)
python3 tui.py

# 3c — Cron mode (no prompt, Telegram confirm)
SCANNER_CRON=1 python3 scanner.py
```

---

## Project Structure

```
Trading/
├── config.py               Single source of truth for all settings (reads from .env)
├── scanner.py              Core engine — indicators, signals, orders, state, Telegram
├── tui.py                  Real-time TUI dashboard (Textual)
├── tui.tcss                Catppuccin Mocha theme for the TUI
├── backtest.py             Walk-forward backtester (stdlib only, no look-ahead)
├── run_scanner.sh          Shell wrapper for launchd (loads .env, runs cron mode)
├── requirements.txt        Python dependencies
├── .env.example            Credential template — copy to .env and fill in values
├── LICENSE                 MIT
├── state.json              Runtime state — written each run, read by TUI (gitignored)
├── scanner.log             Append-only run log (gitignored)
├── backtest_results.json   Output of last backtest run (gitignored)
└── dashboard.html          Auto-generated HTML dashboard (gitignored)

~/Library/LaunchAgents/
└── com.trading.scanner.plist   launchd job — runs every 30 minutes
```

> **Never edit `state.json` or `scanner.log` manually** — they are overwritten/appended on every run.

---

## Running Modes

### Interactive mode (default)

```bash
python3 scanner.py
```

Runs a full scan, prints results, and if a signal fires prompts:

```
Type CONFIRM to place order(s), or SKIP to skip:
```

Type `CONFIRM` (uppercase) to execute. Anything else skips.

### TUI mode (recommended)

```bash
python3 tui.py
```

Full real-time terminal dashboard. See [TUI App](#tui-app) below.

### Cron / Telegram mode

```bash
SCANNER_CRON=1 python3 scanner.py
```

No stdin prompt. If a signal fires, the scanner sends a Telegram alert and waits up to 120 seconds for a `CONFIRM` or `SKIP` reply. This is what `run_scanner.sh` uses with launchd.

---

## TUI App

```bash
pip3 install -r requirements.txt   # one-time
python3 tui.py
```

### Layout

```
┌─ Header ───────────────────────────────────────────────────────────────┐
│  ◉ TRADING SCANNER   F&G: 45 Fear  │  BTC $66,395  RSI:52.1 ↑  ████  │
├─ Left panel (30) ────┬─ Market ─ Positions ─ History ─ Backtest ───── │
│ PORTFOLIO  $2,706    │  ┌─ ETHUSDC ──────┐  ┌─ ADAUSDC ──────┐        │
│ ETH  51%  ████████   │  │ $1,998   RSI 44│  │ $0.248   RSI 38│        │
│ USDC 45%  ███████    │  │ 1d:52  NONE    │  │ 1d:41  NONE    │        │
│ ADA   4%  ▌          │  │ ▁▂▃▄▅▆▇█▇▆▅   │  │ ▃▃▄▅▆▇▇▆▇█▇   │        │
│                      │  └────────────────┘  └────────────────┘        │
│ COOLDOWNS            │                                                  │
│ None active          │                                                  │
│                      │                                                  │
│ PERFORMANCE          │                                                  │
│ 0W / 0L  —% WR       │                                                  │
├─ Log strip ──────────┴──────────────────────────────────────────────── │
│  10:01:30  ETH RSI 44.5 1d:52 — NONE | ADA RSI 38.2 1d:41 — NONE     │
├─ Status bar ───────────────────────────────────────────────────────── │
│  [S] Scan [R] Refresh [P] Panel [E] Equity [C] Settings [L] Log [Q]  │
└────────────────────────────────────────────────────────────────────── ┘
```

### Key bindings

| Key | Action |
|-----|--------|
| `S` | Run a full scan now |
| `R` | Re-read `state.json` from disk |
| `P` | Toggle left portfolio panel |
| `E` | Toggle left panel: portfolio ↔ equity curve |
| `C` | Open settings (scan interval) |
| `L` | Toggle log strip |
| `Q` | Quit |

### Order confirmation modal

When a signal passes all guards, a modal appears automatically:

```
┌─── 🟠 STRONG BUY SIGNAL ──────────────────────┐
│  Pair:    ETHUSDC                               │
│  Entry:   $1,998.30   RSI 28.4                  │
│  TP:      $2,198.13  (+10.0%)                   │
│  SL:      $1,878.39  (-6.0%)                    │
│  Capital: $200 USDC                             │
│  [✓ CONFIRM  Enter/Y]   [✗ SKIP  Esc/N]        │
└─────────────────────────────────────────────────┘
```

TP/SL shown are ATR-estimated — the actual OCO prices are computed from the live fill price after the market order executes.

### Auto-refresh

| Timer | Interval | What it does |
|-------|----------|--------------|
| State watcher | 5 s | Reads `state.json` (disk only — no API calls) |
| Auto-scan | 30 s | Full Binance API scan in background thread |

The TUI and the launchd cron job are independent. When cron writes `state.json`, the TUI detects the update within 5 seconds via the state watcher.

---

## Strategy

### Pairs monitored

`ETHUSDC` · `ADAUSDC` · `DOGEUSDC` · `BNBUSDC` · `SOLUSDC` · `XRPUSDC`

### Timeframes

- **1h candles** — signal generation (RSI, SMA, volume, ATR)
- **1d candles** — trend filter (fetched per pair, last 30 daily candles)

The daily timeframe classifies each pair's broader trend before the 1h signal is evaluated:

| Daily state | daily RSI | Price vs SMA20 | Effect |
|-------------|-----------|----------------|--------|
| **Bullish** | > 45 | above | MODERATE allowed |
| **Neutral** | 30–45 | any | STRONG allowed, MODERATE blocked |
| **Bearish** | < 30 | below | STRONG blocked, EXTREME still fires |

EXTREME signals bypass the daily filter — deep oversold readings are entries regardless of trend.

### Market filters (fetched once per scan)

| Filter | Source | Effect |
|--------|--------|--------|
| **Fear & Greed Index** | alternative.me | Blocks MODERATE entries when F&G ≥ 60; blocks STRONG when F&G ≥ 75 |
| **BTC RSI + SMA20** | Binance 1h klines | Blocks MODERATE entries when BTC is below its SMA20 |

Both are fetched once and shared across all pairs. F&G is cached in `state.json` for 25 hours. If the live fetch fails, the cache is used. If the cache is also expired, neutral 50 is used and a Telegram warning is sent.

### Signal tiers

| Tier | Condition | Capital |
|------|-----------|---------|
| **EXTREME** (quality) | RSI < 25 AND above SMA20 AND F&G < 40 | $200 |
| **EXTREME** (crash)   | RSI < 25 AND (below SMA20 OR F&G ≥ 40) | $100 — falling knife, halved |
| **STRONG**            | RSI < 32 AND above SMA20 AND F&G < 75 | $200 (or $100 if BTC RSI < 35) |
| **MODERATE**          | RSI < 40 AND above SMA20 AND vol surge AND momentum AND F&G < 60 AND BTC above SMA | $200 |

EXTREME always qualifies regardless of BTC context — deep oversold readings are entries even in fear. Position size is halved when the setup is a falling-knife pattern (below SMA or high F&G).

### Per-scan guards

Applied in this order after signal detection:

1. **Correlation cap** — if ≥ 3 candidates, keep only the lowest-RSI pair (BTC-correlated overexposure)
2. **Max positions** — skip all signals if 2 positions are already open
3. **SL cooldown** — skip a symbol for 4 hours after its stop-loss was hit
4. **Open position** — skip if an OCO order already exists for the symbol

> The correlation cap runs **before** the per-symbol guards so it filters on raw signal quality, not on whatever accidentally survives the guards.

### Indicators

All indicators are calculated on **closed candles only** (`klines[:-1]`). The currently-forming candle is always excluded.

#### RSI — Wilder's EMA (period 14)

Matches TradingView / Binance standard. Seeded with a simple average for the first 14 periods, then Wilder's smoothing:

```
avg_gain = (prev_avg_gain × 13 + current_gain) / 14
RSI = 100 − 100 / (1 + avg_gain / avg_loss)
```

Returns `50.0` if fewer than 14 closed candles are available.

#### SMA20 — Simple Moving Average (period 20)

Average of the last 20 closing prices. Returns `None` if insufficient data — callers treat `None` as "below SMA" (conservative).

#### Volume surge

`current_volume > avg_volume_of_previous_candles × 1.3`

The average excludes the current candle to avoid self-referential inflation.

#### Momentum

`close[-1] > close[-5]` — 5-candle lookback filters single-candle spikes.

#### ATR — Wilder's ATR (period 14)

```
true_range = max(high − low, |high − prev_close|, |low − prev_close|)
ATR        = Wilder's EMA of true_range over 14 periods
```

Used for dynamic SL/TP sizing in `place_buy_order()`.

---

## Configuration Reference

All settings live in `config.py` — edit only this file, never `scanner.py` directly:

| Constant | Default | Description |
|----------|---------|-------------|
| `PAIRS` | 6 pairs | Trading pairs to monitor |
| `CAPITAL` | `200.0` | USDC per trade (full-size) |
| `STOP_LOSS` | `0.03` | Fixed SL fallback when ATR disabled (3%) |
| `TAKE_PROFIT` | `0.075` | Fixed TP fallback when ATR disabled (7.5%) |
| `MAX_POSITIONS` | `2` | Maximum concurrent open positions |
| `SL_COOLDOWN_H` | `4` | Hours to pause signals after SL hit |
| `TRAILING_DELTA` | `150` | Trailing stop in basis points; `0` = disabled |
| `ATR_SL_MULT` | `1.5` | SL = ATR × multiplier; `0` = use fixed `STOP_LOSS` |
| `ATR_TP_MULT` | `3.5` | TP = ATR × multiplier → ~2.33:1 R/R |
| `ATR_SL_MIN` | `0.02` | ATR-based SL floor (2%) |
| `ATR_SL_MAX` | `0.06` | ATR-based SL ceiling (6%) |
| `INTERVAL` | `"1h"` | Candle interval |
| `KLINE_LIMIT` | `100` | Candles fetched per pair (must be ≥ 2 × RSI period to converge) |

**ATR floor note:** When ATR < `ATR_SL_MIN / ATR_SL_MULT`, SL is floored to `ATR_SL_MIN` but TP still scales from the floored SL. The apparent R/R improves beyond what the raw ATR justifies — a conservative bias in flat/low-volatility markets.

---

## Order Flow

```
signal confirmed
       │
       ▼
  place_buy_order(symbol, capital, price, closed_klines)
       │
       ├─ 1. Fetch LOT_SIZE + PRICE_FILTER from /exchangeInfo
       │
       ├─ 2. qty = capital / price, rounded DOWN to stepSize
       │      └─ Guard: raise ValueError if qty < min_qty (prevents desync)
       │
       ├─ 3. MARKET BUY
       │      └─ clientOrderId: agent-scanner-buy-{timestamp}
       │
       ├─ 4. Read actual fill price from order response
       │
       ├─ 5. Compute SL/TP %
       │      ├─ ATR enabled + klines provided:
       │      │   atr_pct = ATR / fill_price
       │      │   sl_pct  = clamp(atr_pct × 1.5,  2%, 6%)
       │      │   tp_pct  = sl_pct × (3.5 / 1.5)   →  ~2.33:1 R/R
       │      └─ Fallback (ATR disabled or klines missing):
       │          sl_pct = STOP_LOSS (3%), tp_pct = TAKE_PROFIT (7.5%)
       │
       └─ 6. OCO order
              ├─ TP leg: LIMIT_MAKER at fill × (1 + tp_pct)
              └─ SL leg:
                  ├─ Trailing (TRAILING_DELTA > 0):
                  │   STOP_LOSS with belowTrailingDelta = 150 bps
                  │   activates at fill × (1 − sl_pct)
                  └─ Fixed:
                      STOP_LOSS_LIMIT, limit = stop_price × 0.995
```

### SL outcome tracking

After each scan, `_check_sl_outcomes()` queries `allOrders` for every open trade's OCO ID:

- `LIMIT_MAKER` filled → `tp_hit` status, no cooldown
- `STOP_LOSS_LIMIT` / `STOP_LOSS` filled → `sl_hit` status + SL cooldown set
- Both filled (race condition) → TP takes precedence

---

## Backtest

```bash
python3 backtest.py
```

Fetches 1000 hourly candles (~41 days) per pair from Binance's public API and simulates the same RSI/SMA/Vol/Momentum logic over a rolling 100-candle window. No look-ahead bias — entry is at the close of the signal candle, and exit is scanned forward candle-by-candle.

Results are written to `backtest_results.json` and printed to stdout.

### Limitations

The backtest does **not** simulate:
- Fear & Greed index filter
- BTC context filter
- Correlation cap
- SL cooldowns
- Capital sizing tiers (all trades use $200)
- Slippage or trading fees

### Interpreting results

The most recent run (bear period): 47 trades, 23.4% WR, −4.8% net. Lower than live performance because F&G and BTC filters — which block the majority of falling-knife entries — are absent in the backtest. ADA was the most resilient pair (+6.2% net, 37.5% WR). Use the backtest to validate indicator logic and detect look-ahead bugs, not to project live P&L.

---

## Dashboard

### TUI (recommended)

```bash
python3 tui.py
```

Live terminal app — see [TUI App](#tui-app).

### HTML dashboard (auto-generated)

```bash
open ~/.agent/diagrams/trading-dashboard.html
```

Generated by `generate_dashboard()` at the end of each scan. Self-contained single-file HTML. Shows portfolio allocation, pair tiles, open positions, trade history, and performance stats. No web server required.

### Legacy browser dashboard

```bash
open /Users/jebog/Documents/Claude/Projects/Trading/dashboard.html
```

Reads `state.json` and auto-refreshes every 30 seconds.

---

## Telegram Integration

### Messages sent

| Trigger | Content |
|---------|---------|
| Each scan | All pairs RSI/signal + open positions P&L + win rate |
| Signal found | Pair, entry, ATR-estimated TP/SL, capital |
| Order placed | Fill price, actual TP/SL, OCO order ID |
| TP hit | Symbol confirmation |
| SL hit | Symbol + cooldown duration |
| F&G cache expired | Warning that sentiment filter is inactive |
| Order error | Sanitised exception message (Markdown-safe) |

### Setup

```bash
# 1 — Create a bot via @BotFather on Telegram, copy the token
# 2 — Get your numeric chat ID via @userinfobot or @RawDataBot
# 3 — Add to .env:
TELEGRAM_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_numeric_chat_id
```

In cron mode, reply `CONFIRM` or `SKIP` to the bot within 120 seconds after a signal alert.

---

## launchd Setup (macOS)

### Install

```bash
launchctl load ~/Library/LaunchAgents/com.trading.scanner.plist
```

### Verify

```bash
launchctl list com.trading.scanner
# "LastExitStatus" = 0 → last run succeeded
```

### Reload after changes

```bash
launchctl unload ~/Library/LaunchAgents/com.trading.scanner.plist
launchctl load   ~/Library/LaunchAgents/com.trading.scanner.plist
```

### Watch live

```bash
tail -f /Users/jebog/Documents/Claude/Projects/Trading/scanner.log
```

---

## Credentials

Credentials are loaded from the `.env` file in the project root (via `python-dotenv`):

```bash
cp .env.example .env
# Then edit .env:
BINANCE_API_KEY=your_api_key
BINANCE_SECRET_KEY=your_secret_key
TELEGRAM_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_numeric_chat_id
chmod 600 .env
```

Spot trading permission is required on the Binance API key to place orders. Market data (klines, ticker) works without authentication. `.env` is gitignored and will never be committed.

---

## Troubleshooting

### `No module named 'textual'` / `No module named 'dotenv'`
```bash
pip3 install -r requirements.txt
```

### TUI crashes with `TypeError: 'dict' object is not callable`

`_context` in `tui.py` is shadowing a Textual internal method. Check that line 36 of `scanner.py` reads:
```python
if __name__ == "__main__":
    sys.stdout = TeeLogger()
```
The guard must be present. If missing, importing `scanner` would redirect stdout before Textual initialises.

### No signals firing

RSI above thresholds is normal in trending or neutral markets. EXTREME requires RSI < 25 — this only occurs during significant dips. Check the dashboard or log for current values across all pairs.

### F&G fetch failing

The scanner falls back to a 25-hour cache in `state.json["fg_cache"]`, then to neutral 50 with a Telegram warning. The sentiment filter becomes inactive but signals can still fire (MODERATE will be less filtered). Check internet connectivity if this persists.

### Order rejected: `Filter failure: LOT_SIZE`

The computed quantity is below the exchange minimum. This happens when `CAPITAL / price` is too small. The scanner raises `ValueError` before sending to prevent position-tracking desync.

### SL cooldown stuck

Cooldowns expire automatically. To clear manually:
```bash
python3 -c "
import json
with open('state.json') as f: s = json.load(f)
s['cooldowns'] = {}
with open('state.json','w') as f: json.dump(s, f, indent=2)
print('Cooldowns cleared')
"
```

### Dashboard shows stale data

The HTML dashboard updates only when the scanner runs. Trigger a manual scan:
```bash
python3 scanner.py
```

### launchd not running

```bash
launchctl list com.trading.scanner
```
A non-zero `LastExitStatus` indicates a crash. Check the log:
```bash
tail -50 scanner.log
```
If exit code is 126 (permission denied), verify the Python binary path in the `.plist` matches your installed version:
```bash
ls -l /Users/jebog/.pyenv/versions/3.12.4/bin/python3
```

---

## Disclaimer

This tool is a personal project for educational and experimental purposes. It is not financial advice. Crypto trading involves substantial risk of loss. Past signal performance does not guarantee future results. Use entirely at your own risk.
