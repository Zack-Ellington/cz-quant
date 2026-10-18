"""Live order books for the paper trader.

Two feeds fill the same OrderBookStore:

  KalshiWSFeed   Kalshi's `orderbook_delta` WebSocket channel: one snapshot per
                 market, then signed deltas. Requires an API key (the socket
                 rejects anonymous connections with HTTP 401). Sequence gaps
                 trigger a resubscribe; disconnects reconnect with backoff.
  RestPollFeed   Polls GET /markets/orderbooks in batches of 100. Slower (a full
                 sweep every few seconds) but needs no key, so the environment can
                 run and be tested before a production key is set up.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict

from kalshi_client import load_credentials, signed_headers
from pricing import Book, parse_book

log = logging.getLogger("feeds")

WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"
WS_PATH = "/trade-api/ws/v2"
SUBSCRIBE_CHUNK = 200  # tickers per subscribe command


def _price(p, dollars: bool) -> float:
    return round(float(p) if dollars else float(p) / 100, 6)


def _levels(msg: dict, side: str):
    """[(price_dollars, qty)] from a snapshot, whichever encoding Kalshi sent."""
    for key, dollars in ((f"{side}_dollars_fp", True), (f"{side}_dollars", True), (side, False)):
        if key in msg:
            return [(_price(p, dollars), float(q)) for p, q in msg[key] or []]
    return []


class LocalBook:
    """One market's bids, keyed by price, from Kalshi's YES and NO perspectives."""

    __slots__ = ("yes", "no", "updated")

    def __init__(self):
        self.yes: dict[float, float] = {}
        self.no: dict[float, float] = {}
        self.updated = 0.0

    def snapshot(self, yes, no):
        self.yes = {p: q for p, q in yes if q > 0}
        self.no = {p: q for p, q in no if q > 0}
        self.updated = time.time()

    def apply_delta(self, side: str, price: float, delta: float):
        levels = self.yes if side == "yes" else self.no
        q = levels.get(price, 0.0) + delta
        if q > 1e-9:
            levels[price] = q
        else:
            levels.pop(price, None)
        self.updated = time.time()

    def to_book(self) -> Book:
        return parse_book({"yes_dollars": list(self.yes.items()), "no_dollars": list(self.no.items())})


class OrderBookStore:
    """Books by ticker, plus paper fills that the real book doesn't know about.

    A paper fill takes no real liquidity, so without this overlay the engine would
    "fill" the same resting orders over and over. Consumed quantity is subtracted
    when a book is read and dropped once the real level disappears.
    """

    def __init__(self, on_update=None):
        self.books: dict[str, LocalBook] = defaultdict(LocalBook)
        self.consumed: dict[str, dict[tuple[str, float], float]] = defaultdict(dict)
        self.on_update = on_update  # callback(ticker)
        self.messages = 0

    def _changed(self, ticker):
        self.messages += 1
        b = self.books[ticker]
        overlay = self.consumed.get(ticker)
        if overlay:
            for (side, price) in list(overlay):
                if price not in (b.yes if side == "yes" else b.no):
                    del overlay[(side, price)]
        if self.on_update:
            self.on_update(ticker)

    def snapshot(self, ticker, yes, no):
        self.books[ticker].snapshot(yes, no)
        self._changed(ticker)

    def delta(self, ticker, side, price, delta):
        self.books[ticker].apply_delta(side, price, delta)
        self._changed(ticker)

    def book(self, ticker) -> Book | None:
        b = self.books.get(ticker)
        if b is None or not b.updated:
            return None
        overlay = self.consumed.get(ticker) or {}
        yes = {p: q - overlay.get(("yes", p), 0.0) for p, q in b.yes.items()}
        no = {p: q - overlay.get(("no", p), 0.0) for p, q in b.no.items()}
        return parse_book({"yes_dollars": [(p, q) for p, q in yes.items() if q > 1e-9],
                           "no_dollars": [(p, q) for p, q in no.items() if q > 1e-9]})

    def consume(self, ticker, side_book: str, price: float, qty: float):
        """Record a paper fill. side_book is the YES-view side taken: 'ask' or 'bid'.
        A YES ask at p is a NO bid at 1 - p; a YES bid at p is a YES bid."""
        key = ("no", round(1 - price, 6)) if side_book == "ask" else ("yes", round(price, 6))
        self.consumed[ticker][key] = self.consumed[ticker].get(key, 0.0) + qty

    def age(self, ticker) -> float:
        b = self.books.get(ticker)
        return time.time() - b.updated if b and b.updated else float("inf")


class KalshiWSFeed:
    mode = "websocket"

    def __init__(self, store: OrderBookStore, url: str = WS_URL, credentials=None):
        self.store = store
        self.url = url
        self.key_id, self.private_key = credentials or load_credentials()
        if not self.private_key:
            raise RuntimeError("The Kalshi WebSocket needs KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH.")
        self.wanted: set[str] = set()
        self.subscribed: set[str] = set()
        self._ws = None
        self._next_id = 1
        self._cmd_tickers: dict[int, list[str]] = {}
        self._sid_tickers: dict[int, list[str]] = {}
        self._seq: dict[int, int] = {}
        self.connected = False
        self.reconnects = 0
        self.gaps = 0
        self.errors = 0

    def set_tickers(self, tickers):
        self.wanted = set(tickers)

    async def _subscribe(self, tickers):
        for i in range(0, len(tickers), SUBSCRIBE_CHUNK):
            chunk = tickers[i : i + SUBSCRIBE_CHUNK]
            cid = self._next_id
            self._next_id += 1
            self._cmd_tickers[cid] = chunk
            await self._ws.send(json.dumps({"id": cid, "cmd": "subscribe",
                                            "params": {"channels": ["orderbook_delta"], "market_tickers": chunk}}))
            self.subscribed.update(chunk)

    async def _resubscribe(self, sid):
        tickers = self._sid_tickers.pop(sid, [])
        self._seq.pop(sid, None)
        self.gaps += 1
        cid = self._next_id
        self._next_id += 1
        await self._ws.send(json.dumps({"id": cid, "cmd": "unsubscribe", "params": {"sids": [sid]}}))
        self.subscribed.difference_update(tickers)
        await self._subscribe(tickers)

    def handle(self, raw: str):
        """Apply one message. Returns a sid needing resubscription, or None."""
        m = json.loads(raw)
        typ, sid, msg = m.get("type"), m.get("sid"), m.get("msg") or {}
        if typ == "subscribed":
            cid = m.get("id")
            if cid in self._cmd_tickers:
                self._sid_tickers[msg.get("sid", sid)] = self._cmd_tickers.pop(cid)
            return None
        if typ == "error":
            self.errors += 1
            log.warning("ws error: %s", msg)
            return None
        if typ not in ("orderbook_snapshot", "orderbook_delta"):
            return None
        seq = m.get("seq")
        if sid is not None and seq is not None:
            last = self._seq.get(sid)
            if typ == "orderbook_delta" and last is not None and seq != last + 1:
                return sid
            self._seq[sid] = seq
        ticker = msg.get("market_ticker")
        if typ == "orderbook_snapshot":
            self.store.snapshot(ticker, _levels(msg, "yes"), _levels(msg, "no"))
        else:
            dollars = "price_dollars" in msg
            price = _price(msg["price_dollars"] if dollars else msg["price"], dollars)
            delta = float(msg["delta_fp"] if "delta_fp" in msg else msg["delta"])
            self.store.delta(ticker, msg["side"], price, delta)
        return None

    async def run(self):
        import websockets

        backoff = 1.0
        while True:
            try:
                headers = signed_headers(self.key_id, self.private_key, "GET", WS_PATH)
                async with websockets.connect(self.url, additional_headers=headers, max_size=2**24,
                                              ping_interval=10, open_timeout=15) as ws:
                    self._ws, self.connected, backoff = ws, True, 1.0
                    self.subscribed.clear()
                    self._seq.clear()
                    self._sid_tickers.clear()
                    pending = asyncio.create_task(self._sync_loop())
                    try:
                        async for raw in ws:
                            gap = self.handle(raw)
                            if gap is not None:
                                await self._resubscribe(gap)
                    finally:
                        pending.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # network drop, auth refusal, server close
                log.warning("websocket disconnected: %s", e)
            self.connected = False
            self.reconnects += 1
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    async def _sync_loop(self):
        """Subscribe tickers the universe added (removals are simply ignored downstream)."""
        while True:
            new = sorted(self.wanted - self.subscribed)
            if new:
                await self._subscribe(new)
            await asyncio.sleep(2)

    def status(self):
        return {"mode": self.mode, "connected": self.connected, "subscribed": len(self.subscribed),
                "reconnects": self.reconnects, "seq_gaps": self.gaps, "errors": self.errors}


class RestPollFeed:
    mode = "rest_poll"

    def __init__(self, store: OrderBookStore, client):
        self.store = store
        self.client = client
        self.wanted: list[str] = []
        self.sweeps = 0
        self.last_sweep_s = None
        self.errors = 0

    def set_tickers(self, tickers):
        self.wanted = sorted(tickers)

    async def run(self):
        while True:
            started = time.monotonic()
            tickers = list(self.wanted)
            for i in range(0, len(tickers), 100):
                try:
                    books = await asyncio.to_thread(self.client.orderbooks, tickers[i : i + 100])
                except Exception as e:
                    self.errors += 1
                    log.warning("orderbook poll failed: %s", e)
                    continue
                for t, raw in books.items():
                    b = parse_book(raw)
                    yes = [(p, q) for p, q in b.yes_bids]
                    no = [(round(1 - p, 6), q) for p, q in b.yes_asks]
                    self.store.snapshot(t, yes, no)
            self.sweeps += 1
            self.last_sweep_s = round(time.monotonic() - started, 2)
            await asyncio.sleep(0.5 if tickers else 2)

    def status(self):
        return {"mode": self.mode, "connected": True, "subscribed": len(self.wanted),
                "sweeps": self.sweeps, "sweep_seconds": self.last_sweep_s, "errors": self.errors}
