"""
Unit tests for trading/reconcile.py — boot-time Binance↔DB reconciliation.

All tests use in-memory SQLite and inject a fake fetch_state callable so no
network calls are made. Telegram is mocked.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from unittest.mock import patch

import pytest

import scanner  # for db_init access via the facade
from trading.reconcile import (
    Divergence,
    ReconcileError,
    ReconcileReport,
    _base_asset,
    enforce_boot_gate,
    format_report_telegram,
    reconcile_at_boot,
)


# ── Fixtures ─────────────────────────────────────────────────────────────────

def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", timeout=5.0)
    conn.row_factory = sqlite3.Row
    scanner.db_init(conn)
    return conn


def _insert_trade(conn, *, symbol="SOLUSDC", status="open", oco_id="999",
                  qty=2.0, order_id="o1") -> int:
    """Insert a minimal open trade. Returns the trade id."""
    conn.execute(
        "INSERT INTO trades "
        "(order_id, symbol, time, entry, tp, sl, qty, capital, oco_id, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (order_id, symbol, datetime.now().isoformat(),
         100.0, 105.0, 97.0, qty, 200.0, oco_id, status),
    )
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


# ── _base_asset ──────────────────────────────────────────────────────────────

class TestBaseAsset:
    def test_strips_usdc(self):
        assert _base_asset("ETHUSDC") == "ETH"

    def test_strips_usdt(self):
        assert _base_asset("BNBUSDT") == "BNB"

    def test_unknown_quote_returns_input(self):
        assert _base_asset("WEIRDPAIR") == "WEIRDPAIR"


# ── reconcile_at_boot ────────────────────────────────────────────────────────

class TestReconcileAtBoot:
    def test_no_open_trades_returns_ok_immediately(self):
        conn = _make_conn()
        called = []

        def fake_fetch():
            called.append(True)
            return {}, set()

        report = reconcile_at_boot(conn, fetch_state=fake_fetch)
        assert report.ok is True
        assert report.checked_trades == 0
        assert report.skipped_trades == 0
        # Optimization: should not even call Binance when there's nothing to check
        assert called == []

    def test_all_match_returns_ok(self):
        conn = _make_conn()
        _insert_trade(conn, symbol="SOLUSDC", oco_id="999", qty=2.0)

        def fake_fetch():
            return {"SOL": 2.0, "USDC": 1000.0}, {"999"}

        report = reconcile_at_boot(conn, fetch_state=fake_fetch)
        assert report.ok is True
        assert report.checked_trades == 1
        assert report.divergences == []

    def test_phantom_db_trade_no_position(self):
        """Type A: DB says open, Binance has 0 of the asset."""
        conn = _make_conn()
        tid = _insert_trade(conn, symbol="SOLUSDC", oco_id="999", qty=2.0)

        def fake_fetch():
            return {"USDC": 1000.0}, {"999"}  # no SOL at all

        report = reconcile_at_boot(conn, fetch_state=fake_fetch)
        assert report.ok is False
        assert len(report.divergences) == 1
        d = report.divergences[0]
        assert d.kind == "missing_position"
        assert d.symbol == "SOLUSDC"
        assert d.trade_id == tid
        assert "SOL" in d.detail

    def test_partial_balance_above_50pct_is_ok(self):
        """Fee skim and rounding should not trigger a false positive."""
        conn = _make_conn()
        _insert_trade(conn, symbol="SOLUSDC", oco_id="999", qty=2.0)

        def fake_fetch():
            # 1.998 SOL after 0.1% fee — well above 50% threshold
            return {"SOL": 1.998}, {"999"}

        report = reconcile_at_boot(conn, fetch_state=fake_fetch)
        assert report.ok is True

    def test_balance_below_50pct_triggers_missing_position(self):
        conn = _make_conn()
        _insert_trade(conn, symbol="SOLUSDC", oco_id="999", qty=2.0)

        def fake_fetch():
            return {"SOL": 0.4}, {"999"}  # only 20% left

        report = reconcile_at_boot(conn, fetch_state=fake_fetch)
        assert report.ok is False
        assert report.divergences[0].kind == "missing_position"

    def test_missing_oco(self):
        """Type C: position exists but OCO is gone."""
        conn = _make_conn()
        tid = _insert_trade(conn, symbol="SOLUSDC", oco_id="999", qty=2.0)

        def fake_fetch():
            return {"SOL": 2.0}, set()  # no OCOs at all

        report = reconcile_at_boot(conn, fetch_state=fake_fetch)
        assert report.ok is False
        assert len(report.divergences) == 1
        d = report.divergences[0]
        assert d.kind == "missing_oco"
        assert d.trade_id == tid
        assert "999" in d.detail
        assert "UNPROTECTED" in d.detail

    def test_missing_position_skips_oco_check(self):
        """If position is gone, we shouldn't double-report missing OCO."""
        conn = _make_conn()
        _insert_trade(conn, symbol="SOLUSDC", oco_id="999", qty=2.0)

        def fake_fetch():
            return {}, set()  # no balance, no OCO

        report = reconcile_at_boot(conn, fetch_state=fake_fetch)
        assert len(report.divergences) == 1
        assert report.divergences[0].kind == "missing_position"

    def test_ignored_asset_skipped(self):
        """Trades on RECONCILE_IGNORE_ASSETS bases must not be checked."""
        conn = _make_conn()
        # BNB is in the default ignore list
        _insert_trade(conn, symbol="BNBUSDC", oco_id="999", qty=1.0)

        def fake_fetch():
            return {}, set()  # would be a divergence if checked

        with patch("trading.reconcile.RECONCILE_IGNORE_ASSETS", ["BNB"]):
            report = reconcile_at_boot(conn, fetch_state=fake_fetch)
        assert report.ok is True
        assert report.checked_trades == 0
        assert report.skipped_trades == 1

    def test_no_oco_status_only_checks_position(self):
        """Trades in 'no_oco' status have no oco_id supervision — only check
        that the position still exists."""
        conn = _make_conn()
        _insert_trade(conn, symbol="SOLUSDC", oco_id=None, qty=2.0,
                      status="no_oco")

        def fake_fetch():
            return {"SOL": 2.0}, set()  # no OCO is fine for no_oco status

        report = reconcile_at_boot(conn, fetch_state=fake_fetch)
        assert report.ok is True

    def test_partial_tp_no_oco_is_exempt_from_oco_check(self):
        """partial_tp_no_oco is a known unprotected state — don't re-flag it."""
        conn = _make_conn()
        _insert_trade(conn, symbol="SOLUSDC", oco_id="999", qty=2.0,
                      status="partial_tp_no_oco")

        def fake_fetch():
            return {"SOL": 2.0}, set()  # OCO 999 is gone, but status is exempt

        report = reconcile_at_boot(conn, fetch_state=fake_fetch)
        assert report.ok is True

    def test_multiple_divergences(self):
        conn = _make_conn()
        _insert_trade(conn, symbol="SOLUSDC", oco_id="111", qty=2.0,
                      order_id="a")
        _insert_trade(conn, symbol="ADAUSDC", oco_id="222", qty=100.0,
                      order_id="b")

        def fake_fetch():
            # SOL gone, ADA position OK but OCO missing
            return {"ADA": 100.0}, set()

        # ADA is in default ignore list — patch to empty for this test
        with patch("trading.reconcile.RECONCILE_IGNORE_ASSETS", []):
            report = reconcile_at_boot(conn, fetch_state=fake_fetch)
        assert report.ok is False
        assert len(report.divergences) == 2
        kinds = {d.kind for d in report.divergences}
        assert kinds == {"missing_position", "missing_oco"}

    def test_api_error_returns_failed_report(self):
        conn = _make_conn()
        _insert_trade(conn)

        def fake_fetch():
            raise RuntimeError("Binance HTTP 500")

        # ETH is in default ignore list, use a non-ignored symbol
        conn.execute("DELETE FROM trades")
        _insert_trade(conn, symbol="SOLUSDC")

        report = reconcile_at_boot(conn, fetch_state=fake_fetch)
        assert report.ok is False
        assert report.error is not None
        assert "Binance HTTP 500" in report.error
        assert report.divergences == []


