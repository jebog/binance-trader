"""
Dollar-Cost Averaging (DCA) module for long-term ETH accumulation.

Runs independently of the scanner. Places weekly market buys from the USDC
balance, records them as trades with signal_strength="DCA", and (optionally)
triggers auto-staking via trading.staking.

Capital isolation:
  The `dca_reserved_usdc` kv sentinel reserves a buffer of USDC so the scanner
  cannot accidentally deplete the DCA runway. See trading.analytics._calc_capital
  which subtracts this reserve before sizing scanner trades.

Schedule:
  Idempotent weekly check via `last_dca_run` kv timestamp. Safe to call every
  scan cycle (30s) — only fires when the scheduled day/hour matches AND the
  last buy was >6 days ago.

Design notes:
  - DCA trades live in the same `trades` table as scanner trades but with
    signal_strength="DCA" so they can be filtered out of performance metrics.
  - status="dca_hold" signals that the trade never closes (no TP/SL).
  - All functions accept an optional `conn` parameter for connection pooling.
"""
from __future__ import annotations

import math
import sqlite3
import time
from datetime import datetime, timedelta
from typing import Any, Optional

from config import (
    DCA_AMOUNT_USDC,
    DCA_DAY_OF_WEEK,
    DCA_ENABLED,
    DCA_HOUR,
    DCA_MIN_SCANNER_USDC,
    DCA_RESERVE_MULT,
    DCA_TARGET_ASSET,
    DCA_TARGET_PAIR,
    DCA_TARGET_QTY,
    STAKING_AUTO_STAKE,
    STAKING_ENABLED,
)
from trading.db import (
    db_connect,
    db_init,
    get_kv,
    insert_trade,
    set_kv,
)
from trading.http_client import get, signed_get, signed_post
from trading.notify import send_telegram
from trading.orders import _order_fill_price


# ── Schedule check ────────────────────────────────────────────────────────────

def should_run_dca(conn: Optional[sqlite3.Connection] = None) -> bool:
    """Return True if a DCA buy should fire now.

    Rules:
      1. DCA_ENABLED must be true (feature flag)
      2. Today must be DCA_DAY_OF_WEEK (0=Mon..6=Sun)
      3. Current hour must be >= DCA_HOUR (fires at or after scheduled time)
      4. Last DCA run must be > 6 days ago (prevents double-firing in the same day window)
    """
    if not DCA_ENABLED:
        return False

    now = datetime.now()
    if now.weekday() != DCA_DAY_OF_WEEK:
        return False
    if now.hour < DCA_HOUR:
        return False

    # Check last run timestamp
    _own = conn is None
    if _own:
        conn = db_connect()
        db_init(conn)
    try:
        last_run = get_kv(conn, "last_dca_run") or ""
        if last_run:
            try:
                last_dt = datetime.fromisoformat(last_run)
                age_days = (now - last_dt).total_seconds() / 86400
                if age_days < 6:
                    return False  # already ran this week
            except (ValueError, TypeError):
                pass  # corrupt timestamp, proceed
        return True
    finally:
        if _own:
            conn.close()


# ── Balance check ─────────────────────────────────────────────────────────────

def _get_usdc_balance() -> float:
    """Fetch current free USDC balance from Binance."""
    try:
        acct = signed_get("/api/v3/account", {})
        for b in acct.get("balances", []):
            if b["asset"] == "USDC":
                return float(b["free"])
    except Exception as e:
        print(f"  \u26a0 DCA USDC balance fetch failed: {e}")
    return 0.0


def _get_eth_balance() -> float:
    """Fetch current free ETH balance from Binance (excludes staked BETH)."""
    try:
        acct = signed_get("/api/v3/account", {})
        for b in acct.get("balances", []):
            if b["asset"] == DCA_TARGET_ASSET:
                return float(b["free"])
    except Exception as e:
        print(f"  \u26a0 DCA {DCA_TARGET_ASSET} balance fetch failed: {e}")
    return 0.0


# ── DCA buy execution ─────────────────────────────────────────────────────────

