#!/usr/bin/env python3
"""
Binance Trading Scanner
Config: ETH/ADA/DOGE/BNB (USDC pairs) | $200/trade | SL -3% | TP +7.5%
"""

from __future__ import annotations

import math
import os
import json
import hmac
import hashlib
import time
import subprocess
import urllib.request
import urllib.parse
from datetime import datetime, timedelta
from typing import Any, Optional

SCANNER_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE  = os.path.join(SCANNER_DIR, "state.json")
LOG_FILE    = os.path.join(SCANNER_DIR, "scanner.log")

import sys  # noqa: E402

class TeeLogger:
    """Write to both stdout and the log file (append)."""
    def __init__(self):
        self._log = open(LOG_FILE, "a", buffering=1)
        self._stdout = sys.__stdout__
    def write(self, msg: str) -> None:
        self._stdout.write(msg)
        self._log.write(msg)
    def flush(self) -> None:
        self._stdout.flush()
        self._log.flush()

if __name__ == "__main__":
    sys.stdout = TeeLogger()

# ── Config ────────────────────────────────────────────────────────────────────
from config import (  # noqa: E402
    BINANCE_API_KEY    as API_KEY,
    BINANCE_SECRET_KEY as SECRET_KEY,
    TELEGRAM_TOKEN,
    TELEGRAM_CHAT_ID,
    WEBHOOK_URL,
    PAIRS, CAPITAL,
    MAX_POSITIONS, SL_COOLDOWN_H, MAX_DRAWDOWN_PCT, DIGEST_HOUR,
    DIVERGENCE_ENABLED, DIVERGENCE_LOOKBACK, DIVERGENCE_SWING_DEPTH,
    BTC_DOM_ENABLED, BTC_DOM_CACHE_H, BTC_DOM_RISE_THRESHOLD,
    PARTIAL_TP_ENABLED, PARTIAL_TP1_ATR_MULT, PARTIAL_TP1_QTY_PCT,
    SPLIT_ENTRY_ENABLED, SPLIT_ENTRY_ATR_MULT, SPLIT_ENTRY_TTL_H,
    TRADE_TIMEOUT_ENABLED, TRADE_TIMEOUT_H,
    STOP_LOSS, TAKE_PROFIT,
    TRAILING_DELTA,
    ATR_SL_MULT, ATR_TP_MULT, ATR_SL_MIN, ATR_SL_MAX,
    INTERVAL, KLINE_LIMIT,
)

def send_telegram(text: str) -> None:
    """Send a message to the paired Telegram user (non-blocking)."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    import threading
    def _post():
        try:
            payload = json.dumps({
                "chat_id":    TELEGRAM_CHAT_ID,
                "text":       text,
                "parse_mode": "Markdown",
            }).encode()
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                data=payload, method="POST",
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            print(f"  ⚠ Telegram failed: {e}")
    threading.Thread(target=_post, daemon=True).start()

def send_telegram_sync(text: str) -> None:
    """Send a Telegram message synchronously (blocking). Used before polling replies."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        payload = json.dumps({
            "chat_id":    TELEGRAM_CHAT_ID,
            "text":       text,
            "parse_mode": "Markdown",
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data=payload, method="POST",
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"  ⚠ Telegram sync send failed: {e}")

def telegram_get_updates(offset: int, timeout_sec: int) -> list[dict[str, Any]]:
    """Long-poll Telegram getUpdates. Returns list of update dicts."""
    url = (f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
           f"?offset={offset}&timeout={timeout_sec}")
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout_sec + 5) as r:
            return json.loads(r.read()).get("result", [])
    except Exception as e:
        print(f"  ⚠ Telegram poll failed: {e}")
        return []

def wait_telegram_confirm(symbol: str, timeout: int = 120) -> bool:
    """
    Send a CONFIRM/SKIP prompt then long-poll for the user's reply.
    Returns True on CONFIRM, False on SKIP or timeout.
    """
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return False

    # Baseline: ignore messages older than right now
    updates = telegram_get_updates(offset=0, timeout_sec=0)
    offset = (updates[-1]["update_id"] + 1) if updates else 0

    send_telegram_sync(
        f"Reply *CONFIRM* to place `{symbol}` order or *SKIP* to skip\n"
        f"_(expires in {timeout}s)_"
    )

    deadline = time.time() + timeout
    while time.time() < deadline:
        poll_sec = min(30, int(deadline - time.time()))
        if poll_sec <= 0:
            break
        for upd in telegram_get_updates(offset=offset, timeout_sec=poll_sec):
            offset = upd["update_id"] + 1
            msg     = upd.get("message", {})
            chat_id = str(msg.get("chat", {}).get("id", ""))
            text    = msg.get("text", "").strip().upper()
            if chat_id == str(TELEGRAM_CHAT_ID):
                if text == "CONFIRM":
                    return True
                elif text == "SKIP":
                    send_telegram_sync(f"⏭ `{symbol}` — skipped.")
                    return False
                else:
                    send_telegram_sync("Reply *CONFIRM* to buy or *SKIP* to skip.")

    send_telegram_sync(f"⏰ `{symbol}` — timed out, no order placed.")
    return False

def call_webhook(signal: dict[str, Any]) -> None:
    """POST signal data to Claude Terminal webhook (non-blocking)."""
    if not WEBHOOK_URL:
        return
    import threading
    def _post():
        try:
            payload = json.dumps(signal).encode()
            req = urllib.request.Request(WEBHOOK_URL, data=payload, method="POST",
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            print(f"  ⚠ Webhook call failed: {e}")
    threading.Thread(target=_post, daemon=True).start()

def notify_mac(title: str, message: str) -> None:
    """Send a native macOS notification."""
    title   = title.replace("\\", "\\\\").replace('"', '\\"')
    message = message.replace("\\", "\\\\").replace('"', '\\"')
    script  = f'display notification "{message}" with title "{title}" sound name "Ping"'
    subprocess.run(["osascript", "-e", script], capture_output=True)

def save_state(
    results: list[dict[str, Any]],
    signals: list[dict[str, Any]],
    new_trades: Optional[list[dict[str, Any]]] = None,
    portfolio: Optional[dict[str, Any]] = None,
    fg_regime: Optional[str] = None,
    open_pnl: Optional[float] = None,
    cb_alert_sent_at: Optional[str] = None,
) -> None:
    """Save last scan results to state.json for the dashboard."""
    try:
        state: dict[str, Any] = {"last_scan": datetime.now().isoformat(), "results": results, "signals": signals,
                 "history": [], "trades": [], "cooldowns": {}, "fg_cache": None,
                 "portfolio": None, "logs": []}
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE) as f:
                old = json.load(f)
            state["history"]   = (old.get("history")   or [])[-49:]  # keep last 50
            state["trades"]    = (old.get("trades")    or [])[-99:]  # keep last 100
            state["cooldowns"] = old.get("cooldowns")  or {}         # preserve SL cooldowns
            state["fg_cache"]      = old.get("fg_cache")                     # preserve F&G cache
            state["portfolio"]     = old.get("portfolio")                  # preserve last portfolio
            state["sent_signals"]  = old.get("sent_signals") or {}         # preserve dedup ledger
            state["fg_regime"]        = fg_regime or old.get("fg_regime")     # preserve regime state
            state["open_pnl"]         = open_pnl if open_pnl is not None else old.get("open_pnl")
            state["peak_portfolio_usdc"] = old.get("peak_portfolio_usdc")   # updated below
            state["cb_alert_sent_at"]  = cb_alert_sent_at or old.get("cb_alert_sent_at")
            state["last_digest_date"]  = old.get("last_digest_date")
            state["btc_dom_cache"]     = old.get("btc_dom_cache")             # preserve CoinGecko cache
            state["btc_dom_prev"]      = old.get("btc_dom_prev")             # preserve last dominance value
            state["pending_second_entries"] = old.get("pending_second_entries") or {}  # preserve split-entry pending legs
        if portfolio:
            state["portfolio"] = portfolio                            # overwrite with fresh data
        # Update peak_portfolio_usdc high-water mark (always, even on first state.json write)
        current_total = portfolio["total_usdc"] if portfolio else None
        old_peak: float = state.get("peak_portfolio_usdc") or 0.0
        if current_total is not None and current_total > old_peak:
            state["peak_portfolio_usdc"] = current_total
        elif old_peak > 0:
            state["peak_portfolio_usdc"] = old_peak
        if signals:
            state["history"].append({"time": state["last_scan"], "signals": signals})
        if new_trades:
            state["trades"].extend(new_trades)
        # Embed last 200 lines of log into state.json
        if os.path.exists(LOG_FILE):
            with open(LOG_FILE, "r") as f:
                state["logs"] = f.readlines()[-200:]
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception:
        pass

BASE_URL = "https://api.binance.com"

# ── HTTP helpers ─────────────────────────────────────────────────────────────
def get(path: str, params: Optional[dict[str, Any]] = None) -> Any:
    url = BASE_URL + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "User-Agent": "binance-spot/1.1.0 (Scanner)",
        "X-MBX-APIKEY": API_KEY,
    })
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())

def signed_get(path: str, params: dict[str, Any]) -> Any:
    params["timestamp"] = int(time.time() * 1000)
    query = urllib.parse.urlencode(params)
    sig = hmac.new(SECRET_KEY.encode(), query.encode(), hashlib.sha256).hexdigest()
    params["signature"] = sig
    return get(path, params)

