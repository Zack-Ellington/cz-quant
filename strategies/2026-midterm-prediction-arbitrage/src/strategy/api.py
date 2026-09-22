"""Read-only Kalshi market-data client.

Kalshi's event, market, and order-book endpoints are public, so this client
sends no authentication headers.

Prices come from the **order book**, not the market summary. Kalshi leaves the
``yes_bid`` / ``yes_ask`` / ``last_price`` summary fields empty on these 2026
markets even while a real book of resting orders exists, so the only reliable
source of a quote is ``GET /markets/{ticker}/orderbook``. ``fetch_orderbook``
returns that book normalized to ``{"yes": [[price, size], ...], "no": [...]}``
with prices as 0-1 probabilities.

Tests do not touch the network: they use ``FixtureClient`` instead, which serves
saved books from ``tests/fixtures/``. Both clients satisfy the same interface --
``fetch_event`` and ``fetch_orderbook`` -- so nothing downstream knows which one
it is holding.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Protocol

import httpx

from strategy.constants import DEFAULT_BASE_URL

# A normalized order book: bids to buy YES and bids to buy NO, each a list of
# [price, size] with price in [0, 1], sorted so the best (highest) price is last.
Book = dict[str, list[list[float]]]


class Client(Protocol):
    """The two methods the rest of the strategy needs from a data source."""

    def fetch_event(self, ticker: str) -> dict | None:
        """Return the event dict (with ``markets``), or ``None`` if absent."""
        ...

    def fetch_orderbook(self, ticker: str) -> Book | None:
        """Return the market's normalized order book, or ``None`` if absent."""
        ...


class KalshiClient:
    """Live client against Kalshi's public market-data API."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        retries: int = 3,
        backoff: float = 0.5,
        timeout: float = 20.0,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url, timeout=timeout, follow_redirects=True
        )
        self._retries = retries
        self._backoff = backoff

    def __enter__(self) -> "KalshiClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def fetch_event(self, ticker: str) -> dict | None:
        """Fetch one event and its markets, or ``None`` for a 404."""
        payload = self._get(f"/events/{ticker}", {"with_nested_markets": "true"})
        return _normalize_event(payload) if payload is not None else None

    def fetch_orderbook(self, ticker: str) -> Book | None:
        """Fetch a market's order book, normalized to 0-1 prices.

        Returns ``None`` for a 404 (no such market) and an empty book
        (``{"yes": [], "no": []}``) for a listed market with no resting orders.
        """
        payload = self._get(f"/markets/{ticker}/orderbook", None)
        return _normalize_book(payload) if payload is not None else None

    def _get(self, path: str, params: dict | None) -> dict | None:
        """GET with retries. Returns parsed JSON, or ``None`` on a 404."""
        last_exc: Exception | None = None
        for attempt in range(self._retries):
            try:
                resp = self._client.get(path, params=params)
            except httpx.HTTPError as exc:  # transport-level failure
                last_exc = exc
            else:
                if resp.status_code == 404:
                    return None
                if resp.status_code < 500:
                    resp.raise_for_status()
                    return resp.json()
                last_exc = httpx.HTTPStatusError(
                    f"{resp.status_code} for {path}",
                    request=resp.request,
                    response=resp,
                )
            time.sleep(self._backoff * (2**attempt))
        assert last_exc is not None
        raise last_exc


def _normalize_event(payload: dict) -> dict:
    """Flatten Kalshi's ``{"event": {...}, "markets": [...]}`` into one dict."""
    event = dict(payload.get("event") or {})
    markets = payload.get("markets")
    if markets is None:
        markets = event.get("markets", [])
    event["markets"] = markets
    return event


def _normalize_book(payload: dict) -> Book:
    """Normalize an order-book response to ``{"yes": [...], "no": [...]}``.

    Kalshi returns ``orderbook_fp`` with ``yes_dollars`` / ``no_dollars`` (prices
    already in dollars, i.e. 0-1) and, on some markets, a legacy ``orderbook``
    with ``yes`` / ``no`` in integer cents. Both are handled; each side is a list
    of ``[price, size]`` sorted ascending, so the best price is the last entry.
    """
    raw = payload.get("orderbook_fp")
    if raw is not None:
        return {
            "yes": _levels(raw.get("yes_dollars"), in_dollars=True),
            "no": _levels(raw.get("no_dollars"), in_dollars=True),
        }
    raw = payload.get("orderbook") or {}
    return {
        "yes": _levels(raw.get("yes"), in_dollars=False),
        "no": _levels(raw.get("no"), in_dollars=False),
    }


def _levels(levels: list | None, *, in_dollars: bool) -> list[list[float]]:
    if not levels:
        return []
    scale = 1.0 if in_dollars else 100.0
    return [[float(price) / scale, float(size)] for price, size in levels]


class FixtureClient:
    """Serves saved events and order books from a directory of JSON snapshots.

    Each snapshot file is one JSON object. Book snapshots map a market ticker to
    a normalized book; event snapshots map an event ticker to an event dict. The
    two are told apart by shape (a book has ``yes`` / ``no`` keys), so one client
    can span the Senate, House, and combo snapshots.
    """

    def __init__(self, snapshot_dir: str | Path) -> None:
        self._books: dict[str, Book] = {}
        self._events: dict[str, dict] = {}
        for path in sorted(Path(snapshot_dir).glob("*.json")):
            with path.open(encoding="utf-8") as handle:
                data = json.load(handle)
            for ticker, payload in data.items():
                if isinstance(payload, dict) and "yes" in payload and "no" in payload:
                    self._books[ticker] = payload
                else:
                    self._events[ticker] = payload

    def fetch_event(self, ticker: str) -> dict | None:
        return self._events.get(ticker)

    def fetch_orderbook(self, ticker: str) -> Book | None:
        return self._books.get(ticker)
