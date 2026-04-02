from __future__ import annotations

import json
import os
from typing import Any


def generate_dashboard(state: dict[str, Any]) -> None:
    """Generate a self-contained HTML dashboard from scan state."""
    DASHBOARD_FILE = os.path.join(os.path.expanduser("~/.agent/diagrams"), "trading-dashboard.html")

    results   = state.get("results", [])
    trades    = state.get("trades", [])
    history   = state.get("history", [])
    last_scan = state.get("last_scan", "")
    portfolio = state.get("portfolio") or {}

    # ── Performance stats ─────────────────────────────────────────────────────
    closed = [t for t in trades if t.get("status") in ("tp_hit", "sl_hit")]
    wins   = [t for t in closed if t.get("status") == "tp_hit"]
    losses = [t for t in closed if t.get("status") == "sl_hit"]
    win_rate = (len(wins) / len(closed) * 100) if closed else 0
    avg_win  = (sum(t.get("pnl_pct", 0) for t in wins)   / len(wins))   if wins   else 0
    avg_loss = (sum(t.get("pnl_pct", 0) for t in losses) / len(losses)) if losses else 0

    recent_signals = history[-10:][::-1]
    open_trades = [t for t in trades if t.get("status") == "open"]

    state_json = json.dumps(state, indent=2)

    def rsi_color_cls(rsi):
        if rsi is None:
            return "badge-none"
        if rsi < 25:
            return "badge-red"
        if rsi < 35:
            return "badge-orange"
        if rsi < 45:
            return "badge-yellow"
        return "badge-green"

    def sig_color_cls(sig):
        return {"EXTREME": "badge-red", "STRONG": "badge-orange",
                "MODERATE": "badge-yellow", "NONE": "badge-none"}.get(sig, "badge-none")

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
        pair_cards_html += f"""
        <div class="pair-card">
          <div class="pair-symbol">{r.get("symbol","")}</div>
          <div class="pair-price">{price_fmt}</div>
          <div class="pair-row">
            <span class="badge {rsi_cls}">RSI {r.get("rsi","—")}</span>
            <span class="badge {sig_cls}">{r.get("signal_strength","NONE")}</span>
          </div>
          <div class="pair-change" style="color:{chg_color}">{chg:+.2f}% 24h</div>
          <div class="pair-indicators">
            SMA:<b>{sma_txt}</b> &nbsp;|&nbsp; Vol:<b>{vol_txt}</b> &nbsp;|&nbsp; Mom:<b>{mom_txt}</b>
          </div>
        </div>"""

    if open_trades:
        pos_rows = ""
        for t in open_trades:
            entry = t.get("entry") or 0
            cur   = t.get("current_price") or t.get("entry") or 0
            tp    = t.get("tp") or 0
            sl    = t.get("sl") or 0
            pnl_pct = t.get("pnl_pct") or (((cur - entry) / entry * 100) if entry else 0)
            pnl_usd = t.get("pnl") or 0
            pnl_color = "var(--green)" if pnl_pct >= 0 else "var(--red)"
            pos_rows += f"""<tr>
              <td>{t.get("symbol","")}</td>
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

    history_rows = ""
    for t in reversed(trades[-20:]):
        status = t.get("status", "open")
        if status == "tp_hit":
            outcome = '<span class="badge badge-green">TP \u2713</span>'
        elif status == "sl_hit":
            outcome = '<span class="badge badge-red">SL \u2717</span>'
        else:
            outcome = '<span class="badge badge-none">open</span>'
        ts = t.get("time", t.get("entry_time", ""))[:16] if (t.get("time") or t.get("entry_time")) else "—"
        history_rows += f"""<tr>
          <td>{t.get("symbol","")}</td>
          <td>${(t.get("entry") or 0):.4f}</td>
          <td>{outcome}</td>
          <td>{t.get("signal_strength","—")}</td>
          <td>{ts}</td>
        </tr>"""

    if history_rows:
        history_html = f"""
        <div class="section">
          <h2>Trade History <span class="muted">(last 20)</span></h2>
          <table><thead><tr>
            <th>Symbol</th><th>Entry</th><th>Outcome</th><th>Signal</th><th>Date</th>
          </tr></thead><tbody>{history_rows}</tbody></table>
        </div>"""
    else:
        history_html = """
        <div class="section">
          <h2>Trade History</h2>
          <p class="muted">No closed trades yet.</p>
        </div>"""

    sig_rows = ""
    for entry in recent_signals:
        ts = (entry.get("time") or "")[:16]
        for s in (entry.get("signals") or []):
            sig_rows += f"""<tr>
              <td>{ts}</td>
              <td>{s.get("symbol","")}</td>
              <td>${(s.get("price") or 0):.4f}</td>
              <td>RSI {s.get("rsi","—")}</td>
              <td><span class="badge {sig_color_cls(s.get("signal_strength","NONE"))}">{s.get("signal_strength","NONE")}</span></td>
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

    ASSET_COLORS = {
        "USDC": "var(--teal)", "BTC": "var(--orange)", "ETH": "var(--blue)",
        "BNB":  "var(--yellow)", "ADA": "var(--green)", "SOL": "var(--lavender, #b4befe)",
        "XRP":  "var(--blue)", "DOGE": "var(--yellow)", "LUNA": "var(--red)",
    }
    if portfolio and portfolio.get("assets"):
        total_val = portfolio["total_usdc"]
        fetched   = (portfolio.get("fetched_at") or "")[:16]
        asset_rows = ""
        for a in portfolio["assets"]:
            color  = ASSET_COLORS.get(a["asset"], "var(--text)")
            pct    = a["pct"]
            bar_w  = max(2, round(pct))
            price_fmt = f"${a['price_usdc']:,.4f}" if a["price_usdc"] < 100 else f"${a['price_usdc']:,.2f}"
            qty_fmt   = f"{a['qty']:.6f}".rstrip("0").rstrip(".")
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
    --bg:      #1e1e2e;
    --surface: #313244;
    --green:   #a6e3a1;
    --red:     #f38ba8;
    --orange:  #fab387;
    --yellow:  #f9e2af;
    --blue:    #89b4fa;
    --teal:    #94e2d5;
    --text:    #cdd6f4;
    --muted:   #6c7086;
    --border:  #45475a;
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
  header {{
    display: flex;
    align-items: baseline;
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
  .header-refresh {{
    font-size: 11px;
    color: var(--teal);
    margin-left: auto;
  }}
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
  .grid-pairs {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
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
</style>
</head>
<body>

<header>
  <h1>TRADING DASHBOARD</h1>
  <div class="header-meta">Last scan: {last_scan[:19] if last_scan else "—"}</div>
  <div class="header-refresh">Auto-refreshes each scanner run</div>
</header>

{portfolio_html}

<div class="section">
  <h2>Market Overview</h2>
  <div class="grid-pairs">{pair_cards_html}</div>
</div>

{positions_html}

{history_html}

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
      <div class="stat-label">Wins</div>
      <div class="stat-value" style="color:var(--green)">{len(wins)}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Losses</div>
      <div class="stat-value" style="color:var(--red)">{len(losses)}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Avg Win</div>
      <div class="stat-value" style="color:var(--green)">{avg_win:+.2f}%</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Avg Loss</div>
      <div class="stat-value" style="color:var(--red)">{avg_loss:+.2f}%</div>
    </div>
  </div>
</div>

{signals_html}

<footer>
  <span>{DASHBOARD_FILE}</span>
  <span>Auto-g\u00e9n\u00e9r\u00e9 par scanner.py \u00e0 chaque scan</span>
</footer>

</body>
</html>"""

    os.makedirs(os.path.dirname(DASHBOARD_FILE), exist_ok=True)
    with open(DASHBOARD_FILE, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  \u2713 Dashboard \u2192 {DASHBOARD_FILE}")
