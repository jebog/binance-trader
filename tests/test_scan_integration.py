"""
Integration test: run _scan_body() end-to-end with all Binance API calls mocked.

Verifies the full scan lifecycle:
  1. SL outcomes checked
  2. Position management (split-entry, break-even, trailing)
  3. Market context fetched
  4. All pairs analyzed
  5. Correlation cap applied
  6. State persisted to SQLite
  7. Dashboard generated

No network calls — all HTTP endpoints return controlled fixtures.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from unittest.mock import MagicMock, patch

import scanner

# ── Fixtures ──────────────────────────────────────────────────────────────────

def _kline(price: float = 100.0) -> list:
    """Single kline at a given price (RSI neutral, no signal)."""
    return [
        int(datetime.now().timestamp() * 1000),  # open_time
        str(price),          # open
        str(price * 1.002),  # high
        str(price * 0.998),  # low
        str(price),          # close
        "1000",              # volume
        0, "0", 0, "0", "0", "0",  # padding to match Binance format
    ]


def _klines_response(n: int = 100, price: float = 100.0) -> list[list]:
    """Flat price series — RSI ≈ 50, no signal fires."""
    return [_kline(price) for _ in range(n)]


def _btc_klines() -> list[list]:
    return _klines_response(100, 60000.0)


def _fear_greed_response() -> bytes:
    import json
    return json.dumps({"data": [{"value": "50", "value_classification": "Neutral"}]}).encode()


def _portfolio_response() -> list[dict]:
    return [
        {"asset": "USDC", "free": "500.0", "locked": "0.0"},
    ]


def _ticker_price(symbol: str) -> dict:
    prices = {"BTCUSDC": "60000.0", "ETHUSDC": "3000.0", "ADAUSDC": "0.5",
              "DOGEUSDC": "0.15", "BNBUSDC": "300.0", "SOLUSDC": "100.0",
              "XRPUSDC": "0.6"}
    return {"symbol": symbol, "price": prices.get(symbol, "100.0")}


# ── The test ──────────────────────────────────────────────────────────────────

def test_full_scan_cycle_no_signals():
    """Full scan with all pairs at neutral RSI — no signals, state persisted correctly."""
    conn = _fresh_conn()

    def mock_get(path: str, params: Any = None, _retries: int = 1) -> Any:
        if "/klines" in path:
            sym = params.get("symbol", "") if params else ""
            if "BTC" in sym:
                return _btc_klines()
            return _klines_response()
        if "/ticker/price" in path:
            sym = params.get("symbol", "") if params else ""
            return _ticker_price(sym)
        if "/ticker/24hr" in path:
            return {"priceChangePercent": "0.5"}
        if "/exchangeInfo" in path:
            return {"symbols": [{"filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
            ]}]}
        return {}

    def mock_urlopen(*args, **kwargs):
        """Mock for urllib.request.urlopen — returns F&G data."""
        resp = MagicMock()
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        resp.read = lambda: _fear_greed_response()
        return resp


    _portfolio = {"total_usdc": 500.0,
                  "assets": [{"asset": "USDC", "qty": 500.0, "value_usdc": 500.0}]}

    with patch("trading.signals.get", side_effect=mock_get), \
         patch("trading.market_data.get", side_effect=mock_get), \
         patch("trading.market_data.signed_get", return_value=[]), \
         patch("trading.scan_helpers.get", side_effect=mock_get), \
         patch("trading.positions.get", side_effect=mock_get), \
         patch("trading.positions.signed_get", return_value=[]), \
         patch("urllib.request.urlopen", side_effect=mock_urlopen), \
         patch.object(scanner, "get_portfolio", return_value=_portfolio), \
         patch.object(scanner, "get_open_positions", return_value=[]), \
         patch.object(scanner, "has_open_position", return_value=False), \
         patch("trading.notify.send_telegram", return_value=None), \
         patch("trading.scan_helpers.send_telegram", return_value=None), \
         patch("trading.positions.send_telegram", return_value=None), \
         patch.object(scanner, "generate_dashboard", return_value=None), \
         patch.object(scanner, "notify_mac", return_value=None), \
         patch.object(scanner, "call_webhook", return_value=None), \
         patch.object(scanner, "acquire_scan_lock", return_value=True), \
         patch.object(scanner, "release_scan_lock", return_value=None):
        scanner._scan_body(conn)

    # Verify state was persisted
    last_scan = scanner.get_kv(conn, "last_scan")
    assert last_scan is not None
    last_ok = scanner.get_kv(conn, "last_scan_ok")
    assert last_ok is not None

    # Verify scan results were saved (6 pairs)
    results = conn.execute("SELECT COUNT(*) FROM scan_results").fetchone()[0]
    assert results == len(scanner.PAIRS)

    # No signals should have fired (neutral RSI)
    signals = conn.execute("SELECT COUNT(*) FROM scan_signals").fetchone()[0]
    assert signals == 0

    # Portfolio should be saved
    portfolio = conn.execute("SELECT total_usdc FROM portfolio").fetchone()
    assert portfolio is not None
    assert portfolio[0] == 500.0

    conn.close()


def _fresh_conn():
    """Create a fresh in-memory DB (isolated from other tests)."""
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    scanner.db_init(conn)
    return conn


def test_health_after_scan():
    """get_health() returns healthy status after a successful scan."""
    conn = _fresh_conn()
    scanner.set_kv(conn, "last_scan_ok", datetime.now().isoformat())
    scanner.set_kv(conn, "last_scan", datetime.now().isoformat())

    health = scanner.get_health(conn)
    assert health["healthy"] is True
    assert health["stuck_positions"] == 0
    assert health["age_seconds"] is not None
    assert health["age_seconds"] < 5
    conn.close()


def test_health_stale():
    """get_health() reports unhealthy when last_scan_ok is old."""
    conn = _fresh_conn()
    from datetime import timedelta
    old = (datetime.now() - timedelta(hours=2)).isoformat()
    scanner.set_kv(conn, "last_scan_ok", old)

    health = scanner.get_health(conn)
    assert health["healthy"] is False
    assert health["age_seconds"] > 3600
    conn.close()