# ── format_report_telegram ───────────────────────────────────────────────────

class TestFormatReport:
    def test_ok_message(self):
        report = ReconcileReport(ok=True, checked_trades=3, skipped_trades=2)
        msg = format_report_telegram(report)
        assert "OK" in msg
        assert "3" in msg

    def test_failure_message_lists_divergences(self):
        report = ReconcileReport(
            ok=False,
            checked_trades=2,
            skipped_trades=0,
            divergences=[
                Divergence("missing_position", "SOLUSDC", 42, "no SOL"),
                Divergence("missing_oco", "ADAUSDC", 43, "OCO gone"),
            ],
        )
        msg = format_report_telegram(report)
        assert "FAILED" in msg
        assert "SOLUSDC" in msg
        assert "ADAUSDC" in msg
        assert "missing_position" in msg
        assert "missing_oco" in msg
        assert "will NOT start" in msg

    def test_api_error_message(self):
        report = ReconcileReport(
            ok=False, checked_trades=0, skipped_trades=0,
            error="connection refused",
        )
        msg = format_report_telegram(report)
        assert "API error" in msg
        assert "connection refused" in msg


# ── enforce_boot_gate ────────────────────────────────────────────────────────

class TestEnforceBootGate:
    def test_disabled_is_noop(self):
        conn = _make_conn()
        _insert_trade(conn, symbol="SOLUSDC")  # would normally fail
        with patch("trading.reconcile.RECONCILE_ENABLED", False):
            enforce_boot_gate(conn)  # should not raise

    def test_ok_does_not_raise_or_send_telegram(self):
        conn = _make_conn()
        # No open trades → OK
        with patch("trading.reconcile.send_telegram") as mock_tg:
            enforce_boot_gate(conn)
        mock_tg.assert_not_called()

    def test_failure_raises_and_sends_telegram(self):
        conn = _make_conn()
        _insert_trade(conn, symbol="SOLUSDC", oco_id="999", qty=2.0)

        def fake_fetch():
            return {}, set()

        with patch("trading.reconcile._fetch_binance_state", side_effect=fake_fetch), \
             patch("trading.reconcile.send_telegram") as mock_tg, \
             patch("trading.reconcile.RECONCILE_IGNORE_ASSETS", []):
            with pytest.raises(ReconcileError) as exc_info:
                enforce_boot_gate(conn)

        assert "FAILED" in str(exc_info.value)
        mock_tg.assert_called_once()

    def test_telegram_failure_does_not_swallow_reconcile_error(self):
        conn = _make_conn()
        _insert_trade(conn, symbol="SOLUSDC", oco_id="999", qty=2.0)

        def fake_fetch():
            return {}, set()

        with patch("trading.reconcile._fetch_binance_state", side_effect=fake_fetch), \
             patch("trading.reconcile.send_telegram", side_effect=Exception("tg down")), \
             patch("trading.reconcile.RECONCILE_IGNORE_ASSETS", []):
            with pytest.raises(ReconcileError):
                enforce_boot_gate(conn)
