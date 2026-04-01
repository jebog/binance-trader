# Trading Scanner — Project Guide for Claude

## What this project does

Automated Binance spot trading scanner. Every 30 minutes it:
1. Fetches Fear & Greed index + BTC context (RSI, SMA, dominance)
2. Analyzes 6 USDC pairs with RSI/SMA/volume/momentum/divergence signals
3. Classifies signals as EXTREME / STRONG / MODERATE
4. In cron mode: sends a Telegram alert and waits for CONFIRM/SKIP reply
5. On CONFIRM: places a market buy + OCO (TP + trailing stop)
   - EXTREME quality: buys 50% now, arms a split-entry second leg at 1×ATR below fill
   - PARTIAL_TP_ENABLED: places a standalone TP1 LIMIT_MAKER at 1×ATR for half the qty
6. Generates an HTML dashboard and writes all state to state.db (SQLite, WAL mode)
7. At 8am: sends a daily Telegram digest (7-day P&L summary, open positions, F&G)

---

## File map

| File | Role |
|------|------|
| `config.py` | **Single source of truth for all settings** — edit this, nothing else |
| `scanner.py` | Core engine: signals, orders, state, Telegram, dashboard, digest |
| `tui.py` | Textual TUI — live dashboard, runs scanner in background thread |
| `tui.tcss` | Catppuccin Mocha theme for the TUI |
| `backtest.py` | Walk-forward backtest (70% train / 30% test), writes `backtest_results.json` |
| `state.db` | **Canonical runtime state** (SQLite, WAL mode): trades, cooldowns, caches, kv sentinels. `state.json` no longer written; `state.json.bak` left behind if migration ran. |
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
PAIRS            = [...]     # which symbols to scan (USDC quote)
CAPITAL          = 200.0     # USDC per trade
MAX_POSITIONS    = 2         # max concurrent open positions
SL_COOLDOWN_H    = 4         # hours to block a pair after SL hit
MAX_DRAWDOWN_PCT = 0.15      # halt new orders if portfolio drops >15% from peak
DIGEST_HOUR      = 8         # local hour (0–23) to send morning digest

# SL/TP
TRAILING_DELTA = 150       # trailing stop in basis points; 0 = fixed stop
ATR_SL_MULT    = 1.5       # SL = ATR × 1.5
ATR_TP_MULT    = 3.5       # TP = ATR × 3.5
ATR_SL_MIN/MAX = 0.02/0.06 # SL clamped to [2%, 6%]
STOP_LOSS      = 0.03      # fallback if ATR unavailable
TAKE_PROFIT    = 0.075     # fallback

# T2-2: RSI divergence filter
DIVERGENCE_ENABLED     = True
DIVERGENCE_LOOKBACK    = 20     # candles to scan for swing lows
DIVERGENCE_SWING_DEPTH = 0.005  # swing low must be ≥ 0.5% below neighbors

# T2-3: BTC dominance filter
BTC_DOM_ENABLED        = True
BTC_DOM_CACHE_H        = 1      # CoinGecko cache lifetime (hours)
BTC_DOM_RISE_THRESHOLD = 0.005  # 0.5% scan-over-scan rise = "rising"

# T2-4: Partial TP
PARTIAL_TP_ENABLED   = True
PARTIAL_TP1_ATR_MULT = 1.0    # TP1 at entry × (1 + 1×ATR%)
PARTIAL_TP1_QTY_PCT  = 0.50   # fraction closed at TP1

# T2-1: Split entry (EXTREME quality only)
SPLIT_ENTRY_ENABLED  = True
SPLIT_ENTRY_ATR_MULT = 1.0    # second entry triggers at first_fill × (1 - 1×ATR%)
SPLIT_ENTRY_TTL_H    = 48     # expire pending entry after 48h

# T3-2: Trade timeout
TRADE_TIMEOUT_ENABLED = True
TRADE_TIMEOUT_H       = 72    # force-exit any position open longer than 72h

