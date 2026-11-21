import hashlib
import hmac
import io
import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from decimal import Decimal as D
from unittest.mock import patch

from engine import Desk, Fault, Journal
from exchange import Binance, VenueError
from signals import decision, fixture, replay


class Venue:
    def __init__(self):
        self.book = dict(symbol="BTCUSDT", bidPrice="99.9", askPrice="100", bidQty="1", askQty="1")
        self.uid = "42"
        self.sent = 0
        self.mode = "ok"
        self.result = {}

    def market(self, symbol):
        return dict(symbol=symbol, status="TRADING", isSpotTradingAllowed=True, orderTypes=["LIMIT"],
                    baseAsset="BTC", quoteAsset="USDT", filters=[
                        dict(filterType="LOT_SIZE", minQty="0.001", maxQty="10", stepSize="0.001"),
                        dict(filterType="PRICE_FILTER", minPrice="0.01", maxPrice="10000", tickSize="0.01"),
                        dict(filterType="MIN_NOTIONAL", minNotional="5")]), self.book

    def account(self):
        return self.uid, {"USDT": "100", "BTC": "1"}

    def place(self, ticket):
        self.sent += 1
        self.result = dict(clientOrderId=ticket["id"], symbol=ticket["symbol"], side=ticket["side"],
                           origQty=ticket["qty"], price=ticket["price"], type="LIMIT", timeInForce="IOC",
                           executedQty=ticket["qty"], status="FILLED")
        if self.mode == "timeout":
            raise Fault("Timeout")
        if self.mode == "reject":
            raise VenueError(-1013, 400)
        return self.result

    def lookup(self, ticket):
        return self.result


class DeskTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {"QE_ORDER_CAP": "10", "QE_DAILY_CAP": "30", "QE_ENABLE_LIVE":"YES"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.j = Journal("testnet", self.tmp.name)
        self.addCleanup(self.j.db.close)
        self.v = Venue()
        self.d = Desk(self.j, self.v)

    def ticket(self, qty="0.08", price="100"):
        return self.d.preview("BTCUSDT","BUY",qty,price)["id"]

    def test_signing_and_mode_endpoint(self):
        with patch.dict(os.environ, {"QE_TESTNET_KEY":"dummy-key","QE_TESTNET_SECRET":"dummy-secret"}):
            api = Binance("testnet")
            reqs = []
            def fake(req, timeout):
                reqs.append(req)
                return io.BytesIO(b"{}")
            api.transport.open = fake
            with patch("exchange.time.time_ns", return_value=12345000000):
                api.request("POST", "order", {"symbol":"BTCUSDT"}, signed=True)
            req = reqs[0]
            body = b"symbol=BTCUSDT&timestamp=12345&recvWindow=5000"
            sig = hmac.new(b"dummy-secret", body, hashlib.sha256).hexdigest().encode()
            self.assertEqual(req.data, body + b"&signature=" + sig)
            self.assertEqual(req.full_url, "https://testnet.binance.vision/api/v3/order")
            self.assertEqual(req.get_header("X-mbx-apikey"),"dummy-key")

    def test_paper_signed_requests_blocked(self):
        with self.assertRaises(Fault):
            Binance("paper").request("POST","order",signed=True)

    def test_submit_once(self):
        oid = self.ticket()
        self.assertEqual(self.d.submit(oid)["status"],"FILLED")
        with self.assertRaises(Fault):
            self.d.submit(oid)
        self.assertEqual(self.v.sent,1)

    def test_timeout_restart_lookup(self):
        oid, other = self.ticket(), self.ticket()
        self.v.mode = "timeout"
        self.assertEqual(self.d.submit(oid)["status"],"UNKNOWN")
        fresh = Journal("testnet",self.tmp.name)
        with closing(fresh.db):
            desk = Desk(fresh,self.v)
            with self.assertRaises(Fault):
                desk.submit(other)
            self.assertEqual(desk.status(oid)["status"],"FILLED")
            self.assertEqual(self.v.sent,1)

    def test_missing_order_stays_blocked(self):
        oid = self.ticket()
        self.v.mode = "timeout"
        self.d.submit(oid)
        with patch.object(self.v,"lookup",side_effect=VenueError(-2013,400)):
            with self.assertRaises(VenueError):
                self.d.status(oid)
        self.assertEqual(self.j.row(oid)["status"],"UNKNOWN")

