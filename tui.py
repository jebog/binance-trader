#!/usr/bin/env python3
"""
tui.py — Real-time TUI for the Binance Trading Scanner
Usage: python3 tui.py

Imports scanner.py functions directly.
scanner.py must have TeeLogger guarded behind `if __name__ == "__main__"`.
"""
from __future__ import annotations

import json
import math
import os
import time
from rich.markup import escape as markup_escape
from rich.text import Text
from datetime import datetime, timedelta
from typing import Any

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.message import Message
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    Label,
    ProgressBar,
    RichLog,
    Sparkline,
    Static,
    TabbedContent,
    TabPane,
)

# ── Import scanner functions ──────────────────────────────────────────────────
# TeeLogger is guarded by `if __name__ == "__main__"` in scanner.py,
# so this import does NOT hijack sys.stdout.
from scanner import (
    PAIRS,
    CAPITAL,
    STATE_FILE,
    MAX_POSITIONS,
    LOG_FILE,
    analyze,
    get_portfolio,
    get_open_positions,
    get_fear_greed,
    get_btc_context,
    place_buy_order,
    has_open_position,
    _check_sl_outcomes,
    _load_cooldowns,
    _calc_capital,
    _estimate_sl_tp_pct,
    save_state,
    send_telegram,
    generate_dashboard,
    _escape_md,
)

# ── Constants ─────────────────────────────────────────────────────────────────
AUTO_SCAN_INTERVAL  = 30   # seconds between auto scans
BACKTEST_FILE       = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backtest_results.json")
STATE_READ_INTERVAL = 5    # seconds between state.json polls
LOG_TAIL_LINES      = 80   # lines to keep in log widget

# Catppuccin Mocha hex palette — used in Python markup strings.
# CSS uses $color vars; Python markup needs explicit hex for reliable rendering.
M_GREEN    = "#a6e3a1"
M_RED      = "#f38ba8"
M_ORANGE   = "#fab387"
M_YELLOW   = "#f9e2af"
M_BLUE     = "#89b4fa"
M_TEAL     = "#94e2d5"
M_LAVENDER = "#b4befe"
M_MAUVE    = "#cba6f7"
M_TEXT     = "#cdd6f4"
M_SUBTEXT  = "#a6adc8"
M_MUTED    = "#6c7086"

ASSET_COLORS = {
    "USDC": M_BLUE,
    "ETH":  M_TEAL,
    "BTC":  M_ORANGE,
    "BNB":  M_YELLOW,
    "ADA":  M_LAVENDER,
    "SOL":  M_MAUVE,
    "XRP":  M_BLUE,
    "DOGE": M_YELLOW,
    "LUNA": M_RED,
}


# ── Custom messages ───────────────────────────────────────────────────────────
class ScanComplete(Message):
    def __init__(self, results, signals, context, portfolio, positions):
        super().__init__()
        self.results   = results
        self.signals   = signals
        self.context   = context
        self.portfolio = portfolio
        self.positions = positions

class StateUpdated(Message):
    def __init__(self, state):
        super().__init__()
        self.state = state


