"""Read-only Kalshi market-data client.

Kalshi's series, event, and order-book endpoints are public, so this client sends
no authentication headers.

Every response is normalized to a small, stable shape before anything else sees
it. That keeps the rest of the strategy independent of Kalshi's wire format, and
it is what makes a run replayable: ``snapshot.RecordingClient`` saves exactly
these normalized payloads and ``snapshot.SnapshotClient`` serves them back.

* ``fetch_event``  -> ``{"event_ticker", "title", "mutually_exclusive",
  "markets": [{"ticker", "yes_sub_title", "strike_type", "floor_strike",
  "cap_strike", "last_price", "volume"}]}``
* ``fetch_orderbook`` -> ``{"yes": [[price, size], ...], "no": [...]}``, prices
  as 0-1 probabilities, ascending, best price last, at most ``BOOK_DEPTH``
  levels per side.
* ``fetch_series`` -> ``{"fee_type", "fee_multiplier"}``

Prices come from the order book. Kalshi's legacy integer-cent summary fields
(``yes_bid``, ``yes_ask``, ``last_price``) are null on these markets; the book is
the authoritative source for the best bid and ask and also carries depth.
"""

from __future__ import annotations

import time
from typing import Protocol

import httpx

from strategy.constants import DEFAULT_BASE_URL

# A normalized order book: bids to buy YES and bids to buy NO, each a list of
# [price, size] with price in [0, 1], sorted so the best (highest) price is last.
Book = dict[str, list[list[float]]]

# Levels kept per side. Every estimator reads the top of book; the extra levels
# are kept for inspection and make a saved snapshot self-describing.
BOOK_DEPTH = 5


class Client(Protocol):
    """The data-source interface the strategy depends on."""

    def fetch_event(self, ticker: str) -> dict | None:
        """Normalized event with its markets, or ``None`` if it does not exist."""
        ...

    def fetch_orderbook(self, ticker: str) -> Book | None:
        """Normalized order book, or ``None`` if the market does not exist."""
        ...

    def fetch_series(self, ticker: str) -> dict | None:
        """Normalized fee settings for a series, or ``None`` if absent."""
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
        payload = self._get(f"/events/{ticker}", {"with_nested_markets": "true"})
        return normalize_event(payload) if payload is not None else None

    def fetch_orderbook(self, ticker: str) -> Book | None:
        payload = self._get(f"/markets/{ticker}/orderbook", None)
        return normalize_book(payload) if payload is not None else None

    def fetch_series(self, ticker: str) -> dict | None:
        payload = self._get(f"/series/{ticker}", None)
        return normalize_series(payload) if payload is not None else None

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


def normalize_event(payload: dict) -> dict:
    """Reduce an event response to the fields the strategy uses."""
    event = payload.get("event") or {}
    # With nested markets requested, Kalshi may put them at the top level, inside
    # the event, or both (with one of the two an empty list).
    markets = payload.get("markets") or event.get("markets") or []
    return {
        "event_ticker": event.get("event_ticker"),
        "title": event.get("title"),
        "mutually_exclusive": bool(event.get("mutually_exclusive")),
        "markets": [
            {
                "ticker": m.get("ticker"),
                "yes_sub_title": m.get("yes_sub_title"),
                "strike_type": m.get("strike_type"),
                "floor_strike": m.get("floor_strike"),
                "cap_strike": m.get("cap_strike"),
                "last_price": _to_float(m.get("last_price_dollars")),
                "volume": _to_float(m.get("volume_fp")) or 0.0,
            }
            for m in markets
        ],
    }


def normalize_book(payload: dict, depth: int = BOOK_DEPTH) -> Book:
    """Normalize an order-book response to ``{"yes": [...], "no": [...]}``.

    Kalshi returns ``orderbook_fp`` with ``yes_dollars`` / ``no_dollars`` (prices
    in dollars, i.e. 0-1) and, on some markets, a legacy ``orderbook`` with
    ``yes`` / ``no`` in integer cents. Both are handled. Each side is sorted
    ascending and trimmed to the best ``depth`` levels.
    """
    raw = payload.get("orderbook_fp")
    if raw is not None:
        yes, no, scale = raw.get("yes_dollars"), raw.get("no_dollars"), 1.0
    else:
        raw = payload.get("orderbook") or {}
        yes, no, scale = raw.get("yes"), raw.get("no"), 100.0
    return {"yes": _levels(yes, scale, depth), "no": _levels(no, scale, depth)}


def normalize_series(payload: dict) -> dict:
    series = payload.get("series") or {}
    return {
        "fee_type": series.get("fee_type") or "quadratic",
        "fee_multiplier": float(series.get("fee_multiplier") or 1.0),
    }


def _levels(levels: list | None, scale: float, depth: int) -> list[list[float]]:
    if not levels:
        return []
    parsed = sorted([float(p) / scale, float(s)] for p, s in levels)
    return parsed[-depth:]


def _to_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    return float(value)
