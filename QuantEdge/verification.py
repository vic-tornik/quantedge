"""Operator-run Binance Spot Testnet checks. No production endpoints."""
import json
import time
from decimal import Decimal as D

from engine import TERMINAL, Fault, amount, check_market, dec, limits


def verify(desk, args, report):
    if desk.j.mode != "testnet" or desk.venue.mode != "testnet":
        raise Fault("Run with --mode testnet.")
    if args.ticket:
        return verify_receipt(desk, args.ticket)

    start_ms = time.time_ns() // 1_000_000
    server_ms = int(desk.venue.request("GET", "time")["serverTime"])
    end_ms = time.time_ns() // 1_000_000
    offset = server_ms - (start_ms + end_ms) // 2
    report({"stage": "server_time", "round_trip_ms": end_ms-start_ms, "estimated_offset_ms": offset})
    if end_ms < start_ms or end_ms-start_ms > 1500 or abs(offset) > 750:
        raise Fault("Clock estimate has excessive delay or offset. Synchronize the clock and retry.")
    symbol = args.symbol
    info, book = desk.venue.market(symbol)
    price = amount(book["askPrice"])
    step = amount({f["filterType"]: f for f in info["filters"]}["LOT_SIZE"]["stepSize"])
    qty = (min(amount(args.budget), limits()[0]) / price // step) * step
    if qty <= 0:
        raise Fault("Budget is below one quantity increment.")
    check_market(info, book, symbol, "BUY", qty, price)
    report({"stage": "market_rules", "symbol": symbol, "qty": dec(qty), "price": dec(price),
            "limit_notional_USDT": dec(qty * price)})
    if args.public_only:
        return {"verified": "public clock, market data and local order filters"}
    ticket = desk.preview(symbol, "BUY", dec(qty), dec(price))
    report({"stage": "account_and_preview", "ticket": ticket["id"]})
    desk.venue.validate_test_order(ticket)
    report({"stage": "signed_order_validation", "accepted": True})
    if not args.exercise:
        return {"verified": "signed Testnet validation", "submitted": False}
    result = desk.submit(ticket["id"])
    report({"stage": "testnet_submission", "ticket": ticket["id"], "status": result["status"]})
    if result["status"] == "REJECTED":
        raise Fault("Testnet submission rejected. Inspect the ticket with status.")
    return verify_receipt(desk, ticket["id"])


def verify_receipt(desk, oid):
    # Explicitly query the exchange even when the local journal already has a terminal receipt.
    with desk.j.lock():
        ticket = desk.j.row(oid)
        if ticket["sent"] is None or ticket["status"] == "REJECTED":
            raise Fault("Choose a dispatched Testnet ticket with a possible exchange receipt.")
        if desk.account()[0] != ticket["uid"]:
            raise Fault("Use the Testnet account that submitted this ticket.")
        receipt = desk.venue.lookup(ticket)
        if ticket["status"] in TERMINAL:
            original = json.loads(ticket["result"])
            if (any(receipt[k] != original[k] for k in ("orderId","clientOrderId","symbol","side",
                                                       "status","timeInForce","type"))
                    or any(D(receipt[k]) != D(original[k]) for k in ("origQty","price","executedQty","cummulativeQuoteQty"))):
                raise Fault("Exchange receipt conflicts with the saved execution. Inspect exchange history.")
        else:
            desk.record(ticket, receipt)
    if receipt["status"] not in TERMINAL:
        raise Fault(f"Ticket {oid} is still active. Repeat verify-testnet --ticket {oid}.")
    return {"verified": "terminal order retrieved from Testnet", "ticket": oid,
            "status": receipt["status"], "executed_qty": receipt["executedQty"]}