    def test_explicit_rejection(self):
        self.v.mode = "reject"
        self.assertEqual(self.d.submit(self.ticket())["status"],"REJECTED")

    def test_filters(self):
        for qty,price in [("0.0811","100"),("0.08","100.001"),("0.04","100"),("0.101","100"),
                          ("NaN","100"),("0.08","80"),("-1","100"),("1e-2","100")]:
            with self.subTest(qty=qty,price=price), self.assertRaises(Fault):
                self.ticket(qty,price)

    def test_new_quote_required(self):
        oid = self.ticket()
        self.v.book.update(bidPrice="90",askPrice="90.1")
        with self.assertRaises(Fault):
            self.d.submit(oid)
        self.assertEqual(self.v.sent,0)

    def test_insufficient_sell_balance(self):
        with patch.object(self.v,"account",return_value=("42",{})), self.assertRaises(Fault):
            self.d.preview("BTCUSDT","SELL","0.08","100")

    def test_pause_expiry_identity(self):
        oid = self.ticket()
        self.j.set("paused","1")
        with self.assertRaisesRegex(Fault,"paused"):
            self.d.submit(oid)
        self.j.set("paused","0")
        self.v.uid = "another"
        with self.assertRaisesRegex(Fault,"identity"):
            self.d.submit(oid)
        self.v.uid = "42"
        self.j.db.execute("UPDATE orders SET created=0")
        with self.assertRaisesRegex(Fault,"expired"):
            self.d.submit(oid)
        self.assertEqual(self.v.sent,0)

    def test_live_arming(self):
        j = Journal("live",self.tmp.name)
        with closing(j.db):
            desk = Desk(j,self.v)
            oid = desk.preview("BTCUSDT","BUY","0.08","100")["id"]
            with self.assertRaises(Fault):
                desk.submit(oid)
            with patch.dict(os.environ,{"QE_ENABLE_LIVE":"NO"}), self.assertRaises(Fault):
                desk.submit(oid,True)
            self.assertEqual(self.v.sent,0)
            self.assertEqual(desk.submit(oid, True)["status"], "FILLED")

    def test_daily_cap_concurrent(self):
        os.environ["QE_DAILY_CAP"]="10"
        ids = [self.ticket(),self.ticket()]
        barrier = threading.Barrier(2)
        def worker(oid):
            j = Journal("testnet",self.tmp.name)
            with closing(j.db):
                barrier.wait(timeout=5)
                try:
                    return Desk(j,self.v).submit(oid)["status"]
                except Fault:
                    return "BLOCKED"
        with ThreadPoolExecutor(2) as pool:
            self.assertCountEqual(pool.map(worker,ids),["FILLED","BLOCKED"])
        self.assertEqual(self.v.sent,1)

    def test_paper_partial_fee_and_sell(self):
        j = Journal("paper",self.tmp.name)
        with closing(j.db):
            self.v.book["askQty"]="0.05"
            desk = Desk(j,self.v)
            oid = desk.preview("BTCUSDT","BUY","0.08","100")["id"]
            row = desk.submit(oid)
            self.assertEqual(row["status"],"EXPIRED")
            self.assertEqual(D(json.loads(row["result"])["executedQty"]),D(".05"))
            self.assertEqual(D(j.wallet()["USDT"]),D("94.995"))
            # Min notional at 99.90 requires more base than the partial fill.
            with self.assertRaises(Fault):
                desk.preview("BTCUSDT","SELL","0.08","99.90")
            self.v.book["bidPrice"]="100"
            sell = desk.preview("BTCUSDT","SELL","0.05","100")
            self.assertEqual(desk.submit(sell["id"])["status"],"FILLED")
            self.assertEqual(D(j.wallet()["USDT"]),D("99.990"))
            self.assertEqual(self.v.sent,0)

    def test_strategies(self):
        for method in ("reversion","breakout"):
            result = replay(fixture(),method)
            self.assertGreater(len(result["fills"]),0)
            self.assertEqual(decision([D(100)]*20,method),"HOLD")
        bars = [(n,D(100),D(100)) for n in range(20)] + [(20,D(100),D(101)),(21,D(107),D(110))]
        self.assertEqual(replay(bars,"breakout")["fills"][0],{"bar":21,"side":"BUY","price":"107"})


if __name__ == "__main__":
    unittest.main()
