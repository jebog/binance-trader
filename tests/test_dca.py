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


class _NoCloseConn:
    """Proxy wrapper around a sqlite3.Connection that turns close() into a no-op.

    Used in tests where the code-under-test closes the conn in a `finally`
    block but we still need to inspect the DB afterward.
    """
    def __init__(self, real: sqlite3.Connection) -> None:
        self._real = real

    def __getattr__(self, name):
        return getattr(self._real, name)

    def close(self) -> None:  # no-op
        return None


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
        # Verify 4-decimal precision (Binance spec)
        assert mock_post.call_args[0][1]["amount"] == "0.5000"

    def test_amount_truncated_to_4_decimals(self):
        """Binance enforces 4-decimal precision — truncate (not round) so
        we never stake more than we actually hold."""
        with patch.object(trading.staking, "STAKING_ENABLED", True), \
             patch.object(trading.staking, "signed_post",
                          return_value={"success": True}) as mock_post, \
             patch.object(trading.staking, "send_telegram", return_value=None):
            # 0.12347 rounded-to-4dp = 0.1235 (MORE than held → rejection)
            # 0.12347 truncated-to-4dp = 0.1234 (safe)
            trading.staking.stake_eth(0.12347)
        assert mock_post.call_args[0][1]["amount"] == "0.1234"

    def test_amount_truncates_below_minimum_skips(self):
        """qty that truncates below 0.0001 → skip without hitting API."""
        with patch.object(trading.staking, "STAKING_ENABLED", True), \
             patch.object(trading.staking, "signed_post") as mock_post:
            assert trading.staking.stake_eth(0.00009) is None
            mock_post.assert_not_called()

    def test_get_beth_balance_sums_free_and_locked(self):
        # Legacy BETH path: get_staked_eth() checks BETH in spot balances
        # directly by hardcoded asset name (no longer looks at STAKING_ASSET).
        # db_connect mocked to prevent cache-layer leakage from real state.db.
        conn = _NoCloseConn(_mem_conn())
        with patch.object(trading.staking, "signed_get", return_value={"balances": [
                 {"asset": "BETH", "free": "0.3", "locked": "0.1"},
             ]}), \
             patch.object(trading.staking, "db_connect", return_value=conn):
            assert trading.staking.get_beth_balance() == pytest.approx(0.4)

    def test_get_beth_balance_missing_returns_zero(self):
        conn = _NoCloseConn(_mem_conn())
        with patch.object(trading.staking, "signed_get", return_value={"balances": []}), \
             patch.object(trading.staking, "db_connect", return_value=conn):
            assert trading.staking.get_beth_balance() == 0.0

    def test_get_beth_balance_api_error_returns_zero(self):
        conn = _NoCloseConn(_mem_conn())
        with patch.object(trading.staking, "signed_get", side_effect=Exception("x")), \
             patch.object(trading.staking, "db_connect", return_value=conn):
            assert trading.staking.get_beth_balance() == 0.0


# ═════════════════════════════════════════════════════════════════════════════
# trading.staking — WBETH exchange rate + cross-location resolution
# ═════════════════════════════════════════════════════════════════════════════