def place_dca_buy(conn: Optional[sqlite3.Connection] = None) -> Optional[dict[str, Any]]:
    """Execute one DCA market buy and record it as a trade.

    Returns the trade dict on success, None on failure.
    Idempotent: sets `last_dca_run` kv only after successful buy + insert.
    """
    if not DCA_ENABLED:
        return None

    _own = conn is None
    if _own:
        conn = db_connect()
        db_init(conn)

    try:
        # ── Pre-flight: USDC balance check ────────────────────────────────
        usdc_balance = _get_usdc_balance()
        required = DCA_AMOUNT_USDC + DCA_MIN_SCANNER_USDC
        if usdc_balance < required:
            msg = (
                f"\u26a0 DCA skipped \u2014 insufficient USDC: "
                f"${usdc_balance:.2f} < ${required:.2f} "
                f"(need {DCA_AMOUNT_USDC} for buy + {DCA_MIN_SCANNER_USDC} scanner floor)"
            )
            print(f"  {msg}")
            send_telegram(msg)
            return None

        # ── Fetch current price + precision ───────────────────────────────
        try:
            ticker = get("/api/v3/ticker/price", {"symbol": DCA_TARGET_PAIR})
            price = float(ticker["price"])
        except Exception as e:
            print(f"  \u26a0 DCA price fetch failed: {e}")
            return None

        try:
            info = get("/api/v3/exchangeInfo", {"symbol": DCA_TARGET_PAIR})
            step = 1.0
            min_qty = 0.0
            for f in info["symbols"][0]["filters"]:
                if f["filterType"] == "LOT_SIZE":
                    step = float(f["stepSize"])
                    min_qty = float(f["minQty"])
        except Exception as e:
            print(f"  \u26a0 DCA exchange info fetch failed: {e}")
            return None

        # Compute quantity — use quoteOrderQty (spend exact USDC) to avoid
        # stepSize rounding issues entirely. Binance handles the math.
        qty_raw = DCA_AMOUNT_USDC / price
        qty_prec = len(str(step).rstrip('0').split('.')[-1])
        qty_est = round(math.floor(qty_raw / step) * step, qty_prec)
        if qty_est < min_qty:
            msg = (
                f"\u26a0 DCA skipped \u2014 computed qty {qty_est} < min_qty {min_qty} "
                f"at price ${price:.2f}. Increase DCA_AMOUNT_USDC."
            )
            print(f"  {msg}")
            send_telegram(msg)
            return None

        # ── Place market buy using quoteOrderQty ─────────────────────────
        # quoteOrderQty: spend exact USDC amount, Binance computes qty
        print(f"\n  \U0001f50b DCA BUY: ${DCA_AMOUNT_USDC} of {DCA_TARGET_PAIR} @ ~${price:.2f}")
        try:
            order = signed_post("/api/v3/order", {
                "symbol":         DCA_TARGET_PAIR,
                "side":           "BUY",
                "type":           "MARKET",
                "quoteOrderQty":  DCA_AMOUNT_USDC,
                "newClientOrderId": f"agent-dca-{int(time.time())}",
            })
        except Exception as e:
            msg = f"\u274c DCA buy failed: {e}"
            print(f"  {msg}")
            send_telegram(msg)
            return None

        # ── Extract fill details ──────────────────────────────────────────
        fill_price = _order_fill_price(order) or price
        filled_qty = float(order.get("executedQty", 0))
        quote_spent = float(order.get("cummulativeQuoteQty", DCA_AMOUNT_USDC))

        if filled_qty == 0:
            msg = f"\u26a0 DCA buy returned zero filled qty: {order}"
            print(f"  {msg}")
            send_telegram(msg)
            return None

        # ── Record as trade with signal_strength='DCA' ───────────────────
        trade_dict: dict[str, Any] = {
            "order_id":        str(order.get("orderId")),
            "symbol":          DCA_TARGET_PAIR,
            "time":            datetime.now().isoformat(),
            "entry":           fill_price,
            "tp":              0.0,  # DCA never closes; sentinel values
            "sl":              0.0,
            "qty":             filled_qty,
            "capital":         quote_spent,
            "oco_id":          None,
            "status":          "dca_hold",
            "sl_pct":          0.0,
            "tp_pct":          0.0,
            "breakeven_moved": False,
            "trailing_stage":  0,
            "signal_strength": "DCA",
        }
        insert_trade(conn, trade_dict)

        # ── Update sentinel + send notification ──────────────────────────
        set_kv(conn, "last_dca_run", datetime.now().isoformat())

        msg = (
            f"\U0001f50b *DCA buy*\n"
            f"`{DCA_TARGET_PAIR}` {filled_qty:.6f} @ `${fill_price:.2f}`\n"
            f"Spent: `${quote_spent:.2f}`"
        )
        send_telegram(msg)
        print(f"  \u2713 DCA buy recorded: {filled_qty:.6f} {DCA_TARGET_ASSET} @ ${fill_price:.2f}")

        # ── Auto-stake if enabled ─────────────────────────────────────────
        if STAKING_ENABLED and STAKING_AUTO_STAKE:
            try:
                from trading.staking import stake_eth
                stake_eth(filled_qty)
            except Exception as _se:
                print(f"  \u26a0 Auto-stake failed (manual stake recommended): {_se}")

        return trade_dict

    finally:
        if _own:
            conn.close()


