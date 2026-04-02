from __future__ import annotations

import math
import sqlite3
from datetime import datetime
from typing import Any, Optional

from config import (
    ATR_SL_MULT,
    BREAKEVEN_ATR_MULT,
    BREAKEVEN_ENABLED,
    PARTIAL_TP1_QTY_PCT,
    PARTIAL_TP_ENABLED,
    PROGRESSIVE_TRAILING_ENABLED,
    PROGRESSIVE_TRAILING_STAGES,
    SL_COOLDOWN_H,
    TRADE_TIMEOUT_ENABLED,
    TRADE_TIMEOUT_H,
    TRAILING_DELTA,
)
from trading.db import (
    db_connect,
    db_init,
    get_kv,
    get_open_trades,
    set_kv,
    update_trade_fields,
)
from trading.http_client import get, signed_delete, signed_get, signed_post
from trading.notify import send_telegram
from trading.orders import _order_fill_price, _save_cooldown


# ── Break-even stop (T3-1) ───────────────────────────────────────────────────
def _check_breakeven(trade: dict[str, Any], current_price: float, symbol: str,
                     conn: Optional[sqlite3.Connection] = None) -> bool:
    """If price has risen >= BREAKEVEN_ATR_MULT x ATR above entry, move SL to entry."""
    if not BREAKEVEN_ENABLED or trade.get("breakeven_moved"):
        return False
    sl_pct = trade.get("sl_pct")
    if not sl_pct or ATR_SL_MULT <= 0:
        return False
    atr_pct = sl_pct / ATR_SL_MULT
    entry   = trade.get("entry", 0.0)
    if not entry:
        return False
    trigger = entry * (1 + BREAKEVEN_ATR_MULT * atr_pct)
    if current_price < trigger:
        return False

    oco_id = trade.get("oco_id")
    if not oco_id:
        return False
    try:
        signed_delete("/api/v3/orderList", {"symbol": symbol, "orderListId": oco_id})
        print(f"  Cancelled OCO #{oco_id} for break-even on {symbol}")
    except Exception as cancel_err:
        print(f"  \u26a0 Break-even OCO cancel failed for {symbol}: {cancel_err} \u2014 will retry next scan")
        return False

    try:
        info = get("/api/v3/exchangeInfo", {"symbol": symbol})
        tick = 0.01
        for f in info["symbols"][0]["filters"]:
            if f["filterType"] == "PRICE_FILTER":
                tick = float(f["tickSize"])
        tick_prec = len(str(tick).rstrip("0").split(".")[-1])

        be_sl    = round(round(entry / tick) * tick, tick_prec)
        tp_price = trade.get("tp")
        qty      = trade.get("qty", 0)

        if TRAILING_DELTA > 0:
            new_oco = signed_post("/api/v3/orderList/oco", {
                "symbol":             symbol,
                "side":               "SELL",
                "quantity":           qty,
                "aboveType":          "LIMIT_MAKER",
                "abovePrice":         tp_price,
                "belowType":          "STOP_LOSS",
                "belowStopPrice":     be_sl,
                "belowTrailingDelta": TRAILING_DELTA,
                "belowTimeInForce":   "GTC",
            })
        else:
            sl_limit = round(round(be_sl * 0.995 / tick) * tick, tick_prec)
            new_oco = signed_post("/api/v3/orderList/oco", {
                "symbol":           symbol,
                "side":             "SELL",
                "quantity":         qty,
                "aboveType":        "LIMIT_MAKER",
                "abovePrice":       tp_price,
                "belowType":        "STOP_LOSS_LIMIT",
                "belowStopPrice":   be_sl,
                "belowPrice":       sl_limit,
                "belowTimeInForce": "GTC",
            })

        trade["breakeven_moved"] = True
        trade["sl"]              = be_sl
        trade["oco_id"]          = new_oco.get("orderListId")
        print(f"  \U0001f6e1 Break-even armed for {symbol} \u2014 SL moved to entry ${entry:.4f}")
        send_telegram(f"\U0001f6e1 Break-even armed for `{symbol}` \u2014 SL moved to entry `${entry:.4f}`")

        try:
            _be_c = conn if conn is not None else db_connect()
            update_trade_fields(_be_c, str(trade.get("order_id", "")),
                                breakeven_moved=True, sl=trade["sl"], oco_id=trade["oco_id"])
            if conn is None:
                _be_c.close()
        except Exception:
            pass

        return True

    except Exception as oco_err:
        msg = (f"\U0001f6a8 *BREAKEVEN OCO FAILED* \u2014 `{symbol}` position UNPROTECTED. "
               f"Place OCO manually. Error: `{str(oco_err)[:200]}`")
        print(f"  \u2717 {msg}")
        send_telegram(msg)
        trade["status"] = "no_oco"
        trade["breakeven_moved"] = True
        try:
            _be_c2 = conn if conn is not None else db_connect()
            update_trade_fields(_be_c2, str(trade.get("order_id", "")),
                                status="no_oco", breakeven_moved=True, oco_id=None)
            if conn is None:
                _be_c2.close()
        except Exception:
            pass
        return True