# T3-1: Break-even stop
BREAKEVEN_ENABLED  = True
BREAKEVEN_ATR_MULT = 1.0    # trigger: price ≥ entry × (1 + 1×ATR%)

# T3-4: Volatility-adjusted capital sizing
VOL_SIZING_ENABLED = True
TARGET_RISK_PCT    = 0.015  # target 1.5% portfolio risk per trade
VOL_SIZING_MIN     = 0.25   # floor: never below 25% of CAPITAL
VOL_SIZING_MAX     = 1.00   # ceiling: never above 100% of CAPITAL

# T4-2: 15m entry refinement
ENTRY_REFINE_ENABLED     = True
ENTRY_REFINE_15M_RSI_MAX = 45    # skip if 15m RSI > this (momentum peaked on shorter TF)
ENTRY_REFINE_15M_LIMIT   = 50    # candles to fetch (35 Wilder steps for convergence)

# T4-3: Dynamic pair scoring
PAIR_SCORE_ENABLED    = True
PAIR_SCORE_MIN_TRADES = 3     # min closed trades to compute score (else neutral 0.5)
PAIR_SCORE_LOOKBACK   = 20    # last N closed trades per symbol

# T4-4: Progressive trailing stop
PROGRESSIVE_TRAILING_ENABLED = True
# WARNING: do not reorder stages while open trades are active (trades track by index)
PROGRESSIVE_TRAILING_STAGES = [(1.5, 100), (2.0, 75), (2.5, 50)]  # (atr_mult, bps)
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
| EXTREME | < 25 | No daily/divergence/dom filter — deep panic caught regardless |
| STRONG | < 32 | above 1h SMA20 + daily NOT bearish + divergence not confirmed weak |
| MODERATE | < 40 | above 1h SMA20 + vol surge + momentum up + daily bullish + divergence ok + BTC.D not rising |

F&G < 20 (Extreme Fear) blocks MODERATE signals. BTC RSI < 30 + below SMA blocks STRONG signals.

**Tier 2 signal filters (all fail-open — ambiguous or API-down → allow):**
- **RSI divergence (T2-2)**: blocks STRONG/MODERATE when price makes a lower low AND RSI also makes a lower low (confirmed weakness). `detect_bullish_divergence()` returns `True` (divergence ok), `False` (block), `None` (allow).
- **BTC dominance (T2-3)**: blocks MODERATE when BTC.D rises >0.5% scan-over-scan. `get_btc_dominance()` caches CoinGecko result 1h. Returns `None` on failure → fail-open.

**Tier 3 position management:**
- **Break-even stop (T3-1)**: when price ≥ entry × (1 + `BREAKEVEN_ATR_MULT` × ATR%), cancels OCO and replaces with SL at entry. Fires once per trade (`breakeven_moved=True` guard). `_check_breakeven()` in scanner.py.
- **Trade timeout (T3-2)**: if any open trade has been open > `TRADE_TIMEOUT_H` hours, places a market sell and marks `status="timeout"`. Checked in `scan()` before break-even phase.
- **Volatility-adjusted sizing (T3-4)**: capital per trade = `TARGET_RISK_PCT × portfolio / atr_pct`, clamped to `[VOL_SIZING_MIN, VOL_SIZING_MAX] × CAPITAL`. Falls back to `CAPITAL` if ATR unavailable.

