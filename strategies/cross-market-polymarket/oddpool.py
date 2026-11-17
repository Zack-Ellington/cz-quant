"""Oddpool's search API: Kalshi and Polymarket series, events and markets in one index.

A series groups recurring or related events (Kalshi's KXFEDDECISION, Polymarket's
btc-up-or-down-15m). Searching series by topic returns both venues' series side by
side, so related markets can be paired series against series instead of scanning
every market on both venues. Docs: https://docs.oddpool.com/search/overview

    GET /search/series?q=&exchange=&category=&status=&limit=&offset=
    GET /search/markets?series_id=&status=&limit=&offset=     (bare JSON array)

Needs ODDPOOL_API_KEY (sent as X-API-Key). The search endpoints are on every plan.
"""

from __future__ import annotations

import os
import time
from typing import Any, Iterator

import httpx

BASE = "https://api.oddpool.com"
PAGE = 100


class Oddpool:
    def __init__(self, api_key: str, base: str = BASE, http: Any = None, min_interval: float = 0.2):
        self.base = base.rstrip("/")
        self.http = http or httpx.Client(timeout=20, headers={"X-API-Key": api_key})
        self.min_interval = min_interval
        self._last = 0.0

    @classmethod
    def from_env(cls) -> "Oddpool":
        key = os.environ.get("ODDPOOL_API_KEY")
        if not key:
            raise ValueError("Set ODDPOOL_API_KEY to use Oddpool's search (see .env.example)")
        return cls(key, os.environ.get("ODDPOOL_URL", BASE))

    def _get(self, path: str, params: dict[str, Any]) -> Any:
        for attempt in range(5):
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            response = self.http.get(self.base + path, params=params)
            if response.status_code == 429 or response.status_code >= 500:
                time.sleep(float(response.headers.get("Retry-After") or 2 ** attempt))
                continue
            response.raise_for_status()
            return response.json()
        raise RuntimeError(f"Oddpool {path} retry exhausted")

    def _pages(self, path: str, params: dict[str, Any], limit: int | None) -> Iterator[dict[str, Any]]:
        offset, seen = 0, 0
        while limit is None or seen < limit:
            page = self._get(path, {**params, "limit": PAGE, "offset": offset})
            if not page:
                return
            yield from page
            seen += len(page)
            if len(page) < PAGE:
                return
            offset += PAGE

    def series(self, q: str | None = None, exchange: str | None = None, category: str | None = None,
               status: str = "active", limit: int | None = None) -> list[dict[str, Any]]:
        params = {k: v for k, v in {"q": q, "exchange": exchange, "category": category, "status": status}.items() if v}
        return list(self._pages("/search/series", params, limit))

    def markets(self, series_id: str, status: str = "active", limit: int | None = None) -> list[dict[str, Any]]:
        return list(self._pages("/search/markets", {"series_id": series_id, "status": status}, limit))
