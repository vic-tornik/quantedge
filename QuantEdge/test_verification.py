import json
import os
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from engine import Desk, Fault, Journal
from exchange import Binance
from quantedge import parser, run
from test_quantedge import Venue
from verification import verify


class AcceptanceTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        config = patch.dict(os.environ, {"QE_ORDER_CAP":"10", "QE_DAILY_CAP":"30"})
        config.start()
        self.addCleanup(config.stop)
        self.j = Journal("testnet", folder.name)
        self.addCleanup(self.j.db.close)
        self.v = Venue()
        self.v.mode = "testnet"
        self.v.request = Mock(side_effect=lambda *a: {"serverTime":time.time_ns() // 1000000})
        self.v.validate_test_order = Mock()
        original_place = self.v.place
        def place(ticket):
            receipt = original_place(ticket)
            receipt.update(orderId=381, cummulativeQuoteQty="8", fills=[{"commission":"0.008"}])
            return receipt
        self.v.place = Mock(side_effect=place)
        self.v.lookup = Mock(side_effect=lambda _: {k:v for k,v in self.v.result.items() if k != "fills"})
        self.d = Desk(self.j, self.v)

    def check(self, *flags):
        args = parser().parse_args(["--mode","testnet","verify-testnet",*flags])
        return verify(self.d, args, lambda stage: None)

    def test_public_only(self):
        self.v.account = Mock(side_effect=AssertionError)
        self.check("--public-only")
        self.assertEqual(self.j.history(), [])
        self.v.place.assert_not_called()

    def test_signed_dry_check(self):
        self.assertFalse(self.check()["submitted"])
        self.v.validate_test_order.assert_called_once()
        self.v.place.assert_not_called()
        self.assertEqual(self.j.history()[0]["status"],"PREVIEW")

    def test_exercise_and_read_only_repeat(self):
        result = self.check("--exercise")
        self.assertEqual(result["status"], "FILLED")
        self.assertEqual(self.check("--ticket",result["ticket"]),result)
        self.assertEqual(self.v.lookup.call_count,2)
        self.v.place.assert_called_once()
        self.assertIn("fills",json.loads(self.j.row(result["ticket"])["result"]))

    def test_conflicting_receipt(self):
        result = self.check("--exercise")
        self.v.result["executedQty"] = "0"
        with self.assertRaisesRegex(Fault,"conflicts"):
            self.check("--ticket",result["ticket"])
        self.v.place.assert_called_once()

    def test_bad_clock_blocks_auth(self):
        self.v.request.side_effect = None
        self.v.request.return_value = {"serverTime":1}
        with self.assertRaisesRegex(Fault, "Clock estimate"):
            self.check("--exercise")
        self.v.validate_test_order.assert_not_called()
        self.v.place.assert_not_called()

    def test_signed_failure_blocks_dispatch(self):
        self.v.validate_test_order.side_effect = Fault("invalid key")
        with self.assertRaisesRegex(Fault,"invalid key"):
            self.check("--exercise")
        self.v.place.assert_not_called()

    def test_production_verifier_disabled(self):
        with self.assertRaises(Fault):
            run(parser().parse_args(["--mode","live","verify-testnet","--exercise"]))
        with self.assertRaises(Fault):
            Binance("live").validate_test_order({})

    def test_validation_route_and_payload(self):
        api = Binance("testnet")
        api.request = Mock(return_value={})
        t = dict(symbol="BTCUSDT", side="BUY", id="example", qty="0.08", price="100")
        api.validate_test_order(t)
        api.request.assert_called_once_with("POST","order/test",api.order_parameters(t), signed=True)