**Tier 4 analytics and entry quality:**
- **Performance reporting (T4-1)**: `_compute_perf_stats()` computes per-trade IR (mean/std of pnl_pct), profit factor, max consecutive losses, and per-tier win rates from the trailing 30 days of closed trades. Appended to daily digest if any closed trades exist.
- **15m entry refinement (T4-2)**: before placing an order, `_get_15m_rsi()` fetches 50 15m candles and computes RSI. If RSI > `ENTRY_REFINE_15M_RSI_MAX` (default 45), the signal is deferred this scan (logged + Telegram in cron mode). Fail-open: API error → allow.
- **Dynamic pair scoring (T4-3)**: correlation cap (≥3 candidates) sorts by `_pair_score()` instead of RSI alone. Score = win_rate × profit_factor from last 20 closed trades per symbol. Fewer than 3 trades → neutral score 0.5. All-wins → returns win_rate (avoids huge epsilon-divided value).
- **Progressive trailing stop (T4-4)**: after break-even arms, `_check_progressive_trailing()` tightens trailing delta at 1.5×/2×/2.5×ATR milestones (stages 1/2/3). Each stage cancels OCO and re-places with tighter `new_bps`. Cancel failure → retry next scan (stage not advanced). Re-OCO failure → critical Telegram + `no_oco` + advance stage (prevent retry loop). Stage tracked via `trade["trailing_stage"]` integer.

### Multi-timeframe (daily trend filter)

Each `analyze()` call fetches 30 daily candles to compute `daily_rsi` and check `daily_bullish`:
- **daily_bullish**: daily RSI > 45 AND price above daily SMA20 → MODERATE allowed
- **daily_neutral**: daily RSI ≥ 30 AND not bullish → STRONG allowed, MODERATE blocked
- **daily_bearish**: everything else (RSI < 30, OR RSI 30–45 below SMA) → STRONG blocked, EXTREME still fires

The daily RSI is displayed in TUI pair cards (`1d:XX`) and scan log lines. EXTREME signals bypass the daily filter to catch capitulation bottoms.

### Shared scan helpers (scanner.py — used by both `scan()` and TUI)

| Helper | Purpose |
|--------|---------|
| `build_market_context()` | Fetches F&G + BTC context + BTC dominance → returns full context dict |
| `apply_correlation_cap(candidates, conn)` | If ≥3 candidates, sorts by `_pair_score()` (T4-3) or RSI, keeps top 1 |
| `run_position_management(conn)` | T3-1 break-even + T4-4 progressive trailing on all open trades |
| `run_split_entry_checks(conn)` | T2-1 pending second legs — fire on trigger or expire after TTL |

All accept `conn: Optional[sqlite3.Connection] = None` — shared connection when called from `scan()`, own connection when called standalone from the TUI.

### Scan phases (scanner.py `_scan_body()` and tui.py `action_trigger_scan`)

1. **Check SL outcomes** — scan closed OCO orders, mark tp_hit/sl_hit/partial_tp; detect TP1 fills; compute weighted P&L for partial_tp exits
2. **Trade timeout (T3-2)** — force-exit any position open > `TRADE_TIMEOUT_H` hours; runs BEFORE the OCO loop so `no_oco` trades are also age-checked
3. **Split-entry check (T2-1)** — `run_split_entry_checks()`: check `pending_second_entries`; fire second leg if price ≤ trigger; expire entries older than TTL
4. **Break-even stop (T3-1)** — for each open+partial_tp trade: if price ≥ break-even trigger, cancel OCO + re-place with SL at entry; sets `breakeven_moved=True`
5. **Progressive trailing (T4-4)** — for each break-even-armed trade: check 1.5×/2×/2.5×ATR milestones; tighten trailing delta at each stage; advances `trailing_stage` index
6. **Fetch context** — Fear & Greed, BTC RSI/SMA/price, BTC dominance; fire F&G regime-change alert if threshold crossed
7. **Fetch portfolio** — live Binance balances
8. **Analyze all pairs** — collect candidates; apply divergence (T2-2) and dom-rising (T2-3) gates inside `analyze()`
9. **Correlation cap** — if ≥ 3 candidates, sort by `_pair_score()` win_rate×profit_factor (T4-3); keep top 1
10. **Circuit breaker** — if drawdown from `peak_portfolio_usdc` ≥ `MAX_DRAWDOWN_PCT`, clear candidates and halt
11. **Per-symbol guards** — skip if: open position exists, SL cooldown active, MAX_POSITIONS reached
12. **15m entry refinement (T4-2)** — fetch 50 15m candles; compute RSI; defer if RSI > `ENTRY_REFINE_15M_RSI_MAX`; fail-open
13. **Place orders** — `_place_and_arm()`: market buy → OCO → TP1 LIMIT_MAKER (T2-4) → arm split-entry pending (T2-1 EXTREME quality)
14. **Daily digest** — if `now.hour == DIGEST_HOUR` and not yet sent today, fire 7-day summary + 30-day perf stats (T4-1) to Telegram