def signed_post(path: str, params: dict[str, Any]) -> Any:
    params["timestamp"] = int(time.time() * 1000)
    query = urllib.parse.urlencode(params)
    sig = hmac.new(SECRET_KEY.encode(), query.encode(), hashlib.sha256).hexdigest()
    params["signature"] = sig
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(
        BASE_URL + path, data=data, method="POST",
        headers={
            "User-Agent": "binance-spot/1.1.0 (Scanner)",
            "X-MBX-APIKEY": API_KEY,
            "Content-Type": "application/x-www-form-urlencoded",
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        raise Exception(f"HTTP {e.code} — {body}") from None

def signed_delete(path: str, params: dict[str, Any]) -> Any:
    """Authenticated DELETE request — used to cancel OCO order lists.

    Binance DELETE endpoints read params from the query string, not the body.
    """
    params["timestamp"] = int(time.time() * 1000)
    query = urllib.parse.urlencode(params)
    sig = hmac.new(SECRET_KEY.encode(), query.encode(), hashlib.sha256).hexdigest()
    params["signature"] = sig
    full_url = BASE_URL + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        full_url, data=None, method="DELETE",
        headers={
            "User-Agent": "binance-spot/1.1.0 (Scanner)",
            "X-MBX-APIKEY": API_KEY,
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        raise Exception(f"HTTP {e.code} — {body}") from None

# ── Indicators ───────────────────────────────────────────────────────────────
def calc_rsi(closes: list[float], period: int = 14) -> float:
    """Wilder's EMA RSI — matches TradingView/Binance standard."""
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    if len(gains) < period:
        return 50.0
    # Seed: simple average for the first period
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    # Wilder's smoothing for the remaining values
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    return 100 - (100 / (1 + avg_gain / avg_loss))

def calc_atr(klines: list[list[Any]], period: int = 14) -> Optional[float]:
    """Wilder's ATR — uses high/low/prev_close from raw klines."""
    trs = []
    for i in range(1, len(klines)):
        high       = float(klines[i][2])
        low        = float(klines[i][3])
        prev_close = float(klines[i-1][4])
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    if len(trs) < period:
        return None
    # Seed
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr

def calc_sma(closes: list[float], period: int = 20) -> Optional[float]:
    if len(closes) < period:
        return None  # caller must handle — don't silently return price (price > price = False)
    return sum(closes[-period:]) / period

def detect_bullish_divergence(
    closes: list[float],
    rsi_series: list[float],
    lookback: int = 20,
    swing_depth: float = 0.005,
) -> Optional[bool]:
    """Detect bullish RSI divergence in the last `lookback` candles.

    Finds 3-bar local minima where the middle bar is at least `swing_depth` below
    both neighbours. With < 2 swing lows the pattern is ambiguous → None (allow).

    Returns:
      True  — price lower low + RSI higher low (classic bullish divergence → allow)
      False — price lower low + RSI lower low  (confirmed weakness → block)
      None  — ambiguous (< 2 swings, or price not making lower lows) → allow
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
        return None   # too few swings — ambiguous, allow signal

    i1, i2 = swings[-2], swings[-1]
    price_lower = win_c[i2] < win_c[i1]
    rsi_higher  = win_r[i2] > win_r[i1]

    if price_lower and rsi_higher:
        return True   # bullish divergence
    if price_lower and not rsi_higher:
        return False  # confirmed weakness
    return None       # no divergence pattern — allow

# ── Market context ───────────────────────────────────────────────────────────
def get_fear_greed() -> tuple[int, str, bool]:
    """Fetch Crypto Fear & Greed index — with state.json cache (valid 25h).

    Priority: live fetch → cached value (< 25h old) → fallback 50 + Telegram warning.
    """
    def _read_cache():
        try:
            if os.path.exists(STATE_FILE):
                with open(STATE_FILE) as f:
                    fg = json.load(f).get("fg_cache")
                if fg and (datetime.now() - datetime.fromisoformat(fg["ts"])) < timedelta(hours=25):
                    return int(fg["value"]), fg["classification"]
        except Exception:
            pass
        return None

    def _write_cache(value, classification):
        try:
            state = {}
            if os.path.exists(STATE_FILE):
                with open(STATE_FILE) as f:
                    state = json.load(f)
            state["fg_cache"] = {"value": value, "classification": classification,
                                 "ts": datetime.now().isoformat()}
            with open(STATE_FILE, "w") as f:
                json.dump(state, f, indent=2)
        except Exception:
            pass

    # 1. Live fetch
    try:
        req = urllib.request.Request(
            "https://api.alternative.me/fng/?limit=1",
            headers={"User-Agent": "scanner/1.0"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            entry = json.loads(r.read())["data"][0]
            value, classification = int(entry["value"]), entry["value_classification"]
            _write_cache(value, classification)
            return value, classification, True
    except Exception as e:
        print(f"  ⚠ Fear & Greed fetch failed: {e}")

    # 2. Cache fallback
    cached = _read_cache()
    if cached:
        print(f"  ↩ Using cached F&G: {cached[0]} ({cached[1]})")
        return cached[0], cached[1], True

    # 3. Stale/missing cache — neutral sentinel, regime alerts suppressed
    print("  ⚠ F&G cache expired or missing — using neutral 50, filters may be inactive")
    send_telegram("⚠️ F&G cache expired — sentiment filter inactive, using neutral 50")
    return 50, "Neutral", False  # is_fresh=False → regime check skipped in scan()

def get_btc_context() -> dict[str, Any]:  # {rsi: float, above_sma: bool, price: float}
    """Fetch BTC 1h RSI + SMA trend as a market regime filter."""
    try:
        klines = get("/api/v3/klines", {"symbol": "BTCUSDC", "interval": "1h", "limit": 100})
        closed = klines[:-1]
        closes = [float(k[4]) for k in closed]
        rsi    = calc_rsi(closes)
        sma20  = calc_sma(closes, 20)
        above  = (sma20 is not None) and (closes[-1] > sma20)
        return {"rsi": round(rsi, 1), "above_sma": above, "price": closes[-1]}
    except Exception as e:
        print(f"  ⚠ BTC context fetch failed: {e}")
        return {"rsi": 50.0, "above_sma": True, "price": 0}

# ── BTC dominance helpers (T2-3) ─────────────────────────────────────────────
COINGECKO_GLOBAL = "https://api.coingecko.com/api/v3/global"

def get_btc_dominance() -> Optional[float]:
    """Return current BTC dominance % (0-100), or None on any failure (fail-open).

    Caches the result in state.json["btc_dom_cache"] for BTC_DOM_CACHE_H hours to
    avoid hammering CoinGecko's free tier across repeated scans.
    """
    if not BTC_DOM_ENABLED:
        return None
    try:
        state: dict[str, Any] = {}
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE) as f:
                state = json.load(f)
        cache = state.get("btc_dom_cache") or {}
        cached_at = cache.get("ts")
        if cached_at:
            age_h = (datetime.now() - datetime.fromisoformat(cached_at)).total_seconds() / 3600
            if age_h < BTC_DOM_CACHE_H:
                return float(cache["value"])
        req  = urllib.request.Request(COINGECKO_GLOBAL, headers={"Accept": "application/json"})
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read().decode())
        dom  = float(data["data"]["market_cap_percentage"]["btc"])
        # Surgical patch: re-read state to avoid clobbering concurrent writes
        fresh: dict[str, Any] = {}
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE) as f:
                fresh = json.load(f)
        fresh["btc_dom_cache"] = {"value": dom, "ts": datetime.now().isoformat()}
        with open(STATE_FILE, "w") as f:
            json.dump(fresh, f, indent=2)
        return dom
    except Exception as e:
        print(f"  ⚠ BTC dominance fetch failed: {e}")
        return None


def _is_btc_dom_rising(current: Optional[float]) -> bool:
    """Return True when BTC.D has risen > BTC_DOM_RISE_THRESHOLD since last scan.

    Fail-open: returns False when current is None (CoinGecko down) or when there
    is no previous value on record (first run — no meaningful comparison yet).
    """
    if current is None:
        return False
    try:
        state: dict[str, Any] = {}
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE) as f:
                state = json.load(f)
        prev = state.get("btc_dom_prev")
        if prev is None:
            return False  # first run — no baseline to compare
        return float(current) > float(prev) * (1 + BTC_DOM_RISE_THRESHOLD)
    except Exception:
        return False  # fail-open on any state read error


# ── Signal logic ─────────────────────────────────────────────────────────────
def analyze(symbol: str, context: dict[str, Any]) -> dict[str, Any]:
    klines = get("/api/v3/klines", {"symbol": symbol, "interval": INTERVAL, "limit": KLINE_LIMIT})
    closed = klines[:-1]   # drop the currently-forming candle (incomplete data)
    closes = [float(k[4]) for k in closed]
    vols   = [float(k[5]) for k in closed]

    price     = closes[-1]
    rsi       = calc_rsi(closes)

    # ── RSI divergence series (T2-2) ─────────────────────────────────────────
    # Compute per-candle RSI values for the lookback window with Wilder warm-up buffer.
    rsi_series: Optional[list[float]] = None
    div_result: Optional[bool] = None
    if DIVERGENCE_ENABLED:
        # Buffer = lookback + period + 28 smoothing steps (≈2×period → ~13% seed influence)
        # so the oldest retained RSI value has at least 28 Wilder steps after seeding.
        lb  = DIVERGENCE_LOOKBACK + 14 + 28
        win = closes[-lb:]
        rsi_series = [calc_rsi(win[:i]) for i in range(14, len(win) + 1)]
        rsi_series = rsi_series[-DIVERGENCE_LOOKBACK:]

    sma20     = calc_sma(closes, 20)
    above_sma = (sma20 is not None) and (price > sma20)
    avg_vol   = sum(vols[:-1]) / (len(vols) - 1) if len(vols) > 1 else 0
    vol_surge = avg_vol > 0 and vols[-1] > avg_vol * 1.3
    momentum_up = closes[-1] > closes[-5]   # 5h lookback — filters single-candle noise

    # ── Daily trend filter (multi-timeframe) ─────────────────────────────────
    # Prevents entering 1h oversold signals during a sustained daily downtrend.
    # EXTREME signals bypass this filter — deep panic is worth catching regardless.
    daily_bullish = True   # default: allow if daily fetch fails
    daily_rsi_val = None
    try:
        d_klines  = get("/api/v3/klines", {"symbol": symbol, "interval": "1d", "limit": 30})
        d_closed  = d_klines[:-1]
        d_closes  = [float(k[4]) for k in d_closed]
        d_rsi     = calc_rsi(d_closes)
        d_sma20   = calc_sma(d_closes, 20)
        daily_rsi_val  = round(d_rsi, 1)
        # bullish: daily RSI > 45 AND price above daily SMA20
        # neutral: daily RSI 30-45 (recovery possible) — still allow STRONG
        # bearish: daily price below daily SMA20 AND daily RSI < 35
        d_above_sma    = (d_sma20 is not None) and (d_closes[-1] > d_sma20)
        daily_bullish  = d_rsi > 45 and d_above_sma
        daily_neutral  = d_rsi >= 30 and not daily_bullish   # allow STRONG but block MODERATE
        daily_bearish  = not daily_bullish and not daily_neutral
    except Exception:
        daily_neutral  = False
        daily_bearish  = False

    fg             = context["fg_value"]        # 0-100
    btc_above      = context["btc_above_sma"]
    btc_dom_rising = context.get("btc_dom_rising", False)   # True → BTC.D surging (T2-3)

    # ── RSI divergence gate (T2-2) ────────────────────────────────────────────
    # Block STRONG/MODERATE when price AND RSI both make lower lows (confirmed weakness).
    # EXTREME bypasses — deep panic is worth catching regardless of recent structure.
    divergence_ok = True
    if DIVERGENCE_ENABLED and rsi_series and len(rsi_series) >= 4:
        div_result = detect_bullish_divergence(
            closes, rsi_series, DIVERGENCE_LOOKBACK, DIVERGENCE_SWING_DEPTH,
        )
        if div_result is False:
            divergence_ok = False   # confirmed weakness — block STRONG and MODERATE

    # ── Signal tiers (1h thresholds) ─────────────────────────────────────────
    # EXTREME: deep oversold — always qualifies regardless of market regime.
    # Two sub-cases for sizing (handled in scan()):
    #   - EXTREME_QUALITY: RSI<25 + above SMA + F&G<40 → $200 (rare oversold gem)
    #   - EXTREME_CRASH:   RSI<25 + below SMA or F&G≥40 → $100 (falling knife, cap exposure)
    extreme_signal = rsi < 25
    extreme_quality = extreme_signal and above_sma and fg < 40

    # STRONG: solid oversold + trend alignment + not in euphoria
    # Blocked only if daily is clearly bearish (daily downtrend confirmed)
    strong_signal = rsi < 32 and above_sma and fg < 75 and not daily_bearish and divergence_ok

    # MODERATE: requires clean setup + healthy market regime + daily not bearish
    moderate_signal = (
        rsi < 40 and above_sma and vol_surge and momentum_up
        and fg < 60               # skip when market is greedy
        and btc_above             # skip when BTC in downtrend
        and daily_bullish         # requires confirmed daily uptrend for MODERATE
        and divergence_ok         # skip when RSI confirms weakness (T2-2)
        and not btc_dom_rising    # skip when BTC dominance is surging (T2-3)
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
        "daily_rsi":        daily_rsi_val,   # None if daily fetch failed
        "sma20":            round(sma20, 6) if sma20 is not None else None,
        "above_sma":        above_sma,
        "vol_surge":        vol_surge,
        "momentum":         momentum_up,
        "change24h":        change_pct,
        "buy_signal":       extreme_signal or strong_signal or moderate_signal,
        "signal_strength":  strength,
        "extreme_quality":  extreme_quality,
        "divergence":       div_result,      # True/False/None; None = ambiguous/disabled
        "btc_dom_rising":   btc_dom_rising, # True when BTC.D surging (T2-3)
        "closed_klines":    closed,  # passed to place_buy_order for ATR-based SL/TP
    }

# ── Open position guard ──────────────────────────────────────────────────────
def has_open_position(symbol: str) -> bool:
    """Return True if there is already an open order or OCO for this symbol."""
    try:
        open_orders = signed_get("/api/v3/openOrders", {"symbol": symbol})
        if open_orders:
            return True
        oco_lists = signed_get("/api/v3/openOrderList", {})
        for oco in oco_lists:
            for leg in oco.get("orders", []):
                if leg.get("symbol") == symbol:
                    return True
    except Exception as e:
        print(f"  ⚠ Position check failed for {symbol}: {e}")
    return False

# ── Portfolio ────────────────────────────────────────────────────────────────
def get_open_positions() -> list[dict[str, Any]]:
    """Return open positions with live P&L, sourced from OCO list + state.json trades."""
    try:
        ocos = signed_get("/api/v3/openOrderList", {})
    except Exception:
        return []
    if not ocos:
        return []

    active_symbols = {oco["symbol"] for oco in ocos}

    # Most recent trade per symbol for entry/TP/SL/qty
    trades_by_symbol = {}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                state = json.load(f)
            for trade in reversed(state.get("trades") or []):
                sym = trade.get("symbol")
                if sym in active_symbols and sym not in trades_by_symbol:
                    trades_by_symbol[sym] = trade
        except Exception:
            pass

    positions = []
    for symbol in sorted(active_symbols):
        try:
            current = float(get("/api/v3/ticker/price", {"symbol": symbol})["price"])
        except Exception:
            current = None
        trade   = trades_by_symbol.get(symbol, {})
        entry   = trade.get("entry")
        qty     = trade.get("qty")
        pnl     = (current - entry) * qty if (entry and qty and current) else None
        pnl_pct = (current - entry) / entry * 100 if (entry and current) else None
        positions.append({
            "symbol":  symbol,
            "qty":     qty,
            "entry":   entry,
            "current": current,
            "tp":      trade.get("tp"),
            "sl":      trade.get("sl"),
            "pnl":     pnl,
            "pnl_pct": pnl_pct,
            "time":    trade.get("time"),   # entry timestamp for "held" calculation
        })
    return positions

def get_portfolio() -> Optional[dict[str, Any]]:
    """Fetch account balances + live USDC prices → portfolio snapshot.

    Returns a dict:
      {
        "assets":    [{asset, qty, price_usdc, value_usdc, pct}],
        "total_usdc": float,
        "fetched_at": iso_string,
      }
    Stablecoins (USDC/BUSD/USDT) are priced at 1.0.
    Dust positions (< $0.10) are excluded.
    """
    STABLES = {"USDC", "BUSD", "USDT", "DAI", "TUSD"}
    try:
        account = signed_get("/api/v3/account", {})
        raw = [b for b in account.get("balances", [])
               if float(b["free"]) + float(b["locked"]) > 0]
    except Exception as e:
        print(f"  ⚠ Portfolio fetch failed: {e}")
        return None

    assets = []
    for b in raw:
        asset = b["asset"]
        qty   = float(b["free"]) + float(b["locked"])
        if asset in STABLES:
            price = 1.0
        else:
            # Try USDC pair first, fall back to USDT
            price = None
            for quote in ("USDC", "USDT"):
                try:
                    price = float(get("/api/v3/ticker/price",
                                      {"symbol": asset + quote})["price"])
                    break
                except Exception:
                    continue
        if price is None:
            continue  # untradeable / no price — skip
        value = qty * price
        if value < 0.10:
            continue  # dust
        assets.append({
            "asset":      asset,
            "qty":        qty,
            "price_usdc": price,
            "value_usdc": value,
        })

    total = sum(a["value_usdc"] for a in assets)
    for a in assets:
        a["pct"] = (a["value_usdc"] / total * 100) if total > 0 else 0
    assets.sort(key=lambda a: -a["value_usdc"])

    return {
        "assets":     assets,
        "total_usdc": total,
        "fetched_at": datetime.now().isoformat(),
    }

# ── Cooldown helpers ─────────────────────────────────────────────────────────
def _load_cooldowns() -> dict[str, str]:
    """Return {symbol: expiry_iso} from state.json, pruning expired entries."""
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
        now = datetime.now()
        return {sym: exp for sym, exp in (state.get("cooldowns") or {}).items()
                if datetime.fromisoformat(exp) > now}
    except Exception:
        return {}

def _save_sent_signals(sent_signals: dict[str, str]) -> None:
    """Patch sent_signals into state.json without touching other fields."""
    if not os.path.exists(STATE_FILE):
        return
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
        state["sent_signals"] = sent_signals
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception:
        pass

def _order_fill_price(order: dict[str, Any]) -> Optional[float]:
    """Return actual avg fill price from a FILLED Binance order object.

    Uses cummulativeQuoteQty / executedQty (accurate for trailing stops);
    falls back to the order's price field.
    """
    try:
        qty   = float(order.get("executedQty") or 0)
        quote = float(order.get("cummulativeQuoteQty") or 0)
        if qty > 0:
            return quote / qty
    except Exception:
        pass
    try:
        return float(order["price"])
    except Exception:
        return None

def _save_cooldown(symbol: str) -> None:
    """Record a SL-cooldown for symbol for SL_COOLDOWN_H hours."""
    try:
        state = {}
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE) as f:
                state = json.load(f)
        cooldowns = state.get("cooldowns") or {}
        cooldowns[symbol] = (datetime.now() + timedelta(hours=SL_COOLDOWN_H)).isoformat()
        state["cooldowns"] = cooldowns
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception:
        pass

# ── Split-entry state helpers (T2-1) ─────────────────────────────────────────
def _load_pending_second_entries() -> dict[str, Any]:
    """Return pending_second_entries dict from state.json (empty dict on any failure)."""
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE) as f:
                return json.load(f).get("pending_second_entries") or {}
    except Exception:
        pass
    return {}


def _save_pending_second_entry(symbol: str, data: dict[str, Any]) -> None:
    """Surgical patch: write a single pending second entry to state.json."""
    try:
        state: dict[str, Any] = {}
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE) as f:
                state = json.load(f)
        pending = state.get("pending_second_entries") or {}
        pending[symbol] = data
        state["pending_second_entries"] = pending
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        print(f"  ⚠ Could not persist pending second entry for {symbol}: {e}")


def _clear_pending_second_entry(symbol: str) -> None:
    """Surgical patch: remove a pending second entry from state.json."""
    try:
        state: dict[str, Any] = {}
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE) as f:
                state = json.load(f)
        pending = state.get("pending_second_entries") or {}
        pending.pop(symbol, None)
        state["pending_second_entries"] = pending
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        print(f"  ⚠ Could not clear pending second entry for {symbol}: {e}")


def _place_split_second_entry(
    symbol: str,
    pending: dict[str, Any],
    current_price: float,
    closed_klines: list[list[Any]],
) -> Optional[dict[str, Any]]:
    """Execute the second half of a split entry.

    Flow:
    1. Cancel first OCO (pending["first_oco_id"]).
    2. Buy second half at current_price.
    3. Place combined OCO for total qty at weighted-average entry TP/SL.

    Returns the combined trade dict on success, None on any unrecoverable failure.
    Failures are reported via Telegram; pending entry is preserved for retry when
    the cancel hasn't happened yet (so the next scan can try again).
    """
    first_fill = pending["first_fill"]
    first_qty  = pending["first_qty"]
    capital_half = pending["capital_half"]
    sl_pct = pending["sl_pct"]
    tp_pct = pending["tp_pct"]

    # Step 1: Cancel the first OCO
    first_oco_id = pending["first_oco_id"]
    try:
        signed_delete("/api/v3/orderList", {"symbol": symbol, "orderListId": first_oco_id})
        print(f"  Cancelled first OCO #{first_oco_id} for split entry {symbol}")
    except Exception as cancel_err:
        send_telegram(
            f"⚠ *Split entry cancel failed* — `{symbol}` OCO #{first_oco_id} still active. "
            f"Retry next scan. Error: `{str(cancel_err)[:200]}`"
        )
        return None  # preserve pending entry for retry

    # Step 2: Buy second half
    try:
        # Get lot size for qty computation
        info = get("/api/v3/exchangeInfo", {"symbol": symbol})
        step    = 1.0
        tick    = 0.01
        min_qty = 0.0
        for f in info["symbols"][0]["filters"]:
            if f["filterType"] == "LOT_SIZE":
                step    = float(f["stepSize"])
                min_qty = float(f["minQty"])
            elif f["filterType"] == "PRICE_FILTER":
                tick = float(f["tickSize"])
        qty_prec  = len(str(step).rstrip('0').split('.')[-1])
        tick_prec = len(str(tick).rstrip('0').split('.')[-1])

        qty2_raw = capital_half / current_price
        qty2 = round(math.floor(qty2_raw / step) * step, qty_prec)
        if qty2 == 0 or qty2 < min_qty:
            raise ValueError(f"Second qty {qty2} below min_qty {min_qty}")

        second_order = signed_post("/api/v3/order", {
            "symbol":   symbol,
            "side":     "BUY",
            "type":     "MARKET",
            "quantity": qty2,
            "newClientOrderId": f"agent-scanner-split2-{int(time.time())}",
        })
        second_fill = (float(second_order.get("fills", [{}])[0].get("price", current_price))
                       if second_order.get("fills") else current_price)
        second_qty = float(second_order.get("executedQty", qty2))
    except Exception as buy_err:
        # OCO was already cancelled — CRITICAL: position partially unprotected
        send_telegram(
            f"🚨 *CRITICAL — split second buy FAILED* — `{symbol}` first OCO #{first_oco_id} "
            f"was cancelled but second buy failed. First half UNPROTECTED. "
            f"Error: `{str(buy_err)[:200]}`"
        )
        # Return a sentinel (not None) to tell the caller to clear pending.
        # Returning None is reserved for cancel-failure (caller must preserve pending).
        return {"status": "critical_fail"}  # caller clears pending; no trade to persist

    # Step 3: Place combined OCO at weighted-average entry TP/SL
    total_qty   = first_qty + second_qty
    avg_entry   = (first_fill * first_qty + second_fill * second_qty) / total_qty
    tp2_price   = round(round(avg_entry * (1 + tp_pct) / tick) * tick, tick_prec)
    sl2_price   = round(round(avg_entry * (1 - sl_pct) / tick) * tick, tick_prec)
    total_qty_r = round(math.floor(total_qty / step) * step, qty_prec)
    try:
        if TRAILING_DELTA > 0:
            combined_oco = signed_post("/api/v3/orderList/oco", {
                "symbol":              symbol,
                "side":                "SELL",
                "quantity":            total_qty_r,
                "aboveType":           "LIMIT_MAKER",
                "abovePrice":          tp2_price,
                "belowType":           "STOP_LOSS",
                "belowStopPrice":      sl2_price,
                "belowTrailingDelta":  TRAILING_DELTA,
                "belowTimeInForce":    "GTC",
            })
        else:
            sl2_limit = round(round(sl2_price * 0.995 / tick) * tick, tick_prec)
            combined_oco = signed_post("/api/v3/orderList/oco", {
                "symbol":           symbol,
                "side":             "SELL",
                "quantity":         total_qty_r,
                "aboveType":        "LIMIT_MAKER",
                "abovePrice":       tp2_price,
                "belowType":        "STOP_LOSS_LIMIT",
                "belowStopPrice":   sl2_price,
                "belowPrice":       sl2_limit,
                "belowTimeInForce": "GTC",
            })
    except Exception as oco_err:
        send_telegram(
            f"🚨 *Split entry combined OCO FAILED* — `{symbol}` {total_qty_r} UNPROTECTED. "
            f"Place OCO manually: TP ~${tp2_price} / SL ~${sl2_price}. "
            f"Error: `{str(oco_err)[:200]}`"
        )
        # Return a trade dict with no_oco so the position is visible in state
        return {
            "time":     datetime.now().isoformat(),
            "symbol":   symbol,
            "entry":    avg_entry,
            "tp":       tp2_price,
            "sl":       sl2_price,
            "qty":      total_qty_r,
            "capital":  capital_half * 2,
            "order_id": second_order.get("orderId"),
            "oco_id":   None,
            "status":   "no_oco",
            "split_entry": True,
            "sl_pct":   sl_pct,
            "tp_pct":   tp_pct,
        }

    print(f"  Split entry combined OCO #{combined_oco.get('orderListId')}: "
          f"avg_entry=${avg_entry:.4f} TP=${tp2_price:.4f} SL=${sl2_price:.4f} qty={total_qty_r}")
    send_telegram(
        f"✅ *Split entry complete* — `{symbol}`\n"
        f"Avg entry: `${avg_entry:.4f}` ({first_qty}@${first_fill:.4f} + {second_qty}@${second_fill:.4f})\n"
        f"TP `${tp2_price:.4f}` · SL `${sl2_price:.4f}` | OCO #{combined_oco.get('orderListId')}"
    )
    return {
        "time":     datetime.now().isoformat(),
        "symbol":   symbol,
        "entry":    avg_entry,
        "tp":       tp2_price,
        "sl":       sl2_price,
        "qty":      total_qty_r,
        "capital":  capital_half * 2,
        "order_id": second_order.get("orderId"),
        "oco_id":   combined_oco.get("orderListId"),
        "status":   "open",
        "split_entry": True,
        "sl_pct":   sl_pct,
        "tp_pct":   tp_pct,
    }


# ── Trade timeout handler (T3-2) ─────────────────────────────────────────────
def _handle_trade_timeout(trade: dict[str, Any], symbol: str) -> None:
    """Force-exit a position that has been open longer than TRADE_TIMEOUT_H.

    Steps:
    1. Cancel OCO (best-effort — OCO may already be filled/gone).
    2. If partial_tp, also cancel the original TP1 order.
    3. Market-sell remaining qty.
    4. Record status="timeout" (or "timeout_sell_failed" if sell API fails).
    No SL cooldown — timeout is not a signal failure.
    """
    age_h = (datetime.now() - datetime.fromisoformat(trade["time"])).total_seconds() / 3600
    qty   = trade.get("qty", 0)

    # Cancel OCO (best-effort)
    oco_id = trade.get("oco_id")
    if oco_id:
        try:
            signed_delete("/api/v3/orderList", {"symbol": symbol, "orderListId": oco_id})
            print(f"  Cancelled OCO #{oco_id} for timeout on {symbol}")
        except Exception as e:
            print(f"  ⚠ OCO cancel failed during timeout ({symbol}): {e}")

    # Cancel standalone TP1 order if still open (partial_tp state has already
    # transitioned to the new OCO, so tp1_order_id is the original leg)
    if PARTIAL_TP_ENABLED and trade.get("tp1_order_id") and trade.get("status") == "open":
        try:
            signed_delete("/api/v3/order", {"symbol": symbol, "orderId": trade["tp1_order_id"]})
        except Exception:
            pass  # best-effort; likely already filled or expired

    # Market-sell remaining qty
    try:
        sell_order = signed_post("/api/v3/order", {
            "symbol":   symbol,
            "side":     "SELL",
            "type":     "MARKET",
            "quantity": qty,
        })
        ep        = _order_fill_price(sell_order)
        entry     = trade.get("entry", 0.0)
        pnl_pct   = (ep - entry) / entry * 100 if (ep and entry) else None
        trade["status"]     = "timeout"
        trade["exit_price"] = ep
        trade["pnl_pct"]    = pnl_pct
        trade["exit_time"]  = datetime.now().isoformat()
        pnl_str = f"{pnl_pct:+.2f}%" if pnl_pct is not None else "N/A"
        print(f"  ⏱ Timeout exit {symbol} after {age_h:.0f}h — {pnl_str}")
        send_telegram(f"⏱ Timeout exit `{symbol}` after {age_h:.0f}h — {pnl_str}")
    except Exception as sell_err:
        msg = (f"🚨 *TIMEOUT SELL FAILED* — `{symbol}` position UNPROTECTED after {age_h:.0f}h. "
               f"Manual exit required. Error: `{str(sell_err)[:200]}`")
        print(f"  ✗ {msg}")
        send_telegram(msg)
        trade["status"] = "timeout_sell_failed"


def _check_sl_outcomes() -> None:
    """Check closed OCO orders — if stop leg filled, trigger SL cooldown.
    Also checks for partial TP1 fills (T2-4) on trades with tp1_order_id.
    Also handles trade timeout (T3-2) — force-exit positions open > TRADE_TIMEOUT_H.
    """
    if not os.path.exists(STATE_FILE):
        return
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
        # Include partial_tp trades — their new OCO can still hit TP2 or SL
        active_statuses = ("open", "partial_tp")
        active_trades = [
            t for t in (state.get("trades") or [])
            if t.get("status") in active_statuses
        ]
        if not active_trades:
            return
        oco_ids = {t["oco_id"]: t for t in active_trades if t.get("oco_id")}
        if not oco_ids:
            return
        for oco_id, trade in oco_ids.items():
            symbol = trade["symbol"]
            try:
                # T3-2: Timeout check — runs before any API call to avoid wasted weight
                if TRADE_TIMEOUT_ENABLED:
                    age_h = (datetime.now() - datetime.fromisoformat(trade["time"])).total_seconds() / 3600
                    if age_h >= TRADE_TIMEOUT_H:
                        _handle_trade_timeout(trade, symbol)
                        continue  # skip OCO fill check for this trade

                # Check all orders for the symbol to find TP or SL fill.
                # Scan all filled legs before deciding — prefer TP in edge case
                # where both legs somehow register FILLED (race condition).
                all_orders = signed_get("/api/v3/allOrders", {"symbol": symbol, "limit": 20})
                tp_filled = False
                sl_filled = False
                filled_tp_order: Optional[dict[str, Any]] = None
                filled_sl_order: Optional[dict[str, Any]] = None
                for o in all_orders:
                    if (o.get("status") == "FILLED"
                            and str(o.get("orderListId")) == str(oco_id)):
                        if o.get("type") == "LIMIT_MAKER":
                            tp_filled = True
                            filled_tp_order = o
                        elif o.get("type") in ("STOP_LOSS_LIMIT", "STOP_LOSS"):
                            sl_filled = True
                            filled_sl_order = o

                # Check for partial TP1 fill (T2-4) — only on trades still "open"
                # (not yet transitioned to partial_tp).
                if PARTIAL_TP_ENABLED and trade.get("tp1_order_id") and trade.get("status") == "open":
                    tp1_filled_order = next(
                        (o for o in all_orders
                         if str(o.get("orderId")) == str(trade["tp1_order_id"])
                         and o.get("status") == "FILLED"),
                        None,
                    )
                    if tp1_filled_order:
                        _handle_partial_tp1(trade, tp1_filled_order)
                        # Immediately flush the partial_tp transition to state.json
                        # (surgical patch) so that a crash before the main persistence
                        # loop does not cause double-processing on the next scan.
                        try:
                            _patch: dict[str, Any] = {}
                            if os.path.exists(STATE_FILE):
                                with open(STATE_FILE) as _f:
                                    _patch = json.load(_f)
                            for _t in (_patch.get("trades") or []):
                                if str(_t.get("order_id")) == str(trade.get("order_id")):
                                    _t["status"]     = trade["status"]
                                    _t["partial_tp1"] = trade.get("partial_tp1")
                                    _t["oco_id"]     = trade.get("oco_id")
                                    _t["qty"]        = trade.get("qty")
                                    break
                            with open(STATE_FILE, "w") as _f:
                                json.dump(_patch, _f, indent=2)
                        except Exception:
                            pass  # best-effort; main persistence loop is the authoritative write
                        # Skip OCO terminal-state checks this cycle —
                        # TP2/SL will be caught on the next scan.
                        continue

                if tp_filled:
                    print(f"  ✓ TP hit detected for {symbol}")
                    # Compute final P&L — may be weighted average for partial_tp trades
                    ep = (_order_fill_price(filled_tp_order)
                          if filled_tp_order else trade.get("tp"))
                    entry = trade.get("entry")
                    if trade.get("status") == "partial_tp" and trade.get("partial_tp1"):
                        # Weighted average: TP1 exit (50%) + TP2 exit (50%)
                        p1 = trade["partial_tp1"]
                        tp1_pnl = p1.get("pnl_pct") or 0.0
                        tp2_pnl = (ep - entry) / entry * 100 if (entry and ep) else 0.0
                        final_pnl = tp1_pnl * PARTIAL_TP1_QTY_PCT + tp2_pnl * (1 - PARTIAL_TP1_QTY_PCT)
                    else:
                        final_pnl = (ep - entry) / entry * 100 if (entry and ep) else None
                    send_telegram(f"✅ TP hit on `{symbol}` — target reached")
                    trade["status"]     = "tp_hit"
                    trade["exit_price"] = ep
                    trade["pnl_pct"]    = final_pnl
                    trade["exit_time"]  = datetime.now().isoformat()
                elif sl_filled:
                    print(f"  ⚠ SL hit detected for {symbol} — cooldown {SL_COOLDOWN_H}h")
                    _save_cooldown(symbol)
                    ep = (_order_fill_price(filled_sl_order)
                          if filled_sl_order else trade.get("sl"))
                    entry = trade.get("entry")
                    if trade.get("status") == "partial_tp" and trade.get("partial_tp1"):
                        p1 = trade["partial_tp1"]
                        tp1_pnl = p1.get("pnl_pct") or 0.0
                        sl_pnl  = (ep - entry) / entry * 100 if (entry and ep) else 0.0
                        final_pnl = tp1_pnl * PARTIAL_TP1_QTY_PCT + sl_pnl * (1 - PARTIAL_TP1_QTY_PCT)
                        send_telegram(
                            f"🔴 SL hit on `{symbol}` — partial TP1 was profitable. "
                            f"Net P&L: {final_pnl:+.2f}%. Pausing {SL_COOLDOWN_H}h."
                        )
                    else:
                        final_pnl = (ep - entry) / entry * 100 if (entry and ep) else None
                        send_telegram(f"🔴 SL hit on `{symbol}` — pausing signals {SL_COOLDOWN_H}h")
                    trade["status"]     = "sl_hit"
                    trade["exit_price"] = ep
                    trade["pnl_pct"]    = final_pnl
                    trade["exit_time"]  = datetime.now().isoformat()
            except Exception:
                pass
        # Persist updated trade statuses (tp_hit / sl_hit / partial_tp)
        # Re-read state.json after all API calls to avoid clobbering concurrent writes.
        with open(STATE_FILE) as f:
            state = json.load(f)
        # Build lookup by order_id (oco_ids key is oco_id, not a stable trade key;
        # use order_id as tiebreaker when multiple trades share a symbol — rare).
        resolved_by_order: dict[str, dict[str, Any]] = {
            str(t.get("order_id")): t for _, t in oco_ids.items()
        }
        for t in (state.get("trades") or []):
            resolved = resolved_by_order.get(str(t.get("order_id")))
            if resolved and resolved.get("status") not in ("open",):
                t["status"]     = resolved["status"]
                t["exit_price"] = resolved.get("exit_price")
                t["pnl_pct"]    = resolved.get("pnl_pct")
                t["exit_time"]  = resolved.get("exit_time")
                # Propagate partial_tp fields
                if resolved.get("partial_tp1"):
                    t["partial_tp1"] = resolved["partial_tp1"]
                if resolved.get("oco_id") is not None:
                    t["oco_id"] = resolved["oco_id"]
                if resolved.get("qty") is not None:
                    t["qty"] = resolved["qty"]
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        print(f"  ⚠ SL outcome check failed: {e}")

# ── Partial TP1 handler (T2-4) ───────────────────────────────────────────────
def _handle_partial_tp1(trade: dict[str, Any], tp1_order: dict[str, Any]) -> None:
    """React to a TP1 LIMIT_MAKER fill: record partial exit, cancel original OCO,
    place a new OCO for the remaining qty at TP2/SL.

    Called from _check_sl_outcomes() when trade["tp1_order_id"] is found FILLED.
    All mutations are written back to the caller's in-memory trade dict; the
    caller's persistence loop then flushes them to state.json.
    """
    symbol   = trade["symbol"]
    tp1_fill = _order_fill_price(tp1_order) or trade.get("tp1_price")
    entry    = trade.get("entry", 0.0)
    tp1_pnl  = (tp1_fill - entry) / entry * 100 if (tp1_fill and entry) else None
    trade["partial_tp1"] = {
        "exit_price": tp1_fill,
        "pnl_pct":    tp1_pnl,
        "exit_time":  datetime.now().isoformat(),
    }
    print(f"  ✓ Partial TP1 filled for {symbol} @ ${tp1_fill:.4f} ({tp1_pnl:+.2f}% on half position)")

    # Cancel original OCO (full-position protection) before placing reduced OCO
    oco_id = trade.get("oco_id")
    try:
        signed_delete("/api/v3/orderList", {"symbol": symbol, "orderListId": oco_id})
        print(f"  Cancelled original OCO #{oco_id}")
    except Exception as cancel_err:
        msg = (f"🚨 *Partial TP1 OCO cancel failed* — `{symbol}` original OCO #{oco_id} "
               f"still active. Error: `{str(cancel_err)[:200]}`")
        print(f"  ✗ {msg}")
        send_telegram(msg)
        trade["status"] = "partial_tp_no_oco"
        return

    # Place new OCO for remaining qty at original TP2 / SL levels
    remaining_qty = round(trade.get("qty", 0) - trade.get("tp1_qty", 0), 8)
    tp2_price  = trade.get("tp")
    sl_price   = trade.get("sl")
    try:
        # Re-fetch exchange info for tick/step precision
        info = get("/api/v3/exchangeInfo", {"symbol": symbol})
        step    = 1.0
        tick    = 0.01
        min_qty = 0.0
        for f in info["symbols"][0]["filters"]:
            if f["filterType"] == "LOT_SIZE":
                step    = float(f["stepSize"])
                min_qty = float(f["minQty"])
            elif f["filterType"] == "PRICE_FILTER":
                tick = float(f["tickSize"])
        qty_prec  = len(str(step).rstrip('0').split('.')[-1])
        tick_prec = len(str(tick).rstrip('0').split('.')[-1])
        remaining_qty = round(math.floor(remaining_qty / step) * step, qty_prec)

        if remaining_qty == 0 or remaining_qty < min_qty:
            msg = (f"🚨 *Partial TP1 re-OCO skipped* — `{symbol}` remaining qty "
                   f"{remaining_qty} < min_qty {min_qty}. Manual close required.")
            print(f"  ✗ {msg}")
            send_telegram(msg)
            trade["status"] = "partial_tp_no_oco"
            return

        if TRAILING_DELTA > 0:
            new_oco = signed_post("/api/v3/orderList/oco", {
                "symbol":              symbol,
                "side":                "SELL",
                "quantity":            remaining_qty,
                "aboveType":           "LIMIT_MAKER",
                "abovePrice":          tp2_price,
                "belowType":           "STOP_LOSS",
                "belowStopPrice":      sl_price,
                "belowTrailingDelta":  TRAILING_DELTA,
                "belowTimeInForce":    "GTC",
            })
        else:
            sl_limit = round(round(sl_price * 0.995 / tick) * tick, tick_prec)
            new_oco = signed_post("/api/v3/orderList/oco", {
                "symbol":           symbol,
                "side":             "SELL",
                "quantity":         remaining_qty,
                "aboveType":        "LIMIT_MAKER",
                "abovePrice":       tp2_price,
                "belowType":        "STOP_LOSS_LIMIT",
                "belowStopPrice":   sl_price,
                "belowPrice":       sl_limit,
                "belowTimeInForce": "GTC",
            })
        trade["oco_id"] = new_oco.get("orderListId")
        trade["qty"]    = remaining_qty
        trade["status"] = "partial_tp"
        print(f"  New OCO placed for remaining {remaining_qty} {symbol}: #{trade['oco_id']}")
        send_telegram(
            f"📊 *Partial TP1 hit* — `{symbol}` half closed @ `${tp1_fill:.4f}` "
            f"({tp1_pnl:+.2f}%). Riding remainder to TP2 `${tp2_price:.4f}`."
        )
    except Exception as new_oco_err:
        msg = (f"🚨 *Partial TP1 re-OCO FAILED* — `{symbol}` {remaining_qty} UNPROTECTED. "
               f"Place OCO manually. Error: `{str(new_oco_err)[:200]}`")
        print(f"  ✗ {msg}")
        send_telegram(msg)
        trade["status"] = "partial_tp_no_oco"


# ── Order placement ──────────────────────────────────────────────────────────
def place_buy_order(
    symbol: str,
    capital: float,
    price: float,
    closed_klines: Optional[list[list[Any]]] = None,
) -> tuple[dict[str, Any], Optional[dict[str, Any]], dict[str, Any]]:
    """Place market buy + OCO (TP/SL). Uses ATR-based SL/TP if ATR_SL_MULT > 0 and klines supplied."""
    qty_raw = capital / price
    # Get lot size filter
    info = get("/api/v3/exchangeInfo", {"symbol": symbol})
    step = 1.0
    tick = 0.01
    min_qty = 0.0
    for f in info["symbols"][0]["filters"]:
        if f["filterType"] == "LOT_SIZE":
            step = float(f["stepSize"])
            min_qty = float(f["minQty"])
        elif f["filterType"] == "PRICE_FILTER":
            tick = float(f["tickSize"])
    # Round qty down to stepSize precision
    qty_prec = len(str(step).rstrip('0').split('.')[-1])
    qty = round(math.floor(qty_raw / step) * step, qty_prec)
    # Price precision from tickSize
    tick_prec = len(str(tick).rstrip('0').split('.')[-1])

    # Guard: reject before sending to exchange (avoids position-tracking desync)
    if qty == 0 or qty < min_qty:
        raise ValueError(
            f"Computed qty {qty} is below min_qty {min_qty} for {symbol} "
            f"— capital ${capital:.2f} insufficient at price ${price:.6f}"
        )

    print(f"\n  Placing MARKET BUY: {qty} {symbol} @ ~${price:.4f}")
    order = signed_post("/api/v3/order", {
        "symbol":   symbol,
        "side":     "BUY",
        "type":     "MARKET",
        "quantity": qty,
        "newClientOrderId": f"agent-scanner-buy-{int(time.time())}",
    })
    fill_price = float(order.get("fills", [{}])[0].get("price", price)) if order.get("fills") else price
    actual_qty = float(order.get("executedQty", qty))

    # Compute SL/TP percentages — ATR-based if enabled, else fixed constants
    if ATR_SL_MULT > 0 and closed_klines:
        atr = calc_atr(closed_klines)
        if atr is not None:
            atr_pct = atr / fill_price
            sl_pct = min(max(atr_pct * ATR_SL_MULT, ATR_SL_MIN), ATR_SL_MAX)
            # Note: when ATR < ATR_SL_MIN/ATR_SL_MULT, sl_pct is floored to ATR_SL_MIN
            # but tp_pct still scales from the floored sl_pct → apparent R/R improves.
            tp_pct = sl_pct * (ATR_TP_MULT / ATR_SL_MULT)
            print(f"  ATR: {atr_pct*100:.2f}%  → SL: {sl_pct*100:.2f}%  TP: {tp_pct*100:.2f}%")
        else:
            sl_pct, tp_pct = STOP_LOSS, TAKE_PROFIT
    else:
        sl_pct, tp_pct = STOP_LOSS, TAKE_PROFIT

    # Place OCO: TP (fixed) + SL leg (trailing if TRAILING_DELTA > 0, else fixed stop-loss)
    tp_price = round(round(fill_price * (1 + tp_pct) / tick) * tick, tick_prec)
    sl_price = round(round(fill_price * (1 - sl_pct) / tick) * tick, tick_prec)

    # Build a partial trade record NOW — before OCO — so if OCO fails we can
    # still persist the open position as status="no_oco" for manual review.
    trade_partial = {
        "time":     datetime.now().isoformat(),
        "symbol":   symbol,
        "entry":    fill_price,
        "qty":      actual_qty,
        "capital":  capital,
        "order_id": order.get("orderId"),
        "oco_id":   None,
        "status":   "no_oco",   # overwritten to "open" after successful OCO
    }

    try:
        if TRAILING_DELTA > 0:
            # Trailing stop: activates at sl_price, then trails by TRAILING_DELTA basis points
            print(f"  Filled @ ${fill_price:.4f} | TP: ${tp_price:.4f} | Trailing SL: {TRAILING_DELTA}bps from ${sl_price:.4f}")
            oco = signed_post("/api/v3/orderList/oco", {
                "symbol":              symbol,
                "side":                "SELL",
                "quantity":            actual_qty,
                "aboveType":           "LIMIT_MAKER",
                "abovePrice":          tp_price,
                "belowType":           "STOP_LOSS",
                "belowStopPrice":      sl_price,
                "belowTrailingDelta":  TRAILING_DELTA,
                "belowTimeInForce":    "GTC",
            })
        else:
            sl_limit = round(round(sl_price * 0.995 / tick) * tick, tick_prec)
            print(f"  Filled @ ${fill_price:.4f} | TP: ${tp_price:.4f} | SL: ${sl_price:.4f}")
            oco = signed_post("/api/v3/orderList/oco", {
                "symbol":           symbol,
                "side":             "SELL",
                "quantity":         actual_qty,
                "aboveType":        "LIMIT_MAKER",
                "abovePrice":       tp_price,
                "belowType":        "STOP_LOSS_LIMIT",
                "belowStopPrice":   sl_price,
                "belowPrice":       sl_limit,
                "belowTimeInForce": "GTC",
            })
        print(f"  OCO placed — order list ID: {oco.get('orderListId')}")
    except Exception as oco_err:
        # Market buy already filled — record as no_oco and re-raise so callers
        # can alert/persist the orphaned position.
        print(f"  ✗ OCO failed after fill: {oco_err}")
        send_telegram(
            f"🚨 *OCO FAILED — unprotected position*\n"
            f"`{symbol}` {actual_qty} bought @ `${fill_price:.4f}`\n"
            f"Place TP/SL manually. OCO error: `{str(oco_err)[:200]}`"
        )
        return order, None, trade_partial

    trade = {
        "time":        datetime.now().isoformat(),
        "symbol":      symbol,
        "entry":       fill_price,
        "tp":          tp_price,
        "sl":          sl_price,
        "qty":         actual_qty,
        "capital":     capital,
        "order_id":    order.get("orderId"),
        "oco_id":      oco.get("orderListId"),
        "status":      "open",
        "sl_pct":      sl_pct,
        "tp_pct":      tp_pct,
    }

    # ── Partial TP1 standalone LIMIT_MAKER (T2-4) ────────────────────────────
    # Place a separate LIMIT_MAKER for PARTIAL_TP1_QTY_PCT of the position at TP1
    # (1.0× ATR from entry). If this fills, _handle_partial_tp1() cancels the
    # original OCO and re-OCOs the remaining qty at the original TP2/SL levels.
    if PARTIAL_TP_ENABLED:
        try:
            # TP1 = entry × (1 + ATR% × PARTIAL_TP1_ATR_MULT)
            # ATR% is derived from sl_pct and ATR_SL_MULT so the ratio is consistent.
            atr_pct = sl_pct / ATR_SL_MULT if ATR_SL_MULT > 0 else sl_pct
            tp1_pct = atr_pct * PARTIAL_TP1_ATR_MULT
            tp1_price_raw = fill_price * (1 + tp1_pct)
            tp1_price = round(round(tp1_price_raw / tick) * tick, tick_prec)
            tp1_qty_raw = actual_qty * PARTIAL_TP1_QTY_PCT
            tp1_qty = round(math.floor(tp1_qty_raw / step) * step, qty_prec)
            if tp1_qty >= min_qty and tp1_price < tp_price:
                tp1_order = signed_post("/api/v3/order", {
                    "symbol":           symbol,
                    "side":             "SELL",
                    "type":             "LIMIT_MAKER",
                    "quantity":         tp1_qty,
                    "price":            tp1_price,
                    "newClientOrderId": f"partial-tp1-{int(time.time() * 1000)}",
                })
                trade["tp1_order_id"] = tp1_order.get("orderId")
                trade["tp1_price"]    = tp1_price
                trade["tp1_qty"]      = tp1_qty
                print(f"  Partial TP1 placed: {tp1_qty} @ ${tp1_price:.4f} (orderId:{trade['tp1_order_id']})")
            else:
                print(f"  Partial TP1 skipped: qty {tp1_qty} < min_qty {min_qty} or tp1≥tp2")
        except Exception as tp1_err:
            # TP1 failure is non-fatal — full position still protected by OCO
            print(f"  ⚠ Partial TP1 placement failed (non-fatal): {tp1_err}")

    return order, oco, trade

# ── Dashboard ────────────────────────────────────────────────────────────────
def generate_dashboard(state: dict[str, Any]) -> None:
    """Generate a self-contained HTML dashboard from scan state and write to ~/.agent/diagrams/trading-dashboard.html"""
    DASHBOARD_FILE = os.path.join(os.path.expanduser("~/.agent/diagrams"), "trading-dashboard.html")

    results   = state.get("results", [])
    trades    = state.get("trades", [])
    history   = state.get("history", [])
    last_scan = state.get("last_scan", "")
    portfolio = state.get("portfolio") or {}

    # ── Performance stats ─────────────────────────────────────────────────────
    closed = [t for t in trades if t.get("status") in ("tp_hit", "sl_hit")]
    wins   = [t for t in closed if t.get("status") == "tp_hit"]
    losses = [t for t in closed if t.get("status") == "sl_hit"]
    win_rate = (len(wins) / len(closed) * 100) if closed else 0
    avg_win  = (sum(t.get("pnl_pct", 0) for t in wins)   / len(wins))   if wins   else 0
    avg_loss = (sum(t.get("pnl_pct", 0) for t in losses) / len(losses)) if losses else 0

    # ── Recent signal events (last 10 history entries) ────────────────────────
    recent_signals = history[-10:][::-1]

    # ── Open positions ────────────────────────────────────────────────────────
    open_trades = [t for t in trades if t.get("status") == "open"]

    state_json = json.dumps(state, indent=2)

    # ── RSI badge color helper (JS-side, but pre-compute for static fallback) ─
    def rsi_color_cls(rsi):
        if rsi is None:
            return "badge-none"
        if rsi < 25:
            return "badge-red"
        if rsi < 35:
            return "badge-orange"
        if rsi < 45:
            return "badge-yellow"
        return "badge-green"

    def sig_color_cls(sig):
        return {"EXTREME": "badge-red", "STRONG": "badge-orange",
                "MODERATE": "badge-yellow", "NONE": "badge-none"}.get(sig, "badge-none")

    # ── Pair cards ────────────────────────────────────────────────────────────
    pair_cards_html = ""
    for r in results:
        rsi_cls = rsi_color_cls(r.get("rsi"))
        sig_cls = sig_color_cls(r.get("signal_strength", "NONE"))
        chg = r.get("change24h", 0) or 0
        chg_color = "var(--green)" if chg >= 0 else "var(--red)"
        sma_txt = "above" if r.get("above_sma") else "below"
        vol_txt = "surge" if r.get("vol_surge") else "low"
        mom_txt = "up" if r.get("momentum") else "down"
        price_val = r.get("price", 0) or 0
        price_fmt = f"${price_val:,.6f}" if price_val < 1 else f"${price_val:,.4f}"
        pair_cards_html += f"""
        <div class="pair-card">
          <div class="pair-symbol">{r.get("symbol","")}</div>
          <div class="pair-price">{price_fmt}</div>
          <div class="pair-row">
            <span class="badge {rsi_cls}">RSI {r.get("rsi","—")}</span>
            <span class="badge {sig_cls}">{r.get("signal_strength","NONE")}</span>
          </div>
          <div class="pair-change" style="color:{chg_color}">{chg:+.2f}% 24h</div>
          <div class="pair-indicators">
            SMA:<b>{sma_txt}</b> &nbsp;|&nbsp; Vol:<b>{vol_txt}</b> &nbsp;|&nbsp; Mom:<b>{mom_txt}</b>
          </div>
        </div>"""

    # ── Open positions table ──────────────────────────────────────────────────
    if open_trades:
        pos_rows = ""
        for t in open_trades:
            entry = t.get("entry") or 0
            cur   = t.get("current_price") or t.get("entry") or 0
            tp    = t.get("tp") or 0
            sl    = t.get("sl") or 0
            pnl_pct = t.get("pnl_pct") or (((cur - entry) / entry * 100) if entry else 0)
            pnl_usd = t.get("pnl") or 0
            pnl_color = "var(--green)" if pnl_pct >= 0 else "var(--red)"
            pos_rows += f"""<tr>
              <td>{t.get("symbol","")}</td>
              <td>${entry:.4f}</td>
              <td>${cur:.4f}</td>
              <td>${tp:.4f}</td>
              <td>${sl:.4f}</td>
              <td style="color:{pnl_color}">{pnl_pct:+.2f}%</td>
              <td style="color:{pnl_color}">{pnl_usd:+.2f}$</td>
            </tr>"""
        positions_html = f"""
        <div class="section">
          <h2>Open Positions</h2>
          <table><thead><tr>
            <th>Symbol</th><th>Entry</th><th>Current</th><th>TP</th><th>SL</th><th>P&amp;L%</th><th>P&amp;L$</th>
          </tr></thead><tbody>{pos_rows}</tbody></table>
        </div>"""
    else:
        positions_html = """
        <div class="section">
          <h2>Open Positions</h2>
          <p class="muted">No open positions.</p>
        </div>"""

    # ── Trade history (last 20 closed) ────────────────────────────────────────
    history_rows = ""
    for t in reversed(trades[-20:]):
        status = t.get("status", "open")
        if status == "tp_hit":
            outcome = '<span class="badge badge-green">TP ✓</span>'
        elif status == "sl_hit":
            outcome = '<span class="badge badge-red">SL ✗</span>'
        else:
            outcome = '<span class="badge badge-none">open</span>'
        ts = t.get("time", t.get("entry_time", ""))[:16] if (t.get("time") or t.get("entry_time")) else "—"
        history_rows += f"""<tr>
          <td>{t.get("symbol","")}</td>
          <td>${(t.get("entry") or 0):.4f}</td>
          <td>{outcome}</td>
          <td>{t.get("signal_strength","—")}</td>
          <td>{ts}</td>
        </tr>"""

    if history_rows:
        history_html = f"""
        <div class="section">
          <h2>Trade History <span class="muted">(last 20)</span></h2>
          <table><thead><tr>
            <th>Symbol</th><th>Entry</th><th>Outcome</th><th>Signal</th><th>Date</th>
          </tr></thead><tbody>{history_rows}</tbody></table>
        </div>"""
    else:
        history_html = """
        <div class="section">
          <h2>Trade History</h2>
          <p class="muted">No closed trades yet.</p>
        </div>"""

    # ── Recent signals ────────────────────────────────────────────────────────
    sig_rows = ""
    for entry in recent_signals:
        ts = (entry.get("time") or "")[:16]
        for s in (entry.get("signals") or []):
            sig_rows += f"""<tr>
              <td>{ts}</td>
              <td>{s.get("symbol","")}</td>
              <td>${(s.get("price") or 0):.4f}</td>
              <td>RSI {s.get("rsi","—")}</td>
              <td><span class="badge {sig_color_cls(s.get("signal_strength","NONE"))}">{s.get("signal_strength","NONE")}</span></td>
            </tr>"""

    if sig_rows:
        signals_html = f"""
        <div class="section">
          <h2>Recent Signals <span class="muted">(last 10 scans)</span></h2>
          <table><thead><tr>
            <th>Time</th><th>Symbol</th><th>Price</th><th>RSI</th><th>Tier</th>
          </tr></thead><tbody>{sig_rows}</tbody></table>
        </div>"""
    else:
        signals_html = """
        <div class="section">
          <h2>Recent Signals</h2>
          <p class="muted">No signals recorded yet.</p>
        </div>"""

    # ── Portfolio HTML block ──────────────────────────────────────────────────
    ASSET_COLORS = {
        "USDC": "var(--teal)", "BTC": "var(--orange)", "ETH": "var(--blue)",
        "BNB":  "var(--yellow)", "ADA": "var(--green)", "SOL": "var(--lavender, #b4befe)",
        "XRP":  "var(--blue)", "DOGE": "var(--yellow)", "LUNA": "var(--red)",
    }
    if portfolio and portfolio.get("assets"):
        total_val = portfolio["total_usdc"]
        fetched   = (portfolio.get("fetched_at") or "")[:16]
        asset_rows = ""
        for a in portfolio["assets"]:
            color  = ASSET_COLORS.get(a["asset"], "var(--text)")
            pct    = a["pct"]
            bar_w  = max(2, round(pct))  # min 2px so tiny positions are visible
            price_fmt = f"${a['price_usdc']:,.4f}" if a["price_usdc"] < 100 else f"${a['price_usdc']:,.2f}"
            qty_fmt   = f"{a['qty']:.6f}".rstrip("0").rstrip(".")
            asset_rows += f"""
      <div class="port-row">
        <div class="port-asset" style="color:{color}">{a['asset']}</div>
        <div class="port-qty">{qty_fmt}</div>
        <div class="port-price">{price_fmt}</div>
        <div class="port-value">${a['value_usdc']:,.2f}</div>
        <div class="port-bar-wrap">
          <div class="port-bar" style="width:{bar_w}%;background:{color}"></div>
          <span class="port-pct">{pct:.1f}%</span>
        </div>
      </div>"""
        portfolio_html = f"""
<div class="section">
  <div class="section-head-row">
    <h2>Portfolio</h2>
    <span class="port-total">${total_val:,.2f} <span class="muted">USDC</span></span>
    <span class="muted port-ts">updated {fetched}</span>
  </div>
  <div class="port-header">
    <span>Asset</span><span>Balance</span><span>Price</span><span>Value</span><span>Allocation</span>
  </div>
{asset_rows}
</div>"""
    else:
        portfolio_html = """
<div class="section">
  <h2>Portfolio</h2>
  <p class="muted">No portfolio data — run scanner to fetch.</p>
</div>"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Trading Dashboard</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:wght@400;600;700&family=Fragment+Mono&display=swap">
<script>const STATE = {state_json};</script>
<style>
  :root {{
    --bg:      #1e1e2e;
    --surface: #313244;
    --green:   #a6e3a1;
    --red:     #f38ba8;
    --orange:  #fab387;
    --yellow:  #f9e2af;
    --blue:    #89b4fa;
    --teal:    #94e2d5;
    --text:    #cdd6f4;
    --muted:   #6c7086;
    --border:  #45475a;
  }}
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    background: var(--bg);
    color: var(--text);
    font-family: 'Bricolage Grotesque', sans-serif;
    font-size: 14px;
    padding: 24px;
    min-height: 100vh;
  }}
  header {{
    display: flex;
    align-items: baseline;
    gap: 16px;
    margin-bottom: 28px;
    border-bottom: 1px solid var(--border);
    padding-bottom: 16px;
    flex-wrap: wrap;
  }}
  header h1 {{
    font-size: 22px;
    font-weight: 700;
    letter-spacing: 0.12em;
    color: var(--blue);
  }}
  .header-meta {{
    font-family: 'Fragment Mono', monospace;
    font-size: 12px;
    color: var(--muted);
  }}
  .header-refresh {{
    font-size: 11px;
    color: var(--teal);
    margin-left: auto;
  }}
  .section {{
    margin-bottom: 32px;
  }}
  .section h2 {{
    font-size: 13px;
    font-weight: 600;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--muted);
    margin-bottom: 14px;
  }}
  .grid-pairs {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
    gap: 14px;
  }}
  .pair-card {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 16px;
  }}
  .pair-symbol {{
    font-family: 'Fragment Mono', monospace;
    font-size: 13px;
    font-weight: 600;
    color: var(--blue);
    margin-bottom: 6px;
  }}
  .pair-price {{
    font-family: 'Fragment Mono', monospace;
    font-size: 16px;
    font-weight: 700;
    margin-bottom: 10px;
  }}
  .pair-row {{
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
    margin-bottom: 8px;
  }}
  .pair-change {{
    font-family: 'Fragment Mono', monospace;
    font-size: 12px;
    margin-bottom: 8px;
  }}
  .pair-indicators {{
    font-size: 11px;
    color: var(--muted);
  }}
  .pair-indicators b {{
    color: var(--text);
  }}
  .badge {{
    display: inline-block;
    font-family: 'Fragment Mono', monospace;
    font-size: 11px;
    padding: 2px 8px;
    border-radius: 4px;
    font-weight: 600;
  }}
  .badge-red    {{ background: color-mix(in srgb, var(--red)    20%, transparent); color: var(--red);    border: 1px solid color-mix(in srgb, var(--red)    40%, transparent); }}
  .badge-orange {{ background: color-mix(in srgb, var(--orange) 20%, transparent); color: var(--orange); border: 1px solid color-mix(in srgb, var(--orange) 40%, transparent); }}
  .badge-yellow {{ background: color-mix(in srgb, var(--yellow) 20%, transparent); color: var(--yellow); border: 1px solid color-mix(in srgb, var(--yellow) 40%, transparent); }}
  .badge-green  {{ background: color-mix(in srgb, var(--green)  20%, transparent); color: var(--green);  border: 1px solid color-mix(in srgb, var(--green)  40%, transparent); }}
  .badge-none   {{ background: color-mix(in srgb, var(--muted)  20%, transparent); color: var(--muted);  border: 1px solid color-mix(in srgb, var(--muted)  40%, transparent); }}
  table {{
    width: 100%;
    border-collapse: collapse;
    font-family: 'Fragment Mono', monospace;
    font-size: 12px;
  }}
  thead th {{
    text-align: left;
    padding: 8px 12px;
    color: var(--muted);
    font-weight: 600;
    letter-spacing: 0.06em;
    border-bottom: 1px solid var(--border);
  }}
  tbody tr {{
    border-bottom: 1px solid color-mix(in srgb, var(--border) 50%, transparent);
  }}
  tbody tr:hover {{
    background: color-mix(in srgb, var(--surface) 60%, transparent);
  }}
  tbody td {{
    padding: 8px 12px;
    vertical-align: middle;
  }}
  .stats-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
    gap: 12px;
  }}
  .stat-card {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 14px 18px;
  }}
  .stat-label {{
    font-size: 11px;
    color: var(--muted);
    letter-spacing: 0.08em;
    text-transform: uppercase;
    margin-bottom: 6px;
  }}
  .stat-value {{
    font-family: 'Fragment Mono', monospace;
    font-size: 22px;
    font-weight: 700;
  }}
  .muted {{ color: var(--muted); }}
  footer {{
    margin-top: 40px;
    padding-top: 16px;
    border-top: 1px solid var(--border);
    font-family: 'Fragment Mono', monospace;
    font-size: 11px;
    color: var(--muted);
    display: flex;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 8px;
  }}
  /* ── Portfolio ── */
  .section-head-row {{
    display: flex;
    align-items: baseline;
    gap: 16px;
    margin-bottom: 14px;
    flex-wrap: wrap;
  }}
  .section-head-row h2 {{ margin-bottom: 0; }}
  .port-total {{
    font-family: 'Fragment Mono', monospace;
    font-size: 20px;
    font-weight: 700;
    color: var(--teal);
  }}
  .port-ts {{ font-family: 'Fragment Mono', monospace; font-size: 11px; margin-left: auto; }}
  .port-header {{
    display: grid;
    grid-template-columns: 80px 130px 110px 110px 1fr;
    font-size: 11px;
    color: var(--muted);
    letter-spacing: 0.06em;
    text-transform: uppercase;
    padding: 0 4px 8px;
    border-bottom: 1px solid var(--border);
    margin-bottom: 4px;
  }}
  .port-row {{
    display: grid;
    grid-template-columns: 80px 130px 110px 110px 1fr;
    align-items: center;
    padding: 10px 4px;
    border-bottom: 1px solid color-mix(in srgb, var(--border) 40%, transparent);
    font-family: 'Fragment Mono', monospace;
    font-size: 12px;
  }}
  .port-row:hover {{ background: color-mix(in srgb, var(--surface) 50%, transparent); }}
  .port-asset {{ font-weight: 700; font-size: 13px; }}
  .port-qty, .port-price, .port-value {{ color: var(--text); }}
  .port-value {{ font-weight: 600; }}
  .port-bar-wrap {{
    display: flex;
    align-items: center;
    gap: 8px;
  }}
  .port-bar {{
    height: 6px;
    border-radius: 3px;
    min-width: 4px;
    transition: width 0.3s ease;
  }}
  .port-pct {{ font-size: 11px; color: var(--muted); white-space: nowrap; }}
