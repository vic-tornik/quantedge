import asyncio
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from collector import consume, collect
from dashboard import page
from engine import Journal, Fault
from foundation import ingest_quote, market_view, snapshot, sync_fills, export_postgres
from portfolio import calculate


def fill(side="BUY", qty="1", quote="100", fee="1", asset="USDT"):
    return dict(symbol="BTCUSDT", side=side, qty=qty, quote_qty=quote, commission=fee, commission_asset=asset)


def event(symbol="BTCUSDT", update=1):
    return dict(s=symbol,u=update,b="100",a="102")


def run_inline(coroutine):
    """Drive mocked I/O coroutines without opening Windows event-loop sockets."""
    try:
        coroutine.send(None)
    except StopIteration as result:
        return result.value
    finally:
        coroutine.close()
    raise AssertionError("Mocked coroutine unexpectedly required a real scheduler.")


async def immediate(awaitable, timeout):
    return await awaitable


class FoundationTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent.parent/"work")
        self.addCleanup(self.folder.cleanup)
        self.j=Journal("paper",self.folder.name)
        self.addCleanup(self.j.db.close)

    def test_average_cost_and_fee_accounting(self):
        result=calculate([fill(),fill("SELL","0.5","60","0.6")],{"USDT":"58.4","BTC":"0.5"},{"BTC":"110"})
        self.assertEqual(result["realized_USDT"],"8.9")
        self.assertEqual(result["unrealized_USDT"],"4.5")
        self.assertEqual(result["equity_USDT"],"113.4")
        self.assertEqual(result["positions"]["BTC"]["cost"],"50.5")

    def test_base_commission_and_unknown_fee(self):
        r=calculate([fill(qty="1",fee=".01",asset="BTC")],{},{"BTC":"100"})
        self.assertEqual(r["positions"]["BTC"]["quantity"],"0.99")
        r=calculate([fill(asset="BNB")],{"BNB":".1"},{"BTC":"100"})
        self.assertIsNone(r["realized_USDT"])
        self.assertIsNone(r["equity_USDT"])
        self.assertTrue(r["accounting_issues"])

    def test_unknown_acquisition_cost(self):
        r=calculate([fill("SELL")],{},{"BTC":"100"})
        self.assertIsNone(r["realized_USDT"])

    def test_restart_dedupe_and_stale(self):
        ingest_quote(self.j,event(),100)
        reopened=Journal("paper",self.folder.name)
        try:
            self.assertFalse(ingest_quote(reopened,event(),101))
            self.assertTrue(market_view(reopened,now=120)[0]["stale"])
            self.assertFalse(market_view(reopened,now=105)[0]["stale"])
            self.assertTrue(ingest_quote(reopened,event(update=2),121))
            self.assertEqual(reopened.db.execute("SELECT count(*) FROM market_events").fetchone()[0],2)
        finally:
            reopened.db.close()

    def test_partial_paper_fill_import_is_idempotent(self):
        receipt=json.dumps(dict(executedQty=".05",cummulativeQuoteQty="5",commissionUSDT=".005"))
        self.j.db.execute("INSERT INTO orders VALUES('q1','BTCUSDT','BUY','.08','100',1,1,'EXPIRED','paper',?)",(receipt,))
        sync_fills(self.j,None)
        sync_fills(self.j,None)
        self.assertEqual(self.j.db.execute("SELECT count(*) FROM recorded_fills").fetchone()[0],1)
        ingest_quote(self.j,event())
        ingest_quote(self.j,event("ETHUSDT"))
        r=snapshot(self.j)
        self.assertEqual(r["positions"]["BTC"]["quantity"],"0.05")
        html=page(self.j)
        self.assertIn("Market stream",html)
        self.assertIn("Recorded commissions",html)

    def test_invalid_and_out_of_order_quotes(self):
        for bid,ask in (("NaN","100"),("102","100"),("-1","100")):
            e=event()
            e.update(b=bid,a=ask)
            with self.assertRaises((Fault,ArithmeticError)):
                ingest_quote(self.j,e)
        ingest_quote(self.j,event(update=2))
        self.assertFalse(ingest_quote(self.j,event(update=1)))

    def test_live_trade_identity_conflict(self):
        j=Journal("testnet",self.folder.name)
        try:
            j.set("account","42")
            receipt=json.dumps(dict(orderId=1,executedQty=".05"))
            j.db.execute("INSERT INTO orders VALUES('q2','BTCUSDT','BUY','.08','100',1,1,'EXPIRED','42',?)",(receipt,))
            venue=Mock()
            venue.account.return_value=("42",{})
            venue.request.return_value=[dict(id=2,orderId=1,symbol="BTCUSDT",isBuyer=True,qty=".05",quoteQty="5",
                                            commission=".005",commissionAsset="USDT",time=1000)]
            sync_fills(j,venue)
            sync_fills(j,venue)
            self.assertEqual(j.db.execute("SELECT count(*) FROM recorded_fills").fetchone()[0],1)
            venue.request.return_value[0]["quoteQty"]="6"
            with self.assertRaises(Fault):
                sync_fills(j,venue)
        finally:
            j.db.close()

    def test_stream_consumes_and_services(self):
        class Socket:
            def __init__(self):
                self.events=iter([event(),event("ETHUSDT")])
            async def recv(self):
                return json.dumps({"data":next(self.events)})
        service=Mock()
        with patch("collector.asyncio.wait_for",immediate):
            run_inline(consume(Socket(),self.j,service,max_events=2))
        self.assertFalse(any(q["stale"] for q in market_view(self.j)))
        service.assert_called_once()

    def test_reconnect_after_disconnect(self):
        attempts=[]
        class Connection:
            async def __aenter__(self):
                attempts.append(1)
                raise OSError("disconnect")
            async def __aexit__(self,*args):
                return False
        async def sleeper(delay):
            if len(attempts)==2:
                raise asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError), patch("collector.service"):
            run_inline(collect(self.j,None,lambda _:None,connect=lambda *a,**k:Connection(),
                               sleep=sleeper,service_fn=Mock()))
        self.assertEqual(len(attempts),2)

    def test_stale_socket_rotates(self):
        class Socket:
            async def recv(self):
                await asyncio.sleep(.02)
                return json.dumps({})
        with self.assertRaises(Fault), patch("collector.time.monotonic",side_effect=[0,.01,.02,.04]):
            run_inline(consume(Socket(),self.j,Mock(),stale_seconds=.025))


