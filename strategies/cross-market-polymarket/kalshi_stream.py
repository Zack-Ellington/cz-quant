"""Sequenced Kalshi books; quiet books remain valid on a healthy connection."""
from __future__ import annotations

import json
import logging
import threading
import time
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

from websockets.sync.client import connect
from czquant.kalshi import Kalshi
from czquant.polymarket_us import QuoteUnavailable

log = logging.getLogger(__name__)


class KalshiStreamBook:
    def __init__(self, client: Kalshi, tickers: list[str], max_age: float):
        self.client, self.tickers, self.max_age = client, set(tickers), max_age
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None
        self.socket: Any = None
        self.clear()

    def clear(self) -> None:
        with self.lock:
            self.books: dict[str, dict[str, dict[Decimal, Decimal]]] = {}
            self.received: dict[str, float] = {}
            self.exchange: dict[str, float | None] = {}
            self.sid: int | None = None
            self.seq: int | None = None
            self.healthy_at = 0.0

    def confirmed_pong(self) -> None:
        with self.lock:
            self.healthy_at = time.monotonic()

    def accept(self, message: dict[str, Any]) -> None:
        with self.lock:
            try:
                self._accept(message)
            except Exception:
                self.clear()
                raise

    def _accept(self, message: dict[str, Any]) -> None:
        kind = message.get("type")
        if kind in ("error", "unsubscribed"):
            raise QuoteUnavailable("kalshi_websocket_subscription_lost")
        if kind not in ("orderbook_snapshot", "orderbook_delta"):
            return
        sid, seq, msg = int(message["sid"]), int(message["seq"]), message["msg"]
        if self.seq is not None and (sid != self.sid or seq != self.seq + 1):
            raise QuoteUnavailable("kalshi_websocket_sequence_gap")
        ticker = msg["market_ticker"]
        if ticker not in self.tickers:
            raise ValueError("unexpected Kalshi market")
        # Snapshot timestamps describe transmission; delta timestamps describe changes.
        stamp = message.get("sending_ts_ms", msg.get("ts_ms"))
        exchange_time = float(stamp) / 1000 if stamp is not None else None
        if exchange_time is not None and not -1 <= time.time() - exchange_time <= self.max_age:
            raise QuoteUnavailable("kalshi_websocket_delayed_message")
        if kind == "orderbook_snapshot":
            book = {}
            for side in ("yes", "no"):
                rows = msg[side + "_dollars_fp"]
                book[side] = {Decimal(p): Decimal(q) for p, q in rows}
                if any(not p.is_finite() or not q.is_finite() or not 0 <= p <= 1 or q < 0
                       for p, q in book[side].items()):
                    raise ValueError("invalid snapshot level")
            self.books[ticker] = book
        else:
            if ticker not in self.books:
                raise QuoteUnavailable("kalshi_websocket_missing_snapshot")
            side, price, delta = msg["side"], Decimal(msg["price_dollars"]), Decimal(msg["delta_fp"])
            if side not in ("yes", "no") or not price.is_finite() or not delta.is_finite() or not 0 <= price <= 1:
                raise ValueError("invalid delta")
            levels = self.books[ticker][side]
            quantity = levels.get(price, Decimal(0)) + delta
            if quantity < 0:
                raise ValueError("negative book quantity")
            if quantity:
                levels[price] = quantity
            else:
                levels.pop(price, None)
        self.sid, self.seq = sid, seq
        self.received[ticker], self.exchange[ticker] = time.monotonic(), exchange_time

    def snapshot(self, ticker: str) -> dict[str, Any]:
        with self.lock:
            if set(self.books) != self.tickers:
                raise QuoteUnavailable("kalshi_websocket_unavailable")
            health_age = time.monotonic() - self.healthy_at
            if not self.healthy_at or health_age > self.max_age:
                raise QuoteUnavailable("kalshi_websocket_connection_stale")
            book = self.books[ticker]
            return {"orderbook_fp": {side + "_dollars": [[str(p), str(q)] for p, q in
                    sorted(book[side].items(), reverse=True)] for side in ("yes", "no")},
                    "execution_quote": {"source": "kalshi_websocket", "sid": self.sid, "seq": self.seq,
                        "exchange_time": self.exchange[ticker], "connection_age_seconds": health_age,
                        "receive_age_seconds": time.monotonic() - self.received[ticker]}}

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, name="kalshi-book", daemon=True)
        self.thread.start()

    def _run(self) -> None:
        path = "/trade-api/ws/v2"
        base = urlsplit(self.client.base)
        host = "external-api-ws.kalshi.com" if base.hostname == "external-api.kalshi.com" else base.netloc
        url = ("wss://" if base.scheme == "https" else "ws://") + host + path
        while not self.stop.is_set():
            self.clear()
            try:
                with connect(url, additional_headers=self.client.headers("GET", path),
                             open_timeout=10, close_timeout=1, ping_interval=None) as socket:
                    self.socket = socket
                    socket.send(json.dumps({"id": 1, "cmd": "subscribe", "params": {
                        "channels": ["orderbook_delta"], "market_tickers": sorted(self.tickers)}}))
                    pong, sent = socket.ping(), time.monotonic()
                    while not self.stop.is_set():
                        now = time.monotonic()
                        if pong is not None and now - sent > self.max_age / 2:
                            raise QuoteUnavailable("kalshi_websocket_pong_timeout")
                        if pong is not None and pong.is_set():
                            self.confirmed_pong()
                            pong = None
                        if pong is None and now - sent >= self.max_age / 2:
                            pong, sent = socket.ping(), now
                        try:
                            self.accept(json.loads(socket.recv(timeout=min(.1, self.max_age / 4))))
                        except TimeoutError:
                            continue
            except Exception as exc:
                if not self.stop.is_set():
                    log.warning("Kalshi book connection failed (%s: %s); rebuilding snapshots", type(exc).__name__, exc)
            finally:
                self.socket = None
                self.clear()
            self.stop.wait(1)

    def close(self) -> None:
        self.stop.set()
        if self.socket is not None:
            self.socket.close()
        if self.thread is not None:
            self.thread.join(timeout=2)
        self.clear()
