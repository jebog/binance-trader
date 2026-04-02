from __future__ import annotations

import math
import sqlite3
import time
from datetime import datetime
from typing import Any, Optional

from config import (
    ATR_SL_MAX,
    ATR_SL_MIN,
    ATR_SL_MULT,
    ATR_TP_MULT,
    PARTIAL_TP1_ATR_MULT,
    PARTIAL_TP1_QTY_PCT,
    PARTIAL_TP_ENABLED,
    STOP_LOSS,
    TAKE_PROFIT,
    TRAILING_DELTA,
)
from trading.db import (
    clear_pending_second_entry,
    db_connect,
    load_cooldowns,
    load_pending_second_entries,
    save_cooldown,
    save_pending_second_entry,
)
from trading.http_client import get, signed_delete, signed_post
from trading.indicators import calc_atr
from trading.notify import send_telegram


# ── Cooldown helpers ─────────────────────────────────────────────────────────
def _load_cooldowns() -> dict[str, str]:
    """Return {symbol: expiry_iso}, pruning expired entries."""
    try:
        _lc_conn = db_connect()
        result = load_cooldowns(_lc_conn)
        _lc_conn.close()
        return result
    except Exception:
        return {}


def _order_fill_price(order: dict[str, Any]) -> Optional[float]:
    """Return actual avg fill price from a FILLED Binance order object."""
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
        _cd_conn = db_connect()
        save_cooldown(_cd_conn, symbol)
        _cd_conn.close()
    except Exception:
        pass


# ── Split-entry state helpers (T2-1) ─────────────────────────────────────────
def _load_pending_second_entries(conn: Optional[sqlite3.Connection] = None) -> dict[str, Any]:
    """Return pending_second_entries dict from state.db."""
    _own = conn is None
    try:
        if _own:
            conn = db_connect()
        result = load_pending_second_entries(conn)
        if _own:
            conn.close()
        return result
    except Exception:
        return {}


def _save_pending_second_entry(symbol: str, data: dict[str, Any],
                               conn: Optional[sqlite3.Connection] = None) -> None:
    """Write a single pending second entry to state.db."""
    _own = conn is None
    try:
        if _own:
            conn = db_connect()
        save_pending_second_entry(conn, symbol, data)
        if _own:
            conn.close()
    except Exception as e:
        print(f"  \u26a0 Could not persist pending second entry for {symbol}: {e}")


def _clear_pending_second_entry(symbol: str,
                                conn: Optional[sqlite3.Connection] = None) -> None:
    """Remove a pending second entry from state.db."""
    _own = conn is None
    try:
        if _own:
            conn = db_connect()
        clear_pending_second_entry(conn, symbol)
        if _own:
            conn.close()
    except Exception as e:
        print(f"  \u26a0 Could not clear pending second entry for {symbol}: {e}")


def _place_split_second_entry(
    symbol: str,
    pending: dict[str, Any],
    current_price: float,
    closed_klines: list[list[Any]],
) -> Optional[dict[str, Any]]:
    """Execute the second half of a split entry."""
    first_fill = pending["first_fill"]
    first_qty  = pending["first_qty"]
    capital_half = pending["capital_half"]
    sl_pct = pending["sl_pct"]
    tp_pct = pending["tp_pct"]

    first_oco_id = pending["first_oco_id"]
    try:
        signed_delete("/api/v3/orderList", {"symbol": symbol, "orderListId": first_oco_id})
        print(f"  Cancelled first OCO #{first_oco_id} for split entry {symbol}")
    except Exception as cancel_err:
        send_telegram(
            f"\u26a0 *Split entry cancel failed* \u2014 `{symbol}` OCO #{first_oco_id} still active. "
            f"Retry next scan. Error: `{str(cancel_err)[:200]}`"
        )
        return None

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
        second_fill = _order_fill_price(second_order) or current_price
        second_qty = float(second_order.get("executedQty", qty2))
    except Exception as buy_err:
        send_telegram(
            f"\U0001f6a8 *CRITICAL \u2014 split second buy FAILED* \u2014 `{symbol}` first OCO #{first_oco_id} "
            f"was cancelled but second buy failed. First half UNPROTECTED. "
            f"Error: `{str(buy_err)[:200]}`"
        )
        return {"status": "critical_fail"}

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
            f"\U0001f6a8 *Split entry combined OCO FAILED* \u2014 `{symbol}` {total_qty_r} UNPROTECTED. "
            f"Place OCO manually: TP ~${tp2_price} / SL ~${sl2_price}. "
            f"Error: `{str(oco_err)[:200]}`"
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
            "oco_id":   None,
            "status":   "no_oco",
            "split_entry":     True,
            "signal_strength": "EXTREME",
            "sl_pct":          sl_pct,
            "tp_pct":          tp_pct,
            "breakeven_moved": False,
            "trailing_stage":  0,
        }

    print(f"  Split entry combined OCO #{combined_oco.get('orderListId')}: "
          f"avg_entry=${avg_entry:.4f} TP=${tp2_price:.4f} SL=${sl2_price:.4f} qty={total_qty_r}")
    send_telegram(
        f"\u2705 *Split entry complete* \u2014 `{symbol}`\n"
        f"Avg entry: `${avg_entry:.4f}` ({first_qty}@${first_fill:.4f} + {second_qty}@${second_fill:.4f})\n"
        f"TP `${tp2_price:.4f}` \u00b7 SL `${sl2_price:.4f}` | OCO #{combined_oco.get('orderListId')}"
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
        "split_entry":     True,
        "signal_strength": "EXTREME",
        "sl_pct":          sl_pct,
        "tp_pct":          tp_pct,
        "breakeven_moved": False,
        "trailing_stage":  0,
    }


