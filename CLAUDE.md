# Trading Scanner — Project Guide for Claude

## What this project does

Automated Binance spot trading scanner. Every 30 minutes it:
1. Fetches Fear & Greed index + BTC context (RSI, SMA)
2. Analyzes 6 USDC pairs with RSI/SMA/volume/momentum signals
3. Classifies signals as EXTREME / STRONG / MODERATE
4. In cron mode: sends a Telegram alert and waits for CONFIRM/SKIP reply
5. On CONFIRM: places a market buy + OCO (TP + trailing stop)
6. Generates an HTML dashboard and updates state.json

---

## File map

| File | Role |
|------|------|
| `config.py` | **Single source of truth for all settings** — edit this, nothing else |
| `scanner.py` | Core engine: signals, orders, state, Telegram, dashboard |
| `tui.py` | Textual TUI — live dashboard, runs scanner in background thread |
| `tui.tcss` | Catppuccin Mocha theme for the TUI |
| `backtest.py` | Walk-forward backtest (70% train / 30% test), writes `backtest_results.json` |
| `state.json` | Runtime state: results, trades, cooldowns, portfolio, sent_signals |
| `scanner.log` | Append-only log of every scan run |
| `dashboard.html` | Auto-generated HTML dashboard (written after each scan) |

---

## Configuration — `config.py`

All settings live here. Never hardcode values in scanner.py or backtest.py.

```python
# Credentials
BINANCE_API_KEY / BINANCE_SECRET_KEY   # Binance spot API
TELEGRAM_TOKEN / TELEGRAM_CHAT_ID      # Telegram bot

# Strategy
PAIRS          = [...]     # which symbols to scan (USDC quote)
CAPITAL        = 200.0     # USDC per trade
MAX_POSITIONS  = 2         # max concurrent open positions
SL_COOLDOWN_H  = 4         # hours to block a pair after SL hit

# SL/TP
TRAILING_DELTA = 150       # trailing stop in basis points; 0 = fixed stop
ATR_SL_MULT    = 1.5       # SL = ATR × 1.5
ATR_TP_MULT    = 3.5       # TP = ATR × 3.5
ATR_SL_MIN/MAX = 0.02/0.06 # SL clamped to [2%, 6%]
STOP_LOSS      = 0.03      # fallback if ATR unavailable
TAKE_PROFIT    = 0.075     # fallback
```

---

## How to run

```bash
# Interactive mode (manual CONFIRM prompt)
python3 scanner.py

# Cron mode (Telegram confirmation)
SCANNER_CRON=1 python3 scanner.py

# Live TUI dashboard
python3 tui.py

# Walk-forward backtest
python3 backtest.py
```

The launchd job (`~/Library/LaunchAgents/com.trading.scanner.plist`) runs cron mode every 30 minutes with `SCANNER_CRON=1`.

---

## Architecture

### Signal tiers

| Tier | 1h RSI | Extra conditions |
|------|--------|-----------------|
| EXTREME | < 25 | No daily filter — deep panic caught regardless |
| STRONG | < 32 | above 1h SMA20 + daily NOT bearish (daily RSI ≥ 30 or price above daily SMA) |
| MODERATE | < 40 | above 1h SMA20 + vol surge + momentum up + daily bullish (RSI > 45 AND above daily SMA) |

F&G < 20 (Extreme Fear) blocks MODERATE signals. BTC RSI < 30 + below SMA blocks STRONG signals.

### Multi-timeframe (daily trend filter)

Each `analyze()` call fetches 30 daily candles to compute `daily_rsi` and check `daily_bullish`:
- **daily_bullish**: daily RSI > 45 AND price above daily SMA20 → MODERATE allowed
- **daily_neutral**: daily RSI 30–45 → STRONG allowed, MODERATE blocked
- **daily_bearish**: daily RSI < 30 AND below daily SMA20 → STRONG blocked, EXTREME still fires

The daily RSI is displayed in TUI pair cards (`1d:XX`) and scan log lines. EXTREME signals bypass the daily filter to catch capitulation bottoms.

### Scan phases (scanner.py `scan()` and tui.py `action_trigger_scan`)

