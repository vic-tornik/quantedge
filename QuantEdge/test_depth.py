from concurrent.futures import Future
from unittest.mock import Mock, patch
import json
import unittest

from depth import OrderBook, DepthSession, depth_view
from engine import Fault
from dashboard import page
from collector import consume
import test_foundation as fixtures


def seed(update=100):
    return {"lastUpdateId":update,"bids":[["100","2"],["99","3"]],"asks":[["102","4"],["103","5"]]}


def diff(first=101,last=101,bids=None,asks=None,symbol="BTCUSDT"):
    return {"e":"depthUpdate","s":symbol,"U":first,"u":last,"b":bids or [],"a":asks or []}


class DepthTests(unittest.TestCase):
    setUp = fixtures.FoundationTests.setUp

    def test_absolute_quantities_and_removal(self):
        book=OrderBook("BTCUSDT",seed())
        self.assertTrue(book.apply(diff(bids=[["100","7"]],asks=[["102","0"]])))
        self.assertEqual(book.top()["bids"][0],["100","7"])
        self.assertEqual(book.top()["asks"][0],["103","5"])

    def test_old_and_overlapping_ranges(self):
        book=OrderBook("BTCUSDT",seed())
        self.assertFalse(book.apply(diff(90,100)))
        self.assertTrue(book.apply(diff(99,102)))
        self.assertTrue(book.apply(diff(102,104)))
        self.assertEqual(book.update_id,104)

    def test_gap_is_rejected_without_mutation(self):
        book=OrderBook("BTCUSDT",seed())
        with self.assertRaisesRegex(Fault,"gap"):
            book.apply(diff(102,103,bids=[["100","99"]]))
        self.assertEqual(book.update_id,100)
        self.assertEqual(book.top()["bids"][0],["100","2"])

    def test_invalid_update_is_atomic(self):
        book=OrderBook("BTCUSDT",seed())
        with self.assertRaises(Fault):
            book.apply(diff(bids=[["100","8"]],asks=[["102","NaN"]]))
        self.assertEqual(book.top()["bids"][0],["100","2"])
        with self.assertRaises(Fault):
            book.apply(diff(bids=[["104","8"]]))

    def test_snapshot_coverage_is_conservative(self):
        book=OrderBook("BTCUSDT",seed())
        book.apply(diff(bids=[["98","9"]]))
        self.assertNotIn(["98","9"],book.top()["bids"])
        with self.assertRaisesRegex(Fault,"coverage"):
            book.apply(diff(102,102,bids=[["100","0"],["99","0"]]))

    def test_snapshot_handoff_and_persistence(self):
        future=Future()
        session=DepthSession(self.j,submit=lambda _:future)
        try:
            session.event(diff())
            session.event(diff(102,102,bids=[["100","6"]]))
            self.assertEqual(depth_view(self.j)[0]["book"],None)
            future.set_result(seed())
            session.poll()
            self.j.set("stream_state","connected")
            view=depth_view(self.j)
            self.assertTrue(view[0]["fresh"])
            self.assertEqual(view[0]["book"]["bids"][0],["100","6"])
            self.assertEqual(self.j.db.execute("SELECT count(*) FROM depth_events").fetchone()[0],3)
            self.assertIn("BTCUSDT depth",page(self.j))
            self.j.set("stream_state","disconnected")
            self.assertIsNone(depth_view(self.j)[0]["book"])
        finally:
            session.close()

    def test_snapshot_older_than_buffer_restarts(self):
        future=Future()
        future.set_result(seed())
        session=DepthSession(self.j,submit=lambda _:future)
        try:
            session.event(diff(105,106))
            with self.assertRaisesRegex(Fault,"gap"):
                session.poll()
            self.assertIsNone(depth_view(self.j)[0]["book"])
        finally:
            session.close()

    def test_newer_snapshot_waits_for_bridge(self):
        future=Future()
        future.set_result(seed(110))
        session=DepthSession(self.j,submit=lambda _:future)
        try:
            session.event(diff())
            session.poll()
            self.assertFalse(session.books["BTCUSDT"].ready)
            session.event(diff(111,112))
            self.assertTrue(session.books["BTCUSDT"].ready)
        finally:
            session.close()

    def test_collector_routes_depth_and_quotes(self):
        futures = {symbol:Future() for symbol in ("BTCUSDT","ETHUSDT")}
        for future in futures.values():
            future.set_result(seed())
        session=DepthSession(self.j,submit=lambda symbol:futures[symbol])
        events=iter([diff(),diff(symbol="ETHUSDT"),fixtures.event(),fixtures.event("ETHUSDT")])
        class Socket:
            async def recv(self):
                return json.dumps({"data":next(events)})
        self.j.set("stream_state","connected")
        try:
            with patch("collector.asyncio.wait_for",fixtures.immediate):
                fixtures.run_inline(consume(Socket(),self.j,Mock(),max_events=4,depth=session))
            self.assertTrue(all(item["fresh"] for item in depth_view(self.j)))
        finally:
            session.close()


if __name__=="__main__":
    unittest.main()