# ── Order placement ──────────────────────────────────────────────────────────
def place_buy_order(
    symbol: str,
    capital: float,
    price: float,
    closed_klines: Optional[list[list[Any]]] = None,
) -> tuple[dict[str, Any], Optional[dict[str, Any]], dict[str, Any]]:
    """Place market buy + OCO (TP/SL)."""
    qty_raw = capital / price
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
    qty_prec = len(str(step).rstrip('0').split('.')[-1])
    qty = round(math.floor(qty_raw / step) * step, qty_prec)
    tick_prec = len(str(tick).rstrip('0').split('.')[-1])

    if qty == 0 or qty < min_qty:
        raise ValueError(
            f"Computed qty {qty} is below min_qty {min_qty} for {symbol} "
            f"\u2014 capital ${capital:.2f} insufficient at price ${price:.6f}"
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

    if ATR_SL_MULT > 0 and closed_klines:
        atr = calc_atr(closed_klines)
        if atr is not None:
            atr_pct = atr / fill_price
            sl_pct = min(max(atr_pct * ATR_SL_MULT, ATR_SL_MIN), ATR_SL_MAX)
            tp_pct = sl_pct * (ATR_TP_MULT / ATR_SL_MULT)
            print(f"  ATR: {atr_pct*100:.2f}%  \u2192 SL: {sl_pct*100:.2f}%  TP: {tp_pct*100:.2f}%")
        else:
            sl_pct, tp_pct = STOP_LOSS, TAKE_PROFIT
    else:
        sl_pct, tp_pct = STOP_LOSS, TAKE_PROFIT

    tp_price = round(round(fill_price * (1 + tp_pct) / tick) * tick, tick_prec)
    sl_price = round(round(fill_price * (1 - sl_pct) / tick) * tick, tick_prec)

    trade_partial = {
        "time":     datetime.now().isoformat(),
        "symbol":   symbol,
        "entry":    fill_price,
        "qty":      actual_qty,
        "capital":  capital,
        "order_id": order.get("orderId"),
        "oco_id":   None,
        "status":   "no_oco",
    }

    try:
        if TRAILING_DELTA > 0:
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
        print(f"  OCO placed \u2014 order list ID: {oco.get('orderListId')}")
    except Exception as oco_err:
        print(f"  \u2717 OCO failed after fill: {oco_err}")
        send_telegram(
            f"\U0001f6a8 *OCO FAILED \u2014 unprotected position*\n"
            f"`{symbol}` {actual_qty} bought @ `${fill_price:.4f}`\n"
            f"Place TP/SL manually. OCO error: `{str(oco_err)[:200]}`"
        )
        return order, None, trade_partial

    trade = {
        "time":            datetime.now().isoformat(),
        "symbol":          symbol,
        "entry":           fill_price,
        "tp":              tp_price,
        "sl":              sl_price,
        "qty":             actual_qty,
        "capital":         capital,
        "order_id":        order.get("orderId"),
        "oco_id":          oco.get("orderListId"),
        "status":          "open",
        "sl_pct":          sl_pct,
        "tp_pct":          tp_pct,
        "breakeven_moved": False,
        "trailing_stage":  0,
    }

    if PARTIAL_TP_ENABLED:
        try:
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
                print(f"  Partial TP1 skipped: qty {tp1_qty} < min_qty {min_qty} or tp1\u2265tp2")
        except Exception as tp1_err:
            print(f"  \u26a0 Partial TP1 placement failed (non-fatal): {tp1_err}")

    return order, oco, trade
