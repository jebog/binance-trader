from __future__ import annotations

import json
import urllib.request
from datetime import datetime, timedelta
from typing import Any, Optional

from config import BTC_DOM_CACHE_H, BTC_DOM_ENABLED, BTC_DOM_RISE_THRESHOLD
from trading.db import (
    db_connect,
    db_init,
    get_btc_dom_cache,
    get_fg_cache,
    get_kv,
    get_open_trades,
    set_btc_dom_cache,
    set_fg_cache,
)
from trading.http_client import get, signed_get
from trading.indicators import calc_rsi, calc_sma
from trading.notify import send_telegram

# ── BTC dominance helpers (T2-3) ─────────────────────────────────────────────
COINGECKO_GLOBAL = "https://api.coingecko.com/api/v3/global"


def get_fear_greed() -> tuple[int, str, bool]:
    """Fetch Crypto Fear & Greed index -- with SQLite cache (valid 25h)."""
    def _read_cache():
        try:
            _rc_conn = db_connect()
            fg = get_fg_cache(_rc_conn)
            _rc_conn.close()
            if fg and (datetime.now() - datetime.fromisoformat(fg["ts"])) < timedelta(hours=25):
                return int(fg["value"]), fg["classification"]
        except Exception:
            pass
        return None

    def _write_cache(value, classification):
        try:
            _fg_conn = db_connect()
            set_fg_cache(_fg_conn, value, classification)
            _fg_conn.close()
        except Exception:
            pass

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
        print(f"  \u26a0 Fear & Greed fetch failed: {e}")

    cached = _read_cache()
    if cached:
        print(f"  \u21a9 Using cached F&G: {cached[0]} ({cached[1]})")
        return cached[0], cached[1], True

    print("  \u26a0 F&G cache expired or missing \u2014 using neutral 50, filters may be inactive")
    send_telegram("\u26a0\ufe0f F&G cache expired \u2014 sentiment filter inactive, using neutral 50")
    return 50, "Neutral", False


def get_btc_context() -> dict[str, Any]:
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
        print(f"  \u26a0 BTC context fetch failed: {e}")
        return {"rsi": 50.0, "above_sma": True, "price": 0}


def get_btc_dominance() -> Optional[float]:
    """Return current BTC dominance % (0-100), or None on any failure (fail-open)."""
    if not BTC_DOM_ENABLED:
        return None
    try:
        _bdc_conn = db_connect()
        db_init(_bdc_conn)
        cache = get_btc_dom_cache(_bdc_conn)
        if cache:
            age_h = (datetime.now() - datetime.fromisoformat(cache["ts"])).total_seconds() / 3600
            if age_h < BTC_DOM_CACHE_H:
                _bdc_conn.close()
                return float(cache["value"])
        req  = urllib.request.Request(COINGECKO_GLOBAL, headers={"Accept": "application/json"})
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read().decode())
        dom  = float(data["data"]["market_cap_percentage"]["btc"])
        set_btc_dom_cache(_bdc_conn, dom)
        _bdc_conn.close()
        return dom
    except Exception as e:
        print(f"  \u26a0 BTC dominance fetch failed: {e}")
        return None


def _is_btc_dom_rising(current: Optional[float]) -> bool:
    """Return True when BTC.D has risen > BTC_DOM_RISE_THRESHOLD since last scan."""
    if current is None:
        return False
    try:
        _br_conn = db_connect()
        prev = get_kv(_br_conn, "btc_dom_prev")
        _br_conn.close()
        if prev is None:
            return False
        return float(current) > float(prev) * (1 + BTC_DOM_RISE_THRESHOLD)
    except Exception:
        return False


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
        print(f"  \u26a0 Position check failed for {symbol}: {e}")
    return False


# ── Portfolio ────────────────────────────────────────────────────────────────
def get_open_positions() -> list[dict[str, Any]]:
    """Return open positions with live P&L, sourced from OCO list + state.db trades."""
    try:
        ocos = signed_get("/api/v3/openOrderList", {})
    except Exception:
        return []
    if not ocos:
        return []

    active_symbols = {oco["symbol"] for oco in ocos}

    trades_by_symbol = {}
    try:
        _op_conn = db_connect()
        db_init(_op_conn)
        for trade in reversed(get_open_trades(_op_conn)):
            sym = trade.get("symbol")
            if sym in active_symbols and sym not in trades_by_symbol:
                trades_by_symbol[sym] = trade
        _op_conn.close()
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
            "time":    trade.get("time"),
        })
    return positions


def get_portfolio() -> Optional[dict[str, Any]]:
    """Fetch account balances + live USDC prices -> portfolio snapshot."""
    STABLES = {"USDC", "BUSD", "USDT", "DAI", "TUSD"}
    try:
        account = signed_get("/api/v3/account", {})
        raw = [b for b in account.get("balances", [])
               if float(b["free"]) + float(b["locked"]) > 0]
    except Exception as e:
        print(f"  \u26a0 Portfolio fetch failed: {e}")
        return None

    assets = []
    for b in raw:
        asset = b["asset"]
        qty   = float(b["free"]) + float(b["locked"])
        if asset in STABLES:
            price = 1.0
        else:
            price = None
            for quote in ("USDC", "USDT"):
                try:
                    price = float(get("/api/v3/ticker/price",
                                      {"symbol": asset + quote})["price"])
                    break
                except Exception:
                    continue
        if price is None:
            continue
        value = qty * price
        if value < 0.10:
            continue
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