</style>
</head>
<body>

<header>
  <h1>TRADING DASHBOARD</h1>
  <div class="header-meta">Last scan: {last_scan[:19] if last_scan else "—"}</div>
  <div class="header-refresh">Auto-refreshes each scanner run</div>
</header>

{portfolio_html}

<div class="section">
  <h2>Market Overview</h2>
  <div class="grid-pairs">{pair_cards_html}</div>
</div>

{positions_html}

{history_html}

<div class="section">
  <h2>Performance Stats</h2>
  <div class="stats-grid">
    <div class="stat-card">
      <div class="stat-label">Win Rate</div>
      <div class="stat-value" style="color:var(--green)">{win_rate:.0f}%</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Total Trades</div>
      <div class="stat-value">{len(closed)}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Wins</div>
      <div class="stat-value" style="color:var(--green)">{len(wins)}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Losses</div>
      <div class="stat-value" style="color:var(--red)">{len(losses)}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Avg Win</div>
      <div class="stat-value" style="color:var(--green)">{avg_win:+.2f}%</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Avg Loss</div>
      <div class="stat-value" style="color:var(--red)">{avg_loss:+.2f}%</div>
    </div>
  </div>
</div>

{signals_html}

<footer>
  <span>{DASHBOARD_FILE}</span>
  <span>Auto-généré par scanner.py à chaque scan</span>
