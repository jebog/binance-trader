from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any, Optional

from config import (
    BREAKEVEN_ENABLED,
    BTC_DOM_ENABLED,
    INTERVAL,
    KLINE_LIMIT,
    PAIR_SCORE_ENABLED,
    PROGRESSIVE_TRAILING_ENABLED,
    SPLIT_ENTRY_ATR_MULT,
    SPLIT_ENTRY_ENABLED,
    SPLIT_ENTRY_TTL_H,
)
from trading.analytics import _pair_score
from trading.db import (
    db_connect,
    db_init,
    get_all_trades,
    get_kv,
    get_open_trades,
    insert_trade,
    set_kv,
)
from trading.http_client import get
from trading.market_data import (
    _is_btc_dom_rising,
    get_btc_context,
    get_btc_dominance,
    get_fear_greed,
)
from trading.notify import send_telegram
from trading.orders import (
    _clear_pending_second_entry,
    _load_pending_second_entries,
    _place_split_second_entry,
)
from trading.positions import _check_breakeven, _check_progressive_trailing

# ── Scan lock ────────────────────────────────────────────────────────────────
# Prevents cron + TUI from placing duplicate orders for the same signal.
# Lock TTL is 5 minutes (covers Telegram confirm wait in cron mode).
SCAN_LOCK_TTL_S = 300


def acquire_scan_lock(conn: Optional[sqlite3.Connection] = None,
                      caller: str = "unknown") -> bool:
    """Try to acquire the scan lock. Returns True if acquired, False if held by another.

    The lock is a kv entry 'scan_lock' with value 'caller|iso_timestamp'.
    Stale locks (older than SCAN_LOCK_TTL_S) are automatically reclaimed.
    """
    _own = conn is None
    if _own:
        conn = db_connect()
        db_init(conn)
    try:
        # Atomic read-then-write: read existing lock, check TTL, write new lock.
        # SQLite serializes writers in WAL mode — concurrent callers block at the
        # INSERT/UPDATE in set_kv, so the window between get_kv and set_kv is safe
        # because only one writer can execute at a time.
        existing = get_kv(conn, "scan_lock") or ""
        if existing:
            parts = existing.split("|", 1)
            if len(parts) == 2:
                try:
                    lock_age = (datetime.now() - datetime.fromisoformat(parts[1])).total_seconds()
                    if lock_age < SCAN_LOCK_TTL_S:
                        return False  # lock held by another caller
                except (ValueError, TypeError):
                    pass  # corrupt timestamp — reclaim
        set_kv(conn, "scan_lock", f"{caller}|{datetime.now().isoformat()}")
        return True
    finally:
        if _own:
            conn.close()


def release_scan_lock(conn: Optional[sqlite3.Connection] = None) -> None:
    """Release the scan lock."""
    _own = conn is None
    if _own:
        conn = db_connect()
        db_init(conn)
    try:
        set_kv(conn, "scan_lock", "")
    finally:
        if _own:
            conn.close()


def apply_correlation_cap(
    candidates: list[dict[str, Any]],
    conn: Optional[sqlite3.Connection] = None,
) -> tuple[list[dict[str, Any]], list[str], str]:
    """Apply correlation cap: when >=3 candidates fire simultaneously, keep top 1."""
    if len(candidates) < 3:
        return candidates, [], ""
    if PAIR_SCORE_ENABLED:
        _score_trades: list[dict[str, Any]] = []
        try:
            _c = conn if conn is not None else db_connect()
            _score_trades = get_all_trades(_c)
            if conn is None:
                _c.close()
        except Exception:
            pass
        _scores = {s["symbol"]: _pair_score(s["symbol"], _score_trades) for s in candidates}
        candidates.sort(key=lambda s: _scores[s["symbol"]], reverse=True)
        reason = f"best score ({_scores[candidates[0]['symbol']]:.2f})"
    else:
        candidates.sort(key=lambda s: s["rsi"])
        reason = "lowest RSI"
    dropped = [s["symbol"] for s in candidates[1:]]
    return candidates[:1], dropped, reason


