"""Restart-safe collection store and transactional PostgreSQL analytics export."""
import json
import time
from contextlib import nullcontext
from decimal import Decimal as D
from pathlib import Path

from engine import SYMBOLS, Fault
from portfolio import calculate

DDL = """
CREATE TABLE IF NOT EXISTS depth_events(
 id INTEGER PRIMARY KEY AUTOINCREMENT,symbol TEXT NOT NULL,kind TEXT NOT NULL,
 received REAL NOT NULL,payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS depth_latest(symbol TEXT PRIMARY KEY,received REAL NOT NULL,payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS market_events(
 id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, update_id TEXT NOT NULL,
 bid TEXT NOT NULL, ask TEXT NOT NULL, received REAL NOT NULL);
CREATE TABLE IF NOT EXISTS market_latest(
 symbol TEXT PRIMARY KEY, update_id TEXT, bid TEXT, ask TEXT, received REAL);
CREATE TABLE IF NOT EXISTS recorded_fills(
 id TEXT PRIMARY KEY, ticket TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL,
 qty TEXT NOT NULL, quote_qty TEXT NOT NULL, commission TEXT NOT NULL,
 commission_asset TEXT NOT NULL, ts INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS portfolio_snapshots(
 id INTEGER PRIMARY KEY AUTOINCREMENT, received REAL NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS collector_health(
 component TEXT PRIMARY KEY, state TEXT NOT NULL, received REAL NOT NULL, detail TEXT NOT NULL);
"""


def initialize(journal):
    journal.db.executescript(DDL)


def ingest_quote(journal, event, received=None):
    symbol = event["s"]
    if symbol not in SYMBOLS:
        raise Fault("Unexpected stream symbol.")
    bid, ask = D(event["b"]), D(event["a"])
    if not bid.is_finite() or not ask.is_finite() or bid <= 0 or ask < bid:
        raise Fault("Invalid book ticker.")
    update_id = int(event["u"])
    if update_id < 0:
        raise Fault("Invalid book update ID.")
    received = time.time() if received is None else received
    row = (symbol, str(update_id), str(bid), str(ask), received)
    with journal.lock():
        previous = journal.db.execute("SELECT update_id FROM market_latest WHERE symbol=?", (symbol,)).fetchone()
        if previous and update_id <= int(previous[0]):
            return False
        journal.db.execute("INSERT INTO market_events(symbol,update_id,bid,ask,received) VALUES(?,?,?,?,?)", row)
        journal.db.execute("INSERT OR REPLACE INTO market_latest VALUES(?,?,?,?,?)", row)
    return True


def set_health(journal, component, state, detail=""):
    journal.db.execute("INSERT OR REPLACE INTO collector_health VALUES(?,?,?,?)",
                       (component,state,time.time(),detail))


def health_view(journal):
    return [dict(row) for row in journal.db.execute("SELECT * FROM collector_health ORDER BY component")]


def market_view(journal, stale_seconds=None, now=None):
    if stale_seconds is None:
        stale_seconds = float(journal.config("market_stale_seconds","10"))
    now = time.time() if now is None else now
    result = []
    for symbol in SYMBOLS:
        row = journal.db.execute("SELECT * FROM market_latest WHERE symbol=?", (symbol,)).fetchone()
        if row:
            item = dict(row)
            item["age_seconds"] = max(0, now-item["received"])
            item["stale"] = (not 0 <= now-item["received"] <= stale_seconds
                             or journal.config("stream_state","connected") != "connected")
        else:
            item = {"symbol":symbol, "bid":None, "ask":None, "stale":True, "age_seconds":None}
        result.append(item)
    return result


