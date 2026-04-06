"""
Unit tests for the DCA + staking accumulation layer.

All tests are offline — Binance API calls are mocked at the module level
where the names are looked up (trading.dca.*, trading.orders.*).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

import trading.dca
import trading.orders
import trading.staking
from trading.analytics import _compute_perf_stats, _pair_score
from trading.db import db_init, get_kv, insert_trade


def _mem_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db_init(conn)
    return conn


# ═════════════════════════════════════════════════════════════════════════════
# should_run_dca — schedule gating
# ═════════════════════════════════════════════════════════════════════════════

class TestShouldRunDca:
    def test_disabled_returns_false(self):
        conn = _mem_conn()
        with patch.object(trading.dca, "DCA_ENABLED", False):
            assert trading.dca.should_run_dca(conn) is False
        conn.close()

    def test_wrong_day_returns_false(self):
        """If today is not DCA_DAY_OF_WEEK, skip."""
        conn = _mem_conn()
        today = datetime.now().weekday()
        wrong_day = (today + 1) % 7
        with patch.object(trading.dca, "DCA_ENABLED", True), \
             patch.object(trading.dca, "DCA_DAY_OF_WEEK", wrong_day), \
             patch.object(trading.dca, "DCA_HOUR", 0):
            assert trading.dca.should_run_dca(conn) is False
        conn.close()

    def test_too_early_hour_returns_false(self):
        conn = _mem_conn()
        today = datetime.now().weekday()
        future_hour = 23  # scheduled at 23:00, current hour is earlier
        now_hour = datetime.now().hour
        if now_hour >= 23:
            pytest.skip("Current hour is 23 — can't test 'too early'")
        with patch.object(trading.dca, "DCA_ENABLED", True), \
             patch.object(trading.dca, "DCA_DAY_OF_WEEK", today), \
             patch.object(trading.dca, "DCA_HOUR", future_hour):
            assert trading.dca.should_run_dca(conn) is False
        conn.close()

    def test_recent_run_blocks(self):
        """Last run < 6 days ago → skip."""
        conn = _mem_conn()
        today = datetime.now().weekday()
        recent = (datetime.now() - timedelta(days=2)).isoformat()
        conn.execute("INSERT OR REPLACE INTO kv (key, value) VALUES ('last_dca_run', ?)", (recent,))
        conn.commit()
        with patch.object(trading.dca, "DCA_ENABLED", True), \
             patch.object(trading.dca, "DCA_DAY_OF_WEEK", today), \
             patch.object(trading.dca, "DCA_HOUR", 0):
            assert trading.dca.should_run_dca(conn) is False
        conn.close()

    def test_ready_to_run(self):
        """Right day, right hour, last run > 6 days ago → go."""
        conn = _mem_conn()
        today = datetime.now().weekday()
        old = (datetime.now() - timedelta(days=8)).isoformat()
        conn.execute("INSERT OR REPLACE INTO kv (key, value) VALUES ('last_dca_run', ?)", (old,))
        conn.commit()
        with patch.object(trading.dca, "DCA_ENABLED", True), \
             patch.object(trading.dca, "DCA_DAY_OF_WEEK", today), \
             patch.object(trading.dca, "DCA_HOUR", 0):
            assert trading.dca.should_run_dca(conn) is True
        conn.close()

    def test_corrupt_timestamp_allows_run(self):
        """Garbage last_dca_run value → fail-open, proceed."""
        conn = _mem_conn()
        today = datetime.now().weekday()
        conn.execute("INSERT OR REPLACE INTO kv (key, value) VALUES ('last_dca_run', 'garbage')")
        conn.commit()
        with patch.object(trading.dca, "DCA_ENABLED", True), \
             patch.object(trading.dca, "DCA_DAY_OF_WEEK", today), \
             patch.object(trading.dca, "DCA_HOUR", 0):
            assert trading.dca.should_run_dca(conn) is True
        conn.close()


# ═════════════════════════════════════════════════════════════════════════════
# place_dca_buy — execution + persistence
# ═════════════════════════════════════════════════════════════════════════════

class TestPlaceDcaBuy:
    EXCH_INFO = {"symbols": [{"filters": [
        {"filterType": "LOT_SIZE", "stepSize": "0.0001", "minQty": "0.0001"},
    ]}]}

    def test_insufficient_usdc_skips(self):
        conn = _mem_conn()
        with patch.object(trading.dca, "DCA_ENABLED", True), \
             patch.object(trading.dca, "DCA_AMOUNT_USDC", 40.0), \
             patch.object(trading.dca, "DCA_MIN_SCANNER_USDC", 200.0), \
             patch.object(trading.dca, "signed_get", return_value={"balances": [
                 {"asset": "USDC", "free": "100.0", "locked": "0"},  # only 100 < 240 required
             ]}), \
             patch.object(trading.dca, "signed_post") as mock_post, \
             patch.object(trading.dca, "send_telegram", return_value=None):
            result = trading.dca.place_dca_buy(conn)
        assert result is None
        mock_post.assert_not_called()
        conn.close()

    def test_successful_buy_records_trade(self):
        conn = _mem_conn()
        with patch.object(trading.dca, "DCA_ENABLED", True), \
             patch.object(trading.dca, "STAKING_ENABLED", False), \
             patch.object(trading.dca, "DCA_AMOUNT_USDC", 40.0), \
             patch.object(trading.dca, "DCA_MIN_SCANNER_USDC", 200.0), \
             patch.object(trading.dca, "DCA_TARGET_PAIR", "ETHUSDC"), \
             patch.object(trading.dca, "DCA_TARGET_ASSET", "ETH"), \
             patch.object(trading.dca, "signed_get", return_value={"balances": [
                 {"asset": "USDC", "free": "500.0", "locked": "0"},
             ]}), \
             patch.object(trading.dca, "get", side_effect=[
                 {"price": "2000.0"},          # ticker
                 self.EXCH_INFO,               # exchange info
             ]), \
             patch.object(trading.dca, "signed_post", return_value={
                 "orderId": 12345,
                 "executedQty": "0.02",
                 "cummulativeQuoteQty": "40.0",
                 "fills": [{"price": "2000.0"}],
             }), \
             patch.object(trading.dca, "send_telegram", return_value=None):
            result = trading.dca.place_dca_buy(conn)

        assert result is not None
        assert result["signal_strength"] == "DCA"
        assert result["status"] == "dca_hold"
        assert result["qty"] == pytest.approx(0.02)
        assert result["capital"] == pytest.approx(40.0)

        # Persisted in trades table
        row = conn.execute(
            "SELECT signal_strength, status, qty FROM trades WHERE order_id = ?",
            ("12345",),
        ).fetchone()
        assert row is not None
        assert row["signal_strength"] == "DCA"
        assert row["status"] == "dca_hold"

        # last_dca_run sentinel set
        assert get_kv(conn, "last_dca_run") is not None
        conn.close()

    def test_zero_fill_aborts(self):
        conn = _mem_conn()
        with patch.object(trading.dca, "DCA_ENABLED", True), \
             patch.object(trading.dca, "DCA_AMOUNT_USDC", 40.0), \
             patch.object(trading.dca, "DCA_MIN_SCANNER_USDC", 200.0), \
             patch.object(trading.dca, "signed_get", return_value={"balances": [
                 {"asset": "USDC", "free": "500.0", "locked": "0"},
             ]}), \
             patch.object(trading.dca, "get", side_effect=[
                 {"price": "2000.0"},
                 self.EXCH_INFO,
             ]), \
             patch.object(trading.dca, "signed_post", return_value={
                 "orderId": 9, "executedQty": "0", "cummulativeQuoteQty": "0",
             }), \
             patch.object(trading.dca, "send_telegram", return_value=None):
            result = trading.dca.place_dca_buy(conn)
        assert result is None
        assert get_kv(conn, "last_dca_run") in (None, "")
        conn.close()


# ═════════════════════════════════════════════════════════════════════════════
# get_dca_stats — weighted averaging
# ═════════════════════════════════════════════════════════════════════════════

class TestDcaStats:
    def _insert(self, conn, qty, capital, entry, when="2026-01-01T10:00:00"):
        insert_trade(conn, {
            "order_id": f"dca-{qty}-{capital}",
            "symbol": "ETHUSDC",
            "time": when,
            "entry": entry,
            "tp": 0.0,
            "sl": 0.0,
            "qty": qty,
            "capital": capital,
            "oco_id": None,
            "status": "dca_hold",
            "sl_pct": 0.0,
            "tp_pct": 0.0,
            "breakeven_moved": False,
            "trailing_stage": 0,
            "signal_strength": "DCA",
        })

    def test_empty(self):
        conn = _mem_conn()
        with patch.object(trading.dca, "get", return_value={"price": "2000.0"}):
            stats = trading.dca.get_dca_stats(conn)
        assert stats["n_buys"] == 0
        assert stats["total_qty"] == 0.0
        conn.close()

    def test_weighted_avg_and_progress(self):
        conn = _mem_conn()
        # Two buys: 0.02 ETH at $2000 ($40) and 0.01 ETH at $4000 ($40)
        # Weighted avg = 80 / 0.03 = $2666.67
        self._insert(conn, 0.02, 40.0, 2000.0, "2026-01-01T10:00:00")
        self._insert(conn, 0.01, 40.0, 4000.0, "2026-01-08T10:00:00")

        with patch.object(trading.dca, "get", return_value={"price": "3000.0"}), \
             patch.object(trading.dca, "DCA_TARGET_QTY", 1.0):
            stats = trading.dca.get_dca_stats(conn)

        assert stats["n_buys"] == 2
        assert stats["total_qty"] == pytest.approx(0.03)
        assert stats["total_invested"] == pytest.approx(80.0)
        assert stats["avg_entry"] == pytest.approx(80.0 / 0.03)
        assert stats["current_value"] == pytest.approx(0.03 * 3000.0)  # 90
        assert stats["pnl_usdc"] == pytest.approx(10.0)  # 90 - 80
        assert stats["pnl_pct"] == pytest.approx(12.5)
        assert stats["progress_pct"] == pytest.approx(3.0)  # 0.03 / 1.0
        conn.close()


# ═════════════════════════════════════════════════════════════════════════════
# Reserve management
# ═════════════════════════════════════════════════════════════════════════════

class TestReserve:
    def test_initialize_and_read(self):
        conn = _mem_conn()
        with patch.object(trading.dca, "DCA_AMOUNT_USDC", 40.0), \
             patch.object(trading.dca, "DCA_RESERVE_MULT", 10):
            reserve = trading.dca.initialize_dca_reserve(conn)
            assert reserve == 400.0
            assert trading.dca.get_dca_reserve(conn) == 400.0
        conn.close()

    def test_get_reserve_unset_returns_zero(self):
        conn = _mem_conn()
        assert trading.dca.get_dca_reserve(conn) == 0.0
        conn.close()


# ═════════════════════════════════════════════════════════════════════════════
# DCA trades excluded from scanner metrics
# ═════════════════════════════════════════════════════════════════════════════

class TestMetricExclusion:
    def _closed(self, sym, pnl, strength, exit_time=None):
        return {
            "symbol": sym,
            "status": "tp_hit" if pnl > 0 else "sl_hit",
            "pnl_pct": pnl,
            "exit_time": exit_time or datetime.now().isoformat(),
            "capital": 100.0,
            "signal_strength": strength,
        }

    def test_perf_stats_ignores_dca(self):
        trades = [
            self._closed("ETHUSDC", +5.0, "STRONG"),
            self._closed("ETHUSDC", -2.0, "MODERATE"),
            # DCA 'closed' trades should NEVER exist (they stay dca_hold), but
            # even if one slips through with a status, filter must reject it.
            self._closed("ETHUSDC", +1000.0, "DCA"),
        ]
        stats = _compute_perf_stats(trades)
        assert stats["count"] == 2  # DCA excluded
        assert stats["win_rate"] == 0.5

    def test_pair_score_ignores_dca(self):
        trades = [
            self._closed("ETHUSDC", +5.0, "STRONG"),
            self._closed("ETHUSDC", +5.0, "STRONG"),
            self._closed("ETHUSDC", -2.0, "MODERATE"),
            self._closed("ETHUSDC", +9999.0, "DCA"),  # must not inflate score
        ]
        score_with = _pair_score("ETHUSDC", trades)
        score_without = _pair_score("ETHUSDC", trades[:3])
        assert score_with == score_without


# ═════════════════════════════════════════════════════════════════════════════
# Reserve guard in place_buy_order
# ═════════════════════════════════════════════════════════════════════════════

class TestReserveGuard:
    def test_buy_blocked_when_would_breach_reserve(self):
        """Scanner trade that would drop free USDC below reserve → ValueError."""
        with patch.object(trading.orders, "signed_get", return_value={"balances": [
                 {"asset": "USDC", "free": "500.0", "locked": "0"},
             ]}), \
             patch("config.DCA_ENABLED", True, create=True), \
             patch("trading.dca.get_dca_reserve", return_value=400.0):
            # Buying $200 from $500 leaves $300 < reserve $400 → must raise
            with pytest.raises(ValueError, match="DCA reserve guard"):
                trading.orders.place_buy_order("ETHUSDC", 200.0, 2000.0, None)

    def test_buy_allowed_when_reserve_satisfied(self):
        """Sufficient headroom → guard passes (test uses get mock to fail after guard)."""
        # Guard passes; we expect the next step (exchangeInfo fetch) to be reached.
        # Mock `get` to raise a unique marker so we know the guard allowed through.
        with patch.object(trading.orders, "signed_get", return_value={"balances": [
                 {"asset": "USDC", "free": "1000.0", "locked": "0"},
             ]}), \
             patch("config.DCA_ENABLED", True, create=True), \
             patch("trading.dca.get_dca_reserve", return_value=400.0), \
             patch.object(trading.orders, "get", side_effect=RuntimeError("guard-passed")):
            with pytest.raises(RuntimeError, match="guard-passed"):
                trading.orders.place_buy_order("ETHUSDC", 200.0, 2000.0, None)

    def test_buy_allowed_when_dca_disabled(self):
        """DCA_ENABLED=False → guard is a no-op, no balance fetch."""
        with patch("config.DCA_ENABLED", False, create=True), \
             patch.object(trading.orders, "signed_get") as mock_sg, \
             patch.object(trading.orders, "get", side_effect=RuntimeError("guard-passed")):
            with pytest.raises(RuntimeError, match="guard-passed"):
                trading.orders.place_buy_order("ETHUSDC", 200.0, 2000.0, None)
            mock_sg.assert_not_called()


# ═════════════════════════════════════════════════════════════════════════════
# trading.staking — fail-soft guarantees
# ═════════════════════════════════════════════════════════════════════════════

class TestStaking:
    def test_disabled_returns_none(self):
        with patch.object(trading.staking, "STAKING_ENABLED", False), \
             patch.object(trading.staking, "signed_post") as mock_post:
            assert trading.staking.stake_eth(0.5) is None
            mock_post.assert_not_called()

    def test_zero_qty_returns_none(self):
        with patch.object(trading.staking, "STAKING_ENABLED", True), \
             patch.object(trading.staking, "signed_post") as mock_post:
            assert trading.staking.stake_eth(0) is None
            mock_post.assert_not_called()

    def test_below_binance_minimum_skips(self):
        """qty < 0.0001 ETH → skip without hitting the API."""
        with patch.object(trading.staking, "STAKING_ENABLED", True), \
             patch.object(trading.staking, "signed_post") as mock_post:
            assert trading.staking.stake_eth(0.00005) is None
            mock_post.assert_not_called()

    def test_api_failure_is_fail_soft(self):
        """Stake API exception → returns None, does NOT raise, sends warning."""
        with patch.object(trading.staking, "STAKING_ENABLED", True), \
             patch.object(trading.staking, "signed_post", side_effect=Exception("API down")), \
             patch.object(trading.staking, "send_telegram", return_value=None) as mock_tg:
            result = trading.staking.stake_eth(0.5)
        assert result is None
        mock_tg.assert_called_once()
        assert "failed" in mock_tg.call_args[0][0].lower()

    def test_successful_stake(self):
        with patch.object(trading.staking, "STAKING_ENABLED", True), \
             patch.object(trading.staking, "signed_post",
                          return_value={"success": True}) as mock_post, \
             patch.object(trading.staking, "send_telegram", return_value=None):
            result = trading.staking.stake_eth(0.5)
        assert result == {"success": True}
        # Verify v2 endpoint is called (review flagged v1 as stale)
        assert "/sapi/v2/eth-staking/eth/stake" in mock_post.call_args[0][0]

    def test_get_beth_balance_sums_free_and_locked(self):
        with patch.object(trading.staking, "signed_get", return_value={"balances": [
                 {"asset": "BETH", "free": "0.3", "locked": "0.1"},
             ]}), \
             patch.object(trading.staking, "STAKING_ASSET", "BETH"):
            assert trading.staking.get_beth_balance() == pytest.approx(0.4)

    def test_get_beth_balance_missing_returns_zero(self):
        with patch.object(trading.staking, "signed_get", return_value={"balances": []}):
            assert trading.staking.get_beth_balance() == 0.0

    def test_get_beth_balance_api_error_returns_zero(self):
        with patch.object(trading.staking, "signed_get", side_effect=Exception("x")):
            assert trading.staking.get_beth_balance() == 0.0


# ═════════════════════════════════════════════════════════════════════════════
# Insufficient-balance debounce + reserve auto-init
# ═════════════════════════════════════════════════════════════════════════════

class TestDebounceAndAutoInit:
    def test_insufficient_balance_debounces_telegram(self):
        """Two consecutive low-balance calls → only one Telegram alert."""
        conn = _mem_conn()
        with patch.object(trading.dca, "DCA_ENABLED", True), \
             patch.object(trading.dca, "DCA_AMOUNT_USDC", 40.0), \
             patch.object(trading.dca, "DCA_MIN_SCANNER_USDC", 200.0), \
             patch.object(trading.dca, "signed_get", return_value={"balances": [
                 {"asset": "USDC", "free": "100.0", "locked": "0"},
             ]}), \
             patch.object(trading.dca, "send_telegram", return_value=None) as mock_tg:
            trading.dca.place_dca_buy(conn)
            trading.dca.place_dca_buy(conn)
            trading.dca.place_dca_buy(conn)
        # First call fires, next two suppressed by < 1h debounce
        assert mock_tg.call_count == 1
        conn.close()

    def test_run_dca_check_auto_initializes_reserve(self):
        """First call with unset reserve → auto-init to DCA_AMOUNT × RESERVE_MULT."""
        conn = _mem_conn()
        with patch.object(trading.dca, "DCA_ENABLED", True), \
             patch.object(trading.dca, "DCA_AMOUNT_USDC", 40.0), \
             patch.object(trading.dca, "DCA_RESERVE_MULT", 10), \
             patch.object(trading.dca, "should_run_dca", return_value=False):
            assert trading.dca.get_dca_reserve(conn) == 0.0
            trading.dca.run_dca_check(conn)
            assert trading.dca.get_dca_reserve(conn) == 400.0
        conn.close()

    def test_run_dca_check_preserves_existing_reserve(self):
        """Existing non-zero reserve is NOT overwritten."""
        conn = _mem_conn()
        from trading.db import set_kv
        set_kv(conn, "dca_reserved_usdc", "1234.56")
        with patch.object(trading.dca, "DCA_ENABLED", True), \
             patch.object(trading.dca, "DCA_AMOUNT_USDC", 40.0), \
             patch.object(trading.dca, "DCA_RESERVE_MULT", 10), \
             patch.object(trading.dca, "should_run_dca", return_value=False):
            trading.dca.run_dca_check(conn)
            assert trading.dca.get_dca_reserve(conn) == 1234.56
        conn.close()
