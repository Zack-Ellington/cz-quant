"""One authenticated Polymarket US full-book subscription for execution."""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime
from typing import Any

from websockets.sync.client import connect
from czquant.polymarket_us import PolymarketUS, kalshi_layout

log = logging.getLogger(__name__)


class QuoteUnavailable(ValueError):
    pass


class StreamBook:
    def __init__(self, client: PolymarketUS, slug: str, max_age: float,
                 changed: threading.Event | None = None):
        self.client, self.slug, self.max_age = client, slug, max_age
        self.changed = changed or threading.Event()
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None
        self.socket: Any = None
        self.data: dict[str, Any] | None = None
        self.exchange_time = 0.0
        self.received = 0.0

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, name="polymarket-book", daemon=True)
        self.thread.start()

    def clear(self) -> None:
        with self.lock:
            self.data = None
            self.exchange_time = self.received = 0.0
        self.changed.set()

    def accept(self, message: dict[str, Any]) -> None:
        if message.get("error"):
            self.clear()
            raise QuoteUnavailable("polymarket_websocket_subscription_lost")
        data = message.get("marketData")
        if not data or data.get("marketSlug") != self.slug:
            return
        stamp = datetime.fromisoformat(data["transactTime"].replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError("book timestamp must include timezone")
        # These are full snapshots, not deltas. An absent side is not a snapshot.
        if not isinstance(data.get("bids"), list) or not isinstance(data.get("offers"), list):
            raise ValueError("incomplete book snapshot")
        book = kalshi_layout(data)
        with self.lock:
            if self.data is not None and stamp.timestamp() <= self.exchange_time:
                if stamp.timestamp() == self.exchange_time and self.data != {"book": book, "state": data.get("state")}:
                    self.clear()
                    raise QuoteUnavailable("polymarket_websocket_conflicting_snapshot")
                return
            self.data = {"book": book, "state": data.get("state")}
            self.exchange_time, self.received = stamp.timestamp(), time.monotonic()
        self.changed.set()

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            if self.data is None:
                raise QuoteUnavailable("polymarket_websocket_unavailable")
            exchange_age, receive_age = time.time() - self.exchange_time, time.monotonic() - self.received
            if exchange_age < -1 or exchange_age > self.max_age or receive_age > self.max_age:
                raise QuoteUnavailable("polymarket_websocket_stale")
            if self.data["state"] != "MARKET_STATE_OPEN":
                raise QuoteUnavailable("polymarket_websocket_not_open")
            return {**self.data["book"], "execution_quote": {
                "source": "polymarket_us_websocket", "exchange_time": self.exchange_time,
                "exchange_age_seconds": exchange_age, "receive_age_seconds": receive_age}}

    def _run(self) -> None:
        path = "/v1/ws/markets"
        url = self.client.api.replace("https://", "wss://", 1).replace("http://", "ws://", 1) + path
        while not self.stop.is_set():
            self.clear()
            try:
                with connect(url, additional_headers=self.client.headers("GET", path),
                             open_timeout=10, close_timeout=1, ping_interval=10, ping_timeout=10) as socket:
                    self.socket = socket
                    socket.send(json.dumps({"subscribe": {"requestId": "execution-book",
                        "subscriptionType": "SUBSCRIPTION_TYPE_MARKET_DATA",
                        "marketSlugs": [self.slug], "responsesDebounced": False}}))
                    while not self.stop.is_set():
                        try:
                            self.accept(json.loads(socket.recv(timeout=1)))
                        except TimeoutError:
                            continue
            except Exception as exc:
                if not self.stop.is_set():
                    log.warning("Polymarket book connection failed (%s); reconnecting", type(exc).__name__)
            finally:
                self.socket = None
                self.clear()
            self.stop.wait(1)

    def close(self) -> None:
        self.stop.set()
        socket = self.socket
        if socket is not None:
            socket.close()
        if self.thread is not None:
            self.thread.join(timeout=2)
        self.clear()


def rest_snapshot(client: PolymarketUS, slug: str, max_age: float) -> dict[str, Any]:
    """REST is an explicit mode. Never turn an old snapshot into a current book."""
    feed = StreamBook(client, slug, max_age)
    started = time.monotonic()
    try:
        feed.accept(client.book_raw(slug))
        if time.monotonic() - started > max_age:
            raise QuoteUnavailable("polymarket_rest_request_stale")
        result = feed.snapshot()
    except (KeyError, TypeError, ValueError) as exc:
        raise QuoteUnavailable("polymarket_rest_freshness_unavailable") from exc
    result["execution_quote"]["source"] = "polymarket_us_rest"
    return result
