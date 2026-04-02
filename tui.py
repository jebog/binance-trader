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
import statistics
import time
from datetime import datetime

from rich.markup import escape as markup_escape
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.color import Color
from textual.containers import Container, Horizontal, Vertical
from textual.message import Message
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import (
    Button,
    ContentSwitcher,
    DataTable,
    Input,
    Label,
    LoadingIndicator,
    ProgressBar,
    RichLog,
    Sparkline,
    Static,
    TabbedContent,
    TabPane,
)

# ── Import config + scanner functions ─────────────────────────────────────────
# TeeLogger is guarded by `if __name__ == "__main__"` in scanner.py,
# so this import does NOT hijack sys.stdout.
from config import DIGEST_HOUR
from scanner import (
    LOG_FILE,
    MAX_DRAWDOWN_PCT,
    MAX_POSITIONS,
    PAIRS,
    STATE_FILE,
    _calc_capital,
    _check_fg_regime_change,
    _check_sl_outcomes,
    _escape_md,
    _estimate_sl_tp_pct,
    _fg_regime,
    _load_cooldowns,
    _send_daily_digest,
    acquire_scan_lock,
    analyze,
    apply_correlation_cap,
    build_market_context,
    calc_atr,
    db_connect,
    db_init,
    generate_dashboard,
    get_closed_trades,
    get_kv,
    get_open_positions,
    get_open_trades,
    get_portfolio,
    get_state_dict,
    load_sent_signals,
    place_buy_order,
    release_scan_lock,
    run_position_management,
    run_split_entry_checks,
    save_sent_signal,
    save_state,
    send_telegram,
    set_kv,
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
    def __init__(self, results, signals, context, portfolio, positions, open_pnl=None):
        super().__init__()
        self.results   = results
        self.signals   = signals
        self.context   = context
        self.portfolio = portfolio
        self.positions = positions
        self.open_pnl  = open_pnl   # aggregate unrealized P&L in USDC (None if no positions)

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


# Sparkline tint per signal tier — module-level constant, consistent with M_* palette
_SPARK_COLOR: dict[str, str] = {
    "EXTREME":  M_RED,
    "STRONG":   M_ORANGE,
    "MODERATE": M_YELLOW,
    "NONE":     M_BLUE,
}


# ── Widgets ───────────────────────────────────────────────────────────────────
class PairCard(Widget):
    """Displays one trading pair: text info block + Sparkline price chart."""

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
        if rsi < 25:
            rsi_col = f"bold {M_RED}"
        elif rsi < 32:
            rsi_col = f"bold {M_ORANGE}"
        elif rsi < 40:
            rsi_col = M_YELLOW
        else:
            rsi_col = M_SUBTEXT

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
            if d_rsi < 30:
                d_col = f"bold {M_RED}"
            elif d_rsi < 45:
                d_col = M_YELLOW
            else:
                d_col = M_MUTED
            daily_str = f"  1d:[{d_col}]{d_rsi}[/]"
        else:
            daily_str = ""

        # ATR% computation from closed klines
        atr_str = ""
        klines = result.get("closed_klines") or []
        if klines:
            atr_val = calc_atr(klines)
            if atr_val is not None:
                close_price = float(klines[-1][4])
                if close_price > 0:
                    atr_pct = atr_val / close_price * 100
                    atr_str = f"[{M_SUBTEXT}]ATR:{atr_pct:.1f}%[/]"

        # Divergence status
        div_val = result.get("divergence")
        if div_val is True:
            div_str = f"  [{M_GREEN}]Div:\u2713[/]"
        elif div_val is False:
            div_str = f"  [{M_RED}]Div:\u2717[/]"
        else:
            div_str = ""

        # Split entry badge for EXTREME quality
        split_str = ""
        if tier == "EXTREME" and result.get("extreme_quality"):
            split_str = f"  [{M_MAUVE}]\u2605Split[/]"

        return (
            f"[bold {M_BLUE}]{name:<5}[/] [{chg_col}]{chg_str}[/]\n"
            f"[bold {M_TEXT}]{price_fmt}[/]\n"
            f"1h:[{rsi_col}]{rsi:.1f}[/]{daily_str}  {above}{mom}{vol}\n"
            f"{atr_str}{div_str}{split_str}\n"
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

        # Update sparkline: data + tint color
        # set_styles() does NOT affect Sparkline rendering — must use max_color/min_color
        spark  = self.query_one(".pair-spark", Sparkline)
        klines = (result or {}).get("closed_klines") or []
        if klines:
            spark.data = [float(k[4]) for k in klines[-20:]]
            tint = Color.parse(_SPARK_COLOR.get(tier, M_BLUE))
            spark.max_color = tint
            spark.min_color = tint


class PortfolioWidget(Static):
    def render_portfolio(self, portfolio: dict, open_pnl: float | None = None,
                         peak_usdc: float | None = None) -> str:
        if not portfolio or not portfolio.get("assets"):
            return "[dim]No portfolio data — press [S] to scan[/]"

        total   = portfolio["total_usdc"]
        fetched = (portfolio.get("fetched_at") or "")[:16]
        lines   = [f"[bold {M_TEAL}]${total:,.2f}[/] [dim]USDC[/]  [dim]{fetched}[/]"]

        if open_pnl is not None:
            pnl_col = M_GREEN if open_pnl >= 0 else M_RED
            lines.append(f"[dim]Open P&L:[/] [{pnl_col}]{open_pnl:+.2f} USDC[/]")

        if peak_usdc and total is not None and peak_usdc > total:
            dd_pct = (peak_usdc - total) / peak_usdc * 100
            if dd_pct >= 15:
                lines.append(f"[bold {M_RED}]🛑 HALTED {dd_pct:.1f}% drawdown[/]")
            elif dd_pct >= 10:
                lines.append(f"[dark_orange]⚠ Drawdown: {dd_pct:.1f}%[/]")
        lines.append("")

        for a in portfolio["assets"]:
            pct      = a["pct"]
            bar_len  = max(1, round(pct / 5))   # max 20 chars → 100% = 20
            bar      = "█" * bar_len
            color    = ASSET_COLORS.get(a["asset"], "port-bar-muted")
            qty      = a["qty"]
            val      = a["value_usdc"]

            qty_fmt  = f"{qty:.4f}".rstrip("0").rstrip(".")
            val_fmt  = f"${val:,.2f}"
            pct_fmt  = f"{pct:.1f}%"

            lines.append(
                f"[bold]{a['asset']:<5}[/] {qty_fmt:<12} {val_fmt:<10}"
                f"[{color}]{bar}[/] {pct_fmt}"
            )
        return "\n".join(lines)

    def update_portfolio(self, portfolio: dict, open_pnl: float | None = None,
                         peak_usdc: float | None = None) -> None:
        self.update(self.render_portfolio(portfolio, open_pnl=open_pnl, peak_usdc=peak_usdc))


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

        # Profit factor
        wins_pnl = [float(t.get("pnl_pct") or 0) for t in closed if t.get("status") == "tp_hit"]
        losses_pnl = [float(t.get("pnl_pct") or 0) for t in closed if t.get("status") == "sl_hit"]
        sum_wins = sum(wins_pnl) if wins_pnl else 0
        sum_losses = abs(sum(losses_pnl)) if losses_pnl else 0
        pf = sum_wins / sum_losses if sum_losses > 0 else 0.0

        # Sharpe ratio (non-annualized, per-trade)
        pnl_vals = [float(t.get("pnl_pct") or 0) for t in closed]
        if len(pnl_vals) >= 2:
            std = statistics.stdev(pnl_vals)
            sharpe = statistics.mean(pnl_vals) / std if std > 0 else 0.0
        else:
            sharpe = 0.0

        # Max consecutive losses
        max_consec = 0
        cur_consec = 0
        for t in closed:
            if t.get("status") == "sl_hit":
                cur_consec += 1
                max_consec = max(max_consec, cur_consec)
            else:
                cur_consec = 0

        # Break-even saves: SL hit but exit >= entry and breakeven was armed
        be_saves = sum(
            1 for t in closed
            if t.get("status") == "sl_hit"
            and t.get("breakeven_moved")
            and float(t.get("exit_price") or 0) >= float(t.get("entry") or 1)
        )

        sharpe_col = M_GREEN if sharpe > 0 else M_RED
        pf_col = M_GREEN if pf >= 1.0 else M_ORANGE
        return (
            f"[{col}]{wins}W[/] / [{M_RED}]{losses}L[/]  "
            f"[{col}]{win_rate:.0f}% WR[/]  [dim]({len(closed)} closed)[/]\n"
            f"[{pf_col}]PF:{pf:.1f}[/]  [{sharpe_col}]Sharpe:{sharpe:.2f}[/]\n"
            f"[{M_SUBTEXT}]Streak:{max_consec}[/]  [{M_SUBTEXT}]BE saves:{be_saves}[/]"
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


# ── Equity curve widget ───────────────────────────────────────────────────────
class EquityWidget(Widget):
    """Cumulative P&L sparkline built from closed trades in state.json."""

    def compose(self) -> ComposeResult:
        yield Sparkline([], id="equity-spark", summary_function=max)
        yield Static("[dim]No closed trades yet[/]", id="equity-stats")

    def refresh_equity(self, trades: list[dict]) -> None:
        closed = sorted(
            [t for t in trades if t.get("status") in ("tp_hit", "sl_hit")],
            key=lambda t: t.get("time", ""),
        )
        spark = self.query_one("#equity-spark", Sparkline)
        stats = self.query_one("#equity-stats", Static)

        if not closed:
            spark.data = []
            stats.update("[dim]No closed trades yet[/]")
            return

        # Build cumulative P&L series from entry/tp/sl prices
        cumulative = 0.0
        series: list[float] = []
        for t in closed:
            entry = float(t.get("entry") or 0)
            if entry == 0:
                continue
            # Use exit_price (actual fill) — not tp/sl (activation price is wrong
            # for trailing stops per CLAUDE.md).
            ep = t.get("exit_price") or (t.get("tp") if t["status"] == "tp_hit" else t.get("sl"))
            if not ep:
                continue
            pnl = (float(ep) - entry) / entry * 100
            cumulative += pnl
            series.append(cumulative)

        spark.data = series
        if series:
            total = series[-1]
            col = M_GREEN if total >= 0 else M_RED
            tint = Color.parse(col)
            spark.max_color = tint
            spark.min_color = tint
            stats.update(
                f"[{col}]{total:+.2f}%[/] cumulative  [dim]({len(series)} trades)[/]"
            )


# ── Settings modal ─────────────────────────────────────────────────────────────
class SettingsModal(ModalScreen):
    """Edit runtime settings (scan interval). Dismisses with new interval or None."""

    BINDINGS = [Binding("escape", "dismiss_modal", "Close", show=False)]

    def __init__(self, current_interval: int):
        super().__init__()
        self._current_interval = current_interval

    def compose(self) -> ComposeResult:
        with Container(id="modal-outer"):
            yield Label("⚙  SETTINGS", id="modal-title")
            yield Label("─" * 44, classes="modal-row")
            yield Horizontal(
                Label("Scan interval (s):", classes="modal-label"),
                Input(
                    value=str(self._current_interval),
                    placeholder="seconds (min 10)",
                    id="input-interval",
                    type="integer",
                ),
            )
            yield Label("[dim]Min 10 s — current scans are not interrupted[/]",
                        classes="modal-row")
            yield Label("─" * 44, classes="modal-row")
            with Horizontal(id="modal-buttons"):
                yield Button("✓ Apply  [Enter]", id="btn-confirm")
                yield Button("✗ Cancel  [Esc]",  id="btn-skip")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-confirm":
            self._apply()
        else:
            self.dismiss(None)

    def on_input_submitted(self, _: Input.Submitted) -> None:
        self._apply()

    def _apply(self) -> None:
        try:
            value = max(10, int(self.query_one("#input-interval", Input).value))
            self.dismiss(value)
        except ValueError:
            self.dismiss(None)

    def action_dismiss_modal(self) -> None:
        self.dismiss(None)


# ── Main App ──────────────────────────────────────────────────────────────────
class ScannerApp(App):
    CSS_PATH = "tui.tcss"

    BINDINGS = [
        Binding("s", "trigger_scan",      "Scan",     show=True),
        Binding("r", "refresh_state",     "Refresh",  show=True),
        Binding("p", "toggle_left_panel", "Panel",    show=True),
        Binding("e", "toggle_equity",     "Equity",   show=True),
        Binding("c", "open_settings",     "Settings", show=True),
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
    btc_dom:        reactive[float | None] = reactive(None)
    btc_dom_rising: reactive[bool]  = reactive(False)
    open_pnl_usdc:  reactive[float | None] = reactive(None)

    # Internal state initialised in __init__ (NOT class-level to avoid shared mutable defaults)
    # _pair_results, _portfolio, _positions, _cooldowns, _trades, _scan_ctx, _notified_outcomes

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._pair_results:      list[dict] = []
        self._portfolio:         dict       = {}
        self._positions:         list[dict] = []
        self._cooldowns:         dict       = {}
        self._trades:            list[dict] = []
        self._open_pnl:          float | None = None  # aggregate unrealized P&L from last scan
        self._peak_usdc:         float | None = None  # portfolio high-water mark for drawdown display
        self._scan_ctx:          dict       = {}   # NOT _context — shadows Textual internal
        self._notified_outcomes: set        = set()  # (oco_id|time, status) — no re-toast
        self._scan_bar:          ProgressBar | None = None  # captured on main thread in watch_scanning
        self._scan_interval:     int               = AUTO_SCAN_INTERVAL
        self._scan_timer                           = None   # Timer ref for settings-driven reset
        self._next_scan_at:      float             = 0.0    # monotonic timestamp for countdown

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
            # Left panel — portfolio / equity / cooldowns / performance
            with Vertical(id="left-panel"):
                yield Label("PORTFOLIO", classes="panel-title", id="left-panel-title")
                yield Label("─" * 26, classes="panel-divider")
                # ContentSwitcher: LoadingIndicator until first data, then Portfolio or Equity
                with ContentSwitcher(id="left-switcher", initial="portfolio-loading"):
                    yield LoadingIndicator(id="portfolio-loading")
                    yield PortfolioWidget(id="portfolio-widget")
                    yield EquityWidget(id="equity-widget")
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
                    positions_table.add_columns("Symbol", "Qty", "Entry", "Current", "TP", "SL", "P&L", "Held")
                    yield positions_table

                with TabPane("History", id="tab-history"):
                    history_table = DataTable(id="history-table", show_cursor=False)
                    history_table.add_columns("Time", "Symbol", "Entry", "Exit", "P&L", "Outcome", "Signal")
                    yield history_table

                with TabPane("Backtest", id="tab-backtest"):
                    yield BacktestWidget(id="backtest-widget")

        # ── Log strip
        yield RichLog(id="log-strip", highlight=True, markup=True,
                      max_lines=LOG_TAIL_LINES)

        # ── Status bar
        with Horizontal(id="status-bar"):
            yield Label(
                "[dim][S][/] Scan  [dim][R][/] Refresh  [dim][P][/] Panel  "
                "[dim][E][/] Equity  [dim][C][/] Settings  "
                "[dim][L][/] Log  [dim][Tab][/] Tab  [dim][Q][/] Quit",
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

        # Seed known trade outcomes so we don't toast historical tp/sl hits on startup.
        # Must include CLOSED trades — get_open_trades only returns open/partial_tp.
        try:
            _seed_conn = db_connect()
            db_init(_seed_conn)
            seed_trades = get_open_trades(_seed_conn) + get_closed_trades(_seed_conn)
            _seed_conn.close()
        except Exception:
            seed_trades = []
        for t in seed_trades:
            if t.get("status") in ("tp_hit", "sl_hit"):
                key = (t.get("oco_id") or t.get("time", ""), t["status"])
                self._notified_outcomes.add(key)

        # Hide progress bar until first scan starts
        self.query_one("#scan-progress", ProgressBar).display = False

        # Immediately read state.json for instant display
        self._read_state_file()
        # Start timers
        self.set_interval(STATE_READ_INTERVAL, self._read_state_file)
        self._scan_timer = self.set_interval(self._scan_interval, self.action_trigger_scan)
        # Kick off first scan right away
        self.call_after_refresh(self.action_trigger_scan)

    # ── State file watcher (cheap, disk-only) ─────────────────────────────────
    def _read_state_file(self) -> None:
        # Try SQLite first (WAL mode allows concurrent reader + scanner writer)
        try:
            conn = db_connect()
            db_init(conn)
            state = get_state_dict(conn)
            conn.close()
            self.post_message(StateUpdated(state))
            return
        except Exception:
            pass
        # Fall back to state.json during migration / if state.db missing
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
        self._peak_usdc = state.get("peak_portfolio_usdc")

        self.last_scan = (state.get("last_scan") or "")[:19]

        # BTC.D from cached state
        btc_dom_cache = state.get("btc_dom_cache")
        if btc_dom_cache and btc_dom_cache.get("value") is not None:
            self.btc_dom = float(btc_dom_cache["value"])
        # Open P&L from kv
        if state.get("open_pnl") is not None:
            self.open_pnl_usdc = float(state["open_pnl"])

        # Status bar: health dot + countdown
        self._update_status_bar(state)

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
        self.query_one("#equity-widget",   EquityWidget).refresh_equity(self._trades)
        if self._portfolio:
            self.query_one("#portfolio-widget", PortfolioWidget).update_portfolio(
                self._portfolio, open_pnl=self._open_pnl, peak_usdc=self._peak_usdc
            )
            # Swap LoadingIndicator → portfolio on first data arrival
            switcher = self.query_one("#left-switcher", ContentSwitcher)
            if switcher.current == "portfolio-loading":
                switcher.current = "portfolio-widget"

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
        # Status bar is now primarily driven by _update_status_bar;
        # this watcher updates the fallback when no state is available yet
        if ts == "\u2014":
            self.query_one("#status-last-scan", Label).update("")

    def _update_status_bar(self, state: dict | None = None) -> None:
        """Update status bar with health dot, countdown, and last scan time."""
        parts = []

        # Health dot: green if last_scan_ok < 300s, else red
        healthy = False
        if state:
            last_ok = state.get("last_scan_ok")
            if last_ok:
                try:
                    age = (datetime.now() - datetime.fromisoformat(last_ok)).total_seconds()
                    healthy = age < 300
                except Exception:
                    pass
        dot_col = M_GREEN if healthy else M_RED
        parts.append(f"[{dot_col}]\u25cf[/]")

        # Countdown to next scan
        if self._next_scan_at > 0:
            remaining = max(0, int(self._next_scan_at - time.monotonic()))
            parts.append(f"[{M_SUBTEXT}]Next: {remaining}s[/]")

        # Last scan time
        if self.last_scan and self.last_scan != "\u2014":
            # Show just the time portion
            scan_time = self.last_scan[-8:] if len(self.last_scan) >= 8 else self.last_scan
            parts.append(f"[{M_MUTED}]Last: {scan_time}[/]")

        self.query_one("#status-last-scan", Label).update(
            " | ".join(parts) if parts else ""
        )

    def _update_header_context(self) -> None:
        btc_arrow  = f"[{M_GREEN}]↑[/]" if self.btc_above_sma else f"[{M_RED}]↓[/]"
        fg_col     = M_RED if self.fg_value < 25 else (M_GREEN if self.fg_value > 75 else M_YELLOW)
        btc_rsi_col = M_ORANGE if self.btc_rsi < 40 else M_TEXT

        # BTC.D segment
        btc_dom_part = ""
        if self.btc_dom is not None:
            dom_arrow = "↑" if self.btc_dom_rising else "↓"
            btc_dom_part = (
                f"  [{M_MUTED}]|[/]  [{M_SUBTEXT}]BTC.D:[/]"
                f"[{M_TEXT}]{self.btc_dom:.1f}%{dom_arrow}[/]"
            )

        # Open P&L segment
        pnl_part = ""
        if self.open_pnl_usdc is not None:
            pnl_col = M_GREEN if self.open_pnl_usdc >= 0 else M_RED
            pnl_part = (
                f"  [{M_MUTED}]|[/]  [{M_MUTED}]P&L:[/] "
                f"[{pnl_col}]{self.open_pnl_usdc:+.2f}$[/]"
            )

        self.query_one("#header-context", Label).update(
            f"  [{M_MUTED}]F&G:[/] [{fg_col}]{self.fg_value} {self.fg_class}[/]  "
            f"[{M_MUTED}]|[/]  [{M_SUBTEXT}]BTC[/] [bold {M_TEXT}]${self.btc_price:,.0f}[/]  "
            f"[{M_MUTED}]RSI[/] [{btc_rsi_col}]{self.btc_rsi:.1f}[/] {btc_arrow}"
            f"{btc_dom_part}{pnl_part}"
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
            context = build_market_context()
            self.call_from_thread(setattr, self, "fg_value",     context["fg_value"])
            self.call_from_thread(setattr, self, "fg_class",     context["fg_class"])
            self.call_from_thread(setattr, self, "btc_price",    context["btc_price"])
            self.call_from_thread(setattr, self, "btc_rsi",      context["btc_rsi"])
            self.call_from_thread(setattr, self, "btc_above_sma",context["btc_above_sma"])
            if context.get("btc_dom") is not None:
                self.call_from_thread(setattr, self, "btc_dom",        context["btc_dom"])
                self.call_from_thread(setattr, self, "btc_dom_rising", bool(context.get("btc_dom_rising")))
            self.call_from_thread(self._update_header_context)

            # ── F&G regime-change alert ──────────────────────────────────────
            fg_fresh = context.get("fg_fresh", False)
            fg_value = context["fg_value"]
            try:
                _tui_conn = db_connect()
                db_init(_tui_conn)
                old_fg_regime = get_kv(_tui_conn, "fg_regime") or _fg_regime(fg_value)
                if fg_fresh:
                    new_fg_regime = _check_fg_regime_change(fg_value, context["fg_class"], old_fg_regime)
                else:
                    new_fg_regime = old_fg_regime
                _tui_conn.close()
            except Exception:
                new_fg_regime = None

            tlog("Fetching portfolio...")
            portfolio = get_portfolio()

            tlog("Checking SL outcomes...")
            _check_sl_outcomes()

            tlog("Position management...")
            run_split_entry_checks()
            run_position_management()

            # ── Acquire scan lock (prevents cron + TUI double-ordering) ──────
            if not acquire_scan_lock(caller="tui"):
                tlog("[yellow]⏸ Scan lock held by cron — skipping signal detection[/]")
                # Still update dashboard from DB
                try:
                    _dash_conn = db_connect()
                    db_init(_dash_conn)
                    generate_dashboard(get_state_dict(_dash_conn))
                    _dash_conn.close()
                except Exception:
                    pass
                self.call_from_thread(
                    self.post_message,
                    ScanComplete(results=[], signals=[], context=context,
                                 portfolio=portfolio or {}, positions=get_open_positions(),
                                 open_pnl=None),
                )
                return

            tlog("Analyzing pairs...")
            results    = []
            candidates = []
            cooldowns  = _load_cooldowns()
            positions  = get_open_positions()
            open_count = len(positions)
            _pnl_vals  = [p["pnl"] for p in positions if p.get("pnl") is not None]
            open_pnl   = sum(_pnl_vals) if _pnl_vals else None
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

            # Correlation cap (shared helper — mirrors scanner.py exactly)
            candidates, dropped, cap_reason = apply_correlation_cap(candidates)
            if dropped:
                tlog(f"[yellow]⚠ Correlation cap — keeping {candidates[0]['symbol']} ({cap_reason}), dropping {', '.join(dropped)}[/]")

            # Circuit breaker: mirror scanner.py guard (TUI scans are real orders too)
            if self._peak_usdc and portfolio:
                _cb_current = portfolio.get("total_usdc")
                if _cb_current is not None:
                    _cb_dd = (self._peak_usdc - _cb_current) / self._peak_usdc
                    if _cb_dd >= MAX_DRAWDOWN_PCT:
                        tlog(f"[bold red]🛑 CIRCUIT BREAKER: {_cb_dd*100:.1f}% drawdown — no orders placed[/]")
                        candidates = []

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

            # Save state (include fg_regime for regime-change tracking)
            save_state(
                results,
                [{"symbol": s["symbol"], "price": s["price"], "rsi": s["rsi"],
                  "signal_strength": s["signal_strength"]} for s in signals],
                portfolio=portfolio,
                fg_regime=new_fg_regime,
                open_pnl=open_pnl,
            )
            if portfolio:
                try:
                    _dash_conn = db_connect()
                    db_init(_dash_conn)
                    generate_dashboard(get_state_dict(_dash_conn))
                    _dash_conn.close()
                except Exception:
                    pass

            # ── Telegram scan summary (only when signals found — TUI scans every
            #    30s so routine "no signal" scans stay silent on Telegram) ────
            if signals:
                try:
                    _t_conn = db_connect()
                    db_init(_t_conn)
                    closed = get_closed_trades(_t_conn)
                    _t_conn.close()
                    perf_line = ""
                    if closed:
                        _wins = sum(1 for t in closed if t.get("status") == "tp_hit")
                        _total = len(closed)
                        perf_line = f"\n\U0001f4ca Trades: `{_wins}W/{_total-_wins}L` ({_wins/_total*100:.0f}% WR)"
                except Exception:
                    perf_line = ""

                icons = {"EXTREME": "\U0001f534", "STRONG": "\U0001f7e0",
                         "MODERATE": "\U0001f7e1", "NONE": "\u26aa"}
                btc_trend = "\u2191" if context["btc_above_sma"] else "\u2193"
                tg_lines = [
                    f"\U0001f4ca *Scan {datetime.now().strftime('%H:%M')}*\n"
                    f"F&G: `{context['fg_value']}` {context['fg_class']}  |  "
                    f"BTC `${context['btc_price']:,.0f}` RSI:`{context['btc_rsi']}` {btc_trend}\n"
                ]
                for r in results:
                    icon = icons.get(r["signal_strength"], "\u26aa")
                    pair = r["symbol"].replace("USDC", "")
                    tg_lines.append(
                        f"{icon} `{pair:<5}` ${r['price']:<10.4f} RSI:`{r['rsi']:<5}` "
                        f"24h:`{r['change24h']:+.2f}%`"
                        + (f"  *{r['signal_strength']}*" if r["signal_strength"] != "NONE" else "")
                    )
                if positions:
                    tg_lines.append("\n\U0001f4c8 *Positions*")
                    for p in positions:
                        pair = p["symbol"].replace("USDC", "")
                        pnl_str = (f"{p['pnl_pct']:+.2f}%  `{'%.2f' % p['pnl']}$`"
                                   if p.get("pnl") is not None else "n/a")
                        entry_s = f"${p['entry']:.4f}" if p.get("entry") else "?"
                        cur_s = f"${p['current']:.4f}" if p.get("current") else "?"
                        tg_lines.append(f"`{pair}` {p.get('qty', '?')} \u00b7 {entry_s}\u2192{cur_s} {pnl_str}")
                if perf_line:
                    tg_lines.append(perf_line)
                send_telegram("\n".join(tg_lines))

            # ── Signal dedup (2h suppression per symbol:tier) ──────────────
            if signals:
                SIGNAL_DEDUP_H = 2
                try:
                    _sd_conn = db_connect()
                    db_init(_sd_conn)
                    sent_signals = load_sent_signals(_sd_conn)
                    _sd_conn.close()
                except Exception:
                    sent_signals = {}

                deduped_signals = []
                for s in signals:
                    dedup_key = f"{s['symbol']}:{s['signal_strength']}"
                    last_sent = sent_signals.get(dedup_key)
                    if last_sent:
                        age_h = (datetime.now() - datetime.fromisoformat(last_sent)).total_seconds() / 3600
                        if age_h < SIGNAL_DEDUP_H:
                            tlog(f"[dim]{s['symbol']} — alert suppressed ({age_h:.1f}h ago)[/]")
                            continue
                    deduped_signals.append(s)
                    # Persist dedup timestamp
                    try:
                        _ss_conn = db_connect()
                        db_init(_ss_conn)
                        save_sent_signal(_ss_conn, dedup_key, datetime.now().isoformat())
                        _ss_conn.close()
                    except Exception:
                        pass
                    # Send Telegram signal alert
                    capital = _calc_capital(s, context)
                    sl_pct, tp_pct = _estimate_sl_tp_pct(s)
                    send_telegram(
                        f"\U0001f4e1 *{s['signal_strength']} BUY SIGNAL*\n"
                        f"Pair: `{s['symbol']}`\n"
                        f"Entry: `${s['price']:.4f}` | RSI: `{s['rsi']}`\n"
                        f"TP: `${s['price'] * (1 + tp_pct):.4f}` (+{tp_pct*100:.1f}%)  "
                        f"SL: `${s['price'] * (1 - sl_pct):.4f}` (-{sl_pct*100:.1f}%)\n"
                        f"Cost: `${capital} USDC`"
                    )
                signals = deduped_signals  # only show modals for non-suppressed signals

            # ── Daily digest (once per calendar day at DIGEST_HOUR) ────────
            try:
                _dd_conn = db_connect()
                db_init(_dd_conn)
                now = datetime.now()
                last_digest = get_kv(_dd_conn, "last_digest_date") or ""
                if now.hour == DIGEST_HOUR and str(now.date()) != last_digest:
                    _send_daily_digest(get_state_dict(_dd_conn))
                    set_kv(_dd_conn, "last_digest_date", str(now.date()))
                    tlog("[cyan]Daily digest sent[/]")
                _dd_conn.close()
            except Exception as _dd_e:
                tlog(f"[dim]Digest check failed: {markup_escape(str(_dd_e))}[/]")

            # ── Health sentinel ────────────────────────────────────────────
            try:
                _h_conn = db_connect()
                db_init(_h_conn)
                set_kv(_h_conn, "last_scan_ok", datetime.now().isoformat())
                _h_conn.close()
            except Exception:
                pass

            release_scan_lock()
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
                             portfolio=portfolio or {}, positions=positions,
                             open_pnl=open_pnl),
            )

        except Exception as e:
            tlog(f"[red bold]Scan error: {markup_escape(str(e))}[/]")
            try:
                release_scan_lock()
            except Exception:
                pass
        finally:
            self.call_from_thread(setattr, self, "scanning", False)

    # ── ScanComplete handler (main thread) ────────────────────────────────────
    def on_scan_complete(self, msg: ScanComplete) -> None:
        self._pair_results = msg.results
        self._portfolio    = msg.portfolio
        self._positions    = msg.positions
        self._open_pnl     = msg.open_pnl
        self._scan_ctx      = msg.context
        self.last_scan     = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._next_scan_at = time.monotonic() + self._scan_interval

        # Header enrichment: BTC.D + open P&L
        if msg.context.get("btc_dom") is not None:
            self.btc_dom = msg.context["btc_dom"]
            self.btc_dom_rising = bool(msg.context.get("btc_dom_rising"))
        if msg.open_pnl is not None:
            self.open_pnl_usdc = msg.open_pnl
        self._update_header_context()

        # Update pair cards
        results_by_symbol = {r["symbol"]: r for r in msg.results}
        for sym in PAIRS:
            card = self.query_one(f"#pair-{sym.lower()}", PairCard)
            card.update_result(results_by_symbol.get(sym))

        # Update portfolio panel and dismiss LoadingIndicator immediately
        if msg.portfolio:
            self.query_one("#portfolio-widget", PortfolioWidget).update_portfolio(
                msg.portfolio, open_pnl=msg.open_pnl, peak_usdc=self._peak_usdc
            )
            switcher = self.query_one("#left-switcher", ContentSwitcher)
            if switcher.current == "portfolio-loading":
                switcher.current = "portfolio-widget"

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

            # Symbol decorations: breakeven shield + trailing stage
            sym_display = p["symbol"]
            if p.get("breakeven_moved"):
                sym_display = f"\U0001f6e1 {sym_display}"
            trail_stage = p.get("trailing_stage") or 0
            if trail_stage > 0:
                sym_display = f"{sym_display} S{trail_stage}"

            # "Held" — time since entry (hours or days)
            held_str = "—"
            if p.get("time"):
                try:
                    delta_h = (datetime.now() - datetime.fromisoformat(p["time"])).total_seconds() / 3600
                    held_str = f"{delta_h/24:.1f}d" if delta_h >= 24 else f"{delta_h:.0f}h"
                except Exception:
                    pass
            table.add_row(
                sym_display, qty, entry, cur, tp, sl, pnl_cell, held_str,
            )

    def _refresh_history_table(self, trades: list[dict]) -> None:
        table = self.query_one("#history-table", DataTable)
        table.clear()
        closed = [t for t in trades if t.get("status") in ("tp_hit", "sl_hit")]
        for t in reversed(closed[-20:]):
            status  = t.get("status", "open")
            entry_val = float(t.get("entry") or 0)
            exit_val = t.get("exit_price")

            # Breakeven save detection: SL hit but exited at or above entry
            is_be_save = (
                status == "sl_hit"
                and t.get("breakeven_moved")
                and exit_val is not None
                and float(exit_val) >= entry_val
            )
            outcome_base = {"tp_hit": "TP \u2713", "sl_hit": "SL \u2717", "open": "open"}.get(status, status)
            outcome = f"\U0001f6e1 {outcome_base}" if is_be_save else outcome_base

            ts      = (t.get("time") or "")[:16]
            entry   = f"${entry_val:.4f}"
            exit_str = f"${float(exit_val):.4f}" if exit_val is not None else "\u2014"

            # P&L column — colored
            pnl_pct = t.get("pnl_pct")
            if pnl_pct is not None:
                pnl_cell = Text(f"{float(pnl_pct):+.2f}%",
                                style="bold green" if float(pnl_pct) >= 0 else "bold red")
            else:
                pnl_cell = Text("\u2014")

            table.add_row(
                ts,
                t.get("symbol", "\u2014"),
                entry,
                exit_str,
                pnl_cell,
                outcome,
                t.get("signal_strength", "\u2014"),
            )

    # ── Actions ───────────────────────────────────────────────────────────────
    def action_refresh_state(self) -> None:
        self._read_state_file()

    def action_toggle_left_panel(self) -> None:
        self.query_one("#left-panel").toggle_class("hidden")

    def action_toggle_equity(self) -> None:
        """Cycle left panel content: Portfolio ↔ Equity curve."""
        switcher = self.query_one("#left-switcher", ContentSwitcher)
        title    = self.query_one("#left-panel-title", Label)
        if switcher.current == "equity-widget":
            switcher.current = "portfolio-widget"
            title.update(f"[bold {M_BLUE}]PORTFOLIO[/]")
        else:
            switcher.current = "equity-widget"
            title.update(f"[bold {M_MAUVE}]EQUITY CURVE[/]")

    def action_open_settings(self) -> None:
        """Open settings modal — apply new scan interval without restart."""
        def on_dismiss(new_interval: int | None) -> None:
            if new_interval is not None and new_interval != self._scan_interval:
                self._scan_interval = new_interval
                self._scan_timer.stop()
                self._scan_timer = self.set_interval(new_interval, self.action_trigger_scan)
                self.notify(
                    f"Scan will run every {new_interval}s",
                    title="⚙ Settings applied",
                    severity="information",
                    timeout=5,
                )
        self.push_screen(SettingsModal(self._scan_interval), callback=on_dismiss)

    def action_toggle_log(self) -> None:
        log = self.query_one("#log-strip", RichLog)
        log.toggle_class("hidden")


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = ScannerApp()
    app.run()