### State machine per trade

```
open ──► tp_hit        (LIMIT_MAKER or final OCO TP filled) → exit_price/pnl_pct/exit_time written
     ──► sl_hit        (STOP_LOSS trailing filled) → SL cooldown SL_COOLDOWN_H hours
     ──► partial_tp    (TP1 LIMIT_MAKER filled, T2-4) → original OCO cancelled, new OCO for remaining qty
     │       └──► tp_hit / sl_hit   (final OCO fills; P&L = weighted avg of TP1 50% + exit 50%)
     ──► no_oco        (market buy OK but OCO failed → 🚨 Telegram alert)
     ──► partial_tp_no_oco  (TP1 filled + cancel or re-OCO failed → 🚨 Telegram alert)
```

`exit_price` is computed from the actual Binance fill (`cummulativeQuoteQty / executedQty`) via `_order_fill_price()` — not the stored activation price, which is wrong for trailing stops.

**Split-entry (T2-1) parallel state** — stored in `pending_second_entries` table, not in `trades`:
```
first_half_open + pending_second_entry ──► trigger price hit → cancel first OCO → buy second half
                                                              → combined OCO at weighted-avg entry
                                       ──► TTL expired (48h) → cleared, Telegram notice
                                       ──► cancel fails → pending preserved for retry
                                       ──► second buy fails after cancel → CRITICAL alert, pending cleared
```

### Two-timer TUI architecture

- **5s timer** → `_read_state_file()` — disk only, no API — updates cooldowns, portfolio cache, open P&L, peak drawdown, log tail
- **30s timer** → `action_trigger_scan()` — full API round-trip in `@work(thread=True, exclusive=True)`
- State flows via `ScanComplete` and `StateUpdated` messages from worker → main thread

### Alert deduplication

| Alert | Dedup mechanism |
|-------|----------------|
| Trade signals | `sent_signals` in state.json keyed by `symbol:tier`, suppressed 2h |
| F&G regime change | `fg_regime` in state.json — only fires when bucket changes |
| Circuit breaker | `cb_alert_sent_at` in state.json — suppressed for 4h |
| Daily digest | `last_digest_date` in state.json — once per calendar day |
| Partial TP1 hit | One-shot — fires on state transition to `partial_tp` |
| Split entry armed | One-shot on first-half placement |
| Split entry expired | One-shot on TTL clearance |

### state.db table/key reference

**SQLite tables (see schema in `scanner.py _DB_SCHEMA`):**

| Table | Written by | Purpose |
|-------|-----------|---------|
| `trades` | `insert_trade`, `update_trade_fields` | All trades (no cap). Statuses: `open`, `partial_tp`, `partial_tp_no_oco`, `tp_hit`, `sl_hit`, `no_oco`, `timeout` |
| `portfolio` + `portfolio_assets` | `save_portfolio` | Latest Binance balance snapshot |
| `fg_cache` | `set_fg_cache` in `get_fear_greed` | Cached F&G response (singleton, valid 25h) |
| `btc_dom_cache` | `set_btc_dom_cache` in `get_btc_dominance` | CoinGecko dominance value + timestamp (singleton, 1h cache) |
| `cooldowns` | `save_cooldown` via `_save_cooldown` | SL cooldown expiry timestamps per symbol |
| `sent_signals` | `save_sent_signal` in `scan` | Signal dedup ledger |
| `pending_second_entries` | `save/clear_pending_second_entry` | Split-entry pending legs (T2-1) |
| `scan_results` | `save_scan_results` | Current scan pair data (overwritten each scan) |
| `scan_signals` | `save_scan_results` | Active signals this scan |
| `scan_history` | `append_scan_history` | Rolling 50 scan events |
| `log_lines` | `get_state_dict` | Rolling 200 log lines (cache) |
| `kv` | `set_kv` | Scalar sentinels (see below) |