def build_market_context() -> dict[str, Any]:
    """Fetch F&G, BTC context, and BTC dominance -- returns full context dict."""
    fg_value, fg_class, fg_fresh = get_fear_greed()
    btc_ctx = get_btc_context()
    btc_dom = get_btc_dominance() if BTC_DOM_ENABLED else None
    btc_dom_rising = _is_btc_dom_rising(btc_dom) if BTC_DOM_ENABLED else False
    return {
        "fg_value":      fg_value,
        "fg_class":      fg_class,
        "fg_fresh":      fg_fresh,
        "btc_rsi":       btc_ctx["rsi"],
        "btc_above_sma": btc_ctx["above_sma"],
        "btc_price":     btc_ctx["price"],
        "btc_dom":       btc_dom,
        "btc_dom_rising": btc_dom_rising,
    }


def run_position_management(conn: Optional[sqlite3.Connection] = None) -> None:
    """Run break-even (T3-1) and progressive trailing (T4-4) checks on open trades.

    Fetches open trades once and reuses the list for both phases.
    """
    if not BREAKEVEN_ENABLED and not PROGRESSIVE_TRAILING_ENABLED:
        return
    try:
        _c = conn if conn is not None else db_connect()
        _trades = get_open_trades(_c)
        if conn is None:
            _c.close()
    except Exception:
        _trades = []

    if BREAKEVEN_ENABLED:
        for _be_trade in _trades:
            try:
                _be_sym = _be_trade["symbol"]
                _be_cp = float(get("/api/v3/ticker/price", {"symbol": _be_sym})["price"])
                _check_breakeven(_be_trade, _be_cp, _be_sym, conn)
            except Exception as _be_e:
                print(f"  \u26a0 Break-even check failed for {_be_trade.get('symbol', '?')}: {_be_e}")

    if PROGRESSIVE_TRAILING_ENABLED:
        for _pt_trade in _trades:
            if not _pt_trade.get("breakeven_moved"):
                continue
            try:
                _pt_sym = _pt_trade["symbol"]
                _pt_cp = float(get("/api/v3/ticker/price", {"symbol": _pt_sym})["price"])
                _check_progressive_trailing(_pt_trade, _pt_cp, _pt_sym, conn)
            except Exception as _pt_e:
                print(f"  \u26a0 Progressive trailing check failed for {_pt_trade.get('symbol', '?')}: {_pt_e}")


def run_split_entry_checks(conn: Optional[sqlite3.Connection] = None) -> None:
    """Check pending split-entry second legs (T2-1): fire or expire."""
    if not SPLIT_ENTRY_ENABLED:
        return
    pending_entries = _load_pending_second_entries(conn)
    for sym, pending in list(pending_entries.items()):
        try:
            entry_age_h = (
                datetime.now() - datetime.fromisoformat(pending["time"])
            ).total_seconds() / 3600
            if entry_age_h > SPLIT_ENTRY_TTL_H:
                _clear_pending_second_entry(sym, conn)
                send_telegram(
                    f"\u23f1 *Split entry expired* \u2014 `{sym}` pending second leg cleared "
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
                    print(f"  \u21a9 Split entry cancel failed for {sym} \u2014 will retry next scan")
                elif trade.get("status") == "critical_fail":
                    _clear_pending_second_entry(sym, conn)
                else:
                    _clear_pending_second_entry(sym, conn)
                    try:
                        _c = conn if conn is not None else db_connect()
                        insert_trade(_c, trade)
                        if conn is None:
                            _c.close()
                    except Exception as _e:
                        print(f"  \u26a0 Could not persist split-entry trade: {_e}")
        except Exception as _split_e:
            print(f"  \u26a0 Split entry check failed for {sym}: {_split_e}")