def sync_fills(journal, venue, locked=False):
    # All orders are scanned so a restart or a receipt overwritten by lookup cannot lose fills.
    orders = [dict(r) for r in journal.db.execute("SELECT * FROM orders WHERE sent IS NOT NULL ORDER BY sent,rowid")]
    if journal.mode != "paper":
        uid, _ = venue.account()
        with (nullcontext() if locked else journal.lock()):
            if journal.config("account") is None:
                journal.set("account",uid)
            if uid != journal.config("account"):
                raise Fault("Portfolio collector requires the journal's bound exchange account.")
    for ticket in orders:
        if ticket["status"] in ("SENDING","UNKNOWN"):
            continue
        receipt = {}
        try:
            receipt = json.loads(ticket["result"] or "{}")
        except ValueError:
            continue
        if journal.mode == "paper":
            qty = D(receipt.get("executedQty","0"))
            trades = [] if qty == 0 else [{
                "id":ticket["id"], "qty":str(qty), "quoteQty":receipt["cummulativeQuoteQty"],
                "commission":receipt["commissionUSDT"], "commissionAsset":"USDT",
                "time":ticket["sent"]*1000,
            }]
        elif "orderId" in receipt:
            trades = venue.request("GET","myTrades",{"symbol":ticket["symbol"],"orderId":receipt["orderId"],"limit":1000},signed=True)
            if len(trades) >= 1000:
                raise Fault("Trade pagination required for this order; collection stopped.")
            total = sum((D(t["qty"]) for t in trades),D(0))
            if total != D(receipt["executedQty"]):
                raise Fault("Trade history and order execution disagree. Reconcile the order and retry.")
        else:
            continue
        with (nullcontext() if locked else journal.lock()):
            for trade in trades:
                if journal.mode != "paper" and (trade["orderId"] != receipt["orderId"] or trade["symbol"] != ticket["symbol"]
                        or trade["isBuyer"] != (ticket["side"] == "BUY")):
                    raise Fault("Trade history does not match the order.")
                fid = ticket["symbol"] + ":" + str(trade["id"])
                values = (fid,ticket["id"],ticket["symbol"],ticket["side"],trade["qty"],trade["quoteQty"],
                          trade["commission"],trade["commissionAsset"],trade["time"])
                existing = journal.db.execute("SELECT * FROM recorded_fills WHERE id=?", (fid,)).fetchone()
                if existing and tuple(existing) != values:
                    raise Fault("Recorded fill conflicts with exchange history.")
                journal.db.execute("INSERT OR IGNORE INTO recorded_fills VALUES(?,?,?,?,?,?,?,?,?)", values)


def snapshot(journal, venue=None, stale_seconds=None):
    balances = None
    account_uid = None
    account_received = time.time()
    if journal.mode != "paper":
        # Exchange I/O must complete before acquiring the local write lock.
        account = venue.request("GET","account",signed=True)
        account_uid = str(account["uid"])
        balances = {b["asset"]:str(D(b["free"])+D(b["locked"])) for b in account["balances"]}
        account_received = time.time()
    # Paper wallet and imported fills must refer to the same committed execution state.
    with journal.lock():
        if journal.mode == "paper":
            sync_fills(journal,None,locked=True)
        if account_uid is not None and account_uid != journal.config("account"):
            raise Fault("Exchange account changed.")
        return _snapshot(journal,balances,stale_seconds,account_received)


def _snapshot(journal, balances, stale_seconds, account_received):
    quotes = market_view(journal,stale_seconds)
    marks = {SYMBOLS[q["symbol"]]: str((D(q["bid"])+D(q["ask"]))/2) for q in quotes if not q["stale"]}
    if journal.mode == "paper":
        balances = journal.wallet()
    fills = [dict(r) for r in journal.db.execute("SELECT * FROM recorded_fills ORDER BY ts,rowid")]
    result = calculate(fills, balances, marks)
    result.update(balances=balances, mode=journal.mode, fills=len(fills), market=quotes,
                  received=time.time(), account_received=account_received,
                  scope="Whole wallet equity; cost basis and P&L from QuantEdge-recorded fills")
    if journal.mode != "paper":
        result["accounting_issues"].append("External trades, transfers and opening inventory are outside the tracked P&L.")
    journal.db.execute("INSERT INTO portfolio_snapshots(received,payload) VALUES(?,?)", (result["received"],json.dumps(result)))
    return result


