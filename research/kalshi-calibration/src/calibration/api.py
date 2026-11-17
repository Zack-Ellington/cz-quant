"""Public Kalshi market-data reads: rate-limited, retried, thread-safe."""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Iterator

import httpx

BASE = os.environ.get("KALSHI_API_BASE_URL", "https://external-api.kalshi.com/trade-api/v2").rstrip("/")


class RateLimiter:
    """Shared pacing across threads that adapts to the server.

    Starts at `rate` calls per second. A 429 cuts the rate by 15% (at most once
    per `pause` seconds, so a burst of 429s from parallel threads counts once;
    floor `min_rate`) and holds every thread for `pause` seconds. Every
    `recover` consecutive successes raise it by 5%, back up to `rate`.

    Unauthenticated public reads are limited to roughly 3 requests/second
    (measured Nov 2026: 4/s drew 22% 429s), hence the default of 3.
    """

    def __init__(self, rate: float, min_rate: float = 1.0, pause: float = 1.0, recover: int = 50):
        self.max_rate = rate
        self.rate = rate
        self.min_rate = min_rate
        self.pause = pause
        self.recover = recover
        self.lock = threading.Lock()
        self.next = time.monotonic()
        self.streak = 0
        self.throttles = 0
        self.last_cut = -1e9

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            delay = self.next - now
            self.next = max(now, self.next) + 1.0 / self.rate
        if delay > 0:
            time.sleep(delay)

    def ok(self) -> None:
        with self.lock:
            self.streak += 1
            if self.streak >= self.recover and self.rate < self.max_rate:
                self.rate = min(self.max_rate, self.rate * 1.05)
                self.streak = 0

    def throttled(self) -> None:
        with self.lock:
            now = time.monotonic()
            self.throttles += 1
            self.streak = 0
            if now >= self.last_cut + self.pause:
                self.rate = max(self.min_rate, self.rate * 0.85)
                self.last_cut = now
            self.next = max(self.next, now + self.pause)


class Kalshi:
    def __init__(self, rate: float = 3.0, base: str = BASE):
        self.base = base
        self.limiter = RateLimiter(rate)
        self.http = httpx.Client(timeout=60, limits=httpx.Limits(max_connections=32))

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        for attempt in range(12):
            self.limiter.wait()
            try:
                r = self.http.get(self.base + path, params=params)
            except httpx.TransportError:
                time.sleep(min(30, 2**attempt))
                continue
            if r.status_code == 429:
                self.limiter.throttled()
                continue
            if r.status_code in (500, 502, 503, 504):
                time.sleep(min(30, 0.5 * 2**attempt))
                continue
            r.raise_for_status()
            self.limiter.ok()
            return r.json()
        raise RuntimeError(f"GET {path} {params} failed after retries")

    def pages(self, path: str, key: str, **params: Any) -> Iterator[dict[str, Any]]:
        cursor = None
        while True:
            data = self.get(path, {**params, **({"cursor": cursor} if cursor else {})})
            yield from data.get(key) or []
            cursor = data.get("cursor")
            if not cursor:
                return