**`kv` table keys:**

| Key | Written by | Purpose |
|-----|-----------|---------|
| `peak_portfolio_usdc` | `save_state` | High-water mark for drawdown calculation |
| `fg_regime` | `save_state` | Last F&G regime bucket (`extreme_fear` / `fear` / `neutral` / `greed` / `extreme_greed`) |
| `open_pnl` | `save_state` | Aggregate unrealized P&L from last scan |
| `cb_alert_sent_at` | `save_state` | Timestamp of last circuit breaker Telegram alert |
| `last_scan` | `save_state` | ISO timestamp of last scan completion |
| `last_digest_date` | digest block in `scan` | ISO date of last morning digest send |
| `btc_dom_prev` | `scan` | Previous scan's BTC.D value for rise-detection (T2-3) |

**Trade dict extra fields (Tier 2+):**

| Field | Set by | Meaning |
|-------|--------|---------|
| `sl_pct`, `tp_pct` | `place_buy_order` | Percentage SL/TP used (for downstream TP1 math) |
| `tp1_order_id`, `tp1_price`, `tp1_qty` | `place_buy_order` | Partial TP1 LIMIT_MAKER details (T2-4) |
| `partial_tp1` | `_handle_partial_tp1` | `{exit_price, pnl_pct, exit_time}` for the first half exit |
| `split_entry` | `_place_split_second_entry` | `True` when trade is the combined second-leg result |
| `breakeven_moved` | `_check_breakeven` | `True` once break-even stop has been armed (T3-1) |
| `trailing_stage` | `place_buy_order`, `_check_progressive_trailing` | `0`=original delta; `1/2/3`=stage applied (T4-4) |
| `signal_strength` | `_place_and_arm` | `"EXTREME"/"STRONG"/"MODERATE"` — used by T4-1 per-tier stats and T4-3 pair score |

---

## Key design decisions

**TeeLogger guard** — `scanner.py` line 36: `if __name__ == "__main__": sys.stdout = TeeLogger()`. Without this, importing scanner from tui.py would hijack Textual's stdout.

**`_scan_ctx` not `_context`** — Textual has an internal `App._context()` method. A class attribute named `_context` shadows it and crashes `app.run()`. Always use `_scan_ctx` in `ScannerApp`.

**`$color` in RichLog** — Textual CSS variables (`$green`, `$red`, etc.) work in `Label`/`Static` widgets but NOT in `RichLog`. All `tlog()` calls must use Rich-standard color names (`green`, `red`, `dark_orange`, `yellow`, `cyan`) or hex `#rrggbb`.

**`markup_escape()` for exceptions** — Exception messages from Binance API responses can contain `[`, `]`, `{`, `}` that break Rich's markup parser. Always wrap `str(e)` in `markup_escape()` before passing to `tlog()`.

**OCO failure guard** — If the market buy fills but the OCO POST fails, `place_buy_order` returns `(order, None, trade_partial)` with `status="no_oco"` and immediately fires a Telegram alert. The position appears in state.db for manual intervention.

**SQLite WAL mode** — `db_connect()` sets `PRAGMA journal_mode=WAL; synchronous=NORMAL; busy_timeout=5000`. WAL allows the TUI's 5s state reader to run concurrently with the scanner writer — no locking, no data corruption, no surgical-patch pattern needed. This is why the entire JSON surgical-patch architecture was removed.

**Binance API weight** — Each scan costs ~30 weight. At 30s interval from TUI = ~60/min, well under the 1200/min limit. The 5s state watcher reads only SQLite — zero API calls.

