"""Strategy-owned reference refresh, outside the quote-to-order path."""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

import httpx

from stream_book import QuoteUnavailable

log = logging.getLogger(__name__)


class Metadata:
    # Market state and fee schedules are references, not execution quotes.
    refresh_seconds = 5.0
    max_age = 15.0

    def __init__(self, fetch: Callable[[], Any], changed: threading.Event):
        self.fetch, self.changed = fetch, changed
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.data: Any = None
        self.error: Exception | None = None
        self.started_at = 0.0
        self.thread = threading.Thread(target=self._run, name="pair-metadata", daemon=True)

    def refresh(self) -> None:
        started = time.monotonic()
        try:
            data = self.fetch()
        except Exception as exc:
            with self.lock:
                self.data, self.error = None, exc
            self.changed.set()
            raise
        with self.lock:
            self.data, self.started_at, self.error = data, started, None
        self.changed.set()

    def snapshot(self) -> Any:
        with self.lock:
            if self.error is not None:
                raise self.error
            if self.data is None or time.monotonic() - self.started_at > self.max_age:
                raise QuoteUnavailable("market_metadata_unavailable")
            return self.data

    def _run(self) -> None:
        while not self.stop.is_set():
            try:
                self.refresh()
            except (httpx.HTTPError, TimeoutError, ConnectionError) as exc:
                log.warning("Metadata request failed (%s: %s); retrying", type(exc).__name__, exc)
            except Exception:
                log.exception("Metadata worker failed")
                return  # snapshot() propagates the error to the execution loop.
            self.stop.wait(self.refresh_seconds)

    def close(self) -> None:
        self.stop.set()
        self.thread.join(timeout=2)
