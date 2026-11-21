"""Minimal Binance Spot REST client. Order submissions have no transport retries."""
import hashlib
import hmac
import json
import os
import socket
import ssl
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

HOST = {"live": "https://api.binance.com", "testnet": "https://testnet.binance.vision",
        "paper": "https://data-api.binance.vision"}


class Fault(ValueError):
    pass


class VenueError(Fault):
    def __init__(self, code, status):
        self.code = code
        self.status = status
        super().__init__(f"Exchange returned HTTP {status}, Binance error {code}.")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


class Binance:
    def __init__(self, mode):
        self.mode = mode
        self.transport = build_opener(NoRedirect())

    def request(self, method, path, params=None, signed=False):
        params = dict(params or {})
        headers = {}
        if signed:
            if self.mode == "paper":
                raise Fault("Paper mode cannot sign exchange requests.")
            prefix = "QE_" + self.mode.upper()
            key, secret = (os.environ.get(prefix + suffix) for suffix in ("_KEY", "_SECRET"))
            if not key or not secret:
                raise Fault(f"Set {prefix}_KEY and {prefix}_SECRET in your local terminal.")
            params.update(timestamp=time.time_ns() // 1000000, recvWindow=5000)
            payload = urlencode(params).encode()
            params["signature"] = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
            headers["X-MBX-APIKEY"] = key
        url = HOST[self.mode] + "/api/v3/" + path
        data = urlencode(params)
        body = None
        if method == "POST":
            body = data.encode()
        elif data:
            url += "?" + data
        req = Request(url, data=body, method=method, headers=headers)
        try:
            with self.transport.open(req, timeout=8) as res:
                return json.load(res)
        except HTTPError as error:
            try:
                code = json.loads(error.read())["code"]
            except (ValueError, KeyError, TypeError):
                code = None
            raise VenueError(code, error.code) from None
        except URLError as error:
            reason = error.reason
            if isinstance(reason, socket.gaierror):
                raise Fault("DNS lookup failed. Check the network connection and resolver.") from None
            if isinstance(reason, ssl.SSLError):
                raise Fault("TLS verification or negotiation failed. Check system time and trusted certificates.") from None
            raise Fault(f"Exchange connection failed ({type(reason).__name__}).") from None
        except (OSError, ValueError) as error:
            raise Fault(f"Exchange connection or response failed ({type(error).__name__}).") from None

    def market(self, symbol):
        info = self.request("GET", "exchangeInfo", {"symbol": symbol})["symbols"][0]
        return info, self.quote(symbol)

    def quote(self, symbol):
        return self.request("GET", "ticker/bookTicker", {"symbol": symbol})

    def account(self):
        account = self.request("GET", "account", signed=True)
        if not account["canTrade"] or account["accountType"] != "SPOT":
            raise Fault("Account is unavailable for Spot trading.")
        return str(account["uid"]), {b["asset"]: b["free"] for b in account["balances"]}

    @staticmethod
    def order_parameters(ticket):
        return {
            "symbol": ticket["symbol"], "side": ticket["side"], "type": "LIMIT",
            "timeInForce": "IOC", "quantity": ticket["qty"], "price": ticket["price"],
            "newClientOrderId": ticket["id"], "newOrderRespType": "FULL",
        }

    def place(self, ticket):
        return self.request("POST", "order", self.order_parameters(ticket), signed=True)

    def validate_test_order(self, ticket):
        if self.mode != "testnet":
            raise Fault("This acceptance check requires Binance Spot Testnet.")
        reply = self.request("POST", "order/test", self.order_parameters(ticket), signed=True)
        if reply != {}:
            raise Fault("Unexpected test-order acknowledgement.")

    def lookup(self, ticket):
        return self.request("GET", "order", {"symbol": ticket["symbol"],
                            "origClientOrderId": ticket["id"]}, signed=True)