# ── Order confirmation modal ──────────────────────────────────────────────────
class OrderConfirmModal(ModalScreen):
    """Push onto screen stack when a buy signal fires. Dismisses with True/False."""

    BINDINGS = [
        Binding("y",      "do_confirm", "Confirm", show=False),
        Binding("enter",  "do_confirm", "Confirm", show=False),
        Binding("n",      "do_skip",    "Skip",    show=False),
        Binding("escape", "do_skip",    "Skip",    show=False),
    ]

    def __init__(self, signal: dict, capital: float, sl_pct: float, tp_pct: float):
        super().__init__()
        self.signal  = signal
        self.capital = capital
        self.sl_pct  = sl_pct
        self.tp_pct  = tp_pct

    def compose(self) -> ComposeResult:
        s     = self.signal
        price = s["price"]
        tier  = s["signal_strength"]
        tier_icon = {"EXTREME": "🔴", "STRONG": "🟠", "MODERATE": "🟡"}.get(tier, "⚪")

        with Container(id="modal-outer"):
            yield Label(f"{tier_icon} {tier} BUY SIGNAL", id="modal-title", classes=tier)
            yield Label("─" * 44, classes="modal-row")
            yield Horizontal(
                Label("Pair:   ", classes="modal-label"),
                Label(s["symbol"],  classes="modal-value"),
            )
            yield Horizontal(
                Label("Entry:  ", classes="modal-label"),
                Label(f"${price:,.4f}  RSI {s['rsi']}", classes="modal-value"),
            )
            yield Horizontal(
                Label("TP:     ", classes="modal-label"),
                Label(f"${price*(1+self.tp_pct):,.4f}  (+{self.tp_pct*100:.1f}%)", classes="modal-value modal-value-tp"),
            )
            yield Horizontal(
                Label("SL:     ", classes="modal-label"),
                Label(f"${price*(1-self.sl_pct):,.4f}  (-{self.sl_pct*100:.1f}%)", classes="modal-value modal-value-sl"),
            )
            yield Horizontal(
                Label("Capital:", classes="modal-label"),
                Label(f"${self.capital:.0f} USDC", classes="modal-value modal-value-cap"),
            )
            yield Label("─" * 44, classes="modal-row")
            with Horizontal(id="modal-buttons"):
                yield Button("✓ CONFIRM  [Enter/Y]", id="btn-confirm")
                yield Button("✗ SKIP  [Esc/N]",      id="btn-skip")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "btn-confirm")

    def action_do_confirm(self) -> None:
        self.dismiss(True)

    def action_do_skip(self) -> None:
        self.dismiss(False)


# ── Widgets ───────────────────────────────────────────────────────────────────
class PairCard(Widget):
    """Displays one trading pair: text info block + Sparkline price chart."""

    # Sparkline color per signal tier
    _SPARK_COLOR = {
        "EXTREME":  M_RED,
        "STRONG":   M_ORANGE,
        "MODERATE": M_YELLOW,
        "NONE":     M_BLUE,
    }

    def __init__(self, symbol: str):
        super().__init__(id=f"pair-{symbol.lower()}", classes="pair-card")
        self.symbol = symbol

    def compose(self) -> ComposeResult:
        yield Static(classes="pair-info")
        yield Sparkline([], summary_function=max, classes="pair-spark")

    def render_for(self, result: dict | None) -> str:
        if result is None:
            name = self.symbol.replace("USDC", "")
            return f"[bold {M_BLUE}]{name}[/]\n[dim]Loading...[/]"

        tier    = result.get("signal_strength", "NONE")
        price   = result.get("price", 0)
        rsi     = result.get("rsi", 0)
        chg     = result.get("change24h", 0) or 0
        name    = self.symbol.replace("USDC", "")
        above   = f"[{M_GREEN}]▲[/]" if result.get("above_sma") else f"[{M_RED}]▽[/]"
        mom     = f"[{M_GREEN}]↑[/]" if result.get("momentum")  else f"[{M_RED}]↓[/]"
        vol     = f"[bold {M_YELLOW}]⚡[/]" if result.get("vol_surge") else " "

        # RSI colour — hotter as oversold deepens
        if rsi < 25:   rsi_col = f"bold {M_RED}"
        elif rsi < 32: rsi_col = f"bold {M_ORANGE}"
        elif rsi < 40: rsi_col = M_YELLOW
        else:          rsi_col = M_SUBTEXT

        # Signal tier colour
        sig_col = {
            "EXTREME":  f"bold {M_RED}",
            "STRONG":   f"bold {M_ORANGE}",
            "MODERATE": M_YELLOW,
            "NONE":     "dim",
        }.get(tier, "dim")

        # 24h change colour
        chg_col = M_GREEN if chg >= 0 else M_RED

        price_fmt = f"${price:,.4f}" if price < 100 else f"${price:,.2f}"
        chg_str   = f"{chg:+.2f}%"

        # Daily RSI — multi-TF trend context
        d_rsi = result.get("daily_rsi")
        if d_rsi is not None:
            if d_rsi < 30:   d_col = f"bold {M_RED}"
            elif d_rsi < 45: d_col = M_YELLOW
            else:            d_col = M_MUTED
            daily_str = f"  1d:[{d_col}]{d_rsi}[/]"
        else:
            daily_str = ""

        return (
            f"[bold {M_BLUE}]{name:<5}[/] [{chg_col}]{chg_str}[/]\n"
            f"[bold {M_TEXT}]{price_fmt}[/]\n"
            f"1h:[{rsi_col}]{rsi:.1f}[/]{daily_str}  {above}{mom}{vol}\n"
            f"[{sig_col}]{tier}[/]"
        )

    def update_result(self, result: dict | None) -> None:
        # Update text block
        self.query_one(".pair-info", Static).update(self.render_for(result))

        # Update border class
        tier = (result or {}).get("signal_strength", "NONE")
        self.remove_class("EXTREME", "STRONG", "MODERATE")
        if tier != "NONE":
            self.add_class(tier)

        # Update sparkline with last 20 close prices (tinted by signal tier)
        spark = self.query_one(".pair-spark", Sparkline)
        klines = (result or {}).get("closed_klines") or []
        if klines:
            spark.data = [float(k[4]) for k in klines[-20:]]
        spark.set_styles(f"color: {self._SPARK_COLOR.get(tier, M_BLUE)};")


