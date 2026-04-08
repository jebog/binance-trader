"""
Boot-time reconciliation between state.db and live Binance state.

Goal: detect risk-critical divergences before the scanner starts trading.

Divergences detected (v1):
  A) DB trade open/partial_tp/no_oco/partial_tp_no_oco, but no live spot balance
     on Binance for the base asset (manual sell, dust conversion, etc.)
  C) DB trade has oco_id, but the OCO order list no longer exists on Binance
     (unprotected position — user cancelled OCO manually, or it expired)

Behavior on detection: fail-loud. enforce_boot_gate() raises ReconcileError so
the scanner refuses to start. A Telegram alert with full details is sent first.
The user must inspect, fix the divergence (manually close, re-place OCO, or
mark the trade closed in DB), and restart.

Assets listed in RECONCILE_IGNORE_ASSETS are skipped — these are positions held
outside scanner control (manual buys, DCA, staking wrappers) and would otherwise
generate noise on every boot.

This module has no side effects beyond the network calls and Telegram alert.
It NEVER mutates state.db or places/cancels orders on Binance — auto-heal is
intentionally out of scope for v1.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any, Optional

from config import RECONCILE_ENABLED, RECONCILE_IGNORE_ASSETS
from trading.db import get_open_trades
from trading.http_client import signed_get
from trading.logger import logger
from trading.notify import send_telegram


# Statuses considered "live" — any of these in DB means the scanner expects a
# real position on Binance. partial_tp_no_oco is included because the position
# itself is real (only the OCO supervision is broken).
ACTIVE_STATUSES = ("open", "partial_tp", "no_oco", "partial_tp_no_oco")


class ReconcileError(RuntimeError):
    """Raised by enforce_boot_gate when divergences are found and the scanner
    must NOT proceed with normal trading. The message includes a human-readable
    summary of all divergences."""


@dataclass
class Divergence:
    """A single mismatch between state.db and Binance."""
    kind: str          # 'missing_position' (A) | 'missing_oco' (C)
    symbol: str
    trade_id: Optional[int]
    detail: str        # human-readable explanation


@dataclass
class ReconcileReport:
    """Aggregated reconciliation result."""
    ok: bool
    checked_trades: int
    skipped_trades: int     # ignored because base asset is in RECONCILE_IGNORE_ASSETS
    divergences: list[Divergence] = field(default_factory=list)
    error: Optional[str] = None  # populated only if API calls failed


def _base_asset(symbol: str) -> str:
    """Extract base asset from a USDC pair (ETHUSDC -> ETH). Falls back to
    stripping common quote suffixes for safety."""
    for quote in ("USDC", "USDT", "BUSD", "FDUSD"):
        if symbol.endswith(quote):
            return symbol[: -len(quote)]
    return symbol


def _fetch_binance_state() -> tuple[dict[str, float], set[str]]:
    """Return (balances_by_asset, open_oco_ids).

    balances_by_asset: {'ETH': 0.5, 'BNB': 0.66, ...} — only assets with non-zero
                      total (free + locked).
    open_oco_ids:     {'12345', '67890', ...} — string IDs of all OCO order lists
                      currently open across the account.

    Raises on any API failure — caller decides how to handle (we fail-loud).
    """
    account = signed_get("/api/v3/account", {})
    balances: dict[str, float] = {}
    for b in account.get("balances", []):
        total = float(b["free"]) + float(b["locked"])
        if total > 0:
            balances[b["asset"]] = total

    oco_lists = signed_get("/api/v3/openOrderList", {})
    open_oco_ids = {str(o["orderListId"]) for o in oco_lists}

    return balances, open_oco_ids


def reconcile_at_boot(
    conn: sqlite3.Connection,
    *,
    fetch_state=None,  # injected for tests; defaults to _fetch_binance_state
) -> ReconcileReport:
    """Check open DB trades against live Binance state. Pure function — no DB
    mutation, no Telegram alerts. Returns a ReconcileReport.

    fetch_state is injectable so tests can mock the Binance round-trip without
    monkey-patching signed_get globally. We look it up lazily so patching
    `trading.reconcile._fetch_binance_state` from tests works as expected."""
    if fetch_state is None:
        fetch_state = _fetch_binance_state
    rows = conn.execute(
        "SELECT * FROM trades WHERE status IN "
        "('open','partial_tp','no_oco','partial_tp_no_oco') "
        "ORDER BY time"
    ).fetchall()
    db_trades = [dict(r) for r in rows]

    if not db_trades:
        return ReconcileReport(ok=True, checked_trades=0, skipped_trades=0)

    try:
        balances, open_oco_ids = fetch_state()
    except Exception as e:
        # API failure: we don't know the truth — fail-loud rather than fail-open.
        # The scanner shouldn't trade blind.
        return ReconcileReport(
            ok=False,
            checked_trades=0,
            skipped_trades=0,
            error=f"Binance API error during reconcile: {e}",
        )

    divergences: list[Divergence] = []
    checked = 0
    skipped = 0
    ignore_set = set(RECONCILE_IGNORE_ASSETS)

    for trade in db_trades:
        symbol = trade["symbol"]
        base = _base_asset(symbol)
        if base in ignore_set:
            skipped += 1
            continue
        checked += 1

        # Check A: position must exist on Binance for the base asset.
        # We require qty ≥ 50% of recorded qty to allow for fee skim and rounding.
        recorded_qty = float(trade.get("qty") or 0)
        live_qty = balances.get(base, 0.0)
        if recorded_qty > 0 and live_qty < recorded_qty * 0.5:
            divergences.append(Divergence(
                kind="missing_position",
                symbol=symbol,
                trade_id=trade.get("id"),
                detail=(
                    f"DB expects {recorded_qty:.6f} {base} (status={trade['status']}) "
                    f"but live balance is only {live_qty:.6f} {base}. "
                    f"Possible manual sell or partial fill outside scanner."
                ),
            ))
            # Don't also fail OCO check — missing position implies the OCO is moot.
            continue

        # Check C: if trade has an oco_id, that OCO must still be open on Binance.
        # partial_tp_no_oco is intentionally exempt — it's a known unprotected state.
        oco_id = trade.get("oco_id")
        if (
            oco_id
            and trade["status"] != "partial_tp_no_oco"
            and str(oco_id) not in open_oco_ids
        ):
            divergences.append(Divergence(
                kind="missing_oco",
                symbol=symbol,
                trade_id=trade.get("id"),
                detail=(
                    f"DB trade has oco_id={oco_id} (status={trade['status']}) "
                    f"but no matching open OCO on Binance. "
                    f"Position is UNPROTECTED — no SL, no TP."
                ),
            ))

    return ReconcileReport(
        ok=(len(divergences) == 0),
        checked_trades=checked,
        skipped_trades=skipped,
        divergences=divergences,
    )


def format_report_telegram(report: ReconcileReport) -> str:
    """Render a ReconcileReport as a Markdown Telegram message."""
    if report.error:
        return (
            "🚨 *Reconcile FAILED — API error*\n"
            f"`{report.error}`\n\n"
            "Scanner refused to start. Check Binance connectivity and retry."
        )
    if report.ok:
        return (
            "✅ *Reconcile OK*\n"
            f"Checked: {report.checked_trades} trades · "
            f"Skipped (manual assets): {report.skipped_trades}"
        )

    lines = [
        "🚨 *Reconcile FAILED — divergences detected*",
        f"Checked: {report.checked_trades} · Skipped: {report.skipped_trades} · "
        f"Divergences: {len(report.divergences)}",
        "",
    ]
    for d in report.divergences:
        icon = "👻" if d.kind == "missing_position" else "🛑"
        tid = f"#{d.trade_id}" if d.trade_id else "?"
        lines.append(f"{icon} `{d.symbol}` trade {tid} — *{d.kind}*")
        lines.append(f"   {d.detail}")
        lines.append("")
    lines.append("Scanner will NOT start until divergences are resolved.")
    return "\n".join(lines)


def enforce_boot_gate(conn: sqlite3.Connection) -> None:
    """Boot-time entry point. Runs reconcile, sends Telegram report, raises
    ReconcileError if not OK. No-op if RECONCILE_ENABLED is False.

    Call from scanner.py main and tui.py on_mount BEFORE any trading logic."""
    if not RECONCILE_ENABLED:
        logger.info("reconcile: disabled via RECONCILE_ENABLED=false — skipping")
        return

    logger.info("reconcile: starting boot-time check")
    report = reconcile_at_boot(conn)
    msg = format_report_telegram(report)

    if report.ok:
        logger.info(
            f"reconcile: OK ({report.checked_trades} checked, "
            f"{report.skipped_trades} skipped)"
        )
        # OK case: only log, no Telegram (avoid noise on every boot)
        return

    # Failure case: log + Telegram + raise
    logger.error(f"reconcile: FAILED — {len(report.divergences)} divergences")
    for d in report.divergences:
        logger.error(f"  {d.kind} {d.symbol} trade={d.trade_id}: {d.detail}")
    try:
        send_telegram(msg)
    except Exception as e:
        logger.error(f"reconcile: telegram alert failed: {e}")

    raise ReconcileError(msg)