</footer>

</body>
</html>"""

    os.makedirs(os.path.dirname(DASHBOARD_FILE), exist_ok=True)
    with open(DASHBOARD_FILE, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  ✓ Dashboard → {DASHBOARD_FILE}")


# ── Helpers ──────────────────────────────────────────────────────────────────
def _fg_regime(value: int) -> str:
    """Map a Fear & Greed value (0-100) to a named regime bucket."""
    if value < 20:
        return "extreme_fear"
    elif value < 30:
        return "fear"
    elif value < 50:
        return "neutral"
    elif value < 75:
        return "greed"
    else:
        return "extreme_greed"


def _check_fg_regime_change(fg_value: int, fg_class: str, old_regime: str) -> str:
    """Fire a Telegram alert if F&G has crossed into a new regime. Returns new regime."""
    new_regime = _fg_regime(fg_value)
    if new_regime == old_regime:
        return new_regime

    messages: dict[str, str] = {
        "extreme_fear": f"🔴 *F&G: Extreme Fear* (`{fg_value}`)\nMODERATE signals are now *blocked*.",
        "fear":         f"🟡 *F&G: Fear* (`{fg_value}`)\nEntered Fear zone (20–29).",
        "neutral":      f"🟢 *F&G: Neutral* (`{fg_value}`)\nF&G recovering past the Fear zone.",
        "greed":        f"⚡ *F&G: Greed* (`{fg_value}`)\nMarket turning greedy — tighten risk.",
        "extreme_greed": f"🚨 *F&G: Extreme Greed* (`{fg_value}`)\nConsider reducing exposure.",
    }
    msg = messages.get(new_regime, f"F&G regime changed to {new_regime} ({fg_value})")
    send_telegram(msg)
    print(f"  📡 F&G regime change: {old_regime} → {new_regime} ({fg_value} {fg_class})")
    return new_regime


def _escape_md(text: Any) -> str:
    """Escape Telegram Markdown special characters in arbitrary strings (e.g. exceptions)."""
    for ch in ("*", "_", "`", "[", "]"):
        text = str(text).replace(ch, "\\" + ch)
    return text

def _calc_capital(s: dict[str, Any], context: dict[str, Any]) -> float:
    """Central capital-sizing rule — single source of truth.

    EXTREME + quality (above SMA, F&G<40) → CAPITAL/2 (first split leg; second fires at ATR trigger)
    EXTREME crash (falling knife)          → CAPITAL/2 (falling-knife cap)
    STRONG in weak BTC (RSI<35)            → CAPITAL/2
    Everything else                        → full CAPITAL
    """
    if s["signal_strength"] == "EXTREME" and s.get("extreme_quality"):
        return CAPITAL / 2   # first split leg (second leg fires at ATR trigger)
    if s["signal_strength"] == "EXTREME":
        return CAPITAL / 2   # crash/falling-knife path
    if s["signal_strength"] == "STRONG" and context["btc_rsi"] < 35:
        return CAPITAL / 2
    return CAPITAL

def _estimate_sl_tp_pct(s: dict[str, Any]) -> tuple[float, float]:
    """Estimate SL/TP % for pre-order display — mirrors place_buy_order ATR logic."""
    if ATR_SL_MULT > 0 and s.get("closed_klines"):
        atr = calc_atr(s["closed_klines"])
        if atr is not None:
            atr_pct = atr / s["price"]
            sl_pct = min(max(atr_pct * ATR_SL_MULT, ATR_SL_MIN), ATR_SL_MAX)
            tp_pct = sl_pct * (ATR_TP_MULT / ATR_SL_MULT)
            return sl_pct, tp_pct
    return STOP_LOSS, TAKE_PROFIT

# ── Daily digest ─────────────────────────────────────────────────────────────
def _send_daily_digest(state: dict[str, Any]) -> None:
    """Send an 8am morning digest summarising the last 7 days of trading."""
    now       = datetime.now()
    cutoff    = now - timedelta(days=7)
    trades    = state.get("trades") or []
    portfolio = state.get("portfolio")
    fg_cache  = state.get("fg_cache") or {}
    fg_val    = fg_cache.get("value")
    fg_str    = f"\n*Fear & Greed:* `{fg_val}`" if fg_val is not None else ""

    # 7-day closed trades
    window = []
    for t in trades:
        if t.get("status") not in ("tp_hit", "sl_hit"):
            continue
        try:
            ts = datetime.fromisoformat(t.get("exit_time") or t.get("time", ""))
            if ts >= cutoff:
                window.append(t)
        except Exception:
            pass

    wins   = [t for t in window if t.get("status") == "tp_hit"]
    losses = [t for t in window if t.get("status") == "sl_hit"]
    net_usdc = sum(
        (t.get("pnl_pct") or 0) / 100 * (t.get("capital") or CAPITAL)
        for t in window
    )
    win_usdc  = sum((t.get("pnl_pct") or 0) / 100 * (t.get("capital") or CAPITAL) for t in wins)
    loss_usdc = sum((t.get("pnl_pct") or 0) / 100 * (t.get("capital") or CAPITAL) for t in losses)
    deployed  = sum(t.get("capital") or CAPITAL for t in window) or CAPITAL
    net_pct   = net_usdc / deployed * 100 if deployed else 0.0

    # Open positions
    open_trades = [t for t in trades if t.get("status") == "open"]
    open_lines  = []
    for t in open_trades:
        sym   = t.get("symbol", "?")
        entry = t.get("entry", 0)
        try:
            held_h = (now - datetime.fromisoformat(t["time"])).total_seconds() / 3600
            held_s = f"{held_h:.0f}h" if held_h < 24 else f"{held_h/24:.1f}d"
        except Exception:
            held_s = "?"
        open_lines.append(f"  `{sym}`  entry `${entry:.4f}`  held `{held_s}`")

    portfolio_line = (
        f"\n*Portfolio:* `${portfolio['total_usdc']:,.0f} USDC`"
        if portfolio else ""
    )
    trades_section = (
        f"\n*Last 7 days — {len(window)} trade(s):*\n"
        f"  ✅ TP: {len(wins)}  →  `+${win_usdc:,.2f}`\n"
        f"  ❌ SL: {len(losses)}  →  `${loss_usdc:,.2f}`\n"
        f"  Net: `{'+'if net_usdc>=0 else ''}{net_usdc:,.2f} ({net_pct:+.1f}% on deployed capital)`"
    ) if window else "\n*Last 7 days:* No closed trades"
    open_section = (
        f"\n*Open positions ({len(open_trades)}):*\n" + "\n".join(open_lines)
    ) if open_trades else "\n*Open positions:* None"

    msg = (
        f"📊 *Morning Digest — {now.strftime('%a %b %-d')}*"
        f"{portfolio_line}"
        f"{fg_str}"
        f"{trades_section}"
        f"{open_section}"
        f"\n\n_Next scan in ~30 min_"
    )
    send_telegram(msg)
    print("  📊 Morning digest sent")

# ── Main scan ────────────────────────────────────────────────────────────────
def scan() -> None:
    print(f"\n--- {datetime.now().strftime('%a. %d %b %Y %H:%M:%S')} ---")
    print(f"\n{'='*55}")
    print(f"  TRADING SCANNER — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Pairs: {', '.join(PAIRS)} | Capital: ${CAPITAL}/trade")
    print(f"  SL: -{STOP_LOSS*100:.0f}% | TP: +{TAKE_PROFIT*100:.0f}%")
    print(f"{'='*55}")

    # ── Load persisted state (dedup ledger + regime tracking) ────────────────
    _scan_state: dict[str, Any] = {}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                _scan_state = json.load(f)
        except Exception:
            pass
    sent_signals: dict[str, str] = _scan_state.get("sent_signals") or {}

    # ── Check for SL outcomes from previous trades ───────────────────────────
    # Must run after _scan_state is loaded so old_fg_regime is not overwritten.
    # _check_sl_outcomes() calls save_state() internally with fg_regime=None,
    # which preserves the existing regime value already on disk.
    _check_sl_outcomes()

    # ── Split-entry: check pending second legs (T2-1) ─────────────────────────
    # Run before the main scan so second-leg fills are treated as open positions
    # before the correlation cap and per-symbol guards run.
    if SPLIT_ENTRY_ENABLED:
        pending_entries = _load_pending_second_entries()
        for sym, pending in list(pending_entries.items()):
            try:
                entry_age_h = (
                    datetime.now() - datetime.fromisoformat(pending["time"])
                ).total_seconds() / 3600
                if entry_age_h > SPLIT_ENTRY_TTL_H:
                    _clear_pending_second_entry(sym)
                    send_telegram(
                        f"⏱ *Split entry expired* — `{sym}` pending second leg cleared "
                        f"after {SPLIT_ENTRY_TTL_H}h. No second buy placed."
                    )
                    continue
                cp_resp = get("/api/v3/ticker/price", {"symbol": sym})
                cp = float(cp_resp["price"])
                trigger = pending["first_fill"] * (1 - pending["atr_pct"] * SPLIT_ENTRY_ATR_MULT)
                print(f"  Split entry {sym}: current=${cp:.4f} trigger=${trigger:.4f} "
                      f"(age {entry_age_h:.1f}h / {SPLIT_ENTRY_TTL_H}h TTL)")
                if cp <= trigger:
                    klines = get("/api/v3/klines", {"symbol": sym, "interval": INTERVAL,
                                                     "limit": KLINE_LIMIT})
                    trade = _place_split_second_entry(sym, pending, cp, klines[:-1])
                    if trade is None:
                        # Cancel failed — pending entry preserved so next scan retries
                        print(f"  ↩ Split entry cancel failed for {sym} — will retry next scan")
                    elif trade.get("status") == "critical_fail":
                        # Cancel succeeded but second buy failed → unrecoverable, clear pending
                        _clear_pending_second_entry(sym)
                    else:
                        # Success (or no_oco status — position exists, just unprotected)
                        _clear_pending_second_entry(sym)
                        # Persist combined trade immediately so the correlation cap
                        # and open-position guard count it in this scan.
                        try:
                            _se_state: dict[str, Any] = {}
                            if os.path.exists(STATE_FILE):
                                with open(STATE_FILE) as _f:
                                    _se_state = json.load(_f)
                            _se_state.setdefault("trades", []).append(trade)
                            with open(STATE_FILE, "w") as _f:
                                json.dump(_se_state, _f, indent=2)
                        except Exception as _e:
                            print(f"  ⚠ Could not persist split-entry trade: {_e}")
            except Exception as _split_e:
                print(f"  ⚠ Split entry check failed for {sym}: {_split_e}")

    # ── Market context (fetched once per scan) ────────────────────────────────
    fg_value, fg_class, fg_fresh = get_fear_greed()
    # Bootstrap old_regime to current value on first run to suppress spurious alert
    old_fg_regime: str = _scan_state.get("fg_regime") or _fg_regime(fg_value)
    btc_ctx = get_btc_context()
    btc_dom        = get_btc_dominance() if BTC_DOM_ENABLED else None
    btc_dom_rising = _is_btc_dom_rising(btc_dom) if BTC_DOM_ENABLED else False
    context = {"fg_value": fg_value, "fg_class": fg_class,
                "btc_rsi": btc_ctx["rsi"], "btc_above_sma": btc_ctx["above_sma"],
                "btc_price": btc_ctx["price"],
                "btc_dom": btc_dom, "btc_dom_rising": btc_dom_rising}
    dom_str = f"{btc_dom:.1f}%{'↑' if btc_dom_rising else ''}" if btc_dom is not None else "n/a"
    print(f"  F&G: {fg_value} ({fg_class})  |  BTC: ${btc_ctx['price']:,.0f}  RSI:{btc_ctx['rsi']}  "
          f"SMA:{'above' if btc_ctx['above_sma'] else 'below'}  |  BTC.D:{dom_str}")
    # Surgical patch: save btc_dom_prev for the *next* scan's comparison.
    # Re-read state after network calls to avoid clobbering concurrent writes.
    if BTC_DOM_ENABLED and btc_dom is not None:
        try:
            _dom_state: dict[str, Any] = {}
            if os.path.exists(STATE_FILE):
                with open(STATE_FILE) as f:
                    _dom_state = json.load(f)
            _dom_state["btc_dom_prev"] = btc_dom
            with open(STATE_FILE, "w") as f:
                json.dump(_dom_state, f, indent=2)
        except Exception as e:
            print(f"  ⚠ Could not persist btc_dom_prev: {e}")

    # ── F&G regime-change alert (fires once per threshold crossing) ───────────
    # Skip when F&G data is stale (fallback 50/"Neutral") to avoid spurious alerts
    if fg_fresh:
        new_fg_regime = _check_fg_regime_change(fg_value, fg_class, old_fg_regime)
    else:
        new_fg_regime = old_fg_regime   # preserve regime — data unavailable

    # ── Portfolio snapshot ────────────────────────────────────────────────────
    portfolio = get_portfolio()
    if portfolio:
        total = portfolio["total_usdc"]
        asset_str = "  ".join(
            f"{a['asset']}:{a['qty']:.4f}(${a['value_usdc']:.0f})"
            for a in portfolio["assets"]
        )
        print(f"  Portfolio: ${total:,.2f} USDC total  |  {asset_str}")
    print(f"{'─'*55}")

    signals = []
    all_results = []
    cooldowns = _load_cooldowns()
    _open_pos   = get_open_positions()
    open_count  = len(_open_pos)
    _pnl_vals     = [p["pnl"] for p in _open_pos if p.get("pnl") is not None]
    open_pnl_usdc = sum(_pnl_vals) if _pnl_vals else None

    # ── Phase 1: Analyze all pairs, collect raw candidates ───────────────────
    candidates = []
    for symbol in PAIRS:
        try:
            result = analyze(symbol, context)
            all_results.append(result)
            icon = "🟢" if result["buy_signal"] else "⚪"
            print(f"\n  {icon} {symbol:<12} ${result['price']:<12.6f} RSI:{result['rsi']:<6} "
                  f"24h:{result['change24h']:+.2f}%  Signal:{result['signal_strength']}")
            print(f"     SMA20:{'above' if result['above_sma'] else 'below'} | "
                  f"Vol surge:{'yes' if result['vol_surge'] else 'no'} | "
                  f"Momentum:{'up' if result['momentum'] else 'flat/down'}")
            if result["buy_signal"]:
                candidates.append(result)
        except Exception as e:
            print(f"  ✗ {symbol}: Error — {e}")

    # ── Phase 2: Correlation cap (on raw candidates, BEFORE per-symbol guards) ─
    # ETH/ADA/DOGE/BNB/SOL/XRP are 0.75–0.95 BTC-correlated: ≥3 simultaneous
    # signals = amplified BTC exposure, not independent opportunities. Cap at 1.
    if len(candidates) >= 3:
        candidates.sort(key=lambda s: s["rsi"])
        dropped = [s["symbol"] for s in candidates[1:]]
        candidates = candidates[:1]
        print(f"\n  ⚠ Correlation cap — keeping {candidates[0]['symbol']} (lowest RSI), "
              f"dropping: {', '.join(dropped)}")

    # ── Circuit breaker: halt new orders if drawdown ≥ MAX_DRAWDOWN_PCT ─────────
    peak_usdc    = _scan_state.get("peak_portfolio_usdc") or 0.0
    current_usdc = portfolio["total_usdc"] if portfolio else None
    cb_alert_ts: Optional[str] = None  # set if alert fires this scan
    if peak_usdc and current_usdc:
        drawdown_pct = (peak_usdc - current_usdc) / peak_usdc
        if drawdown_pct >= MAX_DRAWDOWN_PCT:
            # Deduplicate: only fire Telegram once every 4 hours
            cb_last = _scan_state.get("cb_alert_sent_at") or ""
            cb_cooldown_expired = (
                not cb_last
                or (datetime.now() - datetime.fromisoformat(cb_last)).total_seconds() >= 4 * 3600
            )
            if cb_cooldown_expired:
                cb_msg = (
                    f"🛑 *Circuit breaker triggered*\n"
                    f"Drawdown: `{drawdown_pct*100:.1f}%` from peak\n"
                    f"Peak: `${peak_usdc:,.0f}` → Now: `${current_usdc:,.0f}`\n"
                    f"New orders halted until portfolio recovers."
                )
                send_telegram(cb_msg)
                cb_alert_ts = datetime.now().isoformat()
            print(f"  🛑 CIRCUIT BREAKER: {drawdown_pct*100:.1f}% drawdown — no orders placed")
            candidates = []

    # ── Phase 3: Per-symbol guards (open position, cooldown, max positions) ──
    for result in candidates:
        symbol = result["symbol"]
        if open_count >= MAX_POSITIONS:
            print(f"     ⏸ {symbol} — skipped (max positions {MAX_POSITIONS})")
        elif symbol in cooldowns:
            print(f"     ⏸ {symbol} — skipped (SL cooldown until {cooldowns[symbol][:16]})")
        elif has_open_position(symbol):
            print(f"     ⏸ {symbol} — skipped (open position exists)")
        else:
            signals.append(result)

    save_state(all_results, [{"symbol": s["symbol"], "price": s["price"], "rsi": s["rsi"],
                               "signal_strength": s["signal_strength"]} for s in signals],
               portfolio=portfolio, fg_regime=new_fg_regime, open_pnl=open_pnl_usdc,
               cb_alert_sent_at=cb_alert_ts)

    # ── Telegram scan summary ─────────────────────────────────────────────────
    if all_results:
        # Build performance line from closed trades in state.json
        perf_line = ""
        try:
            if os.path.exists(STATE_FILE):
                with open(STATE_FILE) as f:
                    _state = json.load(f)
                closed = [t for t in (_state.get("trades") or [])
                          if t.get("status") in ("tp_hit", "sl_hit")]
                if closed:
                    wins  = sum(1 for t in closed if t.get("status") == "tp_hit")
                    total = len(closed)
                    perf_line = f"\n📊 Trades: `{wins}W/{total-wins}L` ({wins/total*100:.0f}% WR)"
        except Exception:
            pass

        icons = {"EXTREME": "🔴", "STRONG": "🟠", "MODERATE": "🟡", "NONE": "⚪"}
        btc_trend = "↑" if context["btc_above_sma"] else "↓"
        lines = [
            f"📊 *Scan {datetime.now().strftime('%H:%M')}*\n"
            f"F&G: `{context['fg_value']}` {context['fg_class']}  |  "
            f"BTC `${context['btc_price']:,.0f}` RSI:`{context['btc_rsi']}` {btc_trend}\n"
        ]
        for r in all_results:
            icon  = icons.get(r["signal_strength"], "⚪")
            pair  = r["symbol"].replace("USDC", "")
            lines.append(
                f"{icon} `{pair:<5}` ${r['price']:<10.4f} RSI:`{r['rsi']:<5}` 24h:`{r['change24h']:+.2f}%`"
                + (f"  *{r['signal_strength']}*" if r["signal_strength"] != "NONE" else "")
            )
        if _open_pos:
            lines.append("\n📈 *Positions*")
            for p in _open_pos:
                pair    = p["symbol"].replace("USDC", "")
                pnl_str = (f"{p['pnl_pct']:+.2f}%  `{'%.2f' % p['pnl']}$`"
                           if p["pnl"] is not None else "n/a")
                entry_s = f"${p['entry']:.4f}" if p["entry"] else "?"
                cur_s   = f"${p['current']:.4f}" if p["current"] else "?"
                tp_s    = f"${p['tp']:.2f}" if p["tp"] else "?"
                sl_s    = f"${p['sl']:.2f}" if p["sl"] else "?"
                lines.append(
                    f"`{pair}` {p['qty'] or '?'} · {entry_s}→{cur_s} {pnl_str}\n"
                    f"  TP:{tp_s}  SL:{sl_s}"
                )
        if perf_line:
            lines.append(perf_line)
        send_telegram("\n".join(lines))

    if signals:
        SIGNAL_DEDUP_H = 2   # suppress repeat alerts for same symbol+tier within 2 hours
        for s in signals:
            capital = _calc_capital(s, context)
            sl_pct, tp_pct = _estimate_sl_tp_pct(s)
            dedup_key = f"{s['symbol']}:{s['signal_strength']}"
            last_sent = sent_signals.get(dedup_key)
            if last_sent:
                age_h = (datetime.now() - datetime.fromisoformat(last_sent)).total_seconds() / 3600
                if age_h < SIGNAL_DEDUP_H:
                    print(f"     ⏭ {s['symbol']} — alert already sent {age_h:.1f}h ago, skipping Telegram")
                    continue
            msg = (
                f"📡 *{s['signal_strength']} BUY SIGNAL*\n"
                f"Pair: `{s['symbol']}`\n"
                f"Entry: `${s['price']:.4f}` | RSI: `{s['rsi']}`\n"
                f"TP: `${s['price'] * (1 + tp_pct):.4f}` (+{tp_pct*100:.1f}%)  "
                f"SL: `${s['price'] * (1 - sl_pct):.4f}` (-{sl_pct*100:.1f}%)\n"
                f"Cost: `${capital} USDC`"
            )
            send_telegram(msg)
            sent_signals[dedup_key] = datetime.now().isoformat()
        _save_sent_signals(sent_signals)

    if signals:
        cron_mode = os.environ.get("SCANNER_CRON", "") == "1"
        symbols_str = ", ".join(s["symbol"] for s in signals)
        notify_mac("Trading Scanner", f"Signal found: {symbols_str} — open terminal to confirm"
                   if cron_mode else f"Signal found: {symbols_str}")
        for s in signals:
            call_webhook({
                "symbol":          s["symbol"],
                "price":           s["price"],
                "rsi":             s["rsi"],
                "signal_strength": s["signal_strength"],
                "tp":              round(s["price"] * (1 + TAKE_PROFIT), 6),
                "sl":              round(s["price"] * (1 - STOP_LOSS), 6),
                "capital":         CAPITAL,
            })

    print(f"\n{'─'*55}")
    if not signals:
        print("  No buy signals found. Check again in 30 minutes.")
    else:
        print(f"  {len(signals)} signal(s) found!")
        for s in signals:
            capital = _calc_capital(s, context)
            sl_pct, tp_pct = _estimate_sl_tp_pct(s)
            print(f"\n  ► {s['symbol']} — {s['signal_strength']} BUY SIGNAL")
            print(f"    Entry: ~${s['price']:.6f}")
            print(f"    TP:    ~${s['price'] * (1 + tp_pct):.6f} (+{tp_pct*100:.1f}%)")
            print(f"    SL:    ~${s['price'] * (1 - sl_pct):.6f} (-{sl_pct*100:.1f}%)")
            print(f"    Cost:  ${capital} USDC{'  (half-size — downtrend dip)' if s['signal_strength'] == 'EXTREME' else ''}")

        cron_mode = os.environ.get("SCANNER_CRON", "") == "1"
        new_trades = []

        def _place_and_arm(s: dict[str, Any]) -> Optional[dict[str, Any]]:
            """Place buy order and arm split-entry pending leg if applicable."""
            capital = _calc_capital(s, context)
            _, _, trade = place_buy_order(s["symbol"], capital, s["price"], s.get("closed_klines"))
            send_telegram(
                f"✅ *Order placed*\n"
                f"`{s['symbol']}` {trade['qty']} units @ `${trade['entry']:.4f}`\n"
                f"TP `${trade['tp']:.4f}` · SL `${trade['sl']:.4f}`\n"
                f"OCO #{trade['oco_id']}"
            )
            # Arm split entry for EXTREME quality signals (T2-1)
            if (SPLIT_ENTRY_ENABLED
                    and s["signal_strength"] == "EXTREME"
                    and s.get("extreme_quality")
                    and trade.get("status") == "open"):
                atr_pct = trade.get("sl_pct", STOP_LOSS) / ATR_SL_MULT if ATR_SL_MULT > 0 else STOP_LOSS
                pending_data = {
                    "first_fill":    trade["entry"],
                    "first_qty":     trade["qty"],
                    "first_oco_id":  trade["oco_id"],
                    "atr_pct":       atr_pct,
                    "sl_pct":        trade.get("sl_pct", STOP_LOSS),
                    "tp_pct":        trade.get("tp_pct", TAKE_PROFIT),
                    "capital_half":  capital,
                    "time":          datetime.now().isoformat(),
                }
                _save_pending_second_entry(s["symbol"], pending_data)
                trigger_price = trade["entry"] * (1 - atr_pct * SPLIT_ENTRY_ATR_MULT)
                send_telegram(
                    f"🎯 *Split entry armed* — `{s['symbol']}`\n"
                    f"Second leg triggers at `${trigger_price:.4f}` "
                    f"({SPLIT_ENTRY_ATR_MULT}× ATR below entry). "
                    f"TTL: {SPLIT_ENTRY_TTL_H}h."
                )
            return trade

        if cron_mode:
            print("  [CRON MODE] Waiting for Telegram confirmation...")
            for s in signals:
                if wait_telegram_confirm(s["symbol"], timeout=120):
                    try:
                        trade = _place_and_arm(s)
                        new_trades.append(trade)
                    except Exception as e:
                        print(f"  ✗ Order failed for {s['symbol']}: {e}")
                        send_telegram(f"❌ Order failed for `{s['symbol']}`: {_escape_md(e)}")
        else:
            confirm = input("\n  Type CONFIRM to place order(s), or SKIP to skip: ").strip()
            if confirm.upper() == "CONFIRM":
                for s in signals:
                    try:
                        trade = _place_and_arm(s)
                        new_trades.append(trade)
                    except Exception as e:
                        print(f"  ✗ Order failed for {s['symbol']}: {e}")
                        send_telegram(f"❌ Order failed for `{s['symbol']}`: {_escape_md(e)}")
            else:
                print("  Skipped. Run again or wait for next scan.")
        if new_trades:
            save_state(all_results, [{"symbol": s["symbol"], "price": s["price"],
                                      "rsi": s["rsi"], "signal_strength": s["signal_strength"]}
                                     for s in signals], new_trades)
                                     # fg_regime already persisted by the earlier save_state call
    # ── Generate dashboard ────────────────────────────────────────────────────
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE) as f:
                _dash_state = json.load(f)
            generate_dashboard(_dash_state)
    except Exception as e:
        print(f"  ⚠ Dashboard generation failed: {e}")

    # ── Daily digest (8am, once per calendar day) ─────────────────────────────
    try:
        now = datetime.now()
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE) as _df:
                _digest_state = json.load(_df)
            last_digest = _digest_state.get("last_digest_date", "")
            if now.hour == DIGEST_HOUR and str(now.date()) != last_digest:
                _send_daily_digest(_digest_state)
                # Surgical patch: re-read freshest state after send_telegram() network call
                # to avoid overwriting concurrent cooldown writes with a stale snapshot.
                with open(STATE_FILE) as _df:
                    _patch = json.load(_df)
                _patch["last_digest_date"] = str(now.date())
                with open(STATE_FILE, "w") as _df:
                    json.dump(_patch, _df, indent=2)
    except Exception as e:
        print(f"  ⚠ Daily digest failed: {e}")

    print(f"\n{'='*55}\n")

if __name__ == "__main__":
    scan()