class PortfolioWidget(Static):
    def render_portfolio(self, portfolio: dict) -> str:
        if not portfolio or not portfolio.get("assets"):
            return "[dim]No portfolio data — press [S] to scan[/]"

        total   = portfolio["total_usdc"]
        fetched = (portfolio.get("fetched_at") or "")[:16]
        lines   = [f"[bold {M_TEAL}]${total:,.2f}[/] [dim]USDC[/]  [dim]{fetched}[/]\n"]

        for a in portfolio["assets"]:
            pct      = a["pct"]
            bar_len  = max(1, round(pct / 5))   # max 20 chars → 100% = 20
            bar      = "█" * bar_len
            color    = ASSET_COLORS.get(a["asset"], "port-bar-muted")
            qty      = a["qty"]
            val      = a["value_usdc"]
            price    = a["price_usdc"]

            qty_fmt  = f"{qty:.4f}".rstrip("0").rstrip(".")
            val_fmt  = f"${val:,.2f}"
            pct_fmt  = f"{pct:.1f}%"

            lines.append(
                f"[bold]{a['asset']:<5}[/] {qty_fmt:<12} {val_fmt:<10}"
                f"[{color}]{bar}[/] {pct_fmt}"
            )
        return "\n".join(lines)

    def update_portfolio(self, portfolio: dict) -> None:
        self.update(self.render_portfolio(portfolio))


class CooldownWidget(Static):
    def render_cooldowns(self, cooldowns: dict) -> str:
        if not cooldowns:
            return "[dim]No active cooldowns[/]"
        now   = datetime.now()
        lines = []
        for sym, exp_iso in cooldowns.items():
            try:
                remaining = datetime.fromisoformat(exp_iso) - now
                mins      = int(remaining.total_seconds() / 60)
                lines.append(f"[bold {M_RED}]{sym}[/] — [{M_MUTED}]{mins}m left[/]")
            except Exception:
                lines.append(f"[bold {M_RED}]{sym}[/]")
        return "\n".join(lines)

    def update_cooldowns(self, cooldowns: dict) -> None:
        self.update(self.render_cooldowns(cooldowns))


class PerformanceWidget(Static):
    def render_perf(self, trades: list) -> str:
        closed = [t for t in trades if t.get("status") in ("tp_hit", "sl_hit")]
        if not closed:
            return "[dim]No closed trades yet[/]"
        wins     = sum(1 for t in closed if t.get("status") == "tp_hit")
        losses   = len(closed) - wins
        win_rate = wins / len(closed) * 100
        col = M_GREEN if win_rate >= 50 else M_ORANGE
        return (
            f"[{col}]{wins}W[/] / [{M_RED}]{losses}L[/]  [{col}]{win_rate:.0f}% WR[/]\n"
            f"[dim]{len(closed)} total closed trades[/]"
        )

    def update_trades(self, trades: list) -> None:
        self.update(self.render_perf(trades))