@unittest.skipUnless(os.getenv("QE_TEST_POSTGRES_DSN"),"Requires a running PostgreSQL and QE_TEST_POSTGRES_DSN")
class PostgresIntegration(unittest.TestCase):
    setUp = FoundationTests.setUp
    def test_export_retry_and_restart(self):
        from depth import record_depth
        record_depth(self.j,"BTCUSDT","snapshot",{"lastUpdateId":1,"bids":[],"asks":[]})
        ingest_quote(self.j,event())
        snapshot(self.j)
        first=export_postgres(self.j,os.environ["QE_TEST_POSTGRES_DSN"])
        self.assertEqual(export_postgres(self.j,os.environ["QE_TEST_POSTGRES_DSN"]),first)
        import psycopg
        with psycopg.connect(os.environ["QE_TEST_POSTGRES_DSN"]) as pg:
            key=self.j.config("analytics_instance")+":paper"
            self.assertEqual(pg.execute("SELECT count(*) FROM qe_market_events WHERE scope=%s",(key,)).fetchone()[0],1)
            self.assertEqual(pg.execute("SELECT count(*) FROM qe_balances WHERE scope=%s",(key,)).fetchone()[0],3)
            self.assertEqual(pg.execute("SELECT count(*) FROM qe_depth_events WHERE scope=%s",(key,)).fetchone()[0],1)
        reopened=Journal("paper",self.folder.name)
        try:
            self.assertEqual(export_postgres(reopened,os.environ["QE_TEST_POSTGRES_DSN"]),first)
        finally:
            reopened.db.close()


if __name__=="__main__":
    unittest.main()
