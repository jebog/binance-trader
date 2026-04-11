from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Optional

from config import DB_FILE, SL_COOLDOWN_H
from trading.logger import LOG_FILE, logger  # noqa: F401

# ══════════════════════════════════════════════════════════════════════════════
#  SQLite persistence layer
# ══════════════════════════════════════════════════════════════════════════════

_DB_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous  = NORMAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS trades (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id         TEXT    NOT NULL,
    symbol           TEXT    NOT NULL,
    time             TEXT    NOT NULL,
    entry            REAL    NOT NULL,
    tp               REAL    NOT NULL,
    sl               REAL    NOT NULL,
    qty              REAL    NOT NULL,
    capital          REAL    NOT NULL,
    oco_id           TEXT,
    status           TEXT    NOT NULL DEFAULT 'open',
    sl_pct           REAL,
    tp_pct           REAL,
    breakeven_moved  INTEGER NOT NULL DEFAULT 0,
    trailing_stage   INTEGER NOT NULL DEFAULT 0,
    signal_strength  TEXT,
    rsi              REAL,
    tp1_order_id     TEXT,
    tp1_price        REAL,
    tp1_qty          REAL,
    partial_tp1      TEXT,
    split_entry      INTEGER NOT NULL DEFAULT 0,
    exit_price       REAL,
    pnl_pct          REAL,
    exit_time        TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_status   ON trades(status);
CREATE INDEX IF NOT EXISTS idx_trades_symbol   ON trades(symbol);
CREATE INDEX IF NOT EXISTS idx_trades_oco_id   ON trades(oco_id);
CREATE INDEX IF NOT EXISTS idx_trades_order_id ON trades(order_id);

CREATE TABLE IF NOT EXISTS pending_second_entries (
    symbol       TEXT PRIMARY KEY,
    first_fill   REAL NOT NULL,
    first_qty    REAL NOT NULL,
    first_oco_id TEXT NOT NULL,
    atr_pct      REAL NOT NULL,
    sl_pct       REAL NOT NULL,
    tp_pct       REAL NOT NULL,
    capital_half REAL NOT NULL,
    time         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cooldowns (
    symbol     TEXT PRIMARY KEY,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sent_signals (
    key     TEXT PRIMARY KEY,
    sent_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fg_cache (
    id             INTEGER PRIMARY KEY CHECK (id = 1),
    value          INTEGER NOT NULL,
    classification TEXT    NOT NULL,
    ts             TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS btc_dom_cache (
    id    INTEGER PRIMARY KEY CHECK (id = 1),
    value REAL    NOT NULL,
    ts    TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS wbeth_rate_cache (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    exchange_rate REAL    NOT NULL,
    apr           REAL,
    ts            TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS staked_eth_cache (
    id             INTEGER PRIMARY KEY CHECK (id = 1),
    holding_in_eth REAL    NOT NULL DEFAULT 0,
    spot_beth      REAL    NOT NULL DEFAULT 0,
    spot_wbeth     REAL    NOT NULL DEFAULT 0,
    spot_ldwbeth   REAL    NOT NULL DEFAULT 0,
    spot_ldbeth    REAL    NOT NULL DEFAULT 0,
    exchange_rate  REAL    NOT NULL DEFAULT 1.0,
    total_eth      REAL    NOT NULL DEFAULT 0,
    ts             TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS portfolio (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    total_usdc REAL    NOT NULL,
    fetched_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS portfolio_assets (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    asset      TEXT NOT NULL,
    qty        REAL NOT NULL,
    price_usdc REAL NOT NULL,
    value_usdc REAL NOT NULL,
    pct        REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS scan_results (
    symbol          TEXT PRIMARY KEY,
    price           REAL,
    rsi             REAL,
    daily_rsi       REAL,
    sma20           REAL,
    above_sma       INTEGER,
    vol_surge       INTEGER,
    momentum        INTEGER,
    change24h       REAL,
    buy_signal      INTEGER,
    signal_strength TEXT,
    extreme_quality INTEGER,
    divergence      TEXT,
    btc_dom_rising  INTEGER,
    closed_klines   TEXT,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scan_signals (
    symbol          TEXT PRIMARY KEY,
    price           REAL,
    rsi             REAL,
    signal_strength TEXT,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scan_history (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    time    TEXT NOT NULL,
    signals TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS log_lines (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    line      TEXT NOT NULL,
    logged_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

# ── Column list for INSERT / SELECT on trades ─────────────────────────────────
_TRADE_COLS = (
    "order_id", "symbol", "time", "entry", "tp", "sl", "qty", "capital",
    "oco_id", "status", "sl_pct", "tp_pct", "breakeven_moved", "trailing_stage",
    "signal_strength", "rsi", "tp1_order_id", "tp1_price", "tp1_qty",
    "partial_tp1", "split_entry", "exit_price", "pnl_pct", "exit_time",
)


def db_connect() -> sqlite3.Connection:
    """Open state.db with WAL mode. Caller is responsible for closing."""
    conn = sqlite3.connect(DB_FILE, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous  = NORMAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def db_init(conn: sqlite3.Connection) -> None:
    """Create all tables if they don't exist yet (idempotent)."""
    conn.executescript(_DB_SCHEMA)
    conn.commit()


# ── KV helpers ────────────────────────────────────────────────────────────────

def get_kv(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_kv(conn: sqlite3.Connection, key: str, value: Any) -> None:
    v = value.isoformat() if isinstance(value, datetime) else (
        None if value is None else str(value)
    )
    conn.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (key, v))
    conn.commit()


# ── Trade helpers ─────────────────────────────────────────────────────────────

def _row_to_trade(row: sqlite3.Row) -> dict[str, Any]:
    """Convert a trades table row to a dict matching the legacy trade dict shape."""
    d: dict[str, Any] = dict(row)
    d["breakeven_moved"] = bool(d.get("breakeven_moved", 0))
    d["split_entry"]     = bool(d.get("split_entry", 0))
    if d.get("partial_tp1") and isinstance(d["partial_tp1"], str):
        try:
            d["partial_tp1"] = json.loads(d["partial_tp1"])
        except (json.JSONDecodeError, TypeError):
            d["partial_tp1"] = None
    return d


def get_open_trades(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return all trades with status 'open' or 'partial_tp'."""
    rows = conn.execute(
        "SELECT * FROM trades WHERE status IN ('open', 'partial_tp') ORDER BY time"
    ).fetchall()
    return [_row_to_trade(r) for r in rows]


def get_all_trades(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return all trades ordered by entry time (no cap)."""
    rows = conn.execute("SELECT * FROM trades ORDER BY time").fetchall()
    return [_row_to_trade(r) for r in rows]


def get_closed_trades(conn: sqlite3.Connection,
                      limit: Optional[int] = None) -> list[dict[str, Any]]:
    """Return closed trades (tp_hit, sl_hit, timeout) ordered newest-first."""
    q = ("SELECT * FROM trades WHERE status IN "
         "('tp_hit','sl_hit','timeout') ORDER BY exit_time DESC")
    if limit:
        q += f" LIMIT {int(limit)}"
    return [_row_to_trade(r) for r in conn.execute(q).fetchall()]


def insert_trade(conn: sqlite3.Connection, trade: dict[str, Any]) -> None:
    """Insert a new trade row."""
    cols = [c for c in _TRADE_COLS if c in trade]
    vals = []
    for c in cols:
        v = trade[c]
        if c == "breakeven_moved":
            v = int(bool(v))
        elif c == "split_entry":
            v = int(bool(v))
        elif c == "partial_tp1" and isinstance(v, dict):
            v = json.dumps(v)
        vals.append(v)
    placeholders = ", ".join("?" * len(cols))
    conn.execute(
        f"INSERT INTO trades ({', '.join(cols)}) VALUES ({placeholders})",
        vals,
    )
    conn.commit()


def update_trade_fields(conn: sqlite3.Connection,
                        order_id: str, **fields: Any) -> None:
    """Targeted UPDATE on a single trade row by order_id."""
    if not fields:
        return
    updates: list[tuple[str, Any]] = []
    for col, val in fields.items():
        if col == "breakeven_moved":
            val = int(bool(val))
        elif col == "split_entry":
            val = int(bool(val))
        elif col == "partial_tp1" and isinstance(val, dict):
            val = json.dumps(val)
        updates.append((col, val))
    set_clause = ", ".join(f"{c} = ?" for c, _ in updates)
    values = [v for _, v in updates] + [order_id]
    conn.execute(f"UPDATE trades SET {set_clause} WHERE order_id = ?", values)
    conn.commit()


# ── Cooldown helpers ──────────────────────────────────────────────────────────

def load_cooldowns(conn: sqlite3.Connection) -> dict[str, str]:
    """Return active cooldowns and prune expired rows."""
    now_iso = datetime.now().isoformat()
    conn.execute("DELETE FROM cooldowns WHERE expires_at <= ?", (now_iso,))
    conn.commit()
    rows = conn.execute("SELECT symbol, expires_at FROM cooldowns").fetchall()
    return {r["symbol"]: r["expires_at"] for r in rows}


def save_cooldown(conn: sqlite3.Connection, symbol: str) -> None:
    """Upsert an SL cooldown for a symbol."""
    expires = (datetime.now() + timedelta(hours=SL_COOLDOWN_H)).isoformat()
    conn.execute(
        "INSERT OR REPLACE INTO cooldowns (symbol, expires_at) VALUES (?, ?)",
        (symbol, expires),
    )
    conn.commit()


# ── Pending second-entry helpers ──────────────────────────────────────────────

def load_pending_second_entries(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    rows = conn.execute("SELECT * FROM pending_second_entries").fetchall()
    return {r["symbol"]: dict(r) for r in rows}


def save_pending_second_entry(conn: sqlite3.Connection,
                              symbol: str, data: dict[str, Any]) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO pending_second_entries "
        "(symbol, first_fill, first_qty, first_oco_id, atr_pct, sl_pct, tp_pct, capital_half, time) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (symbol, data["first_fill"], data["first_qty"], data["first_oco_id"],
         data["atr_pct"], data["sl_pct"], data["tp_pct"], data["capital_half"], data["time"]),
    )
    conn.commit()


def clear_pending_second_entry(conn: sqlite3.Connection, symbol: str) -> None:
    conn.execute("DELETE FROM pending_second_entries WHERE symbol = ?", (symbol,))
    conn.commit()


# ── Signal dedup helpers ──────────────────────────────────────────────────────

def load_sent_signals(conn: sqlite3.Connection) -> dict[str, str]:
    rows = conn.execute("SELECT key, sent_at FROM sent_signals").fetchall()
    return {r["key"]: r["sent_at"] for r in rows}


def save_sent_signal(conn: sqlite3.Connection, key: str, sent_at: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO sent_signals (key, sent_at) VALUES (?, ?)",
        (key, sent_at),
    )
    conn.commit()


# ── F&G cache helpers ─────────────────────────────────────────────────────────

def get_fg_cache(conn: sqlite3.Connection) -> Optional[dict[str, Any]]:
    row = conn.execute("SELECT * FROM fg_cache WHERE id = 1").fetchone()
    return dict(row) if row else None


def set_fg_cache(conn: sqlite3.Connection,
                 value: int, classification: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO fg_cache (id, value, classification, ts) "
        "VALUES (1, ?, ?, ?)",
        (value, classification, datetime.now().isoformat()),
    )
    conn.commit()


# ── BTC dominance cache helpers ───────────────────────────────────────────────

def get_btc_dom_cache(conn: sqlite3.Connection) -> Optional[dict[str, Any]]:
    row = conn.execute("SELECT * FROM btc_dom_cache WHERE id = 1").fetchone()
    return dict(row) if row else None


def set_btc_dom_cache(conn: sqlite3.Connection, value: float) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO btc_dom_cache (id, value, ts) VALUES (1, ?, ?)",
        (value, datetime.now().isoformat()),
    )
    conn.commit()


# ── WBETH exchange-rate cache ─────────────────────────────────────────────────

def get_wbeth_rate_cache(conn: sqlite3.Connection) -> Optional[dict[str, Any]]:
    row = conn.execute(
        "SELECT exchange_rate, apr, ts FROM wbeth_rate_cache WHERE id = 1"
    ).fetchone()
    return dict(row) if row else None


def set_wbeth_rate_cache(
    conn: sqlite3.Connection,
    exchange_rate: float,
    apr: Optional[float] = None,
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO wbeth_rate_cache (id, exchange_rate, apr, ts) "
        "VALUES (1, ?, ?, ?)",
        (exchange_rate, apr, datetime.now().isoformat()),
    )
    conn.commit()


# ── Staked-ETH-resolution cache ───────────────────────────────────────────────
#
# Full get_staked_eth() result cached for 120s to let the TUI's 5s refresh
# loop read real staking values without making Binance API calls on every
# tick. The TUI's 30s scan worker calls get_staked_eth(force_refresh=True)
# to warm the cache on each real scan boundary.

def get_staked_eth_cache(conn: sqlite3.Connection) -> Optional[dict[str, Any]]:
    row = conn.execute(
        "SELECT holding_in_eth, spot_beth, spot_wbeth, spot_ldwbeth, "
        "spot_ldbeth, exchange_rate, total_eth, ts "
        "FROM staked_eth_cache WHERE id = 1"
    ).fetchone()
    if not row:
        return None
    return {
        "holdingInETH":  float(row["holding_in_eth"]),
        "spot_beth":     float(row["spot_beth"]),
        "spot_wbeth":    float(row["spot_wbeth"]),
        "spot_ldwbeth":  float(row["spot_ldwbeth"]),
        "spot_ldbeth":   float(row["spot_ldbeth"]),
        "exchange_rate": float(row["exchange_rate"]),
        "total_eth":     float(row["total_eth"]),
        "ts":            row["ts"],
    }


def set_staked_eth_cache(conn: sqlite3.Connection, data: dict[str, Any]) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO staked_eth_cache "
        "(id, holding_in_eth, spot_beth, spot_wbeth, spot_ldwbeth, "
        " spot_ldbeth, exchange_rate, total_eth, ts) "
        "VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            float(data.get("holdingInETH", 0.0) or 0.0),
            float(data.get("spot_beth", 0.0) or 0.0),
            float(data.get("spot_wbeth", 0.0) or 0.0),
            float(data.get("spot_ldwbeth", 0.0) or 0.0),
            float(data.get("spot_ldbeth", 0.0) or 0.0),
            float(data.get("exchange_rate", 1.0) or 1.0),
            float(data.get("total_eth", 0.0) or 0.0),
            datetime.now().isoformat(),
        ),
    )
    conn.commit()


# ── Portfolio helpers ─────────────────────────────────────────────────────────

def save_portfolio(conn: sqlite3.Connection,
                   portfolio: dict[str, Any]) -> None:
    total = portfolio.get("total_usdc", 0.0)
    conn.execute(
        "INSERT OR REPLACE INTO portfolio (id, total_usdc, fetched_at) VALUES (1, ?, ?)",
        (total, datetime.now().isoformat()),
    )
    conn.execute("DELETE FROM portfolio_assets")
    for asset in portfolio.get("assets", []):
        conn.execute(
            "INSERT INTO portfolio_assets (asset, qty, price_usdc, value_usdc, pct) "
            "VALUES (?, ?, ?, ?, ?)",
            (asset.get("asset", ""), asset.get("qty", 0.0),
             asset.get("price_usdc", 0.0), asset.get("value_usdc", 0.0),
             asset.get("pct", 0.0)),
        )
    conn.commit()


def db_get_portfolio(conn: sqlite3.Connection) -> Optional[dict[str, Any]]:
    """Read the last portfolio snapshot from the DB."""
    hdr = conn.execute("SELECT * FROM portfolio WHERE id = 1").fetchone()
    if not hdr:
        return None
    assets = [dict(r) for r in conn.execute(
        "SELECT asset, qty, price_usdc, value_usdc, pct FROM portfolio_assets"
    ).fetchall()]
    return {"total_usdc": hdr["total_usdc"], "assets": assets}


# ── Scan result helpers ───────────────────────────────────────────────────────

def save_scan_results(conn: sqlite3.Connection,
                      results: list[dict[str, Any]],
                      signals: list[dict[str, Any]]) -> None:
    now = datetime.now().isoformat()
    for r in results:
        conn.execute(
            "INSERT OR REPLACE INTO scan_results "
            "(symbol, price, rsi, daily_rsi, sma20, above_sma, vol_surge, momentum, "
            "change24h, buy_signal, signal_strength, extreme_quality, divergence, "
            "btc_dom_rising, closed_klines, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (r.get("symbol"), r.get("price"), r.get("rsi"), r.get("daily_rsi"),
             r.get("sma20"), int(bool(r.get("above_sma"))),
             int(bool(r.get("vol_surge"))), int(bool(r.get("momentum"))),
             r.get("change24h"), int(bool(r.get("buy_signal"))),
             r.get("signal_strength"), int(bool(r.get("extreme_quality"))),
             r.get("divergence"), int(bool(r.get("btc_dom_rising"))),
             json.dumps(r.get("closed_klines")) if r.get("closed_klines") else None,
             now),
        )
    conn.execute("DELETE FROM scan_signals")
    for s in signals:
        conn.execute(
            "INSERT INTO scan_signals (symbol, price, rsi, signal_strength, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (s.get("symbol"), s.get("price"), s.get("rsi"),
             s.get("signal_strength"), now),
        )
    conn.commit()


def get_scan_results(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM scan_results ORDER BY symbol").fetchall()
    results = []
    for row in rows:
        r = dict(row)
        if r.get("closed_klines") and isinstance(r["closed_klines"], str):
            try:
                r["closed_klines"] = json.loads(r["closed_klines"])
            except (json.JSONDecodeError, TypeError):
                r["closed_klines"] = []
        results.append(r)
    return results


# ── Scan history helpers ──────────────────────────────────────────────────────

def append_scan_history(conn: sqlite3.Connection,
                        time_iso: str, signals: list[dict[str, Any]]) -> None:
    conn.execute(
        "INSERT INTO scan_history (time, signals) VALUES (?, ?)",
        (time_iso, json.dumps(signals)),
    )
    # Keep rolling 50
    conn.execute(
        "DELETE FROM scan_history WHERE id NOT IN "
        "(SELECT id FROM scan_history ORDER BY id DESC LIMIT 50)"
    )
    conn.commit()


def get_scan_history(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT time, signals FROM scan_history ORDER BY id DESC LIMIT 50"
    ).fetchall()
    result = []
    for row in rows:
        try:
            sigs = json.loads(row["signals"])
        except (json.JSONDecodeError, TypeError):
            sigs = []
        result.append({"time": row["time"], "signals": sigs})
    return result


# ── TUI compatibility bridge ──────────────────────────────────────────────────

def get_state_dict(conn: sqlite3.Connection) -> dict[str, Any]:
    """Reconstruct the legacy state.json dict shape from the SQLite DB."""
    trades = get_all_trades(conn)
    cooldowns = load_cooldowns(conn)
    portfolio = db_get_portfolio(conn)
    fg_cache_row = get_fg_cache(conn)
    btc_dom_row = get_btc_dom_cache(conn)
    results = get_scan_results(conn)
    signals = [dict(r) for r in conn.execute(
        "SELECT * FROM scan_signals ORDER BY symbol"
    ).fetchall()]
    history = get_scan_history(conn)
    pending = load_pending_second_entries(conn)
    sent = load_sent_signals(conn)

    # Scalar KV values
    def _float_kv(key: str) -> Optional[float]:
        v = get_kv(conn, key)
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    # Log tail (same as the embedded logs field; read from disk)
    logs: list[str] = []
    try:
        with open(LOG_FILE, "r") as lf:
            lines = lf.readlines()
            logs = [ln.rstrip("\n") for ln in lines[-200:]]
    except OSError:
        pass

    return {
        "last_scan":           get_kv(conn, "last_scan"),
        "results":             results,
        "signals":             signals,
        "history":             history,
        "trades":              trades,
        "cooldowns":           cooldowns,
        "fg_cache":            dict(fg_cache_row) if fg_cache_row else None,
        "portfolio":           portfolio,
        "sent_signals":        sent,
        "fg_regime":           get_kv(conn, "fg_regime"),
        "open_pnl":            _float_kv("open_pnl"),
        "peak_portfolio_usdc": _float_kv("peak_portfolio_usdc"),
        "cb_alert_sent_at":    get_kv(conn, "cb_alert_sent_at"),
        "last_digest_date":    get_kv(conn, "last_digest_date"),
        "btc_dom_cache":       dict(btc_dom_row) if btc_dom_row else None,
        "btc_dom_prev":        _float_kv("btc_dom_prev"),
        "pending_second_entries": pending,
        "logs":                logs,
    }


# ── One-shot migration: state.json → state.db ─────────────────────────────────

def migrate_from_json(json_path: str, db_path: str) -> None:
    """Import all data from an existing state.json into a fresh state.db."""
    print(f"  \U0001f4e6 Migrating {json_path} \u2192 {db_path} ...")
    with open(json_path) as f:
        old: dict[str, Any] = json.load(f)

    import sqlite3 as _sq3
    conn = _sq3.connect(db_path, timeout=5.0)
    conn.row_factory = _sq3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous  = NORMAL")
    db_init(conn)

    try:
        with conn:
            for t in old.get("trades", []):
                cols_present = [c for c in _TRADE_COLS if c in t]
                vals = []
                for c in cols_present:
                    v = t[c]
                    if c == "breakeven_moved":
                        v = int(bool(v))
                    elif c == "split_entry":
                        v = int(bool(v))
                    elif c == "partial_tp1" and isinstance(v, dict):
                        v = json.dumps(v)
                    vals.append(v)
                ph = ", ".join("?" * len(cols_present))
                conn.execute(
                    f"INSERT OR IGNORE INTO trades ({', '.join(cols_present)}) VALUES ({ph})",
                    vals,
                )

            now_iso = datetime.now().isoformat()
            for symbol, expires_at in (old.get("cooldowns") or {}).items():
                if expires_at > now_iso:
                    conn.execute(
                        "INSERT OR REPLACE INTO cooldowns (symbol, expires_at) VALUES (?, ?)",
                        (symbol, expires_at),
                    )

            for key, sent_at in (old.get("sent_signals") or {}).items():
                conn.execute(
                    "INSERT OR REPLACE INTO sent_signals (key, sent_at) VALUES (?, ?)",
                    (key, sent_at),
                )

            fg = old.get("fg_cache")
            if fg:
                conn.execute(
                    "INSERT OR REPLACE INTO fg_cache (id, value, classification, ts) "
                    "VALUES (1, ?, ?, ?)",
                    (fg.get("value", 50), fg.get("classification", "Neutral"),
                     fg.get("ts", datetime.now().isoformat())),
                )

            btc = old.get("btc_dom_cache")
            if btc:
                conn.execute(
                    "INSERT OR REPLACE INTO btc_dom_cache (id, value, ts) VALUES (1, ?, ?)",
                    (btc.get("value", 0.0), btc.get("ts", datetime.now().isoformat())),
                )

            port = old.get("portfolio")
            if port:
                conn.execute(
                    "INSERT OR REPLACE INTO portfolio (id, total_usdc, fetched_at) "
                    "VALUES (1, ?, ?)",
                    (port.get("total_usdc", 0.0), datetime.now().isoformat()),
                )
                for asset in port.get("assets", []):
                    conn.execute(
                        "INSERT INTO portfolio_assets "
                        "(asset, qty, price_usdc, value_usdc, pct) VALUES (?, ?, ?, ?, ?)",
                        (asset.get("asset", ""), asset.get("qty", 0.0),
                         asset.get("price_usdc", 0.0), asset.get("value_usdc", 0.0),
                         asset.get("pct", 0.0)),
                    )

            for symbol, data in (old.get("pending_second_entries") or {}).items():
                conn.execute(
                    "INSERT OR REPLACE INTO pending_second_entries "
                    "(symbol, first_fill, first_qty, first_oco_id, atr_pct, "
                    "sl_pct, tp_pct, capital_half, time) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (symbol, data.get("first_fill", 0.0), data.get("first_qty", 0.0),
                     data.get("first_oco_id", ""), data.get("atr_pct", 0.0),
                     data.get("sl_pct", 0.0), data.get("tp_pct", 0.0),
                     data.get("capital_half", 0.0), data.get("time", "")),
                )

            now = datetime.now().isoformat()
            for r in old.get("results", []):
                if not r.get("symbol"):
                    continue
                conn.execute(
                    "INSERT OR REPLACE INTO scan_results "
                    "(symbol, price, rsi, daily_rsi, sma20, above_sma, vol_surge, momentum, "
                    "change24h, buy_signal, signal_strength, extreme_quality, divergence, "
                    "btc_dom_rising, closed_klines, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)",
                    (r.get("symbol"), r.get("price"), r.get("rsi"), r.get("daily_rsi"),
                     r.get("sma20"), int(bool(r.get("above_sma"))),
                     int(bool(r.get("vol_surge"))), int(bool(r.get("momentum"))),
                     r.get("change24h"), int(bool(r.get("buy_signal"))),
                     r.get("signal_strength"), int(bool(r.get("extreme_quality"))),
                     r.get("divergence"), int(bool(r.get("btc_dom_rising"))), now),
                )

            for h in old.get("history", []):
                conn.execute(
                    "INSERT INTO scan_history (time, signals) VALUES (?, ?)",
                    (h.get("time", now), json.dumps(h.get("signals", []))),
                )

            kv_map = {
                "fg_regime":           old.get("fg_regime"),
                "btc_dom_prev":        str(old["btc_dom_prev"]) if old.get("btc_dom_prev") is not None else None,
                "peak_portfolio_usdc": str(old["peak_portfolio_usdc"]) if old.get("peak_portfolio_usdc") is not None else None,
                "cb_alert_sent_at":    old.get("cb_alert_sent_at"),
                "last_digest_date":    old.get("last_digest_date"),
                "last_scan":           old.get("last_scan"),
                "open_pnl":            str(old["open_pnl"]) if old.get("open_pnl") is not None else None,
            }
            for key, val in kv_map.items():
                if val is not None:
                    conn.execute(
                        "INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (key, val)
                    )

    except Exception:
        conn.close()
        try:
            os.remove(db_path)
        except OSError:
            pass
        raise

    conn.close()
    os.rename(json_path, json_path + ".bak")
    print(f"  \u2713 Migration complete. Original state saved to {json_path}.bak")


def save_state(
    results: list[dict[str, Any]],
    signals: list[dict[str, Any]],
    new_trades: Optional[list[dict[str, Any]]] = None,
    portfolio: Optional[dict[str, Any]] = None,
    fg_regime: Optional[str] = None,
    open_pnl: Optional[float] = None,
    cb_alert_sent_at: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> None:
    """Save last scan results to state.db."""
    _own_conn = conn is None
    try:
        if _own_conn:
            conn = db_connect()
            db_init(conn)
        now_iso = datetime.now().isoformat()
        save_scan_results(conn, results, signals)
        if signals:
            append_scan_history(conn, now_iso, signals)
        if new_trades:
            for t in new_trades:
                insert_trade(conn, t)
        if portfolio:
            save_portfolio(conn, portfolio)
            current_total = portfolio.get("total_usdc")
            if current_total is not None:
                old_peak_str = get_kv(conn, "peak_portfolio_usdc")
                old_peak = float(old_peak_str) if old_peak_str else 0.0
                if current_total > old_peak:
                    set_kv(conn, "peak_portfolio_usdc", str(current_total))
        if fg_regime is not None:
            set_kv(conn, "fg_regime", fg_regime)
        if open_pnl is not None:
            set_kv(conn, "open_pnl", str(open_pnl))
        if cb_alert_sent_at is not None:
            set_kv(conn, "cb_alert_sent_at", cb_alert_sent_at)
        set_kv(conn, "last_scan", now_iso)
        if _own_conn:
            conn.close()
    except Exception as _e:
        print(f"  \u26a0 SQLite save_state failed: {_e}")
