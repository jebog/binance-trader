"""
Unit tests for the SQLite persistence layer (scanner.py DB helpers).

All tests use in-memory SQLite (:memory:) — no disk I/O, no network calls.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

# ── Bootstrap: point DB_FILE at :memory: for every test ──────────────────────
# db_connect() reads DB_FILE from config, so we patch it at import time.
# We can't use ":memory:" across test functions (each connection is a new DB),
# so each test creates its own in-memory connection via sqlite3.connect directly
# and passes it to the helpers.
import scanner

# ── Helper: create a fresh in-memory DB with the schema applied ───────────────

def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", timeout=5.0)
    conn.row_factory = sqlite3.Row
    scanner.db_init(conn)
    return conn


def _sample_trade(**overrides) -> dict:
    t = {
        "order_id":        "123456",
        "symbol":          "ETHUSDC",
        "time":            datetime.now().isoformat(),
        "entry":           2000.0,
        "tp":              2150.0,
        "sl":              1940.0,
        "qty":             0.1,
        "capital":         200.0,
        "oco_id":          "999",
        "status":          "open",
        "sl_pct":          3.0,
        "tp_pct":          7.5,
        "breakeven_moved": False,
        "trailing_stage":  0,
        "signal_strength": "STRONG",
    }
    t.update(overrides)
    return t


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestDbInit:
    def test_init_is_idempotent(self):
        """Calling db_init twice does not raise."""
        conn = _make_conn()
        scanner.db_init(conn)  # second call
        conn.close()


class TestKv:
    def test_get_missing_returns_default(self):
        conn = _make_conn()
        assert scanner.get_kv(conn, "nonexistent") is None
        assert scanner.get_kv(conn, "nonexistent", "fallback") == "fallback"
        conn.close()

    def test_set_and_get_roundtrip(self):
        conn = _make_conn()
        scanner.set_kv(conn, "peak_portfolio_usdc", "1234.56")
        assert scanner.get_kv(conn, "peak_portfolio_usdc") == "1234.56"
        conn.close()

    def test_overwrite(self):
        conn = _make_conn()
        scanner.set_kv(conn, "fg_regime", "fear")
        scanner.set_kv(conn, "fg_regime", "greed")
        assert scanner.get_kv(conn, "fg_regime") == "greed"
        conn.close()

    def test_none_value(self):
        conn = _make_conn()
        scanner.set_kv(conn, "cb_alert_sent_at", None)
        assert scanner.get_kv(conn, "cb_alert_sent_at") is None
        conn.close()


class TestInsertAndGetTrade:
    def test_insert_and_retrieve_open_trade(self):
        conn = _make_conn()
        trade = _sample_trade()
        scanner.insert_trade(conn, trade)
        open_trades = scanner.get_open_trades(conn)
        assert len(open_trades) == 1
        assert open_trades[0]["symbol"] == "ETHUSDC"
        assert open_trades[0]["entry"] == 2000.0
        conn.close()

    def test_breakeven_moved_roundtrips_as_bool(self):
        conn = _make_conn()
        trade = _sample_trade(breakeven_moved=True)
        scanner.insert_trade(conn, trade)
        result = scanner.get_open_trades(conn)[0]
        assert result["breakeven_moved"] is True
        conn.close()

    def test_partial_tp1_json_roundtrip(self):
        conn = _make_conn()
        partial = {"exit_price": 2050.0, "pnl_pct": 2.5, "exit_time": datetime.now().isoformat()}
        trade = _sample_trade(partial_tp1=partial)
        scanner.insert_trade(conn, trade)
        result = scanner.get_open_trades(conn)[0]
        assert isinstance(result["partial_tp1"], dict)
        assert result["partial_tp1"]["exit_price"] == 2050.0
        conn.close()

    def test_closed_trades_not_in_open(self):
        conn = _make_conn()
        scanner.insert_trade(conn, _sample_trade(order_id="open1", status="open"))
        scanner.insert_trade(conn, _sample_trade(order_id="tp1", status="tp_hit",
                                                  exit_price=2150.0, pnl_pct=7.5,
                                                  exit_time=datetime.now().isoformat()))
        open_trades = scanner.get_open_trades(conn)
        assert len(open_trades) == 1
        assert open_trades[0]["order_id"] == "open1"
        conn.close()

    def test_get_all_trades_includes_closed(self):
        conn = _make_conn()
        scanner.insert_trade(conn, _sample_trade(order_id="open1", status="open"))
        scanner.insert_trade(conn, _sample_trade(order_id="tp1", status="tp_hit",
                                                  exit_time=datetime.now().isoformat()))
        all_trades = scanner.get_all_trades(conn)
        assert len(all_trades) == 2
        conn.close()


class TestUpdateTradeFields:
    def test_update_single_field(self):
        conn = _make_conn()
        scanner.insert_trade(conn, _sample_trade())
        scanner.update_trade_fields(conn, "123456", status="tp_hit")
        result = scanner.get_all_trades(conn)[0]
        assert result["status"] == "tp_hit"
        conn.close()

    def test_update_multiple_fields(self):
        conn = _make_conn()
        scanner.insert_trade(conn, _sample_trade())
        scanner.update_trade_fields(conn, "123456",
                                    breakeven_moved=True, sl=2000.0, oco_id="NEW_OCO")
        result = scanner.get_open_trades(conn)[0]
        assert result["breakeven_moved"] is True
        assert result["sl"] == 2000.0
        assert result["oco_id"] == "NEW_OCO"
        conn.close()

    def test_update_trailing_stage(self):
        conn = _make_conn()
        scanner.insert_trade(conn, _sample_trade())
        scanner.update_trade_fields(conn, "123456", trailing_stage=2)
        result = scanner.get_open_trades(conn)[0]
        assert result["trailing_stage"] == 2
        conn.close()

    def test_update_nonexistent_order_id_is_no_op(self):
        conn = _make_conn()
        scanner.insert_trade(conn, _sample_trade())
        scanner.update_trade_fields(conn, "NONEXISTENT", status="tp_hit")
        result = scanner.get_open_trades(conn)[0]
        assert result["status"] == "open"  # unchanged
        conn.close()

    def test_empty_fields_is_no_op(self):
        conn = _make_conn()
        scanner.insert_trade(conn, _sample_trade())
        scanner.update_trade_fields(conn, "123456")  # no-op
        assert len(scanner.get_open_trades(conn)) == 1
        conn.close()


class TestCooldowns:
    def test_save_and_load(self):
        conn = _make_conn()
        scanner.save_cooldown(conn, "ETHUSDC")
        cooldowns = scanner.load_cooldowns(conn)
        assert "ETHUSDC" in cooldowns
        conn.close()

    def test_expired_cooldowns_pruned_on_load(self):
        conn = _make_conn()
        # Insert an already-expired row directly
        past = (datetime.now() - timedelta(hours=1)).isoformat()
        conn.execute(
            "INSERT INTO cooldowns (symbol, expires_at) VALUES (?, ?)", ("SOLUSDC", past)
        )
        conn.commit()
        cooldowns = scanner.load_cooldowns(conn)
        assert "SOLUSDC" not in cooldowns
        conn.close()

    def test_future_cooldown_not_pruned(self):
        conn = _make_conn()
        future = (datetime.now() + timedelta(hours=2)).isoformat()
        conn.execute(
            "INSERT INTO cooldowns (symbol, expires_at) VALUES (?, ?)", ("ADAUSDC", future)
        )
        conn.commit()
        cooldowns = scanner.load_cooldowns(conn)
        assert "ADAUSDC" in cooldowns
        conn.close()


class TestPendingSecondEntries:
    def _entry(self, symbol="ETHUSDC") -> dict:
        return {
            "first_fill": 2000.0, "first_qty": 0.05, "first_oco_id": "OCO1",
            "atr_pct": 0.02, "sl_pct": 0.03, "tp_pct": 0.075,
            "capital_half": 100.0, "time": datetime.now().isoformat(),
        }

    def test_save_and_load(self):
        conn = _make_conn()
        scanner.save_pending_second_entry(conn, "ETHUSDC", self._entry())
        pending = scanner.load_pending_second_entries(conn)
        assert "ETHUSDC" in pending
        assert pending["ETHUSDC"]["first_fill"] == 2000.0
        conn.close()

    def test_clear_removes_entry(self):
        conn = _make_conn()
        scanner.save_pending_second_entry(conn, "ETHUSDC", self._entry())
        scanner.clear_pending_second_entry(conn, "ETHUSDC")
        pending = scanner.load_pending_second_entries(conn)
        assert "ETHUSDC" not in pending
        conn.close()

    def test_clear_nonexistent_is_no_op(self):
        conn = _make_conn()
        scanner.clear_pending_second_entry(conn, "NONEXISTENT")  # should not raise
        conn.close()

    def test_upsert_overwrites(self):
        conn = _make_conn()
        scanner.save_pending_second_entry(conn, "ETHUSDC", self._entry())
        updated = self._entry()
        updated["first_fill"] = 1950.0
        scanner.save_pending_second_entry(conn, "ETHUSDC", updated)
        pending = scanner.load_pending_second_entries(conn)
        assert pending["ETHUSDC"]["first_fill"] == 1950.0
        conn.close()


class TestFgCache:
    def test_empty_returns_none(self):
        conn = _make_conn()
        assert scanner.get_fg_cache(conn) is None
        conn.close()

    def test_set_and_get_singleton(self):
        conn = _make_conn()
        scanner.set_fg_cache(conn, 25, "Extreme Fear")
        result = scanner.get_fg_cache(conn)
        assert result is not None
        assert result["value"] == 25
        assert result["classification"] == "Extreme Fear"
        conn.close()

    def test_overwrite_singleton(self):
        conn = _make_conn()
        scanner.set_fg_cache(conn, 25, "Extreme Fear")
        scanner.set_fg_cache(conn, 72, "Greed")
        result = scanner.get_fg_cache(conn)
        assert result["value"] == 72
        # Only one row — singleton constraint
        count = conn.execute("SELECT COUNT(*) FROM fg_cache").fetchone()[0]
        assert count == 1
        conn.close()


class TestBtcDomCache:
    def test_empty_returns_none(self):
        conn = _make_conn()
        assert scanner.get_btc_dom_cache(conn) is None
        conn.close()

    def test_set_and_get(self):
        conn = _make_conn()
        scanner.set_btc_dom_cache(conn, 54.23)
        result = scanner.get_btc_dom_cache(conn)
        assert result is not None
        assert result["value"] == pytest.approx(54.23)
        conn.close()

    def test_singleton_enforced(self):
        conn = _make_conn()
        scanner.set_btc_dom_cache(conn, 50.0)
        scanner.set_btc_dom_cache(conn, 55.0)
        count = conn.execute("SELECT COUNT(*) FROM btc_dom_cache").fetchone()[0]
        assert count == 1
        conn.close()


class TestGetStateDictShape:
    def test_all_18_keys_present(self):
        conn = _make_conn()
        state = scanner.get_state_dict(conn)
        expected_keys = {
            "last_scan", "results", "signals", "history", "trades",
            "cooldowns", "fg_cache", "portfolio", "sent_signals",
            "fg_regime", "open_pnl", "peak_portfolio_usdc",
            "cb_alert_sent_at", "last_digest_date", "btc_dom_cache",
            "btc_dom_prev", "pending_second_entries", "logs",
        }
        assert set(state.keys()) >= expected_keys
        conn.close()

    def test_trades_is_list(self):
        conn = _make_conn()
        state = scanner.get_state_dict(conn)
        assert isinstance(state["trades"], list)
        conn.close()

    def test_open_trades_appear_in_state_dict(self):
        conn = _make_conn()
        scanner.insert_trade(conn, _sample_trade())
        state = scanner.get_state_dict(conn)
        assert len(state["trades"]) == 1
        conn.close()


class TestMigrateFromJson:
    def _make_state_json(self, tmp_path: str) -> str:
        state = {
            "last_scan": datetime.now().isoformat(),
            "results": [],
            "signals": [],
            "history": [{"time": datetime.now().isoformat(), "signals": []}],
            "trades": [
                {
                    "order_id": "MIG001", "symbol": "ETHUSDC",
                    "time": datetime.now().isoformat(),
                    "entry": 2000.0, "tp": 2150.0, "sl": 1940.0,
                    "qty": 0.1, "capital": 200.0, "oco_id": "OCO1",
                    "status": "open", "sl_pct": 3.0, "tp_pct": 7.5,
                    "breakeven_moved": False, "trailing_stage": 0,
                    "signal_strength": "STRONG",
                }
            ],
            "cooldowns": {},
            "fg_cache": {"value": 30, "classification": "Fear", "ts": datetime.now().isoformat()},
            "portfolio": {"total_usdc": 1000.0, "assets": []},
            "sent_signals": {"ETHUSDC:STRONG": datetime.now().isoformat()},
            "fg_regime": "fear",
            "open_pnl": -2.5,
            "peak_portfolio_usdc": 1020.0,
            "cb_alert_sent_at": None,
            "last_digest_date": "2026-04-01",
            "btc_dom_cache": {"value": 53.4, "ts": datetime.now().isoformat()},
            "btc_dom_prev": 53.1,
            "pending_second_entries": {},
            "logs": [],
        }
        json_path = os.path.join(tmp_path, "state.json")
        with open(json_path, "w") as f:
            json.dump(state, f)
        return json_path

    def test_migration_imports_trade(self, tmp_path):
        json_path = self._make_state_json(str(tmp_path))
        db_path = os.path.join(str(tmp_path), "state.db")

        scanner.migrate_from_json(json_path, db_path)

        # Check backup created
        assert os.path.exists(json_path + ".bak")
        assert not os.path.exists(json_path)

        # Check trade imported
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        trades = conn.execute("SELECT * FROM trades").fetchall()
        assert len(trades) == 1
        assert trades[0]["order_id"] == "MIG001"
        conn.close()

    def test_migration_imports_fg_cache(self, tmp_path):
        json_path = self._make_state_json(str(tmp_path))
        db_path = os.path.join(str(tmp_path), "state.db")
        scanner.migrate_from_json(json_path, db_path)

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM fg_cache WHERE id=1").fetchone()
        assert row is not None
        assert row["value"] == 30
        conn.close()

    def test_migration_imports_kv_scalars(self, tmp_path):
        json_path = self._make_state_json(str(tmp_path))
        db_path = os.path.join(str(tmp_path), "state.db")
        scanner.migrate_from_json(json_path, db_path)

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        scanner.db_init(conn)
        assert scanner.get_kv(conn, "last_digest_date") == "2026-04-01"
        assert scanner.get_kv(conn, "fg_regime") == "fear"
        conn.close()

    def test_migration_rollback_on_corrupt_json(self, tmp_path):
        json_path = os.path.join(str(tmp_path), "state.json")
        with open(json_path, "w") as f:
            f.write("{INVALID JSON}")
        db_path = os.path.join(str(tmp_path), "state.db")

        with pytest.raises(Exception):
            scanner.migrate_from_json(json_path, db_path)

        # Original file untouched (no .bak rename occurred)
        assert os.path.exists(json_path)
        assert not os.path.exists(db_path)


class TestConcurrentReaderWriter:
    """Verify WAL mode allows a reader and writer to coexist without deadlock."""

    def test_reader_does_not_block_writer(self, tmp_path):
        db_path = os.path.join(str(tmp_path), "state.db")

        # Init schema
        conn_init = sqlite3.connect(db_path)
        conn_init.row_factory = sqlite3.Row
        with patch("scanner.DB_FILE", db_path):
            scanner.db_init(conn_init)
        conn_init.close()

        errors: list[Exception] = []

        def reader():
            try:
                conn = sqlite3.connect(db_path, timeout=5.0)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA journal_mode=WAL")
                # Hold a read transaction open briefly
                conn.execute("BEGIN")
                conn.execute("SELECT * FROM trades").fetchall()
                import time as _t
                _t.sleep(0.05)
                conn.execute("COMMIT")
                conn.close()
            except Exception as e:
                errors.append(e)

        def writer():
            try:
                conn = sqlite3.connect(db_path, timeout=5.0)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA journal_mode=WAL")
                with patch("scanner.DB_FILE", db_path):
                    scanner.insert_trade(conn, _sample_trade())
                conn.close()
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=reader)
        t2 = threading.Thread(target=writer)
        t1.start()
        import time as _t
        _t.sleep(0.01)   # ensure reader starts first
        t2.start()
        t1.join(timeout=3.0)
        t2.join(timeout=3.0)

        assert not errors, f"Concurrent access errors: {errors}"
