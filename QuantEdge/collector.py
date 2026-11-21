"""Binance book-ticker collection with bounded messages and reconnects."""
import asyncio
import json
import os
import random
import time

from engine import SYMBOLS, Fault
from foundation import ingest_quote, sync_fills, snapshot, export_postgres, set_health
from depth import DepthSession

STREAM_HOST = {"paper":"wss://data-stream.binance.vision", "live":"wss://data-stream.binance.vision",
               "testnet":"wss://stream.testnet.binance.vision"}


async def consume(ws, journal, service, stale_seconds=10, interval=10, max_events=None, depth=None):
    last = {symbol:time.monotonic() for symbol in SYMBOLS}
    started = time.monotonic()
    serviced = started
    count = 0
    while True:
        if depth is not None:
            depth.poll()
            depth.check_fresh(started,stale_seconds)
        now = time.monotonic()
        if now-started > 23*3600 or any(now-seen > stale_seconds for seen in last.values()):
            raise Fault("Market stream is stale or needs scheduled rotation.")
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=min(2,stale_seconds))
        except asyncio.TimeoutError:
            raw = None
        if raw is not None:
            data = json.loads(raw)
            event = data.get("data",data)
            if event.get("e") == "serverShutdown":
                raise Fault("Exchange requested stream reconnect.")
            if event.get("e") == "depthUpdate" and depth is not None:
                depth.event(event)
                count += 1
            elif "s" in event:
                accepted = ingest_quote(journal,event)
                if accepted:
                    last[event["s"]] = time.monotonic()
                count += 1
        if time.monotonic()-serviced >= interval:
            set_health(journal,"stream","connected")
            service()
            serviced = time.monotonic()
        if max_events and count >= max_events:
            service()
            return


def service(journal, venue, report):
    try:
        sync_fills(journal,venue)
        state = snapshot(journal,venue)
        set_health(journal,"portfolio","ready")
        report({"event":"portfolio_snapshot", "equity_USDT":state["equity_USDT"], "fills":state["fills"]})
    except Exception as err:
        set_health(journal,"portfolio","error",type(err).__name__)
        report({"event":"portfolio_error","error_type":type(err).__name__})
    if os.getenv("QE_POSTGRES_DSN"):
        try:
            progress=export_postgres(journal,os.environ["QE_POSTGRES_DSN"])
            set_health(journal,"postgres","synced",json.dumps(progress))
            report({"event":"postgres_synced",**progress})
        except Exception as err:
            set_health(journal,"postgres","error",type(err).__name__)
            # Database exceptions can contain the connection string. Keep credentials out of logs.
            report({"event":"postgres_unavailable","error_type":type(err).__name__})
    else:
        set_health(journal,"postgres","not configured")


async def collect(journal, venue, report, stale_seconds=10, connect=None, sleep=asyncio.sleep, service_fn=None):
    if connect is None:
        from websockets.asyncio.client import connect
    worker = None
    if service_fn is None:
        from background import BackgroundService
        worker = BackgroundService(journal,service,report)
        worker.start()
        service_fn = worker.request
    journal.set("market_stale_seconds",str(stale_seconds))
    try:
        await reconnect(journal,report,stale_seconds,connect,sleep,service_fn)
    finally:
        journal.set("stream_state","stopped")
        set_health(journal,"stream","stopped")
        if worker is not None:
            worker.close()


async def reconnect(journal, report, stale_seconds, connect, sleep, service_fn):
    streams = "/".join(s.lower()+suffix for s in SYMBOLS for suffix in ("@bookTicker","@depth@100ms"))
    url = STREAM_HOST[journal.mode]+"/stream?streams="+streams
    delay = 1
    while True:
        depth = None
        connected = None
        try:
            async with connect(url,open_timeout=8,ping_interval=None,max_size=1048576,max_queue=64) as ws:
                with journal.lock():
                    journal.db.execute("DELETE FROM market_latest")
                    journal.db.execute("DELETE FROM depth_latest")
                    journal.set("stream_state","connected")
                    set_health(journal,"stream","connected")
                connected = time.monotonic()
                depth = DepthSession(journal)
                report({"event":"stream_connected","mode":journal.mode})
                await consume(ws,journal,service_fn,stale_seconds,depth=depth)
        except asyncio.CancelledError:
            raise
        except Exception as err:
            if connected is not None and time.monotonic()-connected > 30:
                delay = 1
            journal.set("stream_state","disconnected")
            set_health(journal,"stream","disconnected",type(err).__name__)
            report({"event":"stream_disconnected","error_type":type(err).__name__,"retry_seconds":delay})
            service_fn()
            if depth is not None:
                depth.close()
                depth = None
            await sleep(delay + random.uniform(0,.25))
            delay = min(delay*2,30)
        else:
            delay=1
        finally:
            if depth is not None:
                depth.close()
