"""Decimal weighted-average inventory accounting for recorded fills."""
from decimal import Decimal as D

from engine import SYMBOLS, Fault, dec


def calculate(fills, balances, marks):
    positions = {base: {"quantity": D(0), "cost": D(0)} for base in SYMBOLS.values()}
    realized = D(0)
    fees = {}
    issues = []
    for fill in fills:
        base = SYMBOLS[fill["symbol"]]
        qty, quote, fee = (D(fill[k]) for k in ("qty", "quote_qty", "commission"))
        asset = fill["commission_asset"]
        if (not all(v.is_finite() for v in (qty,quote,fee)) or qty <= 0 or quote <= 0 or fee < 0
                or fill["side"] not in ("BUY","SELL") or (fill["side"] == "BUY" and asset == base and fee >= qty)):
            raise Fault("Invalid recorded fill quantities.")
        fees[asset] = fees.get(asset, D(0)) + fee
        p = positions[base]
        if fill["side"] == "BUY":
            p["quantity"] += qty - (fee if asset == base else D(0))
            p["cost"] += quote + (fee if asset == "USDT" else D(0))
            if p["quantity"] <= 0:
                raise Fault("Commission consumes the entire acquired quantity.")
        else:
            removed = qty + (fee if asset == base else D(0))
            if p["quantity"] < removed:
                issues.append("A sale includes inventory whose acquisition cost is unknown.")
                p["quantity"], p["cost"] = D(0), D(0)
            else:
                basis = p["cost"] * removed / p["quantity"]
                realized += quote - (fee if asset == "USDT" else D(0)) - basis
                p["quantity"] -= removed
                p["cost"] -= basis
        if asset not in (base, "USDT") and fee:
            issues.append("Third-asset commissions require historical fee conversion.")
    equity = D(0)
    unpriced = []
    for asset, quantity in balances.items():
        quantity = D(quantity)
        if not quantity:
            continue
        if asset == "USDT":
            equity += quantity
        elif asset in marks:
            equity += quantity * D(marks[asset])
        else:
            unpriced.append(asset)
    unrealized = D(0)
    for asset, p in positions.items():
        if p["quantity"]:
            if asset not in marks:
                unpriced.append(asset)
            else:
                unrealized += p["quantity"] * D(marks[asset]) - p["cost"]
    return {
        "equity_USDT": dec(equity) if not unpriced else None,
        "realized_USDT": dec(realized) if not issues else None,
        "unrealized_USDT": dec(unrealized) if not issues and not unpriced else None,
        "positions": {a: {k: dec(v) for k,v in p.items()} for a,p in positions.items()},
        "fees": {a: dec(v) for a,v in fees.items()},
        "unpriced_assets": sorted(set(unpriced)), "accounting_issues": sorted(set(issues)),
    }
