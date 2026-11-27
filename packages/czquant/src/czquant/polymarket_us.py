"""Polymarket US (the CFTC-regulated exchange at polymarket.us): market data, orders, account.

Two hosts (docs.polymarket.us):

  gateway  (POLYMARKET_US_GATEWAY_URL, default https://gateway.polymarket.us)
           public events, markets and books; no credentials
  api      (POLYMARKET_US_API_URL, default https://api.polymarket.us)
           orders, positions, balances and activities; every request signed

Signing: an Ed25519 key from polymarket.us/developer (a Key ID and a base64 Secret
Key). Each request carries X-PM-Access-Key (the Key ID), X-PM-Timestamp (Unix ms)
and X-PM-Signature, the base64 signature of timestamp + method + path. The secret is
read from the file named by POLYMARKET_US_SECRET_KEY_PATH, never from the environment.

A market is one YES/NO instrument. A game moneyline is one market: its long side is
one team and its short side the other. Every price on the venue (book levels, order
prices, fills) is the YES (long) side's price, so the market maps onto Kalshi's YES
book exactly: a bid at p buys YES at p, an ask at p buys NO at 1 - p. A market is the
ticker `PMUS:<market slug>` and its book is stored in Kalshi's layout.

Fees (docs.polymarket.us/fees): takers pay theta x C x p x (1 - p) per fill, rounded
to the cent half-to-even, the order's total never above the rounded exact total.
theta is the market's `feeCoefficient` (0.095 at the lowest volume tier) unless the
caller overrides it (`theta`), e.g. for a higher tier or the API promotion.

The venue has no client order ID. An order whose acknowledgement is lost cannot be
recovered by ID; the monitor reports it and the trader halts until an operator checks.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
import uuid
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import Any, Iterator

import httpx
from cryptography.hazmat.primitives.asymmetric import ed25519

from .broker import Broker
from .paper import Paper
from .store import decimal

PREFIX = "PMUS:"
GATEWAY = "https://gateway.polymarket.us"
API = "https://api.polymarket.us"
CENT = Decimal("0.01")
DEFAULT_THETA = Decimal("0.095")  # lowest taker tier; a market's feeCoefficient wins when present
VENUE = "polymarket_us"
TERMINAL = {"ORDER_STATE_FILLED": "filled", "ORDER_STATE_CANCELED": "canceled",
            "ORDER_STATE_REJECTED": "rejected", "ORDER_STATE_EXPIRED": "canceled"}


def ticker(slug: str) -> str:
    return PREFIX + slug


def slug_of(ticker_: str) -> str:
    if not ticker_.startswith(PREFIX):
        raise ValueError(f"not a Polymarket US ticker: {ticker_}")
    return ticker_[len(PREFIX):]


def is_polymarket_us(ticker_: str) -> bool:
    return ticker_.startswith(PREFIX)


def amount(value: Any) -> Decimal | None:
    """An Amount ({"value": "0.55", "currency": "USD"}) or a bare number, as a Decimal."""
    if value is None:
        return None
    if isinstance(value, dict):
        value = value.get("value")
    return None if value in (None, "") else decimal(value)


def fee(fills: list[tuple[Decimal, Decimal]], theta: Decimal) -> Decimal:
    """Taker fee for one order's fills (YES price, contracts): the exact total rounded
    to the cent half-to-even, which the venue says the order's fills never exceed."""
    exact = sum((q * theta * p * (1 - p) for p, q in fills), Decimal(0))
    return exact.quantize(CENT, rounding=ROUND_HALF_EVEN)


def load_key(path: str | Path) -> ed25519.Ed25519PrivateKey:
    """The Ed25519 key from a file holding the base64 Secret Key: a 32-byte seed, or a
    64-byte keypair whose first 32 bytes are the seed."""
    text = Path(path).expanduser().read_text(encoding="utf-8-sig").strip()
    if not text:
        raise ValueError(f"the Polymarket US secret file is empty: {path}. Paste the Secret Key from "
                         "polymarket.us/developer into it (shown only once: if lost, create a new key)")
    raw = base64.b64decode(text)
    if len(raw) not in (32, 64):
        raise ValueError(f"the Polymarket US secret decodes to {len(raw)} bytes; expected 32 or 64")
    return ed25519.Ed25519PrivateKey.from_private_bytes(raw[:32])