def latest_snapshot(journal):
    row = journal.db.execute("SELECT payload FROM portfolio_snapshots ORDER BY id DESC LIMIT 1").fetchone()
    return json.loads(row[0]) if row else None


def export_postgres(journal, dsn):
    try:
        return _export_postgres(journal, dsn)
    except ImportError:
        raise
    except Exception as err:
        raise Fault(f"PostgreSQL export failed ({type(err).__name__}). Check service availability and credentials.") from None


def _export_postgres(journal, dsn):
    # The cursor advances only with the PostgreSQL transaction. Retrying is idempotent.
    import psycopg
    schema = Path(__file__).with_name("schema.sql").read_text()
    with psycopg.connect(dsn, connect_timeout=5) as pg:
        pg.execute(schema)
        pg.execute("SELECT pg_advisory_xact_lock(716492)")
        with journal.lock():
            instance = journal.config("analytics_instance")
            if not instance:
                import uuid
                instance = uuid.uuid4().hex
                journal.set("analytics_instance",instance)
        key = instance + ":" + journal.mode
        cursor = pg.execute("SELECT event_id,snapshot_id FROM qe_cursors WHERE scope=%s FOR UPDATE",(key,)).fetchone()
        event_id, snap_id = cursor or (0,0)
        for r in journal.db.execute("SELECT * FROM market_events WHERE id>? ORDER BY id",(event_id,)):
            pg.execute("INSERT INTO qe_market_events VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                       (key,*tuple(r)))
            event_id = r["id"]
        for r in journal.db.execute("SELECT * FROM orders"):
            pg.execute("""INSERT INTO qe_orders VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
             ON CONFLICT(scope,id) DO UPDATE SET status=EXCLUDED.status,receipt=EXCLUDED.receipt,sent=EXCLUDED.sent""",
                       (key,r["id"],r["symbol"],r["side"],r["qty"],r["price"],r["created"],r["sent"],r["status"],r["result"]))
        for r in journal.db.execute("SELECT * FROM recorded_fills"):
            pg.execute("INSERT INTO qe_fills VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING", (key,*tuple(r)))
        for r in journal.db.execute("SELECT * FROM portfolio_snapshots WHERE id>? ORDER BY id",(snap_id,)):
            pg.execute("INSERT INTO qe_snapshots VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING",(key,r["id"],r["received"],r["payload"]))
            payload=json.loads(r["payload"])
            for asset, quantity in payload["balances"].items():
                pg.execute("INSERT INTO qe_balances VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING",(key,r["id"],asset,quantity))
            snap_id=r["id"]
        pg.execute("""INSERT INTO qe_cursors VALUES(%s,%s,%s) ON CONFLICT(scope) DO UPDATE SET
                    event_id=EXCLUDED.event_id,snapshot_id=EXCLUDED.snapshot_id""",(key,event_id,snap_id))
        depth_cursor = pg.execute("SELECT event_id FROM qe_depth_cursors WHERE scope=%s",(key,)).fetchone()
        depth_id = depth_cursor[0] if depth_cursor else 0
        for r in journal.db.execute("SELECT * FROM depth_events WHERE id>? ORDER BY id",(depth_id,)):
            pg.execute("INSERT INTO qe_depth_events VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                       (key,*tuple(r)))
            depth_id = r["id"]
        pg.execute("""INSERT INTO qe_depth_cursors VALUES(%s,%s) ON CONFLICT(scope) DO UPDATE SET
                   event_id=EXCLUDED.event_id""",(key,depth_id))
    return {"market_events_through":event_id,"snapshots_through":snap_id,"depth_events_through":depth_id}
