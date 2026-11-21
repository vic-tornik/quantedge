"""Local, read-only view of the active trading journal."""
from datetime import datetime, timezone
from decimal import Decimal
from html import escape
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import time
from urllib.parse import urlsplit

from engine import ACTIVE
from foundation import market_view, latest_snapshot, health_view
from depth import depth_view


def page(journal):
    def esc(value):
        return escape(str(value))

    orders = journal.history()
    paused = journal.config("paused") == "1"
    rows = []
    for order in orders:
        executed = "—"
        if order["result"]:
            try:
                executed = json.loads(order["result"]).get("executedQty", "—")
            except (AttributeError, ValueError):
                pass
        created = datetime.fromtimestamp(order["created"], timezone.utc).strftime("%d %b %Y, %H:%M:%S")
        status = order["status"]
        state_class = "warn" if status in ACTIVE else "good" if status == "FILLED" else "neutral"
        side_class = "buy" if order["side"] == "BUY" else "sell"
        rows.append(
            f"<tr><td>{esc(created)}</td><td class=pair>{esc(order['symbol'])}</td>"
            f"<td><span class='{side_class}'>{esc(order['side'])}</span></td>"
            f"<td>{esc(order['qty'])}</td><td>{esc(order['price'])}</td>"
            f"<td>{esc(executed)}</td><td><span class='badge {state_class}'>{esc(status.replace('_', ' '))}</span></td>"
            f"<td><code>{esc(order['id'])}</code></td></tr>"
        )
    rows_html = "".join(rows) or (
        "<tr><td colspan=8><div class=empty><strong>No orders yet</strong>"
        "<span>Your order tickets will appear here after you create a preview.</span></div></td></tr>"
    )
    balances = ""
    if journal.mode == "paper":
        wallet = journal.wallet()
        for asset in ("USDT", "BTC", "ETH"):
            balances += f"<div class=balance><span>{asset}</span><strong>{esc(wallet.get(asset, '0'))}</strong><small>Available</small></div>"
    else:
        balances = ("<div class=account-note>Exchange balances are available in your terminal with "
                    f"<code>python quantedge.py --mode {esc(journal.mode)} balance</code>.</div>")
    attention = sum(1 for item in orders if item["status"] in ACTIVE)
    quotes = market_view(journal)
    market_rows = ""
    for quote in quotes:
        age = "Awaiting stream" if quote["age_seconds"] is None else f"{quote['age_seconds']:.1f}s ago"
        health = "Stale" if quote["stale"] else "Fresh"
        market_rows += (f"<tr><td>{esc(quote['symbol'])}</td><td>{esc(quote['bid'] or '—')}</td>"
                        f"<td>{esc(quote['ask'] or '—')}</td><td>{health}</td><td>{esc(age)}</td></tr>")
    portfolio = latest_snapshot(journal)
    depth_html = ""
    for item in depth_view(journal):
        depth_html += f"<section class=card><div class=section-title><div><h2>{esc(item['symbol'])} depth</h2>"
        if not item["fresh"]:
            depth_html += "<p>Awaiting synchronized, fresh depth</p></div></div></section>"
            continue
        book = item["book"]
        depth_html += f"<p>Update {esc(book['lastUpdateId'])} · top 10 recorded levels</p></div></div>"
        depth_html += "<div class=table-wrap><table style='min-width:480px'><thead><tr><th>Bid · USDT</th><th>Bid quantity</th><th>Ask · USDT</th><th>Ask quantity</th></tr></thead><tbody>"
        for index in range(max(len(book["bids"][:10]),len(book["asks"][:10]))):
            bid = book["bids"][index] if index < len(book["bids"]) else ("—","—")
            ask = book["asks"][index] if index < len(book["asks"]) else ("—","—")
            depth_html += f"<tr><td class=buy>{esc(bid[0])}</td><td>{esc(bid[1])}</td><td class=sell>{esc(ask[0])}</td><td>{esc(ask[1])}</td></tr>"
        depth_html += "</tbody></table></div><p class=note>Depth is limited to the original snapshot coverage.</p></section>"
    health = health_view(journal)
    health_html = ""
    for component in health:
        age = max(0,int(time.time()-component["received"]))
        health_html += (f"<div class=balance><span>{esc(component['component'].capitalize())}</span>"
                        f"<strong>{esc(component['state'])}</strong>"
                        f"<small>Last report {age}s ago · {esc(component['detail'])}</small></div>")
    if not health_html:
        health_html = "<p>No collector reports yet. Start the collector in another terminal.</p>"
    portfolio_html = "<p>Start the collector to populate portfolio snapshots.</p>"
    if portfolio:
        fresh = 0 <= time.time()-portfolio["received"] <= 30
        # Any stale quote invalidates displayed valuations. Last prices remain visible with their age.
        priced = (fresh and all(not q["stale"] for q in quotes)
                  and not any(h["component"] == "portfolio" and h["state"] == "error" for h in health))
        portfolio_html = "<div class='grid'>"
        for label, key in (("Wallet equity · USDT","equity_USDT"),("Tracked realized P&L · USDT","realized_USDT"),
                           ("Tracked unrealized P&L · USDT","unrealized_USDT")):
            value = portfolio.get(key) if priced else None
            portfolio_html += f"<div><div class=label>{label}</div><div class=metric>{esc(value if value is not None else 'Unavailable')}</div></div>"
        portfolio_html += "</div><p class=sub>Snapshot age " + str(max(0,int(time.time()-portfolio["received"]))) + "s · All current marks must be fresh.</p>"
        for issue in portfolio["accounting_issues"]:
            portfolio_html += f"<p class=note>{esc(issue)}</p>"
        if portfolio["unpriced_assets"]:
            portfolio_html += f"<p class=note>Unpriced assets · {esc(', '.join(portfolio['unpriced_assets']))}</p>"
        portfolio_html += "<div class=note>Recorded commissions · " + esc(json.dumps(portfolio["fees"])) + "</div>"
        portfolio_html += "<div class='table-wrap'><table style='min-width:400px'><thead><tr><th>Tracked asset</th><th>Quantity</th><th>Cost · USDT</th></tr></thead><tbody>"
        for asset, position in portfolio["positions"].items():
            portfolio_html += f"<tr><td>{esc(asset)}</td><td>{esc(position['quantity'])}</td><td>{esc(position['cost'])}</td></tr>"
        portfolio_html += "</tbody></table></div>"
        if journal.mode != "paper":
            balances = ""
            for asset, quantity in portfolio["balances"].items():
                if Decimal(quantity) != 0:
                    balances += f"<div class=balance><span>{esc(asset)}</span><strong>{esc(quantity)}</strong><small>Free + locked · snapshot balance</small></div>"
            if not balances:
                balances = "<p>No nonzero balances in the latest snapshot.</p>"
    guidance = ""
    if attention:
        guidance = (
            "<div class='notice' role='status'><strong>Order status needs attention</strong>"
            "<span>Use the ticket ID with the status command to query the exchange before submitting another order.</span>"
            f"<code>python quantedge.py --mode {esc(journal.mode)} status TICKET_ID</code></div>"
        )
    mode = esc(journal.mode.upper())
    state = "Paused" if paused else "Enabled"
    state_class = "warn" if paused else "good"
    now = datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M:%S UTC")
    html = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="5"><title>QuantEdge | Overview</title>