1. **Fetch context** — Fear & Greed, BTC RSI/SMA/price
2. **Fetch portfolio** — live Binance balances
3. **Check SL outcomes** — scan closed OCO orders, mark tp_hit/sl_hit, save cooldowns
4. **Analyze all pairs** — collect candidates (buy_signal == True)
5. **Correlation cap** — if ≥ 3 candidates, keep only lowest RSI (avoid concentrated BTC exposure)
6. **Per-symbol guards** — skip if: open position exists, SL cooldown active, MAX_POSITIONS reached
7. **Place orders** — market buy → OCO (LIMIT_MAKER TP + STOP_LOSS trailing)

### State machine per trade

```
open → tp_hit   (LIMIT_MAKER filled)
     → sl_hit   (STOP_LOSS trailing filled) → SL cooldown SL_COOLDOWN_H hours
     → no_oco   (market fill succeeded but OCO API call failed → 🚨 Telegram alert)
```

### Two-timer TUI architecture

- **5s timer** → `_read_state_file()` — disk only, no API — updates cooldowns, portfolio cache, log tail
- **30s timer** → `action_trigger_scan()` — full API round-trip in `@work(thread=True, exclusive=True)`
- State flows via `ScanComplete` and `StateUpdated` messages from worker → main thread

### Alert deduplication

`sent_signals` dict in `state.json` keyed by `symbol:tier`. Same symbol+tier suppressed for 2 hours (covers 4 scan cycles). Persisted across process restarts.

---

## Key design decisions

**TeeLogger guard** — `scanner.py` line 36: `if __name__ == "__main__": sys.stdout = TeeLogger()`. Without this, importing scanner from tui.py would hijack Textual's stdout.

**`_scan_ctx` not `_context`** — Textual has an internal `App._context()` method. A class attribute named `_context` shadows it and crashes `app.run()`. Always use `_scan_ctx` in `ScannerApp`.

**`$color` in RichLog** — Textual CSS variables (`$green`, `$red`, etc.) work in `Label`/`Static` widgets but NOT in `RichLog`. All `tlog()` calls must use Rich-standard color names (`green`, `red`, `dark_orange`, `yellow`, `cyan`) or hex `#rrggbb`.

**`markup_escape()` for exceptions** — Exception messages from Binance API responses can contain `[`, `]`, `{`, `}` that break Rich's markup parser. Always wrap `str(e)` in `markup_escape()` before passing to `tlog()`.

**OCO failure guard** — If the market buy fills but the OCO POST fails, `place_buy_order` returns `(order, None, trade_partial)` with `status="no_oco"` and immediately fires a Telegram alert. The position appears in state.json for manual intervention.

**Binance API weight** — Each scan costs ~30 weight. At 30s interval from TUI = ~60/min, well under the 1200/min limit. The 5s state watcher reads only disk — zero API calls.

---

## Common tasks

### Add a new trading pair
Edit `config.py`: add the symbol to `PAIRS`. The TUI grid resizes automatically (`on_mount` computes `cols = ceil(n/2)`).

### Change risk parameters
Edit `config.py`: `ATR_SL_MULT`, `ATR_TP_MULT`, `TRAILING_DELTA`. Changes take effect on next scan.

### Disable trailing stop
Set `TRAILING_DELTA = 0` in `config.py`. OCO will use `STOP_LOSS_LIMIT` instead of `STOP_LOSS`.

### Rotate API keys
Update `BINANCE_API_KEY` and `BINANCE_SECRET_KEY` in `config.py`.

### Force a scan immediately
In TUI: press `S`. In terminal: `python3 scanner.py`.

### Check launchd cron status
```bash
launchctl list com.trading.scanner
# LastExitStatus = 0 → last run OK
# StartInterval = 1800 → runs every 30 minutes
```

### Clear a stuck SL cooldown
```python
import json
with open("state.json") as f: s = json.load(f)
del s["cooldowns"]["ETHUSDC"]  # remove specific symbol
with open("state.json", "w") as f: json.dump(s, f, indent=2)
```

### Run backtest after changing strategy params
```bash
python3 backtest.py
# Results written to backtest_results.json
# TUI left panel "BACKTEST" section reads this file automatically
```

---

## What NOT to do

- **Never import scanner.py with TeeLogger active** — guard must stay as `if __name__ == "__main__"`
- **Never use `$color` names in `tlog()` calls** — use Rich color names only
- **Never call `get_open_positions()` more than once per scan** — it makes 2+ signed API calls; store the result and reuse it
- **Never remove the OCO failure guard** — a filled buy with no OCO is an unprotected position
- **Never hardcode credentials or strategy params outside `config.py`**
- **Never rename `_scan_ctx` back to `_context`** — it shadows a Textual internal method