class BacktestWidget(Static):
    def render_backtest(self) -> str:
        try:
            with open(BACKTEST_FILE) as f:
                data = json.load(f)
        except FileNotFoundError:
            return "[dim]No data — run backtest.py[/]"
        except Exception:
            return "[dim]Error reading backtest results[/]"

        # Support both old format (overall) and new walk-forward format (overall_test)
        overall = data.get("overall_test") or data.get("overall") or {}
        by_sym  = data.get("by_symbol", {})
        if not overall.get("n"):
            return "[dim]No backtest trades[/]"

        wr  = overall["win_rate"]
        exp = overall["expectancy"]
        net = overall["net_pct"]
        n   = overall["n"]
        src = "test" if "overall_test" in data else "full"
        col = M_GREEN if wr >= 50 else M_ORANGE
        lines = [
            f"[{col}]{wr:.0f}% WR[/]  [{col}]{exp:+.2f}%/t[/]  [dim]({src})[/]\n"
            f"[dim]{n} trades  Net:{net:+.1f}%[/]",
            "",
        ]
        for sym, d in by_sym.items():
            stats = (d.get("test") or {}).get("stats") or d.get("stats") or {}
            if not stats.get("n"):
                continue
            pair = sym.replace("USDC", "")
            swr  = stats["win_rate"]
            sc   = M_GREEN if swr >= 50 else M_ORANGE
            lines.append(
                f"[{M_SUBTEXT}]{pair:<5}[/] [{sc}]{swr:.0f}%[/][dim]/{stats['n']}t[/]"
                f"  [dim]Exp{stats['expectancy']:+.2f}%[/]"
            )
        return "\n".join(lines)

    def refresh_backtest(self) -> None:
        self.update(self.render_backtest())