# ── Progressive trailing stop (T4-4) ─────────────────────────────────────────
def _check_progressive_trailing(trade: dict[str, Any], current_price: float, symbol: str,
                                conn: Optional[sqlite3.Connection] = None) -> bool:
    """Tighten trailing stop delta when price reaches successive ATR milestones."""
    if not PROGRESSIVE_TRAILING_ENABLED:
        return False
    if not trade.get("breakeven_moved"):
        return False
    current_stage = trade.get("trailing_stage", 0)
    if current_stage >= len(PROGRESSIVE_TRAILING_STAGES):
        return False

    sl_pct = trade.get("sl_pct")
    if not sl_pct or ATR_SL_MULT <= 0:
        return False
    atr_pct = sl_pct / ATR_SL_MULT
    entry   = trade.get("entry", 0.0)
    if not entry:
        return False

    atr_mult_trigger, new_bps = PROGRESSIVE_TRAILING_STAGES[current_stage]
    trigger_price = entry * (1 + atr_mult_trigger * atr_pct)
    if current_price < trigger_price:
        return False

    oco_id = trade.get("oco_id")
    if not oco_id:
        return False
    try:
        signed_delete("/api/v3/orderList", {"symbol": symbol, "orderListId": oco_id})
        print(f"  Cancelled OCO #{oco_id} for progressive trailing stage {current_stage + 1} on {symbol}")
    except Exception as cancel_err:
        print(f"  \u26a0 Progressive trailing OCO cancel failed for {symbol} stage {current_stage + 1}: "
              f"{cancel_err} \u2014 will retry next scan")
        return False

    try:
        info = get("/api/v3/exchangeInfo", {"symbol": symbol})
        tick = 0.01
        for f in info["symbols"][0]["filters"]:
            if f["filterType"] == "PRICE_FILTER":
                tick = float(f["tickSize"])
        tick_prec = len(str(tick).rstrip("0").split(".")[-1])

        be_sl    = trade.get("sl")
        tp_price = trade.get("tp")
        qty      = trade.get("qty", 0)

        if new_bps > 0:
            new_oco = signed_post("/api/v3/orderList/oco", {
                "symbol":             symbol,
                "side":               "SELL",
                "quantity":           qty,
                "aboveType":          "LIMIT_MAKER",
                "abovePrice":         tp_price,
                "belowType":          "STOP_LOSS",
                "belowStopPrice":     be_sl,
                "belowTrailingDelta": new_bps,
                "belowTimeInForce":   "GTC",
            })
        else:
            sl_limit = round(round(be_sl * 0.995 / tick) * tick, tick_prec)
            new_oco = signed_post("/api/v3/orderList/oco", {
                "symbol":           symbol,
                "side":             "SELL",
                "quantity":         qty,
                "aboveType":        "LIMIT_MAKER",
                "abovePrice":       tp_price,
                "belowType":        "STOP_LOSS_LIMIT",
                "belowStopPrice":   be_sl,
                "belowPrice":       sl_limit,
                "belowTimeInForce": "GTC",
            })

        trade["oco_id"]        = new_oco.get("orderListId")
        trade["trailing_stage"] = current_stage + 1
        stage_total = len(PROGRESSIVE_TRAILING_STAGES)
        print(f"  \U0001f3af Progressive trailing stage {current_stage + 1}/{stage_total} armed for {symbol} "
              f"\u2014 delta {new_bps}bps at ${current_price:.4f} ({atr_mult_trigger}\u00d7ATR)")
        send_telegram(
            f"\U0001f3af Trailing tightened `{symbol}` stage {current_stage + 1}/{stage_total}\n"
            f"Delta: `{new_bps}bps` at `${current_price:.4f}` ({atr_mult_trigger}\u00d7ATR)"
        )

        try:
            _pt_c = conn if conn is not None else db_connect()
            update_trade_fields(_pt_c, str(trade.get("order_id", "")),
                                oco_id=trade["oco_id"], trailing_stage=trade["trailing_stage"])
            if conn is None:
                _pt_c.close()
        except Exception:
            pass

        return True

    except Exception as oco_err:
        total = len(PROGRESSIVE_TRAILING_STAGES)
        msg = (f"\U0001f6a8 *PROGRESSIVE TRAILING OCO FAILED* \u2014 `{symbol}` stage "
               f"{current_stage + 1}/{total} UNPROTECTED. "
               f"Place OCO manually. Error: `{str(oco_err)[:200]}`")
        print(f"  \u2717 {msg}")
        send_telegram(msg)
        trade["status"]         = "no_oco"
        trade["trailing_stage"] = current_stage + 1
        try:
            _pt_c2 = conn if conn is not None else db_connect()
            update_trade_fields(_pt_c2, str(trade.get("order_id", "")),
                                status="no_oco", trailing_stage=trade["trailing_stage"],
                                oco_id=None)
            if conn is None:
                _pt_c2.close()
        except Exception:
            pass
        return True


