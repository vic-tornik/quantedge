"""Bounded snapshot/diff synchronization for Binance Spot order books."""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal as D
import json
import time

from engine import SYMBOLS, Fault
from exchange import Binance


def levels(rows):
    result = {}
    for price, quantity in rows:
        p, q = D(price), D(quantity)
        if not p.is_finite() or not q.is_finite() or p <= 0 or q < 0:
            raise Fault("Invalid depth price or quantity.")
        if q:
            result[p] = q
    return result


class OrderBook:
    def __init__(self, symbol, snapshot):
        self.symbol = symbol
        self.update_id = int(snapshot["lastUpdateId"])
        if self.update_id < 0:
            raise Fault("Invalid snapshot update ID.")
        self.bids = levels(snapshot["bids"])
        self.asks = levels(snapshot["asks"])
        self.validate(self.bids,self.asks)
        self.bid_floor = min(self.bids)
        self.ask_ceiling = max(self.asks)
        self.ready = False

    @staticmethod
    def validate(bids, asks):
        if not bids or not asks or max(bids) >= min(asks):
            raise Fault("Depth coverage exhausted or book crossed. Resynchronize.")
        if len(bids) > 20000 or len(asks) > 20000:
            raise Fault("Depth level bound exceeded. Resynchronize.")

    def apply(self, event):
        if event["s"] != self.symbol:
            raise Fault("Depth symbol mismatch.")
        first, last = int(event["U"]), int(event["u"])
        if first < 0 or last < first:
            raise Fault("Invalid depth update range.")
        if last <= self.update_id:
            return False
        if first > self.update_id + 1:
            raise Fault("Depth update gap. Resynchronize.")
        bids, asks = dict(self.bids), dict(self.asks)
        for target, rows, boundary, buy in (
            (bids,event["b"],self.bid_floor,True),
            (asks,event["a"],self.ask_ceiling,False),
        ):
            for price,quantity in rows:
                p,q = D(price),D(quantity)
                if not p.is_finite() or not q.is_finite() or p <= 0 or q < 0:
                    raise Fault("Invalid depth update.")
                # Only retain the price range covered by the original snapshot.
                if (buy and p < boundary) or (not buy and p > boundary):
                    continue
                if q == 0:
                    target.pop(p,None)
                else:
                    target[p] = q
        self.validate(bids,asks)
        self.bids,self.asks = bids,asks
        self.update_id = last
        self.ready = True
        return True

    def top(self, count=20):
        return {
            "symbol":self.symbol, "lastUpdateId":self.update_id,
            "bids":[[str(p),str(self.bids[p])] for p in sorted(self.bids,reverse=True)[:count]],
            "asks":[[str(p),str(self.asks[p])] for p in sorted(self.asks)[:count]],
            "bid_floor":str(self.bid_floor),"ask_ceiling":str(self.ask_ceiling),
        }


def fetch_snapshot(mode, symbol):
    return Binance(mode).request("GET","depth",{"symbol":symbol,"limit":5000})


def record_depth(journal, symbol, kind, payload):
    journal.db.execute("INSERT INTO depth_events(symbol,kind,received,payload) VALUES(?,?,?,?)",
                       (symbol,kind,time.time(),json.dumps(payload)))


class DepthSession:
    def __init__(self, journal, submit=None):
        self.journal = journal
        self.pool = ThreadPoolExecutor(max_workers=2,thread_name_prefix="quantedge-depth") if submit is None else None
        self.submit = submit or (lambda symbol:self.pool.submit(fetch_snapshot,journal.mode,symbol))
        self.pending = {}
        self.buffers = {s:[] for s in SYMBOLS}
        self.buffer_bytes = {s:0 for s in SYMBOLS}
        self.books = {}
        self.published = {}
        self.last_event = {}

    def event(self, event):
        symbol = event["s"]
        if symbol not in SYMBOLS:
            raise Fault("Unexpected depth symbol.")
        record_depth(self.journal,symbol,"diff",event)
        if symbol in self.books:
            if self.books[symbol].apply(event):
                self.last_event[symbol] = time.monotonic()
                self.publish(symbol)
            return
        self.last_event[symbol] = time.monotonic()
        self.buffers[symbol].append(event)
        self.buffer_bytes[symbol] += len(json.dumps(event).encode("utf-8"))
        if len(self.buffers[symbol]) > 2000 or self.buffer_bytes[symbol] > 8*1024*1024:
            raise Fault("Depth snapshot buffer overflow. Resynchronize.")
        if symbol not in self.pending:
            self.pending[symbol] = self.submit(symbol)

    def poll(self):
        for symbol,future in list(self.pending.items()):
            if not future.done():
                continue
            snapshot = future.result()
            record_depth(self.journal,symbol,"snapshot",snapshot)
            book = OrderBook(symbol,snapshot)
            for event in self.buffers[symbol]:
                book.apply(event)
            self.books[symbol] = book
            self.buffers[symbol].clear()
            self.buffer_bytes[symbol] = 0
            del self.pending[symbol]
            if book.ready:
                self.publish(symbol,force=True)

    def publish(self, symbol, force=False):
        now = time.monotonic()
        if not force and now-self.published.get(symbol,0) < .5:
            return
        self.journal.db.execute("INSERT OR REPLACE INTO depth_latest VALUES(?,?,?)",
                               (symbol,time.time(),json.dumps(self.books[symbol].top())))
        self.published[symbol] = now

    def check_fresh(self, started, stale_seconds):
        now = time.monotonic()
        for symbol in SYMBOLS:
            if now-self.last_event.get(symbol,started) > stale_seconds:
                raise Fault("Depth stream is stale. Resynchronize.")

    def close(self):
        for future in self.pending.values():
            future.cancel()
        if self.pool is not None:
            self.pool.shutdown(wait=False,cancel_futures=True)


def depth_view(journal):
    threshold=float(journal.config("market_stale_seconds","10"))
    connected=journal.config("stream_state") == "connected"
    result=[]
    for symbol in SYMBOLS:
        row=journal.db.execute("SELECT received,payload FROM depth_latest WHERE symbol=?",(symbol,)).fetchone()
        age=time.time()-row["received"] if row else None
        fresh=row is not None and connected and 0 <= age <= threshold
        result.append({"symbol":symbol,"fresh":fresh,"age_seconds":age,
                       "book":json.loads(row["payload"]) if fresh else None})
    return result
