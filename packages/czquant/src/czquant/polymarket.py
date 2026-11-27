"""Polymarket as a paper venue: public market data, outcome-token books, fees, results.

Everything here reads Polymarket's public APIs and needs no credentials:

  Gamma  (POLYMARKET_GAMMA_URL, default https://gamma-api.polymarket.com)
         events and markets, with each market's outcomes, outcome tokens, fee
         schedule and resolution
  CLOB   (POLYMARKET_CLOB_URL, default https://clob.polymarket.com)
         order books per outcome token

Each Polymarket outcome is its own token, bought and sold like a YES contract that
pays $1 if that outcome wins. A token is the ticker `PM:<token id>`. Its book is
stored in Kalshi's layout so the shared report, monitor and paper adapter read it
unchanged: the token's bids are YES bids, and an ask at p is a NO bid at 1 - p.

Fees (docs.polymarket.com, "Fees"): only takers pay, per fill,
    fee = shares x rate x p x (1 - p)
rounded to 5 decimals, with each market's rate in its `feeSchedule`. The schedule
also carries an `exponent`; at 1 (every market seen so far) the formula is the
documented one, and other values are applied as (p x (1 - p)) ** exponent.

Live trading on Polymarket needs a wallet and CLOB API credentials (see the
strategy's .env.example). This module never signs or sends an order.
"""

from __future__ import annotations

import json
import os
import time
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Iterator

import httpx

from .http_errors import raise_for_status

from .broker import Broker
from .paper import Paper
from .store import decimal

PREFIX = "PM:"
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
FEE_QUANTUM = Decimal("0.00001")


def ticker(token: str) -> str:
    return PREFIX + str(token)


def token_of(ticker_: str) -> str:
    if not ticker_.startswith(PREFIX):
        raise ValueError(f"not a Polymarket ticker: {ticker_}")
    return ticker_[len(PREFIX):]


def is_polymarket(ticker_: str) -> bool:
    return ticker_.startswith(PREFIX)


def _json_list(value: Any) -> list[Any]:
    """Gamma returns some list fields as JSON text ('["Yes", "No"]')."""
    if isinstance(value, str):
        return json.loads(value) if value else []
    return list(value or [])


def fee(fills: list[tuple[Decimal, Decimal]], schedule: dict[str, Any]) -> Decimal:
    """Taker fee for one order's fills (price, shares)."""
    if not schedule.get("fees_enabled", True):
        return Decimal(0)
    rate = decimal(schedule.get("rate", "0"))
    exponent = int(schedule.get("exponent", 1))
    total = sum((q * rate * (p * (1 - p)) ** exponent for p, q in fills), Decimal(0))
    return total.quantize(FEE_QUANTUM, rounding=ROUND_HALF_UP)


def normalize_market(market: dict[str, Any], token: str) -> dict[str, Any]:
    """One outcome token's market, in the fields the shared tools read.

    status   active while the book accepts orders; closed once trading stops;
             finalized once the result is final, with settlement_value_dollars the
             token's payout (1, 0, or 0.5 for a 50-50 resolution)
    """
    tokens = [str(t) for t in _json_list(market.get("clobTokenIds"))]
    if token not in tokens:
        raise ValueError(f"token {token} is not an outcome of market {market.get('id')}")
    index = tokens.index(token)
    outcomes = _json_list(market.get("outcomes"))
    prices = _json_list(market.get("outcomePrices"))
    schedule = market.get("feeSchedule") or {}
    resolved = market.get("umaResolutionStatus") == "resolved"
    payout = None
    if resolved and market.get("closed") and len(prices) == len(tokens):
        value = decimal(prices[index])
        if 0 <= value <= 1:
            payout = value
    if payout is not None:
        status = "finalized"
    elif market.get("closed") or not market.get("acceptingOrders", True):
        status = "closed"
    else:
        status = "active"
    return {"ticker": ticker(token), "venue": "polymarket", "status": status, "void": False,
            "settlement_value_dollars": None if payout is None else str(payout),
            "outcome": outcomes[index] if index < len(outcomes) else None, "question": market.get("question"),
            "market_id": market.get("id"), "condition_id": market.get("conditionId"), "slug": market.get("slug"),
            "neg_risk": market.get("negRisk"), "end_date": market.get("endDate"),
            "game_start": market.get("gameStartTime"),
            "fee_schedule": {"fees_enabled": bool(market.get("feesEnabled")), "fee_type": market.get("feeType"),
                             "rate": str(schedule.get("rate", "0")), "exponent": schedule.get("exponent", 1),
                             "taker_only": schedule.get("takerOnly", True)},
            "resolution": {"uma_status": market.get("umaResolutionStatus"), "closed": market.get("closed"),
                           "outcome_prices": prices}}


def kalshi_layout(book: dict[str, Any]) -> dict[str, Any]:
    """A token's CLOB book in Kalshi's layout: bids as YES bids, asks as NO bids at 1 - ask."""
    bids = [[str(decimal(b["price"])), str(decimal(b["size"]))] for b in book.get("bids") or []]
    asks = [[str(1 - decimal(a["price"])), str(decimal(a["size"]))] for a in book.get("asks") or []]
    return {"orderbook_fp": {"yes_dollars": bids, "no_dollars": asks},
            "polymarket": {"asset_id": book.get("asset_id"), "timestamp": book.get("timestamp"),
                           "hash": book.get("hash"), "tick_size": book.get("tick_size"),
                           "min_order_size": book.get("min_order_size")}}


