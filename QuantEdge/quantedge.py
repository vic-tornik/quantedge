"""QuantEdge local trading terminal. Run python quantedge.py --help."""
import argparse
import asyncio
import json
import os
import sqlite3
import sys
import time
from decimal import Decimal as D
from dashboard import serve

from engine import Desk, Fault, Journal, SYMBOLS, amount, dec
from exchange import Binance
from signals import candles, decision, fixture, replay
from verification import verify
from foundation import sync_fills, snapshot, export_postgres, health_view, market_view
from depth import depth_view

STRATEGIES = ("breakout", "reversion")


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--mode", choices=("paper", "testnet", "live"), default="paper")
    cmd = p.add_subparsers(dest="action", required=True)
    collect_cmd = cmd.add_parser("collect", help="collect WebSocket quotes and portfolio snapshots")
    collect_cmd.add_argument("--stale-seconds", type=int, default=10)
    cmd.add_parser("portfolio", help="reconcile recorded fills and snapshot portfolio using fresh collected quotes")
    cmd.add_parser("health", help="show collector, persistence and market freshness status")
    cmd.add_parser("depth", help="show synchronized top-20 depth for collected markets")
    cmd.add_parser("export-postgres", help="export durable events and journal data to QE_POSTGRES_DSN")
    v = cmd.add_parser("verify-testnet", help="check public access, signed validation or a virtual order")
    group = v.add_mutually_exclusive_group()
    group.add_argument("--public-only", action="store_true", help="check market access without credentials")
    group.add_argument("--exercise", action="store_true", help="send one virtual-money IOC buy and query it")
    group.add_argument("--ticket", metavar="ID", help="check an existing ticket directly at Testnet")
    v.add_argument("--symbol", choices=SYMBOLS, default="BTCUSDT")
    v.add_argument("--budget", default="8", help="maximum test USDT before fees for the example ticket")
    cmd.add_parser("balance", help="show available BTC, ETH and USDT")
    cmd.add_parser("orders", help="show the latest 100 local tickets")
    cmd.add_parser("dashboard", help="open a local read-only order monitor on port 8765")
    for action in ("quote","preview","signal","watch"):
        c = cmd.add_parser(action)
        c.add_argument("symbol", choices=SYMBOLS)
        if action == "preview":
            c.add_argument("side", choices=("BUY", "SELL"))
            c.add_argument("quantity", help="base asset units")
            c.add_argument("price", help="limit price in USDT per base unit")
        if action in ("signal","watch"):
            c.add_argument("strategy", choices=STRATEGIES)
        if action == "watch":
            c.add_argument("--cycles", type=int, default=1, help="one evaluation per minute")
            c.add_argument("--budget", default="8", help="USDT per paper entry")
    c = cmd.add_parser("replay", help="run both strategies on the bundled offline fixture")
    c = cmd.add_parser("submit")
    c.add_argument("id")
    c.add_argument("--confirm-live", metavar="ORDER_ID", help="repeat this exact ticket ID to arm a live order")
    c = cmd.add_parser("status")
    c.add_argument("id")
    cmd.add_parser("pause", help="persistently block new submissions for this mode")
    cmd.add_parser("resume")
    return p


def show(obj):
    print(json.dumps(obj, indent=2), flush=True)


def watch(args, desk):
    if desk.j.mode != "paper":
        raise Fault("Automatic strategy execution is available in paper mode. Use preview for exchange orders.")
    if not 1 <= args.cycles <= 1440:
        raise Fault("Choose 1 to 1440 cycles.")
    budget = amount(args.budget)
    for n in range(args.cycles):
        bars = candles(desk.venue, args.symbol)
        side = decision([b[2] for b in bars], args.strategy)
        stamp = str(bars[-1][0])
        key = f"evaluated:{args.symbol}:{args.strategy}"
        with desk.j.lock():
            already = desk.j.config(key) == stamp
            if not already:
                desk.j.set(key, stamp)
        event = {"bar_open_utc_ms": stamp, "signal": side}
        if not already and side != "HOLD":
            info, book = desk.venue.market(args.symbol)
            filters = {f["filterType"]: f for f in info["filters"]}
            step = D(filters["LOT_SIZE"]["stepSize"])
            price = D(book["askPrice" if side == "BUY" else "bidPrice"])
            position = D(desk.j.wallet()[SYMBOLS[args.symbol]])
            units = budget / price
            if side == "SELL":
                units = min(position, units)
            if side == "BUY" and position >= step:
                units = D(0)
            qty = (units // step) * step
            if qty:
                try:
                    ticket = desk.preview(args.symbol, side, dec(qty), dec(price))
                    event["order"] = desk.submit(ticket["id"])
                except Fault as exc:
                    event["reason"] = str(exc)
        event["already_evaluated"] = already
        show(event)
        if n + 1 < args.cycles:
            time.sleep(60)


def run(args):
    if args.action == "verify-testnet" and args.mode != "testnet":
        raise Fault("Use --mode testnet verify-testnet.")
    if args.action == "replay":
        return [replay(fixture(), strategy) for strategy in STRATEGIES]
    journal = Journal(args.mode, os.getenv("QE_STATE_DIR"))
    venue = Binance(args.mode)
    desk = Desk(journal, venue)
    try:
        if args.action == "depth":
            return depth_view(journal)
        if args.action == "health":
            return {"mode":journal.mode,"components":health_view(journal),"markets":market_view(journal)}
        if args.action == "collect":
            from collector import collect
            if not 2 <= args.stale_seconds <= 60:
                raise Fault("Choose a stale threshold between 2 and 60 seconds.")
            return asyncio.run(collect(journal,venue,show,args.stale_seconds))
        if args.action == "portfolio":
            sync_fills(journal,venue)
            return snapshot(journal,venue)
        if args.action == "export-postgres":
            if not os.getenv("QE_POSTGRES_DSN"):
                raise Fault("Set QE_POSTGRES_DSN to your PostgreSQL connection string.")
            return export_postgres(journal,os.environ["QE_POSTGRES_DSN"])
        if args.action == "verify-testnet":
            return verify(desk, args, show)
        if args.action == "dashboard":
            return serve(journal)
        if args.action == "balance":
            _, wallet = desk.account()
            return {asset: wallet.get(asset, "0") for asset in ("USDT","BTC","ETH")}
        if args.action == "quote":
            return venue.quote(args.symbol)
        if args.action == "preview":
            return desk.preview(args.symbol, args.side, args.quantity, args.price)
        if args.action == "submit":
            return desk.submit(args.id, args.confirm_live == args.id)
        if args.action == "status":
            return desk.status(args.id)
        if args.action == "orders":
            return journal.history()
        if args.action in ("pause", "resume"):
            paused = args.action == "pause"
            journal.set("paused", "1" if paused else "0")
            return {"mode": args.mode, "submissions_paused": paused}
        if args.action == "signal":
            bars = candles(venue, args.symbol)
            return {"symbol": args.symbol, "strategy": args.strategy, "bar_open_ms": bars[-1][0],
                    "close": dec(bars[-1][2]), "signal": decision([b[2] for b in bars], args.strategy)}
        if args.action == "watch":
            watch(args, desk)
    finally:
        journal.db.close()


if __name__ == "__main__":
    try:
        result = run(parser().parse_args())
        if result is not None:
            show(result)
    except ImportError:
        sys.exit("Install the foundation dependencies with python -m pip install -r requirements.txt")
    except KeyboardInterrupt:
        sys.exit(0)
    except (Fault, sqlite3.Error, OSError, ArithmeticError, KeyError, IndexError, TypeError) as err:
        sys.exit(f"QuantEdge: {err}")