# ── Trade timeout handler (T3-2) ─────────────────────────────────────────────
def _handle_trade_timeout(trade: dict[str, Any], symbol: str) -> None:
    """Force-exit a position that has been open longer than TRADE_TIMEOUT_H."""
    age_h = (datetime.now() - datetime.fromisoformat(trade["time"])).total_seconds() / 3600
    qty   = trade.get("qty", 0)

    oco_id = trade.get("oco_id")
    if oco_id:
        try:
            signed_delete("/api/v3/orderList", {"symbol": symbol, "orderListId": oco_id})
            print(f"  Cancelled OCO #{oco_id} for timeout on {symbol}")
        except Exception as e:
            print(f"  \u26a0 OCO cancel failed during timeout ({symbol}): {e}")

    if PARTIAL_TP_ENABLED and trade.get("tp1_order_id") and trade.get("status") == "open":
        try:
            signed_delete("/api/v3/order", {"symbol": symbol, "orderId": trade["tp1_order_id"]})
        except Exception:
            pass

    try:
        sell_order = signed_post("/api/v3/order", {
            "symbol":   symbol,
            "side":     "SELL",
            "type":     "MARKET",
            "quantity": qty,
        })
        ep    = _order_fill_price(sell_order)
        entry = trade.get("entry", 0.0)
        if trade.get("status") == "partial_tp" and trade.get("partial_tp1"):
            p1        = trade["partial_tp1"]
            tp1_pnl   = p1.get("pnl_pct") or 0.0
            leg2_pnl  = (ep - entry) / entry * 100 if (ep and entry) else 0.0
            pnl_pct   = tp1_pnl * PARTIAL_TP1_QTY_PCT + leg2_pnl * (1 - PARTIAL_TP1_QTY_PCT)
        else:
            pnl_pct   = (ep - entry) / entry * 100 if (ep and entry) else None
        trade["status"]     = "timeout"
        trade["exit_price"] = ep
        trade["pnl_pct"]    = pnl_pct
        trade["exit_time"]  = datetime.now().isoformat()
        pnl_str = f"{pnl_pct:+.2f}%" if pnl_pct is not None else "N/A"
        print(f"  \u23f1 Timeout exit {symbol} after {age_h:.0f}h \u2014 {pnl_str}")
        send_telegram(f"\u23f1 Timeout exit `{symbol}` after {age_h:.0f}h \u2014 {pnl_str}")
    except Exception as sell_err:
        msg = (f"\U0001f6a8 *TIMEOUT SELL FAILED* \u2014 `{symbol}` position UNPROTECTED after {age_h:.0f}h. "
               f"Manual exit required. Error: `{str(sell_err)[:200]}`")
        print(f"  \u2717 {msg}")
        send_telegram(msg)
        trade["status"] = "timeout_sell_failed"