class TestStakedEthResolution:
    """get_staked_eth() must resolve WBETH + LD-prefix balances, not just BETH.

    These tests stub `db_connect` in trading.staking to return an in-memory
    SQLite connection so the real state.db is never touched by unit tests.
    """

    def _patched_conn(self):
        """Return an in-memory SQLite conn with the schema applied."""
        return _mem_conn()

    def test_holding_in_eth_from_staking_account(self):
        """Primary path: /sapi/v2/eth-staking/account.holdingInETH."""
        conn = _mem_conn()

        def sg(url, params=None):
            if "eth-staking/account" in url:
                return {
                    "holdingInETH": "0.0183",
                    "holdings": {"wbethAmount": "0.01671", "bethAmount": "0"},
                }
            if "/api/v3/account" in url:
                return {"balances": []}
            if "rateHistory" in url:
                return {"rows": [
                    {"exchangeRate": "1.0948", "annualPercentageRate": "0.0256"}
                ]}
            return {}

        with patch.object(trading.staking, "signed_get", side_effect=sg), \
             patch.object(trading.staking, "db_connect", return_value=conn):
            staked = trading.staking.get_staked_eth()

        assert staked["holdingInETH"] == pytest.approx(0.0183)
        assert staked["total_eth"] == pytest.approx(0.0183)
        assert staked["exchange_rate"] == pytest.approx(1.0948)
        conn.close()

    def test_ldwbeth_resolved_with_exchange_rate(self):
        """LDWBETH (Simple Earn) is converted via exchange rate when staking
        endpoint returns zero holdingInETH."""
        conn = _mem_conn()

        def sg(url, params=None):
            if "eth-staking/account" in url:
                return {"holdingInETH": "0"}  # no staking account position
            if "/api/v3/account" in url:
                return {"balances": [
                    {"asset": "LDWBETH", "free": "0.01671066", "locked": "0"},
                ]}
            if "rateHistory" in url:
                return {"rows": [{"exchangeRate": "1.0948"}]}
            return {}

        with patch.object(trading.staking, "signed_get", side_effect=sg), \
             patch.object(trading.staking, "db_connect", return_value=conn):
            staked = trading.staking.get_staked_eth()

        assert staked["spot_ldwbeth"] == pytest.approx(0.01671066)
        assert staked["holdingInETH"] == 0.0
        # 0.01671066 × 1.0948 = 0.01829483 ETH
        assert staked["total_eth"] == pytest.approx(0.01671066 * 1.0948)
        conn.close()

    def test_ldbeth_legacy_one_to_one(self):
        """LDBETH (legacy Simple Earn BETH) stays 1:1 with ETH."""
        conn = _mem_conn()

        def sg(url, params=None):
            if "eth-staking/account" in url:
                return {"holdingInETH": "0"}
            if "/api/v3/account" in url:
                return {"balances": [
                    {"asset": "LDBETH", "free": "0.5", "locked": "0"},
                ]}
            if "rateHistory" in url:
                return {"rows": [{"exchangeRate": "1.0948"}]}
            return {}

        with patch.object(trading.staking, "signed_get", side_effect=sg), \
             patch.object(trading.staking, "db_connect", return_value=conn):
            staked = trading.staking.get_staked_eth()

        assert staked["spot_ldbeth"] == pytest.approx(0.5)
        # LDBETH is summed 1:1, NOT scaled by the WBETH rate
        assert staked["total_eth"] == pytest.approx(0.5)
        conn.close()

    def test_holding_in_eth_plus_legacy_beth_both_counted(self):
        """Legacy spot BETH must be added to holdingInETH (separate products)."""
        conn = _mem_conn()

        def sg(url, params=None):
            if "eth-staking/account" in url:
                return {"holdingInETH": "0.1"}
            if "/api/v3/account" in url:
                return {"balances": [
                    {"asset": "BETH", "free": "0.05", "locked": "0"},
                ]}
            if "rateHistory" in url:
                return {"rows": [{"exchangeRate": "1.0948"}]}
            return {}

        with patch.object(trading.staking, "signed_get", side_effect=sg), \
             patch.object(trading.staking, "db_connect", return_value=conn):
            staked = trading.staking.get_staked_eth()

        # holdingInETH (0.10) + spot_beth (0.05) = 0.15 total
        assert staked["total_eth"] == pytest.approx(0.15)
        conn.close()

    def test_all_failures_return_zero(self):
        """Both endpoints raise → fail-soft zero values."""
        conn = _mem_conn()

        with patch.object(trading.staking, "signed_get",
                          side_effect=Exception("network down")), \
             patch.object(trading.staking, "db_connect", return_value=conn):
            staked = trading.staking.get_staked_eth()

        assert staked["total_eth"] == 0.0
        assert staked["holdingInETH"] == 0.0
        assert staked["exchange_rate"] == 1.0  # fallback
        conn.close()