# ── Main App ──────────────────────────────────────────────────────────────────
class ScannerApp(App):
    CSS_PATH = "tui.tcss"

    BINDINGS = [
        Binding("s", "trigger_scan",      "Scan",     show=True),
        Binding("r", "refresh_state",     "Refresh",  show=True),
        Binding("p", "toggle_left_panel", "Panel",    show=True),
        Binding("l", "toggle_log",        "Log",      show=True),
        Binding("q", "quit",              "Quit",     show=True),
    ]

    # Reactives — changes auto-trigger watch_* methods
    scanning:       reactive[bool]  = reactive(False)
    last_scan:      reactive[str]   = reactive("—")
    fg_value:       reactive[int]   = reactive(0)
    fg_class:       reactive[str]   = reactive("—")
    btc_price:      reactive[float] = reactive(0.0)
    btc_rsi:        reactive[float] = reactive(50.0)
    btc_above_sma:  reactive[bool]  = reactive(True)

    # Internal state initialised in __init__ (NOT class-level to avoid shared mutable defaults)
    # _pair_results, _portfolio, _positions, _cooldowns, _trades, _scan_ctx, _notified_outcomes

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._pair_results:      list[dict] = []
        self._portfolio:         dict       = {}
        self._positions:         list[dict] = []
        self._cooldowns:         dict       = {}
        self._trades:            list[dict] = []
        self._scan_ctx:          dict       = {}   # NOT _context — shadows Textual internal
        self._notified_outcomes: set        = set()  # (oco_id|time, status) — no re-toast
        self._scan_bar:          ProgressBar | None = None  # captured on main thread in watch_scanning

    def compose(self) -> ComposeResult:
        # ── Header
        with Horizontal(id="header"):
            yield Label("◉ TRADING SCANNER", id="header-title")
            yield Label("",                  id="header-context")
            yield Label("",                  id="header-scan-status")
            yield ProgressBar(
                total=len(PAIRS),
                show_eta=False,
                show_percentage=False,
                id="scan-progress",
            )

        # ── Main body
        with Horizontal(id="main-layout"):
            # Left panel — portfolio / cooldowns / performance
            with Vertical(id="left-panel"):
                yield Label("PORTFOLIO", classes="panel-title")
                yield Label("─" * 26, classes="panel-divider")
                yield PortfolioWidget(id="portfolio-widget")
                yield Label("")
                yield Label("COOLDOWNS", classes="panel-title")
                yield Label("─" * 26, classes="panel-divider")
                yield CooldownWidget(id="cooldown-widget")
                yield Label("")
                yield Label("PERFORMANCE", classes="panel-title")
                yield Label("─" * 26, classes="panel-divider")
                yield PerformanceWidget(id="perf-widget")

            # Center panel — tabbed: Market / Positions / History / Backtest
            with TabbedContent(id="center-tabs"):
                with TabPane("Market", id="tab-market"):
                    with Container(id="pairs-grid"):
                        for sym in PAIRS:
                            yield PairCard(sym)

                with TabPane("Positions", id="tab-positions"):
                    positions_table = DataTable(id="positions-table", show_cursor=False)
                    positions_table.add_columns("Symbol", "Qty", "Entry", "Current", "TP", "SL", "P&L")
                    yield positions_table

                with TabPane("History", id="tab-history"):
                    history_table = DataTable(id="history-table", show_cursor=False)
                    history_table.add_columns("Time", "Symbol", "Entry", "Outcome", "Signal")
                    yield history_table

                with TabPane("Backtest", id="tab-backtest"):
                    yield BacktestWidget(id="backtest-widget")

        # ── Log strip
        yield RichLog(id="log-strip", highlight=True, markup=True,
                      max_lines=LOG_TAIL_LINES)

        # ── Status bar
        with Horizontal(id="status-bar"):
            yield Label(
                "[dim][S][/] Scan  [dim][R][/] Refresh  "
                "[dim][P][/] Panel  [dim][L][/] Log  "
                "[dim][Tab][/] Switch tab  [dim][Q][/] Quit",
                id="status-keys",
            )
            yield Label("", id="status-last-scan")

    # ── Lifecycle ─────────────────────────────────────────────────────────────
    def on_mount(self) -> None:
        # Dynamic pair grid — works for any number of pairs
        n    = len(PAIRS)
        cols = math.ceil(n / 2) if n <= 8 else math.ceil(math.sqrt(n))
        rows = math.ceil(n / cols)
        grid = self.query_one("#pairs-grid")
        grid.styles.grid_size_columns = cols
        grid.styles.grid_size_rows    = rows

        # Load backtest results from disk (shown immediately, no API call)
        self.query_one("#backtest-widget", BacktestWidget).refresh_backtest()

        # Seed known trade outcomes so we don't toast historical tp/sl hits on startup
        try:
            with open(STATE_FILE) as f:
                init_state = json.load(f)
            for t in (init_state.get("trades") or []):
                if t.get("status") in ("tp_hit", "sl_hit"):
                    key = (t.get("oco_id") or t.get("time", ""), t["status"])
                    self._notified_outcomes.add(key)
        except Exception:
            pass

        # Hide progress bar until first scan starts
        self.query_one("#scan-progress", ProgressBar).display = False

        # Immediately read state.json for instant display
        self._read_state_file()
        # Start timers
        self.set_interval(STATE_READ_INTERVAL, self._read_state_file)
        self.set_interval(AUTO_SCAN_INTERVAL,  self.action_trigger_scan)
        # Kick off first scan right away
        self.call_after_refresh(self.action_trigger_scan)

    # ── State file watcher (cheap, disk-only) ─────────────────────────────────
    def _read_state_file(self) -> None:
        try:
            with open(STATE_FILE) as f:
                state = json.load(f)
            self.post_message(StateUpdated(state))
        except Exception:
            pass

    def on_state_updated(self, msg: StateUpdated) -> None:
        state = msg.state
        # Update lightweight fields from disk state
        self._cooldowns = _load_cooldowns()
        self._trades    = state.get("trades") or []
        if state.get("portfolio"):
            self._portfolio = state["portfolio"]

        self.last_scan = (state.get("last_scan") or "")[:19]

        # Toast on new TP/SL outcomes (seeded on mount to avoid toasting history)
        for t in self._trades:
            if t.get("status") in ("tp_hit", "sl_hit"):
                key = (t.get("oco_id") or t.get("time", ""), t["status"])
                if key not in self._notified_outcomes:
                    self._notified_outcomes.add(key)
                    sym    = t.get("symbol", "?")
                    entry  = t.get("entry", 0)
                    is_tp  = t["status"] == "tp_hit"
                    self.notify(
                        f"{sym}  entry ${entry:.4f}",
                        title="✅ Take Profit hit" if is_tp else "🛑 Stop Loss hit",
                        severity="information" if is_tp else "warning",
                        timeout=15,
                    )

        # Populate pair card sparklines from disk state (gives instant charts on startup)
        results_by_symbol = {r["symbol"]: r for r in (state.get("results") or [])}
        for sym in PAIRS:
            result = results_by_symbol.get(sym)
            if result and result.get("closed_klines"):
                card = self.query_one(f"#pair-{sym.lower()}", PairCard)
                card.update_result(result)

        # Refresh left-panel widgets
        self.query_one("#cooldown-widget", CooldownWidget).update_cooldowns(self._cooldowns)
        self.query_one("#perf-widget",     PerformanceWidget).update_trades(self._trades)
        if self._portfolio:
            self.query_one("#portfolio-widget", PortfolioWidget).update_portfolio(self._portfolio)

        # Tail log file
        self._tail_log()

    def _tail_log(self) -> None:
        # Skip disk tail while a scan is streaming live output to avoid overwriting it.
        if self.scanning:
            return
        log_widget = self.query_one("#log-strip", RichLog)
        try:
            with open(LOG_FILE) as f:
                lines = f.readlines()[-20:]
            log_widget.clear()
            for line in lines:
                log_widget.write(markup_escape(line.rstrip()))
        except Exception:
            pass

    # ── Reactive watchers ─────────────────────────────────────────────────────
    def watch_scanning(self, scanning: bool) -> None:
        self.query_one("#header-scan-status", Label).update(
            f"[{M_TEAL}]◌[/]" if scanning else ""
        )
        # Capture reference on main thread so the scan worker can safely call advance()
        self._scan_bar = self.query_one("#scan-progress", ProgressBar)
        if scanning:
            self._scan_bar.update(progress=0)
            self._scan_bar.display = True
        else:
            self._scan_bar.display = False

    def watch_last_scan(self, ts: str) -> None:
        self.query_one("#status-last-scan", Label).update(
            f"[dim]Last scan: {ts}[/]" if ts != "—" else ""
        )

    def _update_header_context(self) -> None:
        btc_arrow  = f"[{M_GREEN}]↑[/]" if self.btc_above_sma else f"[{M_RED}]↓[/]"
        fg_col     = M_RED if self.fg_value < 25 else (M_GREEN if self.fg_value > 75 else M_YELLOW)
        btc_rsi_col = M_ORANGE if self.btc_rsi < 40 else M_TEXT
        self.query_one("#header-context", Label).update(
            f"  [{M_MUTED}]F&G:[/] [{fg_col}]{self.fg_value} {self.fg_class}[/]  "
            f"[{M_MUTED}]|[/]  [{M_SUBTEXT}]BTC[/] [bold {M_TEXT}]${self.btc_price:,.0f}[/]  "
            f"[{M_MUTED}]RSI[/] [{btc_rsi_col}]{self.btc_rsi:.1f}[/] {btc_arrow}"
        )

    # ── Scan worker (off main thread) ─────────────────────────────────────────
    @work(thread=True, exclusive=True, name="scan-worker")
    def action_trigger_scan(self) -> None:
        self.call_from_thread(setattr, self, "scanning", True)
        log = self.query_one("#log-strip", RichLog)

        def tlog(msg: str) -> None:
            ts = datetime.now().strftime("%H:%M:%S")
            self.call_from_thread(log.write, f"[dim]{ts}[/]  {msg}")

        try:
            tlog("Fetching market context...")
            fg_value, fg_class = get_fear_greed()
            btc_ctx = get_btc_context()
            context = {
                "fg_value":      fg_value,
                "fg_class":      fg_class,
                "btc_rsi":       btc_ctx["rsi"],
                "btc_above_sma": btc_ctx["above_sma"],
                "btc_price":     btc_ctx["price"],
            }
            self.call_from_thread(setattr, self, "fg_value",     fg_value)
            self.call_from_thread(setattr, self, "fg_class",     fg_class)
            self.call_from_thread(setattr, self, "btc_price",    btc_ctx["price"])
            self.call_from_thread(setattr, self, "btc_rsi",      btc_ctx["rsi"])
            self.call_from_thread(setattr, self, "btc_above_sma",btc_ctx["above_sma"])
            self.call_from_thread(self._update_header_context)

            tlog("Fetching portfolio...")
            portfolio = get_portfolio()

            tlog("Checking SL outcomes...")
            _check_sl_outcomes()

            tlog("Analyzing pairs...")
            results    = []
            candidates = []
            cooldowns  = _load_cooldowns()
            positions  = get_open_positions()
            open_count = len(positions)
            scan_bar   = self._scan_bar  # reference captured on main thread in watch_scanning

            for symbol in PAIRS:
                try:
                    result = analyze(symbol, context)
                    results.append(result)
                    tier = result.get("signal_strength", "NONE")
                    chg     = result['change24h']
                    chg_col = "green" if chg >= 0 else "red"
                    tier_col = ("red" if tier == "EXTREME" else
                                "dark_orange" if tier == "STRONG" else
                                "yellow" if tier == "MODERATE" else "dim")
                    d_rsi = result.get("daily_rsi")
                    daily_part = f"  1d:{d_rsi}" if d_rsi is not None else ""
                    tlog(
                        f"[bold]{symbol.replace('USDC','')}[/]  "
                        f"1h:[bold]{result['rsi']:.1f}[/]{daily_part}  "
                        f"[{chg_col}]{chg:+.2f}%[/]  "
                        f"[{tier_col}]{tier}[/]"
                    )
                    if result["buy_signal"]:
                        candidates.append(result)
                except Exception as e:
                    tlog(f"[red]{symbol}: {markup_escape(str(e))}[/]")
                finally:
                    self.call_from_thread(scan_bar.advance, 1)

            # Correlation cap (before per-symbol guards)
            if len(candidates) >= 3:
                candidates.sort(key=lambda s: s["rsi"])
                dropped = [s["symbol"] for s in candidates[1:]]
                candidates = candidates[:1]
                tlog(f"[yellow]⚠ Correlation cap — keeping {candidates[0]['symbol']}, dropping {', '.join(dropped)}[/]")

            # Per-symbol guards
            signals = []
            for result in candidates:
                symbol = result["symbol"]
                if open_count >= MAX_POSITIONS:
                    tlog(f"[dim]{symbol} — skipped (max positions)[/]")
                elif symbol in cooldowns:
                    tlog(f"[dim]{symbol} — skipped (SL cooldown)[/]")
                elif any(p["symbol"] == symbol for p in positions):
                    tlog(f"[dim]{symbol} — skipped (open position)[/]")
                else:
                    signals.append(result)

            # Save state
            save_state(
                results,
                [{"symbol": s["symbol"], "price": s["price"], "rsi": s["rsi"],
                  "signal_strength": s["signal_strength"]} for s in signals],
                portfolio=portfolio,
            )
            if portfolio:
                try:
                    with open(STATE_FILE) as f:
                        dash_state = json.load(f)
                    generate_dashboard(dash_state)
                except Exception:
                    pass

            tlog(f"[green]Scan complete — {len(results)} pairs, {len(signals)} signal(s)[/]")

            if signals:
                sig_summary = ", ".join(
                    f"{s['symbol'].replace('USDC','')} {s['signal_strength']} RSI {s['rsi']}"
                    for s in signals
                )
                self.call_from_thread(
                    self.notify,
                    sig_summary,
                    title=f"🔔 {len(signals)} signal{'s' if len(signals) > 1 else ''} detected",
                    severity="warning",
                    timeout=12,
                )

            self.call_from_thread(
                self.post_message,
                ScanComplete(results=results, signals=signals, context=context,
                             portfolio=portfolio or {}, positions=positions),
            )

        except Exception as e:
            tlog(f"[red bold]Scan error: {markup_escape(str(e))}[/]")
        finally:
            self.call_from_thread(setattr, self, "scanning", False)

    # ── ScanComplete handler (main thread) ────────────────────────────────────
    def on_scan_complete(self, msg: ScanComplete) -> None:
        self._pair_results = msg.results
        self._portfolio    = msg.portfolio
        self._positions    = msg.positions
        self._scan_ctx      = msg.context
        self.last_scan     = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Update pair cards
        results_by_symbol = {r["symbol"]: r for r in msg.results}
        for sym in PAIRS:
            card = self.query_one(f"#pair-{sym.lower()}", PairCard)
            card.update_result(results_by_symbol.get(sym))

        # Update portfolio panel
        if msg.portfolio:
            self.query_one("#portfolio-widget", PortfolioWidget).update_portfolio(msg.portfolio)

        # Update positions table
        self._refresh_positions_table(msg.positions)

        # Update history table
        self._refresh_history_table(self._trades)

        # Queue order modals for each signal
        for signal in msg.signals:
            capital         = _calc_capital(signal, msg.context)
            sl_pct, tp_pct  = _estimate_sl_tp_pct(signal)
            # Store pending before pushing (closure captures correctly)
            self._push_order_modal(signal, capital, sl_pct, tp_pct)

    def _push_order_modal(self, signal: dict, capital: float,
                          sl_pct: float, tp_pct: float) -> None:
        def on_dismiss(confirmed: bool) -> None:
            self._place_order_async(signal, capital, confirmed)

        self.push_screen(
            OrderConfirmModal(signal, capital, sl_pct, tp_pct),
            callback=on_dismiss,
        )

    # ── Order placement worker ────────────────────────────────────────────────
    @work(thread=True, name="order-worker")
    def _place_order_async(self, signal: dict, capital: float, confirmed: bool) -> None:
        log = self.query_one("#log-strip", RichLog)

        def tlog(msg: str) -> None:
            ts = datetime.now().strftime("%H:%M:%S")
            self.call_from_thread(log.write, f"[dim]{ts}[/]  {msg}")

        if not confirmed:
            tlog(f"[dim]⏭ {signal['symbol']} — skipped by user[/]")
            return

        tlog(f"[bold cyan]Placing order for {signal['symbol']}...[/]")
        try:
            _, _, trade = place_buy_order(
                signal["symbol"],
                capital,
                signal["price"],
                signal.get("closed_klines"),
            )
            tlog(
                f"[green bold]✓ Order placed — {signal['symbol']}  "
                f"qty {trade['qty']}  entry ${trade['entry']:.4f}  "
                f"TP ${trade['tp']:.4f}  SL ${trade['sl']:.4f}[/]"
            )
            self.call_from_thread(
                self.notify,
                f"{signal['symbol']}  qty {trade['qty']}  @ ${trade['entry']:.4f}"
                f"\nTP ${trade['tp']:.4f}  ·  SL ${trade['sl']:.4f}",
                title="✅ Order placed",
                severity="information",
                timeout=15,
            )
            send_telegram(
                f"✅ *Order placed*\n"
                f"`{signal['symbol']}` {trade['qty']} @ `${trade['entry']:.4f}`\n"
                f"TP `${trade['tp']:.4f}` · SL `${trade['sl']:.4f}`\n"
                f"OCO #{trade['oco_id']}"
            )
            # Persist and refresh
            self.call_from_thread(self._read_state_file)
        except Exception as e:
            tlog(f"[red bold]✗ Order failed: {markup_escape(str(e))}[/]")
            self.call_from_thread(
                self.notify,
                markup_escape(str(e)[:120]),
                title=f"✗ Order failed — {signal['symbol']}",
                severity="error",
                timeout=20,
            )
            send_telegram(f"❌ Order failed for `{_escape_md(signal['symbol'])}`: {_escape_md(str(e)[:200])}")

    # ── Table refresh helpers ─────────────────────────────────────────────────
    def _refresh_positions_table(self, positions: list[dict]) -> None:
        table = self.query_one("#positions-table", DataTable)
        table.clear()
        if not positions:
            return
        for p in positions:
            pnl_pct  = p.get("pnl_pct")
            pnl_str  = f"{pnl_pct:+.2f}%" if pnl_pct is not None else "—"
            pnl_cell = Text(pnl_str, style="bold green" if (pnl_pct or 0) >= 0 else "bold red")
            entry    = f"${p['entry']:.4f}"    if p.get("entry")   else "—"
            cur_val  = p.get("current")
            cur      = f"${cur_val:.4f}"       if cur_val          else "—"
            tp       = f"${p['tp']:.4f}"       if p.get("tp")      else "—"
            sl       = f"${p['sl']:.4f}"       if p.get("sl")      else "—"
            qty      = str(p.get("qty") or "—")
            table.add_row(
                p["symbol"], qty, entry, cur, tp, sl, pnl_cell,
            )

    def _refresh_history_table(self, trades: list[dict]) -> None:
        table = self.query_one("#history-table", DataTable)
        table.clear()
        closed = [t for t in trades if t.get("status") in ("tp_hit", "sl_hit")]
        for t in reversed(closed[-10:]):
            status  = t.get("status", "open")
            outcome = {"tp_hit": "TP ✓", "sl_hit": "SL ✗", "open": "open"}.get(status, status)
            ts      = (t.get("time") or "")[:16]
            entry   = f"${t.get('entry', 0):.4f}"
            table.add_row(
                ts,
                t.get("symbol", "—"),
                entry,
                outcome,
                t.get("signal_strength", "—"),
            )

    # ── Actions ───────────────────────────────────────────────────────────────
    def action_refresh_state(self) -> None:
        self._read_state_file()

    def action_toggle_left_panel(self) -> None:
        panel = self.query_one("#left-panel")
        panel.toggle_class("hidden")

    def action_toggle_log(self) -> None:
        log = self.query_one("#log-strip", RichLog)
        log.toggle_class("hidden")


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = ScannerApp()
    app.run()