def kalshi_layout(book: dict[str, Any]) -> dict[str, Any]:
    """A market's book in Kalshi's layout: bids as YES bids, offers as NO bids at 1 - price."""
    data = book.get("marketData", book)
    bids = [[str(amount(b["px"])), str(decimal(b["qty"]))] for b in data.get("bids") or []]
    asks = [[str(1 - amount(o["px"])), str(decimal(o["qty"]))] for o in data.get("offers") or []]  # type: ignore[operator]
    return {"orderbook_fp": {"yes_dollars": bids, "no_dollars": asks},
            "polymarket_us": {"slug": data.get("marketSlug"), "state": data.get("state"),
                              "transact_time": data.get("transactTime")}}


def normalize_market(market: dict[str, Any], book: dict[str, Any] | None = None) -> dict[str, Any]:
    """A market in the fields the shared tools read.

    status  active while MARKET_STATUS_OPEN; finalized once MARKET_STATUS_RESOLVED and
            the book's settlement price is final (settlement_value_dollars, the YES
            payout); closed otherwise
    """
    status_raw = market.get("status")
    stats = ((book or {}).get("marketData") or book or {}).get("stats") or {}
    settlement = amount(stats.get("settlementPx"))
    final = (status_raw == "MARKET_STATUS_RESOLVED" and settlement is not None
             and not stats.get("settlementPreliminaryFlag") and 0 <= settlement <= 1)
    if final:
        status = "finalized"
    elif status_raw == "MARKET_STATUS_OPEN" and market.get("active", True) and not market.get("closed"):
        status = "active"
    else:
        status = "closed"
    sides = {("long" if s.get("long") else "short"): s for s in market.get("marketSides") or []}
    return {"ticker": ticker(market["slug"]), "venue": VENUE, "status": status, "void": False,
            "settlement_value_dollars": str(settlement) if final else None,
            "slug": market["slug"], "market_id": market.get("id"), "question": market.get("question"),
            "venue_status": status_raw, "game_start": market.get("gameStartTime"), "end_date": market.get("endDate"),
            "long": (sides.get("long") or {}).get("description"), "short": (sides.get("short") or {}).get("description"),
            "fee_coefficient": market.get("feeCoefficient"), "tick": market.get("orderPriceMinTickSize"),
            "minimum_quantity": market.get("minimumTradeQty")}


