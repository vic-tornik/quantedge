"""Closed-candle signal evaluation and deterministic, fee-aware paper replay."""
from decimal import Decimal as D
from statistics import mean

from engine import FEE, Fault, dec


def decision(closes, strategy):
    if len(closes) < 21:
        return "HOLD"
    last = closes[-1]
    history = closes[-21:-1]
    if strategy == "breakout":
        if last > max(history):
            return "BUY"
        if last < min(history):
            return "SELL"
    elif strategy == "reversion":
        avg = mean(history)
        if last < avg * D("0.99"):
            return "BUY"
        if last >= avg:
            return "SELL"
    else:
        raise Fault("Unknown strategy.")
    return "HOLD"


def candles(venue, symbol):
    # Anchor to exchange time before getting candles to exclude the forming bar.
    cutoff = int(venue.request("GET", "time")["serverTime"])
    bars = venue.request("GET", "klines", {"symbol": symbol, "interval": "1m", "limit": 100})
    closed = [(int(b[0]), D(b[1]), D(b[4])) for b in bars if int(b[6]) < cutoff]
    if len(closed) < 21 or cutoff - closed[-1][0] > 125000:
        raise Fault("Insufficient recent closed candles.")
    if any(b[0] - a[0] != 60000 for a,b in zip(closed, closed[1:])):
        raise Fault("Gap in one-minute candle history.")
    return closed


def replay(bars, strategy):
    cash, position = D(100), D(0)
    closes, trades = [], []
    previous = "HOLD"
    for ts, opening, close in bars:
        if previous == "BUY" and position == 0:
            value = min(D(10), cash / (1 + FEE))
            position = value / opening
            cash -= value * (1 + FEE)
            trades.append({"bar": ts, "side": "BUY", "price": dec(opening)})
        elif previous == "SELL" and position > 0:
            cash += position * opening * (1 - FEE)
            position = D(0)
            trades.append({"bar": ts, "side": "SELL", "price": dec(opening)})
        closes.append(close)
        previous = decision(closes, strategy)
    return {"strategy": strategy, "bars": len(closes), "initial_USDT": "100",
            "ending_marked_equity_USDT": dec(round(cash + position * closes[-1], 4)),
            "fills": trades}


def fixture():
    levels = [100 + (i % 9) // 3 for i in range(25)] + list(range(103,117)) + list(range(116,90,-1)) + list(range(91,111))
    return [(i * 60000, D(levels[max(i-1,0)]), D(v)) for i,v in enumerate(levels)]
