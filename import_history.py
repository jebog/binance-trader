#!/usr/bin/env python3
"""
Import historical Binance trades into state.db.

Groups fills by orderId into logical orders, pairs buy→sell sequences per symbol,
computes P&L, and inserts as closed trades. Skips orders already in state.db.

Usage:
    python3 import_history.py              # import from Binance API
    python3 import_history.py --dry-run    # preview without writing
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from typing import Any

from config import PAIRS
from trading.db import db_connect, db_init, insert_trade
from trading.http_client import signed_get


def fetch_all_trades(symbol: str) -> list[dict[str, Any]]:
    """Fetch up to 1000 account trades for a symbol."""
    return signed_get("/api/v3/myTrades", {"symbol": symbol, "limit": 1000})


def aggregate_orders(fills: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group fills by orderId into aggregated orders with weighted avg price."""
    by_order: dict[int, list[dict]] = {}
    for f in fills:
        oid = f["orderId"]
        by_order.setdefault(oid, []).append(f)

    orders = []
    for oid, order_fills in sorted(by_order.items(), key=lambda x: x[1][0]["time"]):
        total_qty = sum(float(f["qty"]) for f in order_fills)
        total_quote = sum(float(f["quoteQty"]) for f in order_fills)
        avg_price = total_quote / total_qty if total_qty > 0 else 0.0
        total_commission = sum(float(f["commission"]) for f in order_fills)
        commission_asset = order_fills[0].get("commissionAsset", "")
        is_buyer = order_fills[0]["isBuyer"]
        ts = order_fills[0]["time"]
        oco_id = order_fills[0].get("orderListId", -1)

        orders.append({
            "orderId":          oid,
            "symbol":           order_fills[0]["symbol"],
            "side":             "BUY" if is_buyer else "SELL",
            "qty":              round(total_qty, 8),
            "avg_price":        round(avg_price, 8),
            "quote_total":      round(total_quote, 8),
            "commission":       round(total_commission, 8),
            "commission_asset": commission_asset,
            "time":             ts,
            "oco_id":           oco_id if oco_id != -1 else None,
            "iso_time":         datetime.fromtimestamp(ts / 1000, tz=timezone.utc).isoformat(),
        })

    return orders