class PolymarketUS:
    """Public gateway reads and signed account requests, paced under the venue's
    25 requests/s per IP. Reads retry; writes never do (an uncertain write is left
    for the monitor and the operator)."""

    def __init__(self, key_id: str | None = None, key: ed25519.Ed25519PrivateKey | None = None,
                 gateway_url: str = GATEWAY, api_url: str = API, http: Any = None,
                 min_interval: float = 0.06, dry_run: bool = False):
        self.key_id, self.key = key_id, key
        self.gateway, self.api = gateway_url.rstrip("/"), api_url.rstrip("/")
        self.http = http or httpx.Client(timeout=30, headers={"User-Agent": "czquant"})
        self.min_interval, self.dry_run = min_interval, dry_run
        self._last = 0.0

    @classmethod
    def from_env(cls, authenticated: bool = True, dry_run: bool = False) -> "PolymarketUS":
        key_id = os.environ.get("POLYMARKET_US_KEY_ID") if authenticated else None
        key = None
        if key_id:
            path = os.environ.get("POLYMARKET_US_SECRET_KEY_PATH")
            if not path:
                raise ValueError("POLYMARKET_US_KEY_ID is set but POLYMARKET_US_SECRET_KEY_PATH is not")
            key = load_key(path)
        return cls(key_id, key, os.environ.get("POLYMARKET_US_GATEWAY_URL", GATEWAY),
                   os.environ.get("POLYMARKET_US_API_URL", API), dry_run=dry_run)

    @property
    def authenticated(self) -> bool:
        return bool(self.key_id and self.key)

    def headers(self, method: str, path: str) -> dict[str, str]:
        if not self.authenticated:
            raise ValueError("Polymarket US credentials are required, and POLYMARKET_US_KEY_ID is not set in "
                             "this shell: load the strategy's .env first (bash: set -a; source .env; set +a)")
        assert self.key is not None and self.key_id is not None
        ts = str(int(time.time() * 1000))
        signature = self.key.sign((ts + method + path).encode())
        return {"X-PM-Access-Key": self.key_id, "X-PM-Timestamp": ts,
                "X-PM-Signature": base64.b64encode(signature).decode(), "Content-Type": "application/json"}

    def _pace(self) -> None:
        wait = self.min_interval - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    def _request(self, method: str, base: str, path: str, signed: bool, params: Any = None,
                 body: dict[str, Any] | None = None) -> Any:
        for attempt in range(5 if method == "GET" else 1):
            self._pace()
            headers = self.headers(method, path) if signed else {}
            response = self.http.request(method, base + path, params=params, headers=headers,
                                         content=None if body is None else json.dumps(body))
            if method == "GET" and response.status_code in (429, 500, 502, 503, 504) and attempt < 4:
                time.sleep(0.5 * 2 ** attempt)
                continue
            response.raise_for_status()
            return response.json() if response.content else {}
        raise RuntimeError("read retry exhausted")

    def public(self, path: str, params: Any = None) -> Any:
        return self._request("GET", self.gateway, path, False, params)

    def private(self, method: str, path: str, params: Any = None, body: dict[str, Any] | None = None) -> Any:
        return self._request(method, self.api, path, True, params, body)

    # -- public ----------------------------------------------------------------

    def events(self, page: int = 100, max_events: int | None = None, **params: Any) -> Iterator[dict[str, Any]]:
        offset = 0
        while max_events is None or offset < max_events:
            rows = self.public("/v1/events", {**params, "limit": page, "offset": offset}).get("events") or []
            yield from rows
            offset += len(rows)
            if len(rows) < page:
                return

    def markets_by_id(self, ids: list[str]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for i in range(0, len(ids), 50):
            chunk = ids[i:i + 50]
            out += self.public("/v1/markets", [("id", x) for x in chunk] + [("limit", len(chunk))]).get("markets") or []
        return out

    def market_raw(self, slug: str) -> dict[str, Any]:
        return self.public(f"/v1/market/slug/{slug}")["market"]

    def book_raw(self, slug: str) -> dict[str, Any]:
        """The current book: from the market WebSocket when it has a fresh snapshot,
        else a REST read. The gateway serves REST books through a CDN with
        Cache-Control max-age=30, so a plain read can return a book up to 30 s old:
        in a live game that showed edges that no longer existed (live IOCs expired
        unfilled; paper filled against them). A unique query string forces a fresh read."""
        stream = getattr(self, "stream", None)
        if stream is not None:
            book = stream.book(slug)
            if book is not None:
                return book
            stream.fallbacks += 1
        return self.public(f"/v1/markets/{slug}/book", {"_": uuid.uuid4().hex})

    def start_stream(self, slugs: list[str], max_age: float = 2.0) -> "BookStream":
        """Keep `slugs`' books current over the market WebSocket (needs credentials)."""
        if not self.authenticated:
            raise ValueError("the market WebSocket needs Polymarket US credentials")
        self.stream = BookStream(self, slugs, max_age=max_age)
        self.stream.start()
        return self.stream

    def book(self, ticker_: str) -> dict[str, Any]:
        return kalshi_layout(self.book_raw(slug_of(ticker_)))

    def books(self, slugs: list[str]) -> dict[str, dict[str, Any]]:
        """Books by slug in Kalshi's layout. The gateway has no batch book read."""
        return {slug: kalshi_layout(self.book_raw(slug)) for slug in slugs}

    def market(self, ticker_: str, fresh: bool = True) -> dict[str, Any]:
        slug = slug_of(ticker_)
        raw = self.market_raw(slug)
        book = self.book_raw(slug) if raw.get("status") == "MARKET_STATUS_RESOLVED" else None
        return normalize_market(raw, book)

    # -- account ---------------------------------------------------------------

    def create_order(self, body: dict[str, Any]) -> dict[str, Any]:
        if self.dry_run:
            self.headers("POST", "/v1/orders")  # prove the key signs; nothing is sent
            return {"dry_run": True, "request": body}
        return self.private("POST", "/v1/orders", body=body)

    def order(self, order_id: str) -> dict[str, Any]:
        return self.private("GET", f"/v1/order/{order_id}")["order"]

    def open_orders(self, slugs: list[str] | None = None) -> list[dict[str, Any]]:
        return self.private("GET", "/v1/orders/open", [("slugs", s) for s in slugs or []]).get("orders") or []

    def cancel(self, order_id: str, slug: str) -> dict[str, Any]:
        if self.dry_run:
            return {"dry_run": True}
        return self.private("POST", f"/v1/order/{order_id}/cancel", body={"marketSlug": slug})

    def balance(self) -> dict[str, Any]:
        rows = self.private("GET", "/v1/account/balances").get("balances") or []
        if not rows:
            # An account that has never been funded lists no balance at all.
            return {"currency": "USD", "currentBalance": "0", "buyingPower": "0", "unfunded": True}
        usd = [r for r in rows if r.get("currency", "USD") == "USD"]
        if len(usd) != 1:
            raise ValueError(f"expected one USD balance, got {len(usd)}")
        return usd[0]

    def positions(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        cursor = None
        while True:
            data = self.private("GET", "/v1/portfolio/positions", {"limit": 100, **({"cursor": cursor} if cursor else {})})
            out.update(data.get("positions") or {})
            cursor = data.get("nextCursor")
            if data.get("eof") or not cursor:
                return out

    def activities(self, types: list[str] | None = None) -> Iterator[dict[str, Any]]:
        cursor = None
        while True:
            params: list[tuple[str, Any]] = [("limit", 100), ("sortOrder", "SORT_ORDER_ASCENDING")]
            params += [("types", t) for t in types or []] + ([("cursor", cursor)] if cursor else [])
            data = self.private("GET", "/v1/portfolio/activities", params)
            yield from data.get("activities") or []
            cursor = data.get("nextCursor")
            if data.get("eof") or not cursor:
                return


log = logging.getLogger(__name__)

class BookStream:
    """Live books from wss://api.polymarket.us/v1/ws/markets.

    Each SUBSCRIPTION_TYPE_MARKET_DATA message is a full snapshot of one market's book
    (verified live: about ten a second during a game), so the newest message per slug is
    the book. A background thread holds the connection, resubscribes after reconnecting
    with exponential backoff, and records when each snapshot arrived. `book()` returns a
    snapshot only while it is younger than `max_age` seconds; callers fall back to REST.
    """

    URL = "wss://api.polymarket.us/v1/ws/markets"
    PATH = "/v1/ws/markets"
    CHUNK = 100  # documented maximum markets per subscription

    def __init__(self, client: "PolymarketUS", slugs: list[str], max_age: float = 2.0,
                 connect: Any = None, clock: Any = time.monotonic):
        self.client, self.slugs, self.max_age = client, list(dict.fromkeys(slugs)), max_age
        self._connect, self._clock = connect, clock
        self._lock = threading.Lock()
        self._books: dict[str, tuple[float, dict[str, Any]]] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.connected, self.reconnects, self.fallbacks, self.messages = False, 0, 0, 0
        self.last_error: str | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="pmus-book-stream", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def book(self, slug: str) -> dict[str, Any] | None:
        with self._lock:
            entry = self._books.get(slug)
        if entry is None or self._clock() - entry[0] > self.max_age:
            return None
        return entry[1]

    def handle(self, raw: str | bytes) -> None:
        message = json.loads(raw)
        data = message.get("marketData")
        if isinstance(data, dict) and data.get("marketSlug"):
            with self._lock:
                self._books[str(data["marketSlug"])] = (self._clock(), {"marketData": data})
            self.messages += 1
        elif message.get("error"):
            self.last_error = str(message["error"])[:300]
            log.warning("Polymarket US market stream: %s", self.last_error)

    def subscriptions(self) -> list[str]:
        return [json.dumps({"subscribe": {"requestId": f"md-{uuid.uuid4().hex[:12]}",
                                          "subscriptionType": "SUBSCRIPTION_TYPE_MARKET_DATA",
                                          "marketSlugs": self.slugs[i:i + self.CHUNK]}})
                for i in range(0, len(self.slugs), self.CHUNK)]

    def _open(self) -> Any:
        if self._connect is not None:
            return self._connect()
        from websockets.sync.client import connect
        headers = self.client.headers("GET", self.PATH)
        headers.pop("Content-Type", None)
        return connect(self.URL, additional_headers=headers, open_timeout=10)

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                with self._open() as ws:
                    for request in self.subscriptions():
                        ws.send(request)
                    self.connected, backoff = True, 1.0
                    while not self._stop.is_set():
                        try:
                            raw = ws.recv(timeout=5)
                        except TimeoutError:
                            continue  # no update in 5 s: the books simply age out
                        self.handle(raw)
            except Exception as exc:  # network, handshake or protocol: reconnect
                self.last_error = f"{type(exc).__name__}: {exc}"[:300]
                log.warning("Polymarket US market stream disconnected: %s", self.last_error)
            self.connected = False
            if self._stop.is_set():
                return
            self.reconnects += 1
            self._stop.wait(backoff)
            backoff = min(backoff * 2, 30.0)


def position_quantity(position: dict[str, Any]) -> Decimal:
    """Net contracts in a market, long positive and short negative (Kalshi's sign)."""
    raw = position.get("netPositionDecimal")
    return decimal(raw if raw not in (None, "") else position.get("netPosition", "0"))


def held(broker: Broker, ticker_: str, side: str) -> Decimal:
    return sum((lot["quantity"] for lot in broker.state().lots.values()
                if lot["market"] == ticker_ and lot["side"] == side), Decimal(0))


def order_body(order: dict[str, Any], closing: Decimal) -> dict[str, Any]:
    """A Kalshi-shaped YES-book order as a Polymarket US limit order.

    A bid buys YES (BUY_LONG) unless it closes held NO (SELL_SHORT); an ask buys NO
    (BUY_SHORT) unless it closes held YES (SELL_LONG). The price stays the YES price.
    An order that would both close and open is refused: send the two parts apart.
    """
    quantity = decimal(order["count"])
    bid = order["side"] == "bid"
    if 0 < closing < quantity:
        raise ValueError("an order may close or open a position, not both; split it")
    if order.get("reduce_only") and closing < quantity:
        raise ValueError("reduce-only order exceeds the held position")
    closes = closing >= quantity
    intent = ("ORDER_INTENT_SELL_SHORT" if closes else "ORDER_INTENT_BUY_LONG") if bid else \
             ("ORDER_INTENT_SELL_LONG" if closes else "ORDER_INTENT_BUY_SHORT")
    tif = {"immediate_or_cancel": "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL",
           "fill_or_kill": "TIME_IN_FORCE_FILL_OR_KILL"}.get(order.get("time_in_force", "immediate_or_cancel"))
    if tif is None or order.get("post_only"):
        raise NotImplementedError("Polymarket US live orders are taker IOC or FOK only")
    return {"marketSlug": slug_of(order["ticker"]), "type": "ORDER_TYPE_LIMIT",
            "price": {"value": str(decimal(order["price"])), "currency": "USD"},
            "quantity": float(quantity), "tif": tif, "intent": intent,
            "synchronousExecution": True, "maxBlockTime": "5"}


class PolymarketUSLive:
    """Sends a broker order to Polymarket US and books its fills from the synchronous
    execution reports (source `pmus-exec:<execution id>`), so the ledger is current
    when the trader sizes its next leg. Called by Broker.submit with the broker."""

    mode = "live"
    venue = "polymarket_us"
    policy = "polymarket-us-live-v1"
    takes_broker = True

    def __init__(self, client: PolymarketUS):
        self.client = client

    def create_order(self, order: dict[str, Any], broker: Broker) -> dict[str, Any]:
        identifier = order["client_order_id"]
        closing = held(broker, order["ticker"], "no" if order["side"] == "bid" else "yes")
        try:
            body = order_body(order, closing)
        except (ValueError, NotImplementedError):
            broker.finish(identifier, "rejected")
            raise
        response = self.client.create_order(body)
        if response.get("dry_run"):
            broker.finish(identifier, "dry_run")
            broker.observe("dry_run_order", {"client_order_id": identifier, "request": body})
            return {"client_order_id": identifier, "status": "dry_run", "filled": "0", "request": body}
        venue_id = response.get("id")
        with broker.store.transaction():
            broker.store.db.execute("UPDATE orders SET venue_id=? WHERE id=?", (venue_id, identifier))
        return self.book(broker, identifier, venue_id, response.get("executions") or [], response)

    @staticmethod
    def book(broker: Broker, identifier: str, venue_id: str | None, executions: list[dict[str, Any]],
             response: dict[str, Any]) -> dict[str, Any]:
        state = None
        for execution in executions:
            qty = decimal(execution.get("lastShares") or "0")
            if qty > 0:
                price = amount(execution.get("lastPx"))
                if price is None:
                    raise ValueError("execution lacks a price")
                charge = amount(execution.get("commissionNotionalCollected")) or Decimal(0)
                broker.fill(identifier, str(qty), str(price), str(charge), f"pmus-exec:{execution['id']}",
                            {"venue": VENUE, "venue_order_id": venue_id, "trade_id": execution.get("tradeId"),
                             "execution": execution})
            state = (execution.get("order") or {}).get("state") or state
        filled = decimal(broker._order(identifier)["filled"])
        status = TERMINAL.get(state or "")
        if status:
            broker.finish(identifier, status)
        else:
            # No terminal state in the reply: leave the reservation and let the monitor
            # read the order. The trader halts on any order left in this state.
            broker.finish(identifier, "unknown", release=False)
            status = "unknown"
        return {"client_order_id": identifier, "status": status, "filled": str(filled),
                "order": {"order_id": venue_id}, "response": response}


class PolymarketUSPaper(Paper):
    """IOC/FOK paper fills against a Polymarket US market's book at its taker fee, with
    the shared paper policy otherwise (see czquant.paper)."""

    venue = "polymarket_us"
    policy = "polymarket-us-ioc-depth-v1"

    def __init__(self, client: PolymarketUS, latency: float = 0.5, theta: Decimal | None = None):
        super().__init__(client, latency)
        self.theta = theta

    def fee_schedule(self, market: dict[str, Any]) -> dict[str, Any]:
        coefficient = market.get("fee_coefficient")
        theta = self.theta if self.theta is not None else (
            decimal(coefficient) if coefficient is not None else DEFAULT_THETA)
        return {"theta": str(theta)}

    def fee(self, fills: list[tuple[Decimal, Decimal]], schedule: dict[str, Any]) -> Decimal:
        return fee(fills, decimal(schedule["theta"]))

    def create_order(self, order: dict[str, Any], broker: Broker) -> dict[str, Any]:
        slug_of(order["ticker"])
        return super().create_order(order, broker)