# ── DCA statistics ────────────────────────────────────────────────────────────

def get_dca_stats(conn: Optional[sqlite3.Connection] = None) -> dict[str, Any]:
    """Return cumulative DCA statistics: qty, avg_entry, invested, current_value, pnl_pct."""
    _own = conn is None
    if _own:
        conn = db_connect()
        db_init(conn)

    try:
        rows = conn.execute(
            "SELECT entry, qty, capital, time FROM trades "
            "WHERE signal_strength = 'DCA' AND status = 'dca_hold' "
            "ORDER BY time ASC"
        ).fetchall()

        stats: dict[str, Any] = {
            "n_buys":         len(rows),
            "total_qty":      0.0,
            "total_invested": 0.0,
            "avg_entry":      0.0,
            "current_price":  0.0,
            "current_value":  0.0,
            "pnl_usdc":       0.0,
            "pnl_pct":        0.0,
            "target_qty":     DCA_TARGET_QTY,
            "progress_pct":   0.0,
            "first_buy":      None,
            "last_buy":       None,
        }

        if not rows:
            return stats

        for r in rows:
            qty = float(r["qty"] or 0)
            capital = float(r["capital"] or 0)
            stats["total_qty"]      += qty
            stats["total_invested"] += capital

        if stats["total_qty"] > 0:
            stats["avg_entry"] = stats["total_invested"] / stats["total_qty"]

        stats["first_buy"] = rows[0]["time"]
        stats["last_buy"]  = rows[-1]["time"]
        stats["progress_pct"] = (stats["total_qty"] / DCA_TARGET_QTY * 100) if DCA_TARGET_QTY > 0 else 0.0

        # Fetch current price for unrealized P&L
        try:
            ticker = get("/api/v3/ticker/price", {"symbol": DCA_TARGET_PAIR})
            cp = float(ticker["price"])
            stats["current_price"] = cp
            stats["current_value"] = stats["total_qty"] * cp
            stats["pnl_usdc"]      = stats["current_value"] - stats["total_invested"]
            if stats["total_invested"] > 0:
                stats["pnl_pct"] = stats["pnl_usdc"] / stats["total_invested"] * 100
        except Exception:
            pass

        return stats

    finally:
        if _own:
            conn.close()


# ── Reserve management ────────────────────────────────────────────────────────

def initialize_dca_reserve(conn: Optional[sqlite3.Connection] = None) -> float:
    """Set the DCA USDC reserve based on config. Called on first DCA activation.

    Reserve = DCA_AMOUNT_USDC × DCA_RESERVE_MULT (weeks of buffer).
    Returns the reserved amount.
    """
    _own = conn is None
    if _own:
        conn = db_connect()
        db_init(conn)
    try:
        reserve = DCA_AMOUNT_USDC * DCA_RESERVE_MULT
        set_kv(conn, "dca_reserved_usdc", str(reserve))
        return reserve
    finally:
        if _own:
            conn.close()


def get_dca_reserve(conn: Optional[sqlite3.Connection] = None) -> float:
    """Return the current DCA USDC reserve (0 if not set)."""
    _own = conn is None
    if _own:
        conn = db_connect()
        db_init(conn)
    try:
        val = get_kv(conn, "dca_reserved_usdc")
        return float(val) if val else 0.0
    finally:
        if _own:
            conn.close()


def next_dca_time() -> datetime:
    """Return the next scheduled DCA run time (for countdown display)."""
    now = datetime.now()
    days_ahead = (DCA_DAY_OF_WEEK - now.weekday()) % 7
    if days_ahead == 0 and now.hour >= DCA_HOUR:
        days_ahead = 7  # already passed today's slot
    next_dt = (now + timedelta(days=days_ahead)).replace(
        hour=DCA_HOUR, minute=0, second=0, microsecond=0
    )
    return next_dt


# ── Run-DCA orchestration (called from scan loops) ───────────────────────────

def run_dca_check(conn: Optional[sqlite3.Connection] = None) -> Optional[dict[str, Any]]:
    """Check if DCA should run now, and if so execute a buy.

    This is the public entry point called from scanner.py._scan_body()
    and tui.py's scan worker. Cheap when not scheduled (single DB read).
    """
    if not DCA_ENABLED:
        return None
    if not should_run_dca(conn):
        return None
    return place_dca_buy(conn)
