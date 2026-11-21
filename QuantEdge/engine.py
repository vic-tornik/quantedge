"""Durable local orders and bounded Spot execution."""
import json
import os
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from decimal import Decimal as D
from pathlib import Path

from exchange import Fault, VenueError

SYMBOLS = {"BTCUSDT": "BTC", "ETHUSDT": "ETH"}
TERMINAL = {"FILLED", "EXPIRED", "EXPIRED_IN_MATCH", "CANCELED", "REJECTED"}
ACTIVE = {"SENDING", "UNKNOWN", "NEW", "PARTIALLY_FILLED", "PENDING_NEW", "PENDING_CANCEL"}
FEE = D("0.001")


def amount(value):
    s = str(value)
    if not re.fullmatch(r"[0-9]{1,12}(\.[0-9]{1,12})?", s) or D(s) <= 0:
        raise Fault("Enter a positive plain decimal with at most 12 integer and 12 fractional digits.")
    return D(s)


def dec(value):
    return format(value, "f")


class Journal:
    def __init__(self, mode, home=None):
        path = Path(home or Path.home() / ".quantedge")
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.mode = mode
        self.db = sqlite3.connect(path / (mode + ".sqlite"), isolation_level=None, timeout=15)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS orders(
              id TEXT PRIMARY KEY, symbol TEXT, side TEXT, qty TEXT, price TEXT,
              created INTEGER, sent INTEGER, status TEXT, uid TEXT, result TEXT);
            CREATE TABLE IF NOT EXISTS config(k TEXT PRIMARY KEY, v TEXT);
            CREATE TABLE IF NOT EXISTS balances(asset TEXT PRIMARY KEY, free TEXT);
            INSERT OR IGNORE INTO balances VALUES('USDT','100'),('BTC','0'),('ETH','0');
        """)
        from foundation import initialize
        initialize(self)

    @contextmanager
    def lock(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def config(self, key, default=None):
        item = self.db.execute("SELECT v FROM config WHERE k=?", (key,)).fetchone()
        return item[0] if item else default

    def set(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO config VALUES(?,?)", (key, value))

    def row(self, oid):
        row = self.db.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        if not row:
            raise Fault(f"Unknown order ID in {self.mode} mode.")
        return dict(row)

    def history(self):
        return [dict(row) for row in self.db.execute("SELECT * FROM orders ORDER BY rowid DESC LIMIT 100")]

    def wallet(self):
        return dict(self.db.execute("SELECT asset,free FROM balances"))


def limits():
    return (amount(os.getenv("QE_ORDER_CAP", "10")), amount(os.getenv("QE_DAILY_CAP", "30")))


def check_market(info, book, symbol, side, qty, price):
    if (info["symbol"] != symbol or book["symbol"] != symbol or info["status"] != "TRADING"
            or not info["isSpotTradingAllowed"] or "LIMIT" not in info["orderTypes"]
            or info["quoteAsset"] != "USDT" or info["baseAsset"] != SYMBOLS[symbol]):
        raise Fault("Pair unavailable for Spot limit orders.")
    filters = {f["filterType"]: f for f in info["filters"]}
    for value, name, low, high, step in (
        (price, "PRICE_FILTER", "minPrice", "maxPrice", "tickSize"),
        (qty, "LOT_SIZE", "minQty", "maxQty", "stepSize"),
    ):
        f = filters[name]
        lower, upper, interval = (D(f[k]) for k in (low, high, step))
        if (lower and value < lower) or (upper and value > upper) or (interval and value % interval):
            raise Fault(f"{name} failed. Minimum {lower}, maximum {upper}, increment {interval}.")
    total = qty * price
    for name in ("MIN_NOTIONAL", "NOTIONAL"):
        if name in filters:
            f = filters[name]
            if total < D(f["minNotional"]) or ("maxNotional" in f and total > D(f["maxNotional"])):
                raise Fault(f"{name} failed for {dec(total)} USDT.")
    bid, ask = amount(book["bidPrice"]), amount(book["askPrice"])
    if bid > ask or (ask - bid) / bid > D(".005"):
        raise Fault("Crossed or wide bid/ask spread.")
    if abs(price / (ask if side == "BUY" else bid) - 1) > D(".01"):
        raise Fault("Limit price must be within 1% of the current executable quote.")
    if total > limits()[0]:
        raise Fault("Order notional exceeds QE_ORDER_CAP.")


class Desk:
    def __init__(self, journal, venue):
        self.j = journal
        self.venue = venue

    def market(self, symbol, side, qty, price):
        start = time.monotonic()
        info, book = self.venue.market(symbol)
        check_market(info, book, symbol, side, qty, price)
        if time.monotonic() - start > 5:
            raise Fault("Market request exceeded the five-second freshness window.")
        return book

    def account(self):
        if self.j.mode == "paper":
            return "paper", self.j.wallet()
        return self.venue.account()

    def preview(self, symbol, side, quantity, limit_price):
        if symbol not in SYMBOLS or side not in ("BUY", "SELL"):
            raise Fault("Choose BTCUSDT or ETHUSDT and BUY or SELL.")
        qty, price = amount(quantity), amount(limit_price)
        uid, wallet = self.account()
        book = self.market(symbol, side, qty, price)
        self.funds(wallet, symbol, side, qty, price)
        oid = "qe" + uuid.uuid4().hex
        self.j.db.execute("INSERT INTO orders VALUES(?,?,?,?,?,?,NULL,'PREVIEW',?,NULL)",
                          (oid, symbol, side, dec(qty), dec(price), int(time.time()), uid))
        return dict(self.j.row(oid), bid=book["bidPrice"], ask=book["askPrice"],
                    limit_notional=dec(qty * price), mode=self.j.mode)

    @staticmethod
    def funds(wallet, symbol, side, qty, price):
        asset, required = ("USDT", qty * price * (1 + FEE)) if side == "BUY" else (SYMBOLS[symbol], qty)
        if D(wallet.get(asset, "0")) < required:
            raise Fault(f"Insufficient free {asset}.")

    def submit(self, oid, arm=False):
        if self.j.mode == "live" and (not arm or os.getenv("QE_ENABLE_LIVE") != "YES"):
            raise Fault("Live submission requires QE_ENABLE_LIVE=YES and explicit live confirmation.")
        with self.j.lock():
            t = self.j.row(oid)
            if t["status"] != "PREVIEW":
                raise Fault(f"Order is already {t['status']}. Use status; do not submit again.")
            if self.j.config("paused") == "1":
                raise Fault("Submissions paused.")
            if self.j.db.execute("SELECT 1 FROM orders WHERE status NOT IN ('PREVIEW','FILLED','EXPIRED','EXPIRED_IN_MATCH','CANCELED','REJECTED') LIMIT 1").fetchone():
                raise Fault("Resolve the outstanding order using status before trading.")
            uid, wallet = self.account()
            if uid != t["uid"] or uid != self.j.config("account", uid):
                raise Fault("Account identity changed. Use the original account.")
            qty, price = D(t["qty"]), D(t["price"])
            self.funds(wallet, t["symbol"], t["side"], qty, price)
            book = self.market(t["symbol"], t["side"], qty, price)
            now = int(time.time())
            if not 0 <= now - t["created"] <= 60:
                raise Fault("Preview expired. Create a fresh preview.")
            today = now - now % 86400
            usage = sum((D(q)*D(p) for q,p in self.j.db.execute(
                "SELECT qty,price FROM orders WHERE sent>=?", (today,))), D(0))
            if usage + qty*price > limits()[1]:
                raise Fault("UTC daily submitted-notional cap reached.")
            self.j.set("account", uid)
            if self.j.mode == "paper":
                result = self.paper_fill(t, book, wallet)
                self.j.db.execute("UPDATE orders SET sent=?,status=?,result=? WHERE id=?",
                                  (now, result["status"], json.dumps(result), oid))
                return self.j.row(oid)
            self.j.db.execute("UPDATE orders SET sent=?,status='SENDING' WHERE id=?", (now, oid))
        # The reservation is durable before any request can leave this process.
        with self.j.lock():
            if self.j.config("paused") == "1":
                self.j.db.execute("UPDATE orders SET status='REJECTED' WHERE id=?", (oid,))
            elif not 0 <= time.time() - t["created"] <= 60:
                self.j.db.execute("UPDATE orders SET status='REJECTED',result=? WHERE id=?",
                                  ("Preview expired before dispatch", oid))
            else:
                try:
                    self.record(t, self.venue.place(t))
                except VenueError as err:
                    certain = err.status == 400 and err.code in {-1013,-1100,-1111,-2010,-1021,-1022}
                    self.j.db.execute("UPDATE orders SET status=?,result=? WHERE id=?",
                                      ("REJECTED" if certain else "UNKNOWN", str(err), oid))
                except (OSError, ValueError, KeyError, TypeError, ArithmeticError):
                    self.j.db.execute("UPDATE orders SET status='UNKNOWN' WHERE id=?", (oid,))
        return self.j.row(oid)

    def record(self, t, result):
        if (result["clientOrderId"] != t["id"] or result["symbol"] != t["symbol"]
                or result["side"] != t["side"] or result["status"] not in TERMINAL | ACTIVE
                or D(result["origQty"]) != D(t["qty"]) or D(result["price"]) != D(t["price"])
                or result["type"] != "LIMIT" or result["timeInForce"] != "IOC"
                or not 0 <= D(result["executedQty"]) <= D(t["qty"])):
            raise Fault("Exchange result mismatches the original ticket.")
        self.j.db.execute("UPDATE orders SET status=?,result=? WHERE id=?",
                          (result["status"], json.dumps(result), t["id"]))

    def status(self, oid):
        with self.j.lock():
            t = self.j.row(oid)
            if self.j.mode != "paper" and t["status"] in ACTIVE:
                if self.account()[0] != t["uid"]:
                    raise Fault("Use the original account to query this order.")
                self.record(t, self.venue.lookup(t))
        return self.j.row(oid)

    def paper_fill(self, t, book, wallet):
        qty, limit_price = D(t["qty"]), D(t["price"])
        buying = t["side"] == "BUY"
        price = D(book["askPrice" if buying else "bidPrice"])
        size = D(book["askQty" if buying else "bidQty"])
        crosses = price <= limit_price if buying else price >= limit_price
        filled = min(size, qty) if crosses else D(0)
        total = filled * price
        fee = total * FEE
        base = SYMBOLS[t["symbol"]]
        balances = {base: D(wallet[base]) + (filled if buying else -filled),
                    "USDT": D(wallet["USDT"]) + (-total if buying else total) - fee}
        self.j.db.executemany("UPDATE balances SET free=? WHERE asset=?",
                              [(dec(v), k) for k,v in balances.items()])
        return {"status": "FILLED" if filled == qty else "EXPIRED", "executedQty": dec(filled),
                "cummulativeQuoteQty": dec(total), "commissionUSDT": dec(fee)}