class TestWbethExchangeRate:
    """get_wbeth_exchange_rate() caches 1h in state.db, falls back gracefully."""

    def test_fresh_cache_skips_api_call(self):
        """Cache < 1h old → return cached value without API call."""
        from trading.db import set_wbeth_rate_cache
        conn = _mem_conn()
        set_wbeth_rate_cache(conn, 1.0948, 0.0256)

        with patch.object(trading.staking, "db_connect", return_value=conn), \
             patch.object(trading.staking, "signed_get") as mock_sg:
            rate = trading.staking.get_wbeth_exchange_rate()

        assert rate == pytest.approx(1.0948)
        mock_sg.assert_not_called()
        conn.close()

    def test_stale_cache_refetched(self):
        """Cache > 1h old → fetch fresh, update cache, return new value."""
        real = _mem_conn()
        # Proxy prevents staking.get_wbeth_exchange_rate()'s finally-block
        # from closing the test conn before we inspect the cache row.
        conn = _NoCloseConn(real)

        stale_ts = (datetime.now() - timedelta(hours=2)).isoformat()
        real.execute(
            "INSERT INTO wbeth_rate_cache (id, exchange_rate, apr, ts) "
            "VALUES (1, 1.05, 0.02, ?)",
            (stale_ts,),
        )
        real.commit()

        with patch.object(trading.staking, "db_connect", return_value=conn), \
             patch.object(trading.staking, "signed_get", return_value={
                 "rows": [{"exchangeRate": "1.0948",
                           "annualPercentageRate": "0.0256"}]
             }):
            rate = trading.staking.get_wbeth_exchange_rate()

        assert rate == pytest.approx(1.0948)
        # Cache should now be refreshed
        row = real.execute(
            "SELECT exchange_rate FROM wbeth_rate_cache WHERE id = 1"
        ).fetchone()
        assert float(row[0]) == pytest.approx(1.0948)
        real.close()

    def test_api_failure_falls_back_to_stale_cache(self):
        """API unreachable but stale cache exists → return stale, log warning."""
        conn = _mem_conn()
        stale_ts = (datetime.now() - timedelta(hours=5)).isoformat()
        conn.execute(
            "INSERT INTO wbeth_rate_cache (id, exchange_rate, apr, ts) "
            "VALUES (1, 1.07, 0.025, ?)",
            (stale_ts,),
        )
        conn.commit()

        with patch.object(trading.staking, "db_connect", return_value=conn), \
             patch.object(trading.staking, "signed_get",
                          side_effect=Exception("API down")):
            rate = trading.staking.get_wbeth_exchange_rate()

        assert rate == pytest.approx(1.07)  # stale value
        conn.close()

    def test_api_failure_no_cache_returns_fallback(self):
        """Both API and cache unavailable → safe fallback of 1.0."""
        conn = _mem_conn()

        with patch.object(trading.staking, "db_connect", return_value=conn), \
             patch.object(trading.staking, "signed_get",
                          side_effect=Exception("API down")):
            rate = trading.staking.get_wbeth_exchange_rate()

        assert rate == 1.0
        conn.close()

    def test_malformed_response_falls_through(self):
        """Response missing the `rows` key → fall through to fallback."""
        conn = _mem_conn()

        with patch.object(trading.staking, "db_connect", return_value=conn), \
             patch.object(trading.staking, "signed_get",
                          return_value={"unexpected": "shape"}):
            rate = trading.staking.get_wbeth_exchange_rate()

        assert rate == 1.0
        conn.close()


# ═════════════════════════════════════════════════════════════════════════════
# trading.staking — staked_eth_cache (120s TTL) + force_refresh
# ═════════════════════════════════════════════════════════════════════════════

