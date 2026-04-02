from __future__ import annotations

import json
import math
import os
from typing import Any

from trading.indicators import calc_atr


def generate_dashboard(state: dict[str, Any]) -> None:
    """Generate a self-contained HTML dashboard from scan state."""
    DASHBOARD_FILE = os.path.join(
        os.path.expanduser("~/.agent/diagrams"), "trading-dashboard.html"
    )

    results = state.get("results", [])
    trades = state.get("trades", [])
    history = state.get("history", [])
    last_scan = state.get("last_scan", "")
    portfolio = state.get("portfolio") or {}

    # ── Header enrichment data ────────────────────────────────────────────────
    fg_cache = state.get("fg_cache")
    fg_value = fg_cache.get("value") if fg_cache else None
    fg_class = fg_cache.get("classification", "") if fg_cache else ""
    open_pnl = state.get("open_pnl")
    btc_dom_cache = state.get("btc_dom_cache")
    btc_dom = float(btc_dom_cache["value"]) if btc_dom_cache and btc_dom_cache.get("value") is not None else None
    btc_dom_prev = state.get("btc_dom_prev")
    btc_dom_rising = btc_dom is not None and btc_dom_prev is not None and (btc_dom - btc_dom_prev) > 0.005

    # ── Performance stats ─────────────────────────────────────────────────────
    closed = [t for t in trades if t.get("status") in ("tp_hit", "sl_hit", "timeout")]
    wins = [t for t in closed if t.get("status") == "tp_hit"]
    losses = [t for t in closed if t.get("status") in ("sl_hit", "timeout")]
    win_rate = (len(wins) / len(closed) * 100) if closed else 0
    avg_win = (sum(t.get("pnl_pct", 0) for t in wins) / len(wins)) if wins else 0
    avg_loss = (sum(t.get("pnl_pct", 0) for t in losses) / len(losses)) if losses else 0

    # Profit factor
    total_win_pnl = sum(t.get("pnl_pct", 0) for t in wins)
    total_loss_pnl = sum(abs(t.get("pnl_pct", 0)) for t in losses)
    profit_factor = (total_win_pnl / total_loss_pnl) if total_loss_pnl > 0 else (float("inf") if total_win_pnl > 0 else 0)

    # Sharpe (per-trade, not annualized)
    pnl_series = [t.get("pnl_pct", 0) for t in closed]
    if len(pnl_series) >= 2:
        mean_pnl = sum(pnl_series) / len(pnl_series)
        std_pnl = math.sqrt(sum((x - mean_pnl) ** 2 for x in pnl_series) / (len(pnl_series) - 1))
        sharpe = (mean_pnl / std_pnl) if std_pnl > 0 else 0
    else:
        mean_pnl = pnl_series[0] if pnl_series else 0
        sharpe = 0

    # Max consecutive losses
    max_consec_loss = 0
    cur_consec = 0
    sorted_closed = sorted(closed, key=lambda t: t.get("time", t.get("entry_time", "")))
    for t in sorted_closed:
        if t.get("status") in ("sl_hit", "timeout"):
            cur_consec += 1
            max_consec_loss = max(max_consec_loss, cur_consec)
        else:
            cur_consec = 0

    # Breakeven saves: SL hit but exit_price >= entry (breakeven_moved was active)
    breakeven_saves = sum(
        1 for t in closed
        if t.get("status") == "sl_hit"
        and t.get("breakeven_moved")
        and float(t.get("exit_price") or 0) >= float(t.get("entry") or 1)
    )

    # Net P&L %
    net_pnl = sum(pnl_series)

    recent_signals = history[-10:][::-1]
    open_trades = [t for t in trades if t.get("status") in ("open", "partial_tp")]

    state_json = json.dumps(state, indent=2)

    DASH = "\u2014"  # em-dash (kept outside f-strings for Python 3.11 compat)

    # ── Helper functions ──────────────────────────────────────────────────────
    def rsi_color_cls(rsi: Any) -> str:
        if rsi is None:
            return "badge-none"
        rsi = float(rsi)
        if rsi < 25:
            return "badge-red"
        if rsi < 35:
            return "badge-orange"
        if rsi < 45:
            return "badge-yellow"
        return "badge-green"

    def sig_color_cls(sig: str) -> str:
        return {
            "EXTREME": "badge-red",
            "STRONG": "badge-orange",
            "MODERATE": "badge-yellow",
            "NONE": "badge-none",
        }.get(sig, "badge-none")

    def fg_color(val: Any) -> str:
        if val is None:
            return "var(--muted)"
        val = int(val)
        if val <= 25:
            return "var(--red)"
        if val <= 40:
            return "var(--orange)"
        if val <= 60:
            return "var(--yellow)"
        if val <= 75:
            return "var(--green)"
        return "var(--teal)"

    # ── Header chips ──────────────────────────────────────────────────────────
    header_chips = ""
    if fg_value is not None:
        fc = fg_color(fg_value)
        header_chips += (
            f'<div class="header-chip">'
            f'<span class="chip-label">F&amp;G</span>'
            f'<span class="chip-value" style="color:{fc}">{fg_value}</span>'
            f'<span class="chip-sub" style="color:{fc}">{fg_class}</span>'
            f"</div>"
        )
    if btc_dom is not None:
        dom_arrow = "\u2191" if btc_dom_rising else "\u2193"
        dom_color = "var(--red)" if btc_dom_rising else "var(--green)"
        header_chips += (
            f'<div class="header-chip">'
            f'<span class="chip-label">BTC.D</span>'
            f'<span class="chip-value">{btc_dom:.1f}%</span>'
            f'<span class="chip-sub" style="color:{dom_color}">{dom_arrow}</span>'
            f"</div>"
        )
    if open_pnl is not None:
        pnl_c = "var(--green)" if open_pnl >= 0 else "var(--red)"
        header_chips += (
            f'<div class="header-chip">'
            f'<span class="chip-label">Open P&amp;L</span>'
            f'<span class="chip-value" style="color:{pnl_c}">{open_pnl:+.2f}$</span>'
            f"</div>"
        )

    # ── Pair cards ────────────────────────────────────────────────────────────
    pair_cards_html = ""
    for r in results:
        rsi_cls = rsi_color_cls(r.get("rsi"))
        sig_cls = sig_color_cls(r.get("signal_strength", "NONE"))
        chg = r.get("change24h", 0) or 0
        chg_color = "var(--green)" if chg >= 0 else "var(--red)"
        sma_txt = "above" if r.get("above_sma") else "below"
        vol_txt = "surge" if r.get("vol_surge") else "low"
        mom_txt = "up" if r.get("momentum") else "down"
        price_val = r.get("price", 0) or 0
        price_fmt = f"${price_val:,.6f}" if price_val < 1 else f"${price_val:,.4f}"

        # ATR% from closed_klines
        klines = r.get("closed_klines") or []
        atr = calc_atr(klines) if klines else None
        atr_pct = (atr / price_val * 100) if atr and price_val else None
        atr_html = (
            f'<span class="badge badge-none">ATR {atr_pct:.1f}%</span>'
            if atr_pct is not None
            else ""
        )

        # Divergence status
        div_val = r.get("divergence")
        if div_val is True:
            div_html = '<span class="badge badge-green">Div \u2713</span>'
        elif div_val is False:
            div_html = '<span class="badge badge-red">Div \u2717</span>'
        else:
            div_html = ""

        # Daily RSI
        d_rsi = r.get("daily_rsi")
        if d_rsi is not None:
            d_rsi_val = float(d_rsi)
            if d_rsi_val < 30:
                d_cls = "badge-red"
            elif d_rsi_val < 45:
                d_cls = "badge-orange"
            else:
                d_cls = "badge-green"
            daily_html = f'<span class="badge {d_cls}">1d:{d_rsi_val:.0f}</span>'
        else:
            daily_html = ""

        pair_cards_html += f"""
        <div class="pair-card">
          <div class="pair-symbol">{r.get("symbol", "")}</div>
          <div class="pair-price">{price_fmt}</div>
          <div class="pair-row">
            <span class="badge {rsi_cls}">RSI {r.get("rsi") or DASH}</span>
            <span class="badge {sig_cls}">{r.get("signal_strength", "NONE")}</span>
            {daily_html}
          </div>
          <div class="pair-row">
            {atr_html}{div_html}
          </div>
          <div class="pair-change" style="color:{chg_color}">{chg:+.2f}% 24h</div>
          <div class="pair-indicators">
            SMA:<b>{sma_txt}</b> &nbsp;|&nbsp; Vol:<b>{vol_txt}</b> &nbsp;|&nbsp; Mom:<b>{mom_txt}</b>
          </div>
        </div>"""

    # ── Open positions ────────────────────────────────────────────────────────
    if open_trades:
        pos_rows = ""
        for t in open_trades:
            entry = t.get("entry") or 0
            cur = t.get("current_price") or t.get("entry") or 0
            tp = t.get("tp") or 0
            sl = t.get("sl") or 0
            pnl_pct = t.get("pnl_pct") or (((cur - entry) / entry * 100) if entry else 0)
            pnl_usd = t.get("pnl") or 0
            pnl_color = "var(--green)" if pnl_pct >= 0 else "var(--red)"
            status = t.get("status", "open")

            # Breakeven + trailing indicators
            indicators = ""
            if t.get("breakeven_moved"):
                indicators += '<span class="indicator-shield" title="Breakeven armed">\U0001f6e1</span>'
            trail_stage = t.get("trailing_stage") or 0
            if trail_stage > 0:
                indicators += f'<span class="indicator-stage" title="Trailing stage {trail_stage}">S{trail_stage}</span>'
            if status == "partial_tp":
                indicators += '<span class="indicator-partial" title="Partial TP1 filled">TP1</span>'

            pos_rows += f"""<tr>
              <td>{t.get("symbol", "")}{indicators}</td>
              <td>${entry:.4f}</td>
              <td>${cur:.4f}</td>
              <td>${tp:.4f}</td>
              <td>${sl:.4f}</td>
              <td style="color:{pnl_color}">{pnl_pct:+.2f}%</td>
              <td style="color:{pnl_color}">{pnl_usd:+.2f}$</td>
            </tr>"""
        positions_html = f"""
        <div class="section">
          <h2>Open Positions</h2>
          <table><thead><tr>
            <th>Symbol</th><th>Entry</th><th>Current</th><th>TP</th><th>SL</th><th>P&amp;L%</th><th>P&amp;L$</th>
          </tr></thead><tbody>{pos_rows}</tbody></table>
        </div>"""
    else:
        positions_html = """
        <div class="section">
          <h2>Open Positions</h2>
          <p class="muted">No open positions.</p>
        </div>"""

    # ── Trade history ─────────────────────────────────────────────────────────
    history_rows = ""
    for t in reversed(trades[-20:]):
        status = t.get("status", "open")
        if status == "tp_hit":
            outcome = '<span class="badge badge-green">TP \u2713</span>'
        elif status == "sl_hit":
            outcome = '<span class="badge badge-red">SL \u2717</span>'
        elif status == "timeout":
            outcome = '<span class="badge badge-orange">TMO</span>'
        elif status == "partial_tp":
            outcome = '<span class="badge badge-yellow">TP1</span>'
        elif status in ("no_oco", "partial_tp_no_oco"):
            outcome = '<span class="badge badge-red">NO OCO</span>'
        else:
            outcome = '<span class="badge badge-none">open</span>'

        ts = ""
        raw_ts = t.get("time") or t.get("entry_time") or ""
        if raw_ts:
            ts = raw_ts[:16]

        # Exit price
        exit_price = t.get("exit_price")
        exit_fmt = f"${exit_price:.4f}" if exit_price else "\u2014"

        # P&L% colored
        pnl = t.get("pnl_pct")
        if pnl is not None:
            pnl_color = "var(--green)" if pnl >= 0 else "var(--red)"
            pnl_fmt = f'<span style="color:{pnl_color}">{pnl:+.2f}%</span>'
        else:
            pnl_fmt = "\u2014"

        # Breakeven save marker
        is_be_save = (
            status == "sl_hit"
            and t.get("breakeven_moved")
            and exit_price is not None
            and float(exit_price) >= float(t.get("entry") or 1)
        )
        be_marker = ' <span class="indicator-shield" title="Breakeven save">\U0001f6e1</span>' if is_be_save else ""

        history_rows += f"""<tr>
          <td>{t.get("symbol", "")}</td>
          <td>${(t.get("entry") or 0):.4f}</td>
          <td>{exit_fmt}</td>
          <td>{outcome}{be_marker}</td>
          <td>{pnl_fmt}</td>
          <td>{t.get("signal_strength") or DASH}</td>
          <td>{ts}</td>
        </tr>"""

    if history_rows:
        history_html = f"""
        <div class="section">
          <h2>Trade History <span class="muted">(last 20)</span></h2>
          <table><thead><tr>
            <th>Symbol</th><th>Entry</th><th>Exit</th><th>Outcome</th><th>P&amp;L%</th><th>Signal</th><th>Date</th>
          </tr></thead><tbody>{history_rows}</tbody></table>
        </div>"""
    else:
        history_html = """
        <div class="section">
          <h2>Trade History</h2>
          <p class="muted">No closed trades yet.</p>
        </div>"""

    # ── Recent signals ────────────────────────────────────────────────────────
    sig_rows = ""
    for entry in recent_signals:
        ts = (entry.get("time") or "")[:16]
        for s in entry.get("signals") or []:
            sig_rows += f"""<tr>
              <td>{ts}</td>
              <td>{s.get("symbol", "")}</td>
              <td>${(s.get("price") or 0):.4f}</td>
              <td>RSI {s.get("rsi") or DASH}</td>
              <td><span class="badge {sig_color_cls(s.get("signal_strength", "NONE"))}">{s.get("signal_strength", "NONE")}</span></td>
            </tr>"""

    if sig_rows:
        signals_html = f"""
        <div class="section">
          <h2>Recent Signals <span class="muted">(last 10 scans)</span></h2>
          <table><thead><tr>
            <th>Time</th><th>Symbol</th><th>Price</th><th>RSI</th><th>Tier</th>
          </tr></thead><tbody>{sig_rows}</tbody></table>
        </div>"""
    else:
        signals_html = """
        <div class="section">
          <h2>Recent Signals</h2>
          <p class="muted">No signals recorded yet.</p>
        </div>"""

    # ── Portfolio ─────────────────────────────────────────────────────────────
    ASSET_COLORS = {
        "USDC": "var(--teal)", "BTC": "var(--orange)", "ETH": "var(--blue)",
        "BNB": "var(--yellow)", "ADA": "var(--green)", "SOL": "var(--lavender)",
        "XRP": "var(--blue)", "DOGE": "var(--yellow)", "LUNA": "var(--red)",
    }
    if portfolio and portfolio.get("assets"):
        total_val = portfolio["total_usdc"]
        fetched = (portfolio.get("fetched_at") or "")[:16]
        asset_rows = ""
        for a in portfolio["assets"]:
            color = ASSET_COLORS.get(a["asset"], "var(--text)")
            pct = a["pct"]
            bar_w = max(2, round(pct))
            price_fmt = (
                f"${a['price_usdc']:,.4f}"
                if a["price_usdc"] < 100
                else f"${a['price_usdc']:,.2f}"
            )
            qty_fmt = f"{a['qty']:.6f}".rstrip("0").rstrip(".")
            asset_rows += f"""
      <div class="port-row">
        <div class="port-asset" style="color:{color}">{a['asset']}</div>
        <div class="port-qty">{qty_fmt}</div>
        <div class="port-price">{price_fmt}</div>
        <div class="port-value">${a['value_usdc']:,.2f}</div>
        <div class="port-bar-wrap">
          <div class="port-bar" style="width:{bar_w}%;background:{color}"></div>
          <span class="port-pct">{pct:.1f}%</span>
        </div>
      </div>"""
        portfolio_html = f"""
<div class="section">
  <div class="section-head-row">
    <h2>Portfolio</h2>
    <span class="port-total">${total_val:,.2f} <span class="muted">USDC</span></span>
    <span class="muted port-ts">updated {fetched}</span>
  </div>
  <div class="port-header">
    <span>Asset</span><span>Balance</span><span>Price</span><span>Value</span><span>Allocation</span>
  </div>
{asset_rows}
</div>"""
    else:
        portfolio_html = """
<div class="section">
  <h2>Portfolio</h2>
  <p class="muted">No portfolio data \u2014 run scanner to fetch.</p>
</div>"""

    # ── Equity curve SVG ──────────────────────────────────────────────────────
    equity_svg = ""
    if len(sorted_closed) >= 2:
        cum_pnl = []
        running = 0.0
        for t in sorted_closed:
            running += t.get("pnl_pct", 0)
            cum_pnl.append(running)

        svg_w, svg_h = 600, 160
        pad_x, pad_y = 40, 20
        plot_w = svg_w - 2 * pad_x
        plot_h = svg_h - 2 * pad_y

        y_min = min(min(cum_pnl), 0)
        y_max = max(max(cum_pnl), 0)
        y_range = y_max - y_min or 1

        n = len(cum_pnl)
        points = []
        for i, v in enumerate(cum_pnl):
            x = pad_x + (i / max(n - 1, 1)) * plot_w
            y = pad_y + plot_h - ((v - y_min) / y_range) * plot_h
            points.append(f"{x:.1f},{y:.1f}")

        # Zero line y
        zero_y = pad_y + plot_h - ((0 - y_min) / y_range) * plot_h

        # Line color based on final value
        line_color = "#a6e3a1" if cum_pnl[-1] >= 0 else "#f38ba8"

        # Fill area: build closed polygon from line to zero
        fill_points = points.copy()
        fill_points.append(f"{pad_x + plot_w:.1f},{zero_y:.1f}")
        fill_points.append(f"{pad_x:.1f},{zero_y:.1f}")

        equity_svg = f"""
    <div class="section">
      <h2>Equity Curve <span class="muted">(cumulative P&amp;L %)</span></h2>
      <div class="equity-wrap">
        <svg viewBox="0 0 {svg_w} {svg_h}" class="equity-svg">
          <rect x="0" y="0" width="{svg_w}" height="{svg_h}" fill="#181825" rx="8"/>
          <line x1="{pad_x}" y1="{zero_y:.1f}" x2="{pad_x + plot_w}" y2="{zero_y:.1f}"
                stroke="#45475a" stroke-width="1" stroke-dasharray="4,3"/>
          <polygon points="{' '.join(fill_points)}"
                   fill="{line_color}" fill-opacity="0.1"/>
          <polyline points="{' '.join(points)}"
                    fill="none" stroke="{line_color}" stroke-width="2"
                    stroke-linejoin="round" stroke-linecap="round"/>
          <text x="{pad_x - 4}" y="{zero_y:.1f}" fill="#6c7086" font-size="10"
                text-anchor="end" dominant-baseline="middle">0%</text>
          <text x="{pad_x - 4}" y="{pad_y + 4}" fill="#6c7086" font-size="10"
                text-anchor="end">{y_max:+.1f}%</text>
          <text x="{pad_x - 4}" y="{pad_y + plot_h}" fill="#6c7086" font-size="10"
                text-anchor="end">{y_min:+.1f}%</text>
          <circle cx="{points[-1].split(',')[0]}" cy="{points[-1].split(',')[1]}"
                  r="3" fill="{line_color}"/>
          <text x="{float(points[-1].split(',')[0]) + 6}"
                y="{float(points[-1].split(',')[1])}"
                fill="{line_color}" font-size="11" font-weight="600"
                dominant-baseline="middle">{cum_pnl[-1]:+.2f}%</text>
        </svg>
      </div>
    </div>"""

    # ── Performance stats cards ───────────────────────────────────────────────
    pf_display = f"{profit_factor:.2f}" if profit_factor != float("inf") else "\u221e"
    sharpe_color = "var(--green)" if sharpe > 0 else "var(--red)" if sharpe < 0 else "var(--muted)"

    stats_html = f"""
    <div class="section">
      <h2>Performance Stats</h2>
      <div class="stats-grid">
        <div class="stat-card">
          <div class="stat-label">Win Rate</div>
          <div class="stat-value" style="color:var(--green)">{win_rate:.0f}%</div>
        </div>
        <div class="stat-card">
          <div class="stat-label">Total Trades</div>
          <div class="stat-value">{len(closed)}</div>
        </div>
        <div class="stat-card">
          <div class="stat-label">Wins / Losses</div>
          <div class="stat-value">
            <span style="color:var(--green)">{len(wins)}</span>
            <span style="color:var(--muted)">/</span>
            <span style="color:var(--red)">{len(losses)}</span>
          </div>
        </div>
        <div class="stat-card">
          <div class="stat-label">Avg Win</div>
          <div class="stat-value" style="color:var(--green)">{avg_win:+.2f}%</div>
        </div>
        <div class="stat-card">
          <div class="stat-label">Avg Loss</div>
          <div class="stat-value" style="color:var(--red)">{avg_loss:+.2f}%</div>
        </div>
        <div class="stat-card">
          <div class="stat-label">Net P&amp;L</div>
          <div class="stat-value" style="color:{"var(--green)" if net_pnl >= 0 else "var(--red)"}">{net_pnl:+.2f}%</div>
        </div>
        <div class="stat-card">
          <div class="stat-label">Profit Factor</div>
          <div class="stat-value" style="color:{"var(--green)" if profit_factor >= 1 else "var(--red)"}">{pf_display}</div>
        </div>
        <div class="stat-card">
          <div class="stat-label">Sharpe (per-trade)</div>
          <div class="stat-value" style="color:{sharpe_color}">{sharpe:.2f}</div>
        </div>
        <div class="stat-card">
          <div class="stat-label">Max Consec. Loss</div>
          <div class="stat-value" style="color:var(--red)">{max_consec_loss}</div>
        </div>
        <div class="stat-card">
          <div class="stat-label">Breakeven Saves</div>
          <div class="stat-value" style="color:var(--teal)">{breakeven_saves}</div>
        </div>
      </div>
    </div>"""

    # ── Assemble HTML ─────────────────────────────────────────────────────────
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Trading Dashboard</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:wght@400;600;700&family=Fragment+Mono&display=swap">
<script>const STATE = {state_json};</script>
<style>
  :root {{
    --bg:       #1e1e2e;
    --surface:  #313244;
    --crust:    #181825;
    --green:    #a6e3a1;
    --red:      #f38ba8;
    --orange:   #fab387;
    --yellow:   #f9e2af;
    --blue:     #89b4fa;
    --teal:     #94e2d5;
    --lavender: #b4befe;
    --text:     #cdd6f4;
    --muted:    #6c7086;
    --border:   #45475a;
  }}
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    background: var(--bg);
    color: var(--text);
    font-family: 'Bricolage Grotesque', sans-serif;
    font-size: 14px;
    padding: 24px;
    min-height: 100vh;
  }}

  /* ── Header ──────────────────────────────────────────────────────────────── */
  header {{
    display: flex;
    align-items: center;
    gap: 16px;
    margin-bottom: 28px;
    border-bottom: 1px solid var(--border);
    padding-bottom: 16px;
    flex-wrap: wrap;
  }}
  header h1 {{
    font-size: 22px;
    font-weight: 700;
    letter-spacing: 0.12em;
    color: var(--blue);
  }}
  .header-meta {{
    font-family: 'Fragment Mono', monospace;
    font-size: 12px;
    color: var(--muted);
  }}
  .header-chips {{
    display: flex;
    gap: 12px;
    margin-left: auto;
    flex-wrap: wrap;
  }}
  .header-chip {{
    display: flex;
    align-items: center;
    gap: 6px;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 6px 12px;
    font-family: 'Fragment Mono', monospace;
    font-size: 12px;
  }}
  .chip-label {{
    color: var(--muted);
    font-size: 10px;
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }}
  .chip-value {{
    font-weight: 700;
    font-size: 14px;
  }}
  .chip-sub {{
    font-size: 11px;
  }}

  /* ── Sections ────────────────────────────────────────────────────────────── */
  .section {{
    margin-bottom: 32px;
  }}
  .section h2 {{
    font-size: 13px;
    font-weight: 600;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--muted);
    margin-bottom: 14px;
  }}

  /* ── Pair cards ──────────────────────────────────────────────────────────── */
  .grid-pairs {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
    gap: 14px;
  }}
  .pair-card {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 16px;
  }}
  .pair-symbol {{
    font-family: 'Fragment Mono', monospace;
    font-size: 13px;
    font-weight: 600;
    color: var(--blue);
    margin-bottom: 6px;
  }}
  .pair-price {{
    font-family: 'Fragment Mono', monospace;
    font-size: 16px;
    font-weight: 700;
    margin-bottom: 10px;
  }}
  .pair-row {{
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
    margin-bottom: 8px;
  }}
  .pair-change {{
    font-family: 'Fragment Mono', monospace;
    font-size: 12px;
    margin-bottom: 8px;
  }}
  .pair-indicators {{
    font-size: 11px;
    color: var(--muted);
  }}
  .pair-indicators b {{
    color: var(--text);
  }}

  /* ── Badges ──────────────────────────────────────────────────────────────── */
  .badge {{
    display: inline-block;
    font-family: 'Fragment Mono', monospace;
    font-size: 11px;
    padding: 2px 8px;
    border-radius: 4px;
    font-weight: 600;
  }}
  .badge-red    {{ background: color-mix(in srgb, var(--red)    20%, transparent); color: var(--red);    border: 1px solid color-mix(in srgb, var(--red)    40%, transparent); }}
  .badge-orange {{ background: color-mix(in srgb, var(--orange) 20%, transparent); color: var(--orange); border: 1px solid color-mix(in srgb, var(--orange) 40%, transparent); }}
  .badge-yellow {{ background: color-mix(in srgb, var(--yellow) 20%, transparent); color: var(--yellow); border: 1px solid color-mix(in srgb, var(--yellow) 40%, transparent); }}
  .badge-green  {{ background: color-mix(in srgb, var(--green)  20%, transparent); color: var(--green);  border: 1px solid color-mix(in srgb, var(--green)  40%, transparent); }}
  .badge-none   {{ background: color-mix(in srgb, var(--muted)  20%, transparent); color: var(--muted);  border: 1px solid color-mix(in srgb, var(--muted)  40%, transparent); }}

  /* ── Tables ──────────────────────────────────────────────────────────────── */
  table {{
    width: 100%;
    border-collapse: collapse;
    font-family: 'Fragment Mono', monospace;
    font-size: 12px;
  }}
  thead th {{
    text-align: left;
    padding: 8px 12px;
    color: var(--muted);
    font-weight: 600;
    letter-spacing: 0.06em;
    border-bottom: 1px solid var(--border);
  }}
  tbody tr {{
    border-bottom: 1px solid color-mix(in srgb, var(--border) 50%, transparent);
  }}
  tbody tr:hover {{
    background: color-mix(in srgb, var(--surface) 60%, transparent);
  }}
  tbody td {{
    padding: 8px 12px;
    vertical-align: middle;
  }}

  /* ── Position indicators ─────────────────────────────────────────────────── */
  .indicator-shield {{
    margin-left: 6px;
    font-size: 13px;
  }}
  .indicator-stage {{
    margin-left: 4px;
    font-family: 'Fragment Mono', monospace;
    font-size: 10px;
    background: color-mix(in srgb, var(--teal) 20%, transparent);
    color: var(--teal);
    border: 1px solid color-mix(in srgb, var(--teal) 40%, transparent);
    padding: 1px 5px;
    border-radius: 3px;
    font-weight: 600;
  }}
  .indicator-partial {{
    margin-left: 4px;
    font-family: 'Fragment Mono', monospace;
    font-size: 10px;
    background: color-mix(in srgb, var(--yellow) 20%, transparent);
    color: var(--yellow);
    border: 1px solid color-mix(in srgb, var(--yellow) 40%, transparent);
    padding: 1px 5px;
    border-radius: 3px;
    font-weight: 600;
  }}

  /* ── Stats grid ──────────────────────────────────────────────────────────── */
  .stats-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
    gap: 12px;
  }}
  .stat-card {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 14px 18px;
  }}
  .stat-label {{
    font-size: 11px;
    color: var(--muted);
    letter-spacing: 0.08em;
    text-transform: uppercase;
    margin-bottom: 6px;
  }}
  .stat-value {{
    font-family: 'Fragment Mono', monospace;
    font-size: 22px;
    font-weight: 700;
  }}

  /* ── Equity curve ────────────────────────────────────────────────────────── */
  .equity-wrap {{
    background: var(--crust);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 12px;
    overflow: hidden;
  }}
  .equity-svg {{
    width: 100%;
    max-width: 700px;
    height: auto;
    display: block;
    font-family: 'Fragment Mono', monospace;
  }}

  /* ── Portfolio ───────────────────────────────────────────────────────────── */
  .section-head-row {{
    display: flex;
    align-items: baseline;
    gap: 16px;
    margin-bottom: 14px;
    flex-wrap: wrap;
  }}
  .section-head-row h2 {{ margin-bottom: 0; }}
  .port-total {{
    font-family: 'Fragment Mono', monospace;
    font-size: 20px;
    font-weight: 700;
    color: var(--teal);
  }}
  .port-ts {{ font-family: 'Fragment Mono', monospace; font-size: 11px; margin-left: auto; }}
  .port-header {{
    display: grid;
    grid-template-columns: 80px 130px 110px 110px 1fr;
    font-size: 11px;
    color: var(--muted);
    letter-spacing: 0.06em;
    text-transform: uppercase;
    padding: 0 4px 8px;
    border-bottom: 1px solid var(--border);
    margin-bottom: 4px;
  }}
  .port-row {{
    display: grid;
    grid-template-columns: 80px 130px 110px 110px 1fr;
    align-items: center;
    padding: 10px 4px;
    border-bottom: 1px solid color-mix(in srgb, var(--border) 40%, transparent);
    font-family: 'Fragment Mono', monospace;
    font-size: 12px;
  }}
  .port-row:hover {{ background: color-mix(in srgb, var(--surface) 50%, transparent); }}
  .port-asset {{ font-weight: 700; font-size: 13px; }}
  .port-qty, .port-price, .port-value {{ color: var(--text); }}
  .port-value {{ font-weight: 600; }}
  .port-bar-wrap {{
    display: flex;
    align-items: center;
    gap: 8px;
  }}
  .port-bar {{
    height: 6px;
    border-radius: 3px;
    min-width: 4px;
    transition: width 0.3s ease;
  }}
  .port-pct {{ font-size: 11px; color: var(--muted); white-space: nowrap; }}

  .muted {{ color: var(--muted); }}
  footer {{
    margin-top: 40px;
    padding-top: 16px;
    border-top: 1px solid var(--border);
    font-family: 'Fragment Mono', monospace;
    font-size: 11px;
    color: var(--muted);
    display: flex;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 8px;
  }}
</style>
</head>
<body>

<header>
  <h1>TRADING DASHBOARD</h1>
  <div class="header-meta">Last scan: {last_scan[:19] if last_scan else DASH}</div>
  <div class="header-chips">{header_chips}</div>
</header>

{portfolio_html}

<div class="section">
  <h2>Market Overview</h2>
  <div class="grid-pairs">{pair_cards_html}</div>
</div>

{positions_html}

{history_html}

{stats_html}

{equity_svg}

{signals_html}

<footer>
  <span>{DASHBOARD_FILE}</span>
  <span>Auto-generated by scanner.py on each scan</span>
</footer>

</body>
</html>"""

    os.makedirs(os.path.dirname(DASHBOARD_FILE), exist_ok=True)
    with open(DASHBOARD_FILE, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  \u2713 Dashboard \u2192 {DASHBOARD_FILE}")
