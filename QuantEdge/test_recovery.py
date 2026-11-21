import threading
import time
import unittest
from unittest.mock import Mock

from background import BackgroundService
from dashboard import page
from engine import Journal
from foundation import ingest_quote, market_view, snapshot, set_health, health_view
import test_foundation as fixtures


class RecoveryTests(unittest.TestCase):
    setUp = fixtures.FoundationTests.setUp

    def test_background_request_does_not_wait_for_account_work(self):
        entered = threading.Event()
        release = threading.Event()
        identities = []
        def task(journal,venue,report):
            identities.append(id(journal.db))
            entered.set()
            if not release.wait(10):
                raise RuntimeError("Test release was not signalled.")
        reports = []
        worker = BackgroundService(self.j,task,reports.append)
        worker.start()
        try:
            self.assertTrue(entered.wait(10))
            worker.request()
            self.assertTrue(worker.requested.is_set())
            self.assertNotEqual(identities[0],id(self.j.db))
            ingest_quote(self.j,fixtures.event())
            self.assertFalse(market_view(self.j)[0]["stale"])
        finally:
            release.set()
            worker.close()
        self.assertFalse(worker.thread.is_alive())
        self.assertFalse(any(r["event"] == "portfolio_worker_failed" for r in reports))

    def test_account_fetch_releases_local_writer(self):
        j = Journal("testnet",self.folder.name)
        peer = Journal("testnet",self.folder.name)
        try:
            j.set("account","42")
            def account(*args,**kwargs):
                peer.set("write_during_account_fetch","allowed")
                return {"uid":42,"balances":[{"asset":"USDT","free":"100","locked":"0"}]}
            venue=Mock()
            venue.request.side_effect=account
            result=snapshot(j,venue)
            self.assertEqual(result["equity_USDT"],"100")
            self.assertEqual(peer.config("write_during_account_fetch"),"allowed")
        finally:
            peer.db.close()
            j.db.close()

    def test_disconnect_invalidates_cached_quotes_and_configured_threshold(self):
        self.j.set("market_stale_seconds","30")
        ingest_quote(self.j,fixtures.event(),100)
        self.assertFalse(market_view(self.j,now=120)[0]["stale"])
        self.j.set("stream_state","disconnected")
        self.assertTrue(market_view(self.j,now=101)[0]["stale"])

    def test_health_dashboard_and_failure_state(self):
        ingest_quote(self.j,fixtures.event())
        ingest_quote(self.j,fixtures.event("ETHUSDT"))
        snapshot(self.j)
        set_health(self.j,"portfolio","error","Fault")
        self.assertEqual(health_view(self.j)[0]["state"],"error")
        html=page(self.j)
        self.assertIn("Collector health",html)
        self.assertIn("Tracked asset",html)
        self.assertIn("Unavailable",html)
        self.assertNotIn("__HEALTH__",html)


if __name__=="__main__":
    unittest.main()