**`_order_fill_price()` for exit tracking** — SL/TP fills use `cummulativeQuoteQty / executedQty` from the filled Binance order object (falls back to `price`). Never use the stored `trade["sl"]` or `trade["tp"]` as exit price — for trailing stops the actual fill is higher than the initial activation price.

**`last_digest_date` is a simple `set_kv()` call** — No re-read needed. WAL mode means the digest's `set_kv()` after `send_telegram()` won't clobber any concurrent scanner write; SQLite serializes writers automatically.

**F&G `is_fresh` guard** — `get_fear_greed()` returns `(value, classification, is_fresh: bool)`. Regime-change alerts only fire when `is_fresh=True` to prevent spurious alerts from the stale `(50, "Neutral")` fallback.

**Divergence warm-up buffer (T2-2)** — RSI series for `detect_bullish_divergence()` is computed with `lb = lookback + 14 + 28` candles so the oldest retained value has ≥28 Wilder smoothing steps (~13% seed contamination). Never reduce this buffer.

**`signed_delete` params in query string** — Binance DELETE endpoints read params from the URL query string, not the body. `signed_delete()` appends params to the URL; do not refactor to send them as `data=`.

**`_place_split_second_entry` sentinel returns** — Returns `None` (cancel failed → caller preserves pending entry for retry), a `{"status":"critical_fail"}` dict (second buy failed after cancel → caller clears pending), or a normal trade dict (success). Never treat all `None`/falsy returns the same way.

**`partial_tp` counts as open** — `_check_sl_outcomes` uses `active_statuses = ("open", "partial_tp")`. `partial_tp_no_oco` is intentionally excluded (manual intervention required, no scanner supervision).

---

## Common tasks

### Add a new trading pair
Edit `config.py`: add the symbol to `PAIRS`. The TUI grid resizes automatically (`on_mount` computes `cols = ceil(n/2)`).

### Change risk parameters
Edit `config.py`: `ATR_SL_MULT`, `ATR_TP_MULT`, `TRAILING_DELTA`. Changes take effect on next scan.

### Disable trailing stop
Set `TRAILING_DELTA = 0` in `config.py`. OCO will use `STOP_LOSS_LIMIT` instead of `STOP_LOSS`.

### Adjust circuit breaker threshold
Edit `MAX_DRAWDOWN_PCT` in `config.py` (default `0.15` = 15%). Set to `1.0` to effectively disable it.

