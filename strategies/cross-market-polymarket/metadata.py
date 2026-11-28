"""Strategy-owned reference refresh, outside the quote-to-order path."""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from stream_book import QuoteUnavailable


class Metadata:
    # Market state and fee schedules are references, not execution quotes.
    refresh_seconds = 5.0
    max_age = 15.0

    def __init__(self, fetch: Callable[[], Any], changed: threading.Event):
        self.fetch, self.changed = fetch, changed
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.data: Any = None
        self.started_at = 0.0
        self.thread = threading.Thread(target=self._run, name="pair-metadata", daemon=True)

    def refresh(self) -> None:
        started = time.monotonic()
        try:
            data = self.fetch()
        except Exception:
            with self.lock:
                self.data = None
            self.changed.set()
            raise
        with self.lock:
            self.data, self.started_at = data, started
        self.changed.set()

    def snapshot(self) -> Any:
        with self.lock:
            if self.data is None or time.monotonic() - self.started_at > self.max_age:
                raise QuoteUnavailable("market_metadata_unavailable")
            return self.data

    def _run(self) -> None:
        while not self.stop.is_set():
            try:
                self.refresh()
            except Exception as exc:
                logging.getLogger(__name__).warning("Metadata unavailable (%s); retrying", type(exc).__name__)
            self.stop.wait(self.refresh_seconds)

    def close(self) -> None:
        self.stop.set()
        self.thread.join(timeout=2)