# ── Partial TP1 handler (T2-4) ───────────────────────────────────────────────
def _handle_partial_tp1(trade: dict[str, Any], tp1_order: dict[str, Any]) -> None:
    """React to a TP1 LIMIT_MAKER fill."""
    symbol   = trade["symbol"]
    tp1_fill = _order_fill_price(tp1_order) or trade.get("tp1_price")
    entry    = trade.get("entry", 0.0)
    tp1_pnl  = (tp1_fill - entry) / entry * 100 if (tp1_fill and entry) else None
    trade["partial_tp1"] = {
        "exit_price": tp1_fill,
        "pnl_pct":    tp1_pnl,
        "exit_time":  datetime.now().isoformat(),
    }
    print(f"  \u2713 Partial TP1 filled for {symbol} @ ${tp1_fill:.4f} ({tp1_pnl:+.2f}% on half position)")

    oco_id = trade.get("oco_id")
    try:
        signed_delete("/api/v3/orderList", {"symbol": symbol, "orderListId": oco_id})
        print(f"  Cancelled original OCO #{oco_id}")
    except Exception as cancel_err:
        msg = (f"\U0001f6a8 *Partial TP1 OCO cancel failed* \u2014 `{symbol}` original OCO #{oco_id} "
               f"still active. Error: `{str(cancel_err)[:200]}`")
        print(f"  \u2717 {msg}")
        send_telegram(msg)
        trade["status"] = "partial_tp_no_oco"
        return

    remaining_qty = round(trade.get("qty", 0) - trade.get("tp1_qty", 0), 8)
    tp2_price  = trade.get("tp")
    sl_price   = trade.get("sl")
    try:
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
            msg = (f"\U0001f6a8 *Partial TP1 re-OCO skipped* \u2014 `{symbol}` remaining qty "
                   f"{remaining_qty} < min_qty {min_qty}. Manual close required.")
            print(f"  \u2717 {msg}")
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
            f"\U0001f4ca *Partial TP1 hit* \u2014 `{symbol}` half closed @ `${tp1_fill:.4f}` "
            f"({tp1_pnl:+.2f}%). Riding remainder to TP2 `${tp2_price:.4f}`."
        )
    except Exception as new_oco_err:
        msg = (f"\U0001f6a8 *Partial TP1 re-OCO FAILED* \u2014 `{symbol}` {remaining_qty} UNPROTECTED. "
               f"Place OCO manually. Error: `{str(new_oco_err)[:200]}`")
        print(f"  \u2717 {msg}")
        send_telegram(msg)
        trade["status"] = "partial_tp_no_oco"