def pair_trades(orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pair BUY→SELL sequences into complete trades with P&L."""
    trades = []
    pending_buy: dict[str, dict] | None = None

    # Sort by time
    for order in sorted(orders, key=lambda o: o["time"]):
        if order["side"] == "BUY":
            pending_buy = order
        elif order["side"] == "SELL" and pending_buy is not None:
            entry = pending_buy["avg_price"]
            exit_p = order["avg_price"]
            pnl_pct = (exit_p - entry) / entry * 100 if entry > 0 else 0.0
            capital = pending_buy["quote_total"]

            # Determine outcome
            if pnl_pct > 0:
                status = "tp_hit"
            else:
                status = "sl_hit"

            trades.append({
                "order_id":        str(pending_buy["orderId"]),
                "symbol":          order["symbol"],
                "time":            pending_buy["iso_time"],
                "entry":           entry,
                "tp":              exit_p if pnl_pct > 0 else entry * 1.05,
                "sl":              exit_p if pnl_pct <= 0 else entry * 0.97,
                "qty":             pending_buy["qty"],
                "capital":         round(capital, 2),
                "oco_id":          order.get("oco_id"),
                "status":          status,
                "exit_price":      exit_p,
                "pnl_pct":         round(pnl_pct, 4),
                "exit_time":       order["iso_time"],
                "signal_strength": "IMPORTED",
                "sl_pct":          0.03,
                "tp_pct":          0.075,
                "breakeven_moved": False,
                "trailing_stage":  0,
                # Metadata
                "buy_order_id":    pending_buy["orderId"],
                "sell_order_id":   order["orderId"],
                "buy_commission":  pending_buy["commission"],
                "sell_commission": order["commission"],
            })
            pending_buy = None

    return trades


def sync_history(conn: Any = None) -> int:
    """Import new Binance trades into state.db. Returns count of new trades imported.

    Reusable by TUI auto-import. Idempotent — skips existing order_ids.
    """
    _own = conn is None
    if _own:
        conn = db_connect()
        db_init(conn)

    existing = {row[0] for row in conn.execute("SELECT order_id FROM trades").fetchall()}
    all_trades: list[dict] = []

    for symbol in PAIRS:
        try:
            fills = fetch_all_trades(symbol)
            orders = aggregate_orders(fills)
            trades = pair_trades(orders)
            new = [t for t in trades if str(t["order_id"]) not in existing]
            all_trades.extend(new)
        except Exception:
            pass

    for t in sorted(all_trades, key=lambda x: x["time"]):
        trade_dict = {
            "order_id": t["order_id"], "symbol": t["symbol"], "time": t["time"],
            "entry": t["entry"], "tp": t["tp"], "sl": t["sl"], "qty": t["qty"],
            "capital": t["capital"], "oco_id": t.get("oco_id"), "status": t["status"],
            "exit_price": t["exit_price"], "pnl_pct": t["pnl_pct"],
            "exit_time": t["exit_time"], "signal_strength": t["signal_strength"],
            "sl_pct": t["sl_pct"], "tp_pct": t["tp_pct"],
            "breakeven_moved": False, "trailing_stage": 0,
        }
        insert_trade(conn, trade_dict)

    if _own:
        conn.close()
    return len(all_trades)


def main() -> None:
    dry_run = "--dry-run" in sys.argv

    conn = db_connect()
    db_init(conn)

    # Get existing order IDs to avoid duplicates
    existing = {row[0] for row in conn.execute("SELECT order_id FROM trades").fetchall()}

    all_trades: list[dict] = []
    for symbol in PAIRS:
        print(f"  Fetching {symbol}...", end=" ", flush=True)
        try:
            fills = fetch_all_trades(symbol)
            orders = aggregate_orders(fills)
            trades = pair_trades(orders)
            new = [t for t in trades if str(t["order_id"]) not in existing]
            all_trades.extend(new)
            print(f"{len(fills)} fills → {len(orders)} orders → {len(trades)} trades ({len(new)} new)")
        except Exception as e:
            print(f"ERROR: {e}")

    if not all_trades:
        print("\n  No new trades to import.")
        conn.close()
        return

    # Sort by time
    all_trades.sort(key=lambda t: t["time"])

    print(f"\n{'='*60}")
    print(f"  {'Symbol':<10} {'Entry':>10} {'Exit':>10} {'P&L':>8} {'Status':<8} {'Date'}")
    print(f"{'='*60}")
    total_pnl = 0.0
    for t in all_trades:
        pair = t["symbol"].replace("USDC", "")
        pnl_str = f"{t['pnl_pct']:+.2f}%"
        date_str = t["time"][:10]
        icon = "\u2705" if t["status"] == "tp_hit" else "\U0001f534"
        print(f"  {icon} {pair:<8} ${t['entry']:>9.4f} ${t['exit_price']:>9.4f} {pnl_str:>8} {t['status']:<8} {date_str}")
        total_pnl += t["pnl_pct"]

    print(f"{'='*60}")
    print(f"  Total: {len(all_trades)} trades  Net P&L: {total_pnl:+.2f}%")

    if dry_run:
        print("\n  [DRY RUN] No trades written to state.db.")
    else:
        for t in all_trades:
            trade_dict = {
                "order_id":        t["order_id"],
                "symbol":          t["symbol"],
                "time":            t["time"],
                "entry":           t["entry"],
                "tp":              t["tp"],
                "sl":              t["sl"],
                "qty":             t["qty"],
                "capital":         t["capital"],
                "oco_id":          t.get("oco_id"),
                "status":          t["status"],
                "exit_price":      t["exit_price"],
                "pnl_pct":         t["pnl_pct"],
                "exit_time":       t["exit_time"],
                "signal_strength": t["signal_strength"],
                "sl_pct":          t["sl_pct"],
                "tp_pct":          t["tp_pct"],
                "breakeven_moved": False,
                "trailing_stage":  0,
            }
            insert_trade(conn, trade_dict)
        print(f"\n  ✓ {len(all_trades)} trades imported to state.db")

    conn.close()


if __name__ == "__main__":
    main()