### Change digest time
Edit `DIGEST_HOUR` in `config.py`. The digest fires within that calendar hour on the next scan boundary (up to 30 min after the hour starts).

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
import sqlite3
conn = sqlite3.connect("state.db")
conn.execute("DELETE FROM cooldowns WHERE symbol = 'ETHUSDC'")
conn.commit(); conn.close()
```

### Reset the circuit breaker peak
```python
import sqlite3
conn = sqlite3.connect("state.db")
conn.execute("DELETE FROM kv WHERE key = 'peak_portfolio_usdc'")  # re-established on next scan
conn.commit(); conn.close()
```

### Force a digest resend today
```python
import sqlite3
conn = sqlite3.connect("state.db")
conn.execute("INSERT OR REPLACE INTO kv (key, value) VALUES ('last_digest_date', '')")
conn.commit(); conn.close()   # fires on next scan at DIGEST_HOUR
```

### Run backtest after changing strategy params
```bash
python3 backtest.py
# Results written to backtest_results.json
# TUI left panel "BACKTEST" section reads this file automatically
```

### Disable the divergence filter
Set `DIVERGENCE_ENABLED = False` in `config.py`. `analyze()` will skip the RSI series computation entirely.

### Disable the BTC dominance filter
Set `BTC_DOM_ENABLED = False` in `config.py`. `get_btc_dominance()` returns `None` immediately; no CoinGecko call is made.

### Disable partial TP1
Set `PARTIAL_TP_ENABLED = False` in `config.py`. The standalone TP1 LIMIT_MAKER placement is skipped; the full position remains protected by the OCO until TP2 or SL.

### Disable split entry
Set `SPLIT_ENTRY_ENABLED = False` in `config.py`. EXTREME quality signals still use `CAPITAL/2` (the falling-knife cap applies to all EXTREME signals regardless). No second-leg pending entry is armed.

### Clear a stuck pending split entry
```python
import sqlite3
conn = sqlite3.connect("state.db")
conn.execute("DELETE FROM pending_second_entries WHERE symbol = 'ETHUSDC'")
# or: conn.execute("DELETE FROM pending_second_entries") to clear all
conn.commit(); conn.close()
```

### Clear stale BTC dominance cache
```python
import sqlite3
conn = sqlite3.connect("state.db")
conn.execute("DELETE FROM btc_dom_cache")
conn.execute("DELETE FROM kv WHERE key = 'btc_dom_prev'")
conn.commit(); conn.close()
```

### Disable break-even stop
Set `BREAKEVEN_ENABLED = False` in `config.py`. SL remains at original ATR-based level for entire trade duration.

### Disable trade timeout
Set `TRADE_TIMEOUT_ENABLED = False` in `config.py`. Positions can stay open indefinitely until OCO fills.

### Disable 15m entry refinement
Set `ENTRY_REFINE_ENABLED = False` in `config.py`. Orders placed without checking 15m RSI momentum state.

### Disable dynamic pair scoring
Set `PAIR_SCORE_ENABLED = False` in `config.py`. Correlation cap falls back to lowest-RSI sort (original behavior).

### Disable progressive trailing
Set `PROGRESSIVE_TRAILING_ENABLED = False` in `config.py`. Break-even stop stays at entry price for all ATR milestones.

### Reset trailing stage for a trade
If a trade's `trailing_stage` gets stuck (e.g., after manual OCO intervention):
```python
import sqlite3
conn = sqlite3.connect("state.db")
conn.execute("""
    UPDATE trades SET trailing_stage = 0
    WHERE symbol = 'ETHUSDC' AND status = 'open'
""")
conn.commit(); conn.close()
```

---

## What NOT to do

- **Never import scanner.py with TeeLogger active** — guard must stay as `if __name__ == "__main__"`
- **Never use `$color` names in `tlog()` calls** — use Rich color names only
- **Never call `get_open_positions()` more than once per scan** — it makes 2+ signed API calls; store the result and reuse it
- **Never remove the OCO failure guard** — a filled buy with no OCO is an unprotected position
- **Never hardcode credentials or strategy params outside `config.py`**
- **Never rename `_scan_ctx` back to `_context`** — it shadows a Textual internal method
- **Never use `trade["sl"]` or `trade["tp"]` as exit price** — use `_order_fill_price()` on the filled order object; activation price is wrong for trailing stops
- **Never write to state.json** — it is no longer the persistence layer; `state.db` is canonical. Use `update_trade_fields()`, `set_kv()`, or the appropriate table helper for all state mutations
- **Never treat all falsy returns from `_place_split_second_entry` the same** — `None` means preserve pending (retry); `{"status":"critical_fail"}` means clear pending (unrecoverable)
- **Never put `signed_delete` params in the request body** — Binance DELETE reads the query string only
- **Never reduce `DIVERGENCE_LOOKBACK + 14 + 28` warm-up buffer** — fewer than 28 Wilder smoothing steps corrupts the oldest RSI values
- **Never add `partial_tp_no_oco` to `active_statuses`** — these positions need manual intervention; the scanner should not monitor them automatically
- **Never reorder `PROGRESSIVE_TRAILING_STAGES` while trades are active** — trades track progress via integer index; reordering changes which trigger/bps applies to the current stage
- **Never annualize the per-trade IR in `_compute_perf_stats`** — trades have variable hold times, not fixed scan-period returns; annualizing with 17520 (scan periods/year) produces meaningless values
- **Never skip the 15m RSI warm-up candles** — `ENTRY_REFINE_15M_LIMIT=50` gives 35 Wilder smoothing steps; fewer than ~28 produces RSI values with >20% error near thresholds