class TestStakedEthCache:
    """get_staked_eth() caches its 7-field result for 120s in state.db."""

    def test_fresh_cache_hit_returns_without_api_call(self):
        """Cache < 120s old → return cached dict, no signed_get calls."""
        from trading.db import set_staked_eth_cache
        real = _mem_conn()
        set_staked_eth_cache(real, {
            "holdingInETH": 0.5,
            "spot_beth": 0.0,
            "spot_wbeth": 0.0,
            "spot_ldwbeth": 0.0,
            "spot_ldbeth": 0.0,
            "exchange_rate": 1.1,
            "total_eth": 0.55,
        })
        conn = _NoCloseConn(real)

        with patch.object(trading.staking, "db_connect", return_value=conn), \
             patch.object(trading.staking, "signed_get") as mock_sg:
            staked = trading.staking.get_staked_eth()

        assert staked["total_eth"] == pytest.approx(0.55)
        assert staked["holdingInETH"] == pytest.approx(0.5)
        mock_sg.assert_not_called()  # cache hit → zero API calls
        real.close()

    def test_stale_cache_refetches_from_api(self):
        """Cache > 120s old → fetch fresh via signed_get, rewrite cache."""
        real = _mem_conn()
        # Manually insert a stale row (5 min old)
        stale_ts = (datetime.now() - timedelta(minutes=5)).isoformat()
        real.execute(
            "INSERT INTO staked_eth_cache "
            "(id, holding_in_eth, spot_beth, spot_wbeth, spot_ldwbeth, "
            " spot_ldbeth, exchange_rate, total_eth, ts) "
            "VALUES (1, 0.3, 0, 0, 0, 0, 1.09, 0.327, ?)",
            (stale_ts,),
        )
        real.commit()
        conn = _NoCloseConn(real)

        def sg(url, params=None):
            if "eth-staking/account" in url:
                return {"holdingInETH": "0.7"}
            if "/api/v3/account" in url:
                return {"balances": []}
            if "rateHistory" in url:
                return {"rows": [{"exchangeRate": "1.0948"}]}
            return {}

        with patch.object(trading.staking, "db_connect", return_value=conn), \
             patch.object(trading.staking, "signed_get", side_effect=sg):
            staked = trading.staking.get_staked_eth()

        assert staked["holdingInETH"] == pytest.approx(0.7)  # fresh value, not 0.3
        assert staked["total_eth"] == pytest.approx(0.7)
        # Cache should be rewritten with the new value
        row = real.execute(
            "SELECT holding_in_eth FROM staked_eth_cache WHERE id = 1"
        ).fetchone()
        assert float(row[0]) == pytest.approx(0.7)
        real.close()

    def test_force_refresh_bypasses_fresh_cache(self):
        """force_refresh=True ignores a fresh cache, always hits Binance."""
        from trading.db import set_staked_eth_cache
        real = _mem_conn()
        # Fresh cache with stale-looking values
        set_staked_eth_cache(real, {
            "holdingInETH": 0.1,
            "spot_beth": 0.0,
            "spot_wbeth": 0.0,
            "spot_ldwbeth": 0.0,
            "spot_ldbeth": 0.0,
            "exchange_rate": 1.0,
            "total_eth": 0.1,
        })
        conn = _NoCloseConn(real)

        def sg(url, params=None):
            if "eth-staking/account" in url:
                return {"holdingInETH": "0.9"}  # fresh value
            if "/api/v3/account" in url:
                return {"balances": []}
            if "rateHistory" in url:
                return {"rows": [{"exchangeRate": "1.0948"}]}
            return {}

        with patch.object(trading.staking, "db_connect", return_value=conn), \
             patch.object(trading.staking, "signed_get", side_effect=sg) as mock_sg:
            staked = trading.staking.get_staked_eth(force_refresh=True)

        assert staked["holdingInETH"] == pytest.approx(0.9)  # live, not cached 0.1
        assert mock_sg.called  # API was actually hit
        real.close()

    def test_corrupt_cache_ts_falls_through_to_refresh(self):
        """Corrupt ts → cache read fails → fall through to fresh fetch."""
        real = _mem_conn()
        real.execute(
            "INSERT INTO staked_eth_cache "
            "(id, holding_in_eth, spot_beth, spot_wbeth, spot_ldwbeth, "
            " spot_ldbeth, exchange_rate, total_eth, ts) "
            "VALUES (1, 0.2, 0, 0, 0, 0, 1.09, 0.218, ?)",
            ("not-a-valid-iso-timestamp",),
        )
        real.commit()
        conn = _NoCloseConn(real)

        def sg(url, params=None):
            if "eth-staking/account" in url:
                return {"holdingInETH": "0.4"}
            if "/api/v3/account" in url:
                return {"balances": []}
            if "rateHistory" in url:
                return {"rows": [{"exchangeRate": "1.0948"}]}
            return {}

        with patch.object(trading.staking, "db_connect", return_value=conn), \
             patch.object(trading.staking, "signed_get", side_effect=sg):
            staked = trading.staking.get_staked_eth()

        assert staked["holdingInETH"] == pytest.approx(0.4)  # fresh, not stale 0.2
        real.close()


