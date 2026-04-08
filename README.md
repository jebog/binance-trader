# Binance Trading Scanner

Personal macOS algorithmic scanner that monitors 6 spot pairs every 30 minutes, detects multi-tier RSI + SMA + volume + sentiment buy signals, filters with RSI divergence and BTC dominance, places confirmed market orders with ATR-based OCO exit brackets, partial take-profit at 1×ATR, and split entries for EXTREME quality signals. Sends a daily 8am Telegram digest. Includes a max-drawdown circuit breaker, break-even stop, trade timeout, progressive trailing stop, volatility-adjusted position sizing, 15m entry refinement, and dynamic pair scoring. Persists all state to SQLite (WAL mode).

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
12. [Boot Reconciliation](#boot-reconciliation)
13. [Credentials](#credentials)
14. [Troubleshooting](#troubleshooting)

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
├── scanner.py              Thin facade — re-exports from trading/, owns scan() + TeeLogger
├── trading/                Package — all logic split into focused modules:
│   ├── db.py               SQLite schema, CRUD, save_state, get_state_dict
│   ├── http_client.py      Binance REST API helpers (1-retry on transient errors)
│   ├── indicators.py       calc_rsi, calc_sma, calc_atr, divergence detection
│   ├── market_data.py      F&G, BTC context, dominance, portfolio
│   ├── signals.py          analyze(), 15m RSI gate
│   ├── orders.py           place_buy_order, split-entry, cooldowns
│   ├── positions.py        break-even, trailing, timeout, SL outcomes
│   ├── analytics.py        perf stats, pair score, digest, capital sizing
│   ├── dashboard.py        HTML dashboard generation
│   ├── scan_helpers.py     Shared helpers (context, correlation cap, position mgmt)
│   ├── reconcile.py        Boot-time Binance↔DB drift check (fail-loud, blocks startup on divergence)
│   ├── notify.py           Telegram, webhook, macOS notifications
│   └── logger.py           Structured logging setup, path constants
├── tui.py                  Real-time TUI dashboard (Textual)
├── tui.tcss                Catppuccin Mocha theme for the TUI
├── backtest.py             Walk-forward backtester + Monte Carlo + Sharpe/drawdown
├── tests/                  201 tests (indicators, DB, backtest, integration)
├── run_scanner.sh          Shell wrapper for launchd (loads .env, runs cron mode)
├── requirements.txt        Python dependencies
├── .env.example            Credential template — copy to .env and fill in values
├── LICENSE                 MIT
├── state.db                SQLite runtime state — WAL mode, canonical store (gitignored)
├── scanner.log             Append-only structured log (gitignored)
└── backtest_results.json   Output of last backtest run (gitignored)

~/.agent/diagrams/
└── trading-dashboard.html  Auto-generated HTML dashboard (updated each scan)

~/Library/LaunchAgents/
└── com.trading.scanner.plist   launchd job — runs every 30 minutes
```

> **Never edit `state.db` with a full-replacement write** — use targeted SQL (`UPDATE`, `DELETE`, `INSERT OR REPLACE`) to avoid corrupting concurrent reads.

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

### Cron / Telegram mode (optional)

```bash
SCANNER_CRON=1 python3 scanner.py
```

No stdin prompt. If a signal fires, the scanner sends a Telegram alert and waits up to 120 seconds for a `CONFIRM` or `SKIP` reply. This is what `run_scanner.sh` uses with launchd.

**Cron is optional.** The TUI has full feature parity (signal dedup, F&G regime alerts, daily digest, Telegram summaries, health sentinel). To enable cron, set `CRON_ENABLED=true` in `.env`. To disable, set it to `false` — the launchd job exits immediately.

---

## TUI App

```bash
pip3 install -r requirements.txt   # one-time
python3 tui.py
```

### Layout

```
┌─ Header ─────────────────────────────────────────────────────────────────────┐
│ ◉ TRADING SCANNER  F&G:45 Fear │ BTC $66,395 RSI:52 ↑ │ BTC.D:56%↑ │ P&L:+$4│
├─ Left panel (30) ────┬─ Market ─ Positions ─ History ─ Backtest ──────────── │
│ PORTFOLIO  $2,706    │  ┌─ ETHUSDC ──────┐  ┌─ ADAUSDC ──────┐              │
│ Open P&L: +$4.20     │  │ $1,998   RSI 44│  │ $0.248   RSI 38│              │
│ ETH  51%  ████████   │  │ 1d:52  NONE    │  │ 1d:41  NONE    │              │
│ USDC 45%  ███████    │  │ ATR:2.1% Div:✓ │  │ ATR:1.8% Div:✓ │              │
│ ADA   4%  ▌          │  │ ▁▂▃▄▅▆▇█▇▆▅   │  │ ▃▃▄▅▆▇▇▆▇█▇   │              │
│                      │  └────────────────┘  └────────────────┘              │
│ COOLDOWNS            │                                                       │
│ None active          │  Positions tab: Symbol 🛡S2 │ Entry │ P&L +2.1% (grn)│
│                      │  History tab:   Exit price + P&L% + 🛡 BE saves      │
│ PERFORMANCE          │                                                       │
│ 5W / 2L  71% WR     │                                                       │
│ PF:2.3  Sharpe:0.85 │                                                       │
│ Streak:2  BE saves:1│                                                       │
├─ Log strip ──────────┴───────────────────────────────────────────────────── │
│  10:01:30  ETH 1h:44.5 1d:52 +0.3% NONE  │  ADA 1h:38.2 1d:41 -1.2% NONE  │
├─ Status bar ─────────────────────────────────────────────────────────────── │
│  [S]can [E]quity [L]og [⚙]Settings     ● Next: 25s │ Last: 10:01:30       │
└──────────────────────────────────────────────────────────────────────────── ┘
```

**Dashboard features:**
- **Header**: F&G index, BTC price/RSI/trend, BTC dominance with rising indicator, colored open P&L
- **Pair cards**: Price sparkline, 1h + daily RSI, ATR% volatility, divergence status, split-entry badge for EXTREME
- **Positions tab**: Colored P&L, 🛡 break-even icon, S1/S2/S3 trailing stage on symbol
- **History tab**: 20 rows with exit price, colored P&L%, 🛡 breakeven save marker
- **Performance**: Win rate, profit factor, Sharpe ratio, max loss streak, breakeven saves
- **Status bar**: Health dot (● green/red), countdown to next scan, last scan timestamp
- **Portfolio panel**: Drawdown warning `⚠ Drawdown: X.X%` (orange ≥10%) or `🛑 HALTED X.X%` (red ≥15%)

### Key bindings

| Key | Action |
|-----|--------|
| `S` | Run a full scan now |
| `R` | Re-read `state.db` from disk |
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
| State watcher | 5 s | Reads `state.db` via SQLite WAL (disk only — no API calls) |
| Auto-scan | 30 s | Full Binance API scan in background thread |

The TUI and the launchd cron job are independent. When cron writes `state.db`, the TUI detects the update within 5 seconds via the state watcher. WAL mode allows the reader and writer to run concurrently without locking.

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
| **Bearish** | < 30, or 30–45 below SMA | — | STRONG blocked, EXTREME still fires |

EXTREME signals bypass the daily filter — deep oversold readings are entries regardless of trend.

### Market filters (fetched once per scan)

| Filter | Source | Effect |
|--------|--------|--------|
| **Fear & Greed Index** | alternative.me | Blocks MODERATE when F&G ≥ 60; blocks STRONG when F&G ≥ 75 |
| **BTC SMA20** | Binance 1h klines | Blocks MODERATE when BTC is below its 1h SMA20 |
| **BTC RSI** | Binance 1h klines | Halves STRONG position size to $100 when BTC RSI < 35 |
| **BTC dominance** (T2-3) | CoinGecko /global | Blocks MODERATE when BTC.D rises >0.5% scan-over-scan (fail-open on API error) |

F&G is cached 25h. BTC.D is cached 1h. Fail-open on any API failure — signals are never blocked by unavailable external data.

### Signal tiers

| Tier | Condition | Capital |
|------|-----------|---------|
| **EXTREME** (quality) | RSI < 25 AND above SMA20 AND F&G < 40 | $100 — first split leg; second fires at 1×ATR below entry |
| **EXTREME** (crash)   | RSI < 25 AND (below SMA20 OR F&G ≥ 40) | $100 — falling knife, no split entry |
| **STRONG**            | RSI < 32 AND above SMA20 AND F&G < 75 AND divergence ok | $200 (or $100 if BTC RSI < 35) |
| **MODERATE**          | RSI < 40 AND above SMA20 AND vol surge AND momentum AND F&G < 60 AND BTC above SMA AND divergence ok AND BTC.D not rising | $200 |

EXTREME always bypasses the daily trend filter, divergence filter, and BTC dominance filter — deep panic is always worth entering.

**Additional signal-quality filters (T2-2 / T2-3):**
- **RSI Divergence** — if the last two price swing lows form a lower low but RSI also forms a lower low, STRONG and MODERATE are blocked. If divergence is detected (RSI makes a higher low while price makes a lower low) or data is ambiguous, signals proceed.
- **BTC Dominance surge** — if BTC.D rose >0.5% since the previous scan, MODERATE is blocked (altcoins tend to bleed when dominance surges).

### Per-scan guards

Applied in this order after signal detection:

1. **Dynamic pair scoring (T4-3)** — if ≥ 3 candidates, keep the highest-scoring pair (win_rate × profit_factor from last 20 trades); falls back to lowest RSI if fewer than 3 trades per symbol
2. **Circuit breaker** — if portfolio has dropped ≥ `MAX_DRAWDOWN_PCT` (15%) from its peak, all candidates are cleared and a Telegram alert is sent (at most once per 4 hours)
3. **Max positions** — skip a signal if `MAX_POSITIONS` are already open
4. **SL cooldown** — skip a symbol for 4 hours after its stop-loss was hit
5. **Open position** — skip if an OCO order already exists for the symbol
6. **15m entry refinement (T4-2)** — skip if the 15m RSI > 45 (momentum peaked on the shorter timeframe)

> The correlation cap and circuit breaker both run **before** the per-symbol guards so they filter on raw signal quality, not on whatever accidentally survives the guards.

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
| `MAX_DRAWDOWN_PCT` | `0.15` | Halt new orders if portfolio drops >15% from its high-water mark |
| `DIGEST_HOUR` | `8` | Local hour (0–23) at which the morning Telegram digest is sent |
| `TRAILING_DELTA` | `150` | Trailing stop in basis points; `0` = disabled |
| `ATR_SL_MULT` | `1.5` | SL = ATR × multiplier; `0` = use fixed `STOP_LOSS` |
| `ATR_TP_MULT` | `3.5` | TP = ATR × multiplier → ~2.33:1 R/R |
| `ATR_SL_MIN` | `0.02` | ATR-based SL floor (2%) |
| `ATR_SL_MAX` | `0.06` | ATR-based SL ceiling (6%) |
| `INTERVAL` | `"1h"` | Candle interval |
| `KLINE_LIMIT` | `100` | Candles fetched per pair (must be ≥ 2 × RSI period to converge) |
| `DIVERGENCE_ENABLED` | `True` | Enable RSI divergence filter (T2-2) |
| `DIVERGENCE_LOOKBACK` | `20` | Candles to scan for swing lows |
| `DIVERGENCE_SWING_DEPTH` | `0.005` | Minimum swing depth (0.5%) for a local low to qualify |
| `BTC_DOM_ENABLED` | `True` | Enable BTC dominance filter (T2-3) |
| `BTC_DOM_CACHE_H` | `1` | CoinGecko cache lifetime in hours |
| `BTC_DOM_RISE_THRESHOLD` | `0.005` | Dominance rise % to trigger MODERATE block |
| `PARTIAL_TP_ENABLED` | `True` | Enable partial TP1 at 1×ATR (T2-4) |
| `PARTIAL_TP1_ATR_MULT` | `1.0` | TP1 distance = ATR × this multiplier |
| `PARTIAL_TP1_QTY_PCT` | `0.50` | Fraction of position closed at TP1 |
| `SPLIT_ENTRY_ENABLED` | `True` | Enable split entry for EXTREME quality signals (T2-1) |
| `SPLIT_ENTRY_ATR_MULT` | `1.0` | Second entry trigger = first_fill × (1 − ATR × this) |
| `SPLIT_ENTRY_TTL_H` | `48` | Expire pending second entry after this many hours |
| `TRADE_TIMEOUT_ENABLED` | `True` | Force-exit positions open longer than `TRADE_TIMEOUT_H` (T3-2) |
| `TRADE_TIMEOUT_H` | `72` | Hours before a position is force-exited |
| `BREAKEVEN_ENABLED` | `True` | Move SL to entry once price reaches 1×ATR gain (T3-1) |
| `BREAKEVEN_ATR_MULT` | `1.0` | Break-even trigger = entry × (1 + this × ATR%) |
| `VOL_SIZING_ENABLED` | `True` | Scale position size by volatility (T3-4) |
| `TARGET_RISK_PCT` | `0.015` | Target 1.5% portfolio risk per trade |
| `VOL_SIZING_MIN` | `0.25` | Floor: never below 25% of CAPITAL |
| `VOL_SIZING_MAX` | `1.00` | Ceiling: never above 100% of CAPITAL |
| `ENTRY_REFINE_ENABLED` | `True` | Skip order if 15m RSI > threshold (T4-2) |
| `ENTRY_REFINE_15M_RSI_MAX` | `45` | 15m RSI threshold; higher → momentum peaked |
| `PAIR_SCORE_ENABLED` | `True` | Sort correlation-cap candidates by win_rate × profit_factor (T4-3) |
| `PAIR_SCORE_MIN_TRADES` | `3` | Minimum closed trades to compute score |
| `PAIR_SCORE_LOOKBACK` | `20` | Last N closed trades per symbol |
| `PROGRESSIVE_TRAILING_ENABLED` | `True` | Tighten trailing delta at ATR milestones (T4-4) |
| `PROGRESSIVE_TRAILING_STAGES` | `[(1.5,100),(2.0,75),(2.5,50)]` | (ATR multiplier trigger, new bps); do not reorder while trades are open |
| `CRON_ENABLED` | `false` | Set `true` in `.env` to enable launchd cron job; TUI has full parity |
| `RECONCILE_ENABLED` | `True` | Boot-time Binance↔DB drift check (fail-loud); set `false` in `.env` to bypass |
| `RECONCILE_IGNORE_ASSETS` | `["BNB","XRP","ETH","BETH"]` | Base assets held outside scanner control (manual buys, DCA, staking wrappers) |

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
       ├─ 6. OCO order
       │      ├─ TP leg: LIMIT_MAKER at fill × (1 + tp_pct)
       │      └─ SL leg:
       │          ├─ Trailing (TRAILING_DELTA > 0):
       │          │   STOP_LOSS with belowTrailingDelta = 150 bps
       │          │   activates at fill × (1 − sl_pct)
       │          └─ Fixed:
       │              STOP_LOSS_LIMIT, limit = stop_price × 0.995
       │
       ├─ 7. Partial TP1 LIMIT_MAKER (if PARTIAL_TP_ENABLED)
       │      Standalone SELL LIMIT_MAKER for qty × 50% at fill × (1 + 1×ATR%)
       │      Failure is non-fatal — full position still protected by OCO
       │
       └─ 8. Arm split entry (if SPLIT_ENTRY_ENABLED + EXTREME quality)
              Stores {first_fill, first_qty, first_oco_id, atr_pct, …} in
              state.db pending_second_entries table
              Trigger: current_price ≤ first_fill × (1 − 1×ATR%)
              TTL: 48 hours
```

### Partial TP flow (T2-4)

When the TP1 LIMIT_MAKER fills, `_handle_partial_tp1()` is called:
1. Record TP1 exit: `exit_price`, `pnl_pct`, stored in `trade["partial_tp1"]`
2. Cancel the original full-position OCO via DELETE `/api/v3/orderList`
3. Place a new OCO for the remaining 50% at the original TP2 and SL prices
4. Trade status → `partial_tp` (still counted as open)

When the final OCO fills, P&L = `TP1_pnl × 0.5 + final_pnl × 0.5` (weighted average).

### Position management

After an order is placed, each scan runs the following checks in order:

| Phase | Condition | Action |
|-------|-----------|--------|
| **Break-even stop (T3-1)** | price ≥ entry × (1 + 1×ATR%) | Cancel OCO, re-place with SL at entry; fires once (`breakeven_moved` guard) |
| **Progressive trailing (T4-4)** | price ≥ entry × (1 + 1.5/2/2.5×ATR%) | Tighten trailing delta to 100/75/50 bps at each milestone; tracked by `trailing_stage` index |
| **Trade timeout (T3-2)** | trade age > `TRADE_TIMEOUT_H` (72h) | Cancel OCO, market-sell remaining qty; status → `timeout` |

Volatility-adjusted sizing (T3-4): capital per trade = `TARGET_RISK_PCT × portfolio / atr_pct`, clamped to `[25%, 100%] × CAPITAL`. Falls back to `CAPITAL` when ATR is unavailable.

### SL outcome tracking

After each scan, `_check_sl_outcomes()` queries `allOrders` for every open/partial_tp trade's OCO ID:

- `LIMIT_MAKER` filled → `tp_hit` status, no cooldown
- `STOP_LOSS_LIMIT` / `STOP_LOSS` filled → `sl_hit` status + SL cooldown set
- TP1 `LIMIT_MAKER` (standalone) filled → `partial_tp` transition, new OCO placed
- Both OCO legs filled (race condition) → TP takes precedence

On terminal outcome, three fields are written to the trade row in `state.db` via `update_trade_fields()`:
- `exit_price` — actual avg fill price from `cummulativeQuoteQty / executedQty`
- `pnl_pct` — `(exit_price − entry) / entry × 100` (weighted avg for partial_tp trades)
- `exit_time` — ISO timestamp of the outcome detection

---

## Backtest

```bash
python3 backtest.py
```

Fetches 1000 hourly candles (~41 days) per pair from Binance's public API and simulates the same RSI/SMA/Vol/Momentum logic over a rolling 100-candle window. No look-ahead bias — entry is at the close of the signal candle, and exit is scanned forward candle-by-candle.

Results are written to `backtest_results.json` and printed to stdout.

### Simulated features

| Feature | Status |
|---------|--------|
| RSI divergence filter (T2-2) | ✅ |
| Split entry second leg (T2-1) | ✅ EXTREME signals fire second leg at trigger |
| Partial TP1 (T2-4) | ✅ Weighted P&L when TP1 hits on a prior candle |
| Break-even stop (T3-1) | ✅ SL moves to entry when price reaches trigger |
| Progressive trailing (T4-4) | ✅ SL tightens at ATR milestones |
| Volatility-adjusted sizing (T3-4) | ✅ Kelly-style formula from config |
| Trade timeout (72h) | ✅ Force-close at market |

### Risk metrics

| Metric | Description |
|--------|-------------|
| Sharpe ratio | Per-trade mean / std (not annualized — variable hold times) |
| Max drawdown | Largest peak-to-trough in cumulative P&L curve |
| Max consecutive losses | Longest SL streak |
| Monte Carlo P5/P50/P95 | Bootstrap 1000 sims for net P&L and max DD confidence intervals |

### Limitations

The backtest does **not** simulate:
- Fear & Greed index filter (not available historically)
- BTC context filter
- Correlation cap
- SL cooldowns
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

Generated by `generate_dashboard()` at the end of each scan. Self-contained single-file HTML — no web server required. Shows portfolio allocation, pair tiles, open positions, trade history, and performance stats.

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
| F&G regime change | Threshold crossed (20 / 30 / 50) with regime description; once per crossing, deduped via `fg_regime` key in `kv` table |
| Circuit breaker | Drawdown %, peak vs current portfolio; at most once per 4 hours |
| Daily digest (8am) | 7-day closed trade summary (wins/losses/net P&L), portfolio total, F&G, open positions with time-held |
| F&G cache expired | Warning that sentiment filter is inactive |
| Partial TP1 hit (T2-4) | Symbol, TP1 fill price, pnl%, TP2 target |
| Partial TP1 re-OCO failed (T2-4) | 🚨 CRITICAL — remaining qty unprotected |
| Split entry armed (T2-1) | Symbol, trigger price, TTL |
| Split entry complete (T2-1) | Avg entry, TP, SL, combined OCO ID |
| Split entry expired (T2-1) | Symbol, TTL hit |
| Split second buy failed (T2-1) | 🚨 CRITICAL — first half unprotected |
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
tail -f scanner.log
```

---

## Boot Reconciliation

At every startup of `scanner.py` or `tui.py`, a reconciliation pass compares the open trades stored in `state.db` against the live state on Binance. If anything has drifted while the scanner was offline, **startup is aborted** with a Telegram alert — the scanner refuses to trade against an inconsistent picture of the world.

### What it detects

| Type | What it means | Why it matters |
|---|---|---|
| **`missing_position`** | DB trade is `open`/`partial_tp`/`no_oco`, but the live Binance balance for the base asset is below 50% of the recorded qty | You sold manually via the app, ran a dust conversion, or something fed the position out of the scanner's view. The scanner would otherwise treat a phantom long as real and skip new entries on that pair. |
| **`missing_oco`** | DB trade has an `oco_id`, but no matching OCO exists in `/api/v3/openOrderList` | The position is **unprotected** — no SL, no TP. You cancelled the OCO from the Binance app, or it expired. Highest priority divergence. |

The 50% threshold on `missing_position` is intentionally permissive: BNB-fee skim, partial TP1 fills, and split entries all leave less than `qty` in the wallet legitimately. Only a *meaningful* drop counts as a divergence.

`partial_tp_no_oco` trades are **exempt** from the `missing_oco` check — that status is the *known* unprotected state and re-flagging it on every boot would just be noise.

### Behavior on detection

The reconciliation is **fail-loud, no auto-heal** by design:

1. Both startup paths (`scanner.py` `__main__` and `tui.py` `__main__`) call `enforce_boot_gate(conn)` before any trading logic
2. If the report is OK → log line, no Telegram, scanner proceeds normally
3. If divergences are found → **Telegram alert with full details** + `ReconcileError` raised + process exits with code `1`
4. **You must inspect, fix manually, and restart** — the scanner does not silently re-place orders or mark trades closed on its own

The rationale: any divergence reflects a state we don't fully understand. Silent correction can compound the damage if the underlying logic has a bug. Better to bother the human once than to autonomously turn a small inconsistency into a large one.

### Whitelisting manually-held assets

The reconciliation reads **base-asset balances**, not symbol-specific positions (Binance spot has no per-pair "position" concept). If you also hold an asset outside scanner control — manual buys, DCA accumulation, BETH staking wrappers — it must be excluded, otherwise a manual sell on that asset would false-trigger `missing_position` on a still-valid scanner trade.

```python
# config.py
RECONCILE_IGNORE_ASSETS = ["BNB", "XRP", "ETH", "BETH"]
```

Any DB trade whose symbol's base asset is in this list is skipped entirely during reconciliation.

### Resolving a divergence

**`missing_oco` — position unprotected, two paths:**

```bash
# Option 1: re-place the OCO via the Binance app, then update the DB
sqlite3 state.db "UPDATE trades SET oco_id='<new_list_id>' WHERE id=<trade_id>;"

# Option 2: accept it as a known-unprotected state (scanner skips OCO checks
# but still ages out the position via TRADE_TIMEOUT_H)
sqlite3 state.db "UPDATE trades SET status='no_oco', oco_id=NULL WHERE id=<trade_id>;"
```

**`missing_position` — DB thinks you're long but the position is gone:**

```python
import sqlite3
from datetime import datetime
conn = sqlite3.connect("state.db")
conn.execute("""
    UPDATE trades
    SET status='sl_hit', exit_price=?, exit_time=?, pnl_pct=?
    WHERE id=?
""", (1.234, datetime.now().isoformat(), -1.5, 42))  # adjust to actual exit
conn.commit(); conn.close()
```

Use `sl_hit` for a loss (also triggers `SL_COOLDOWN_H` on the symbol), `tp_hit` for a profit, or `timeout` to avoid both side effects.

### Manual dry-run (check without starting the scanner)

```python
from trading.db import db_connect, db_init
from trading.reconcile import reconcile_at_boot, format_report_telegram

conn = db_connect(); db_init(conn)
report = reconcile_at_boot(conn)
print(format_report_telegram(report))
print(f"OK: {report.ok} | Checked: {report.checked_trades} | Skipped: {report.skipped_trades}")
for d in report.divergences:
    print(f"  {d.kind} {d.symbol} trade={d.trade_id}: {d.detail}")
conn.close()
```

### Disabling temporarily

Set `RECONCILE_ENABLED=false` in `.env`. **Only use this when you're about to fix a divergence manually and need the scanner running first** — running with the gate off in production defeats its purpose.

### What's deliberately out of scope (v1)

Two divergence types are **not** detected in v1 and may be added later:

- **Type B** — A live Binance position with no matching DB row (would catch crashes between market buy and `insert_trade`). Currently impossible to distinguish from a manual buy, so it would generate constant noise.
- **Type D** — Quantity mismatch within 50–100% of the recorded qty (partial sells via the app). The 50% threshold catches the obvious cases; finer-grained drift detection requires an audit-log table to track expected vs actual deltas over time.

Auto-heal (re-placing missing OCOs, marking phantom trades closed) is also intentionally out of scope. If you want it, build it as a *separate* module with its own opt-in flag rather than coupling detection and correction in the same code path.

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

`tui.py` has a class attribute named `_scan_ctx` specifically to avoid shadowing Textual's internal `_context()` method. If you see this error after modifying `tui.py`, check that no class attribute or variable is named `_context` in `ScannerApp`.

### TUI output corrupted or scanner log missing on startup

The `TeeLogger` in `scanner.py` must be guarded:
```python
if __name__ == "__main__":
    sys.stdout = TeeLogger()
```
Without this guard, importing `scanner` from `tui.py` would redirect stdout before Textual initialises, corrupting terminal output.

### No signals firing

RSI above thresholds is normal in trending or neutral markets. EXTREME requires RSI < 25 — this only occurs during significant dips. Check the dashboard or log for current values across all pairs.

### F&G fetch failing

The scanner falls back to a 25-hour cache in the `fg_cache` SQLite table, then to neutral 50 with a Telegram warning. The sentiment filter becomes inactive but signals can still fire (MODERATE will be less filtered). Check internet connectivity if this persists.

### Order rejected: `Filter failure: LOT_SIZE`

The computed quantity is below the exchange minimum. This happens when `CAPITAL / price` is too small. The scanner raises `ValueError` before sending to prevent position-tracking desync.

### SL cooldown stuck

Cooldowns expire automatically. To clear manually:
```bash
python3 -c "
import sqlite3
conn = sqlite3.connect('state.db')
conn.execute('DELETE FROM cooldowns')
conn.commit(); conn.close()
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