<style>
:root{color-scheme:dark;--bg:#0c1115;--panel:#151d21;--border:#29363a;--muted:#94a6a3;--text:#edf3f0;--mint:#9be3c1;--amber:#ebc47d}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 "Segoe UI",Arial,sans-serif}
.shell{max-width:1500px;margin:auto;padding:0 42px 60px}.top{height:82px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid var(--border);gap:20px}
.brand{display:flex;align-items:center;gap:12px;font-size:18px;font-weight:700;letter-spacing:-.04em}.mark{display:grid;place-items:center;width:34px;height:34px;background:var(--mint);color:#0c2118;border-radius:10px;font-size:18px}
.top-right{display:flex;align-items:center;gap:12px;color:var(--muted)}.mode{color:var(--mint);border:1px solid #416b59;background:#192d25;border-radius:7px;padding:5px 10px;font-size:11px;font-weight:700;letter-spacing:.08em}
.heading{display:flex;align-items:end;justify-content:space-between;gap:20px;margin:48px 0 27px}.eyebrow{text-transform:uppercase;color:var(--mint);letter-spacing:.13em;font-size:11px;font-weight:700;margin:0 0 10px}
h1{font-size:clamp(30px,4vw,46px);letter-spacing:-.045em;line-height:1.1;margin:0 0 10px}p{margin:0;color:var(--muted)}.refresh{font-size:12px;text-align:right}.refresh span{display:block;color:var(--text);margin-top:4px}
.grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:14px}.card{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:22px}.label{color:var(--muted);font-size:12px}.metric{font-size:26px;letter-spacing:-.04em;font-weight:650;margin:13px 0 4px}.sub{font-size:12px;color:var(--muted)}.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--mint);margin-right:9px}.dot.warn{background:var(--amber)}
.content{display:grid;grid-template-columns:minmax(0,1fr) 300px;gap:14px;margin-top:14px}.section-title{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;margin-bottom:22px}.section-title h2{font-size:16px;letter-spacing:-.02em;margin:0 0 4px}.section-title p{font-size:12px}.count{color:var(--muted);font-size:12px;white-space:nowrap}
.table-wrap{overflow:auto;margin:0 -22px}table{border-collapse:collapse;width:100%;min-width:850px;text-align:left;font-size:12px}th{color:var(--muted);font-weight:500;background:#192327}th,td{padding:13px 14px;border-bottom:1px solid #273438;white-space:nowrap}th:first-child,td:first-child{padding-left:22px}tr:last-child td{border:0}.pair{font-weight:700;color:#f0f7f2}.buy{color:#a3e7c3}.sell{color:#f2b5a7}.badge{padding:5px 8px;border-radius:5px;font-size:10px;font-weight:700;letter-spacing:.03em}.good{color:#a3e7c3;background:#223b30}.warn{color:#f1ca85;background:#433723}.neutral{color:#bccbc7;background:#29383a}code{font:11px Consolas,monospace;color:#bdcdc7}.empty{text-align:center;padding:40px 12px;white-space:normal}.empty strong,.empty span{display:block}.empty span{color:var(--muted);margin-top:5px}
.balance{display:grid;grid-template-columns:1fr auto;gap:2px;align-items:center;padding:16px 0;border-bottom:1px solid var(--border)}.balance:first-of-type{padding-top:0}.balance strong{font-size:15px;font-weight:600;overflow-wrap:anywhere;text-align:right}.balance small{color:var(--muted);grid-column:1/-1}.account-note{font-size:12px;color:var(--muted);overflow-wrap:anywhere}.account-note code{display:block;margin-top:10px;white-space:normal;line-height:1.6}
.note{margin-top:18px;padding-top:18px;border-top:1px solid var(--border);font-size:12px;color:var(--muted)}.footer{margin-top:20px;color:var(--muted);font-size:12px}
.notice{margin-top:14px;border:1px solid #725a35;background:#312a1e;border-radius:10px;padding:15px 18px;color:#f1d199}.notice strong,.notice span,.notice code{display:block}.notice span{margin-top:4px;color:#ddd0b8}.notice code{margin-top:8px;overflow-wrap:anywhere;color:#f1d199}
.depth-grid{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:14px;margin-top:14px}.depth-grid .card{min-width:0}
@media(max-width:960px){.content,.depth-grid{grid-template-columns:1fr}.grid{grid-template-columns:repeat(3,minmax(0,1fr))}}
@media(max-width:620px){.shell{padding:0 18px 36px}.top{height:70px}.top-right>span:first-child{display:none}.heading{align-items:start;flex-direction:column;margin:34px 0 22px}.refresh{text-align:left}.grid{grid-template-columns:1fr}.card{padding:18px}.table-wrap{margin:0 -18px}th:first-child,td:first-child{padding-left:18px}.metric{margin:7px 0 2px}}
</style></head><body><div class="shell"><header class="top"><div class="brand"><span class="mark">Q</span>QuantEdge</div>
<div class="top-right"><span>Binance Spot</span><span class="mode">__MODE__</span></div></header>
<main><div class="heading"><div><p class="eyebrow">Trading workspace</p><h1>Account overview</h1><p>Monitor your balances and order activity.</p></div>
<div class="refresh">Journal updated <span>__NOW__</span>Refreshes every 5 seconds</div></div>
<section class="grid" aria-label="Account status">
<div class="card"><div class="label">Order submissions</div><div class="metric"><span class="dot __DOT__"></span>__STATE__</div><div class="sub">Current mode · __MODE__</div></div>
<div class="card"><div class="label">Orders requiring attention</div><div class="metric">__ATTENTION__</div><div class="sub">Within the latest 100 tickets</div></div>
<div class="card"><div class="label">Recent tickets</div><div class="metric">__COUNT__</div><div class="sub">Latest 100 journal entries</div></div></section>
__GUIDANCE__
<section class="card" style="margin-top:14px"><div class="section-title"><div><h2>Portfolio</h2><p>Wallet valuation and recorded-fill accounting</p></div></div>__PORTFOLIO__</section>
<section class="card" style="margin-top:14px"><div class="section-title"><div><h2>Market stream</h2><p>Binance best bid and ask · collector freshness</p></div></div>
<div class="table-wrap"><table style="min-width:550px"><thead><tr><th>Market</th><th>Bid · USDT</th><th>Ask · USDT</th><th>Feed</th><th>Received</th></tr></thead><tbody>__MARKET__</tbody></table></div></section>
<div class="content"><section class="card"><div class="section-title"><div><h2>Order activity</h2><p>Last known order status from the local journal</p></div><span class="count">Latest 100</span></div>
<div class="table-wrap"><table><thead><tr><th>Created · UTC</th><th>Market</th><th>Side</th><th>Quantity</th><th>Limit · USDT</th><th>Executed</th><th>Status</th><th>Ticket ID</th></tr></thead><tbody>__ROWS__</tbody></table></div></section>
<aside class="card"><div class="section-title"><div><h2>Available balances</h2><p>__BALANCE_LABEL__</p></div></div>__BALANCES__
<div class="note">Orders and exchange balances are managed in the QuantEdge terminal. Exchange order status is updated when you run the status command.</div></aside></div>
<section class="card" style="margin-top:14px"><div class="section-title"><div><h2>Collector health</h2><p>Last reported component state</p></div></div>__HEALTH__</section>
<div class="depth-grid">__DEPTH__</div>
<p class="footer">Local journal · Read-only dashboard</p></main></div></body></html>"""
    replacements = {
        "__MODE__": mode, "__NOW__": now, "__DOT__": state_class,
        "__STATE__": state, "__ATTENTION__": str(attention), "__COUNT__": str(len(orders)),
        "__ROWS__": rows_html, "__BALANCES__": balances,
        "__GUIDANCE__": guidance,
        "__MARKET__": market_rows, "__PORTFOLIO__": portfolio_html,
        "__HEALTH__": health_html,
        "__DEPTH__": depth_html,
        "__BALANCE_LABEL__": "Paper wallet" if journal.mode == "paper" else "Latest account snapshot",
    }
    for key, value in replacements.items():
        html = html.replace(key, value)
    return html


def serve(journal):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.headers.get("Host") not in ("localhost:8765", "127.0.0.1:8765") or urlsplit(self.path).path != "/":
                self.send_error(404)
                return
            data = page(journal).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    with HTTPServer(("127.0.0.1", 8765), Handler) as server:
        print(f"QuantEdge {journal.mode} dashboard at http://127.0.0.1:8765 (Ctrl+C to close)", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