def _check_sl_outcomes(conn: Optional[sqlite3.Connection] = None) -> None:
    """Check closed OCO orders -- if stop leg filled, trigger SL cooldown."""
    _own_conn = conn is None
    try:
        if _own_conn:
            conn = db_connect()
            db_init(conn)
        _lock_ts = get_kv(conn, "sl_check_lock") or ""
        if _lock_ts:
            try:
                _lock_age = (datetime.now() - datetime.fromisoformat(_lock_ts)).total_seconds()
                if _lock_age < 60:
                    return  # finally block handles conn.close()
            except (ValueError, TypeError):
                pass
        set_kv(conn, "sl_check_lock", datetime.now().isoformat())
        active_trades = get_open_trades(conn)
    except Exception as _e:
        print(f"  \u26a0 _check_sl_outcomes: DB read failed: {_e}")
        if _own_conn and conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        return
    try:
        if not active_trades:
            return

        if TRADE_TIMEOUT_ENABLED:
            timed_out_ids: set[str] = set()
            timed_out_trades: list[dict[str, Any]] = []
            for trade in active_trades:
                try:
                    age_h = (datetime.now() - datetime.fromisoformat(trade["time"])).total_seconds() / 3600
                    if age_h >= TRADE_TIMEOUT_H:
                        _handle_trade_timeout(trade, trade["symbol"])
                        timed_out_ids.add(str(trade.get("order_id", "")))
                        timed_out_trades.append(trade)
                except Exception as _to_e:
                    print(f"  \u26a0 Timeout check failed for {trade.get('symbol', '?')}: {_to_e}")
            # Persist timed-out trades to DB immediately
            for t in timed_out_trades:
                _oid = str(t.get("order_id", ""))
                if _oid:
                    try:
                        update_trade_fields(conn, _oid,
                                            status=t["status"],
                                            exit_price=t.get("exit_price"),
                                            pnl_pct=t.get("pnl_pct"),
                                            exit_time=t.get("exit_time"))
                    except Exception:
                        pass
            if timed_out_ids:
                active_trades = [t for t in active_trades if str(t.get("order_id", "")) not in timed_out_ids]

        oco_ids = {t["oco_id"]: t for t in active_trades if t.get("oco_id")}
        if not oco_ids:
            return
        for oco_id, trade in oco_ids.items():
            symbol = trade["symbol"]
            try:
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

                if PARTIAL_TP_ENABLED and trade.get("tp1_order_id") and trade.get("status") == "open":
                    tp1_filled_order = next(
                        (o for o in all_orders
                         if str(o.get("orderId")) == str(trade["tp1_order_id"])
                         and o.get("status") == "FILLED"),
                        None,
                    )
                    if tp1_filled_order:
                        _handle_partial_tp1(trade, tp1_filled_order)
                        try:
                            _oid = str(trade.get("order_id", ""))
                            if _oid:
                                update_trade_fields(conn, _oid,
                                    status=trade["status"],
                                    partial_tp1=trade.get("partial_tp1"),
                                    oco_id=trade.get("oco_id"),
                                    qty=trade.get("qty"))
                        except Exception:
                            pass
                        continue

                if tp_filled:
                    print(f"  \u2713 TP hit detected for {symbol}")
                    ep = (_order_fill_price(filled_tp_order)
                          if filled_tp_order else trade.get("tp"))
                    entry = trade.get("entry")
                    if trade.get("status") == "partial_tp" and trade.get("partial_tp1"):
                        p1 = trade["partial_tp1"]
                        tp1_pnl = p1.get("pnl_pct") or 0.0
                        tp2_pnl = (ep - entry) / entry * 100 if (entry and ep) else 0.0
                        final_pnl = tp1_pnl * PARTIAL_TP1_QTY_PCT + tp2_pnl * (1 - PARTIAL_TP1_QTY_PCT)
                    else:
                        final_pnl = (ep - entry) / entry * 100 if (entry and ep) else None
                    send_telegram(f"\u2705 TP hit on `{symbol}` \u2014 target reached")
                    trade["status"]     = "tp_hit"
                    trade["exit_price"] = ep
                    trade["pnl_pct"]    = final_pnl
                    trade["exit_time"]  = datetime.now().isoformat()
                elif sl_filled:
                    print(f"  \u26a0 SL hit detected for {symbol} \u2014 cooldown {SL_COOLDOWN_H}h")
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
                            f"\U0001f534 SL hit on `{symbol}` \u2014 partial TP1 was profitable. "
                            f"Net P&L: {final_pnl:+.2f}%. Pausing {SL_COOLDOWN_H}h."
                        )
                    else:
                        final_pnl = (ep - entry) / entry * 100 if (entry and ep) else None
                        send_telegram(f"\U0001f534 SL hit on `{symbol}` \u2014 pausing signals {SL_COOLDOWN_H}h")
                    trade["status"]     = "sl_hit"
                    trade["exit_price"] = ep
                    trade["pnl_pct"]    = final_pnl
                    trade["exit_time"]  = datetime.now().isoformat()
            except Exception as _te:
                print(f"  \u26a0 Trade outcome check failed for {symbol}: {_te}")
        resolved_by_order: dict[str, dict[str, Any]] = {
            str(t.get("order_id")): t for _, t in oco_ids.items()
        }
        try:
            for order_id, resolved in resolved_by_order.items():
                if resolved.get("status") not in ("open",):
                    fields: dict[str, Any] = {
                        "status":     resolved["status"],
                        "exit_price": resolved.get("exit_price"),
                        "pnl_pct":    resolved.get("pnl_pct"),
                        "exit_time":  resolved.get("exit_time"),
                    }
                    if resolved.get("partial_tp1"):
                        fields["partial_tp1"] = resolved["partial_tp1"]
                    if resolved.get("oco_id") is not None:
                        fields["oco_id"] = resolved["oco_id"]
                    if resolved.get("qty") is not None:
                        fields["qty"] = resolved["qty"]
                    update_trade_fields(conn, order_id, **fields)
        except Exception as _e:
            print(f"  \u26a0 SQLite trade outcome write failed: {_e}")
    except Exception as e:
        print(f"  \u26a0 SL outcome check failed: {e}")
    finally:
        if _own_conn and conn is not None:
            try:
                conn.close()
            except Exception:
                pass