# ═════════════════════════════════════════════════════════════════════════════
# get_dca_stats — new staking yield fields
# ═════════════════════════════════════════════════════════════════════════════

class TestDcaStatsStakingYield:
    def _insert(self, conn, qty, capital, entry, when="2026-01-01T10:00:00"):
        insert_trade(conn, {
            "order_id": f"dca-yield-{qty}-{capital}",
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

    def test_stats_includes_staking_yield_fields(self):
        """get_dca_stats should populate staked_eth + yield from get_staked_eth
        when include_staking=True is explicitly opt-in."""
        conn = _mem_conn()
        self._insert(conn, 0.0183, 39.92, 2181.67)

        # Simulate rebase yield: actual staked = 0.01835 (slightly above DCA cost basis)
        fake_staked = {"total_eth": 0.01835, "holdingInETH": 0.01835,
                       "exchange_rate": 1.0948, "spot_beth": 0,
                       "spot_wbeth": 0, "spot_ldwbeth": 0, "spot_ldbeth": 0}

        with patch.object(trading.dca, "get", return_value={"price": "2300.0"}), \
             patch("trading.staking.get_staked_eth", return_value=fake_staked):
            stats = trading.dca.get_dca_stats(conn, include_staking=True)

        assert stats["staked_eth"] == pytest.approx(0.01835)
        assert stats["staking_yield"] == pytest.approx(0.00005)  # 0.01835 - 0.0183
        assert stats["staking_yield_pct"] == pytest.approx(
            (0.00005 / 0.0183) * 100
        )
        conn.close()

    def test_stats_yield_zero_when_no_buys(self):
        """Empty DCA ledger → yield fields default to 0.0, no division by zero."""
        conn = _mem_conn()
        with patch.object(trading.dca, "get", return_value={"price": "2300.0"}):
            stats = trading.dca.get_dca_stats(conn, include_staking=True)
        assert stats["staking_yield"] == 0.0
        assert stats["staking_yield_pct"] == 0.0
        conn.close()

    def test_stats_staking_failure_is_fail_soft(self):
        """get_staked_eth() raising must NOT break DCA stats — yield stays 0."""
        conn = _mem_conn()
        self._insert(conn, 0.02, 40.0, 2000.0)

        with patch.object(trading.dca, "get", return_value={"price": "2100.0"}), \
             patch("trading.staking.get_staked_eth",
                   side_effect=Exception("staking API down")):
            stats = trading.dca.get_dca_stats(conn, include_staking=True)

        assert stats["n_buys"] == 1  # core stats still populated
        assert stats["pnl_pct"] == pytest.approx(5.0)
        assert stats["staked_eth"] == 0.0  # fail-soft default
        assert stats["staking_yield"] == 0.0
        conn.close()

    def test_stats_skips_staking_when_flag_off(self):
        """include_staking=False (default) → yield fields stay 0.0, no
        get_staked_eth call, no risk of real API hit from test."""
        conn = _mem_conn()
        self._insert(conn, 0.02, 40.0, 2000.0)

        # Patch get_staked_eth to a sentinel that would fail if called
        def should_not_be_called():
            raise AssertionError("include_staking=False should skip get_staked_eth")

        with patch.object(trading.dca, "get", return_value={"price": "2100.0"}), \
             patch("trading.staking.get_staked_eth", side_effect=should_not_be_called):
            stats = trading.dca.get_dca_stats(conn)  # default include_staking=False

        assert stats["n_buys"] == 1
        assert stats["pnl_pct"] == pytest.approx(5.0)
        assert stats["staked_eth"] == 0.0
        assert stats["staking_yield"] == 0.0
        conn.close()


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