class Polymarket:
    """Public Gamma and CLOB reads, paced to `min_interval` seconds between requests."""

    def __init__(self, gamma_url: str = GAMMA, clob_url: str = CLOB, http: Any = None,
                 min_interval: float = 0.1, market_ttl: float = 30.0):
        self.gamma, self.clob = gamma_url.rstrip("/"), clob_url.rstrip("/")
        self.http = http or httpx.Client(timeout=30, headers={"User-Agent": "czquant"})
        self.min_interval, self.market_ttl = min_interval, market_ttl
        self._last = 0.0
        self._markets: dict[str, tuple[float, dict[str, Any]]] = {}  # token -> (read at, gamma market)

    @classmethod
    def from_env(cls) -> "Polymarket":
        return cls(os.environ.get("POLYMARKET_GAMMA_URL", GAMMA), os.environ.get("POLYMARKET_CLOB_URL", CLOB))

    def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        for attempt in range(5):
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            response = self.http.request(method, url, **kwargs)
            if response.status_code in (429, 500, 502, 503, 504) and attempt < 4:
                time.sleep(0.5 * 2 ** attempt)
                continue
            raise_for_status(response, "Polymarket")
            return response.json()
        raise RuntimeError("read retry exhausted")

    # -- Gamma ---------------------------------------------------------------

    def events(self, max_events: int | None = None, **params: Any) -> Iterator[dict[str, Any]]:
        """Every event matching the filters (e.g. tag_slug="nba", closed="false"), up to
        max_events. Paged by cursor: Gamma refuses large offsets on /events."""
        cursor, seen = None, 0
        while max_events is None or seen < max_events:
            query = {**params, "limit": 100, **({"after_cursor": cursor} if cursor else {})}
            page = self._request("GET", self.gamma + "/events/keyset", params=query)
            events = page.get("events") or []
            yield from events
            seen += len(events)
            cursor = page.get("next_cursor")
            if not events or not cursor:
                return

    def remember(self, market: dict[str, Any]) -> None:
        """Cache a Gamma market under each of its tokens (an event read already has them)."""
        now = time.monotonic()
        for token in _json_list(market.get("clobTokenIds")):
            self._markets[str(token)] = (now, market)

    def gamma_market(self, token: str, fresh: bool = False) -> dict[str, Any]:
        cached = self._markets.get(token)
        if cached and not fresh and time.monotonic() - cached[0] < self.market_ttl:
            return cached[1]
        # Gamma's /markets leaves closed markets out unless asked, and settlement needs them.
        for closed in ("false", "true"):
            rows = self._request("GET", self.gamma + "/markets", params={"clob_token_ids": token, "closed": closed})
            if rows:
                self.remember(rows[0])
                return rows[0]
        raise LookupError(f"no Polymarket market has token {token}")

    def market_by_slug(self, slug: str) -> dict[str, Any]:
        """A Gamma market by its slug, open or closed."""
        for closed in ("false", "true"):
            rows = self._request("GET", self.gamma + "/markets", params={"slug": slug, "closed": closed})
            if rows:
                self.remember(rows[0])
                return rows[0]
        raise LookupError(f"no Polymarket market has slug {slug}")

    def market(self, ticker_: str, fresh: bool = False) -> dict[str, Any]:
        token = token_of(ticker_)
        return normalize_market(self.gamma_market(token, fresh), token)

    # -- CLOB ----------------------------------------------------------------

    def books(self, tokens: list[str]) -> dict[str, dict[str, Any]]:
        """Raw CLOB books by token, 50 per request."""
        out: dict[str, dict[str, Any]] = {}
        for i in range(0, len(tokens), 50):
            rows = self._request("POST", self.clob + "/books", json=[{"token_id": t} for t in tokens[i:i + 50]])
            for row in rows or []:
                out[str(row["asset_id"])] = row
        return out

    def book(self, ticker_: str) -> dict[str, Any]:
        token = token_of(ticker_)
        raw = self._request("GET", self.clob + "/book", params={"token_id": token})
        return kalshi_layout(raw)


class PolymarketPaper(Paper):
    """IOC/FOK paper fills against a token's CLOB book, at Polymarket's taker fee.

    The shared paper policy otherwise: fills walk the visible book, consumed depth
    stays consumed until the level is gone for five minutes, and cash is checked
    before anything is recorded. Orders are Kalshi-shaped (bid buys the token, ask
    sells a held token); the book is read through `Polymarket.book`.
    """

    venue = "polymarket"
    policy = "polymarket-ioc-depth-v1"

    def __init__(self, client: Polymarket, latency: float = 0.5):
        super().__init__(client, latency)

    def fee_schedule(self, market: dict[str, Any]) -> dict[str, Any]:
        return market["fee_schedule"]

    def fee(self, fills: list[tuple[Decimal, Decimal]], schedule: dict[str, Any]) -> Decimal:
        return fee(fills, schedule)

    def create_order(self, order: dict[str, Any], broker: Broker) -> dict[str, Any]:
        token_of(order["ticker"])  # refuse anything that isn't a Polymarket token
        return super().create_order(order, broker)
