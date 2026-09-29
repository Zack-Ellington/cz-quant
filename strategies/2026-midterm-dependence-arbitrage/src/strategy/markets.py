"""Quotes for the aggregate markets: combo, chamber control, same party, seats.

These are the markets the race-level model is tested against:

* the four combo legs (the target of the strategy),
* the House and Senate control markets (``CONTROLH`` / ``CONTROLS``), which the
  combo settles on, so they give exact model-free cross-checks,
* the same-party market, which must equal DD + RR,
* the Democratic seat-count markets, whose bucket prices describe the whole
  distribution of seats and so reveal how much races move together.

A ``Quote`` is the YES side of one binary market: best bid, best ask (one minus
the best NO bid), the contracts resting at each, last trade, and traded volume. Seat buckets are parsed from
their titles ("Below 210", "210-213", "51", "Above 52") and checked against the
strike fields, then validated to tile the chamber with no gaps or overlaps.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from strategy import constants
from strategy.api import Book, Client


@dataclass(frozen=True)
class Quote:
    """YES side of one binary market. Prices are 0-1 probabilities."""

    ticker: str
    bid: float | None
    ask: float | None
    last: float | None = None
    volume: float = 0.0
    bid_size: float = 0.0  # contracts bid at ``bid``
    ask_size: float = 0.0  # contracts offered at ``ask`` (NO bids at 1 - ask)

    @property
    def series(self) -> str:
        return self.ticker.split("-")[0]

    @property
    def mid(self) -> float | None:
        if self.bid is not None and self.ask is not None:
            return (self.bid + self.ask) / 2.0
        if self.bid is not None:
            return self.bid
        return self.ask

    @property
    def spread(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        return self.ask - self.bid


def quote_from_book(ticker: str, book: Book | None, summary: dict | None = None) -> Quote:
    """Build a ``Quote`` from a normalized book and the event's market summary."""
    summary = summary or {}
    yes = (book or {}).get("yes") or []
    no = (book or {}).get("no") or []
    best_yes = max(p for p, _ in yes) if yes else None
    best_no = max(p for p, _ in no) if no else None
    last = summary.get("last_price")
    return Quote(
        ticker=ticker,
        bid=best_yes,
        ask=1.0 - best_no if best_no is not None else None,
        last=last if last else None,
        volume=float(summary.get("volume") or 0.0),
        bid_size=sum(s for p, s in yes if p == best_yes),
        ask_size=sum(s for p, s in no if p == best_no),
    )


def load_quote(client: Client, ticker: str, event: dict | None) -> Quote:
    """Fetch one market's book and pair it with its summary from ``event``."""
    summary = None
    if event is not None:
        summary = next((m for m in event["markets"] if m["ticker"] == ticker), None)
    return quote_from_book(ticker, client.fetch_orderbook(ticker), summary)


# --- Seat-count buckets ------------------------------------------------------


@dataclass(frozen=True)
class Bucket:
    lo: int  # inclusive
    hi: int  # inclusive
    quote: Quote


@dataclass(frozen=True)
class SeatMarket:
    """A mutually-exclusive set of buckets covering 0..chamber size."""

    event: str
    buckets: tuple[Bucket, ...]

    def probabilities(self) -> list[float]:
        """Bucket probabilities: midpoints normalized to sum to one."""
        mids = [b.quote.mid or 0.0 for b in self.buckets]
        total = sum(mids)
        if total <= 0:
            raise ValueError(f"{self.event}: no priced buckets")
        return [m / total for m in mids]

    def overround(self) -> float:
        """Sum of bucket midpoints minus one (the book's overround)."""
        return sum(b.quote.mid or 0.0 for b in self.buckets) - 1.0

    def prob_at_least(self, seats: int) -> float:
        """Normalized P(seats >= ``seats``); ``seats`` must be a bucket edge."""
        probs = self.probabilities()
        if not any(b.lo == seats for b in self.buckets):
            raise ValueError(f"{self.event}: {seats} is not a bucket edge")
        return sum(p for b, p in zip(self.buckets, probs) if b.lo >= seats)


_BELOW = re.compile(r"^below\s+(\d+)$")
_ABOVE = re.compile(r"^above\s+(\d+)$")
_RANGE = re.compile(r"^(\d+)\s*-\s*(\d+)$")
_EXACT = re.compile(r"^(\d+)$")


def parse_bucket(
    title: str,
    strike_type: str | None,
    floor: float | None,
    cap: float | None,
    *,
    size: int,
) -> tuple[int, int]:
    """Return the inclusive seat range ``(lo, hi)`` a bucket market covers.

    The title is the primary source. When the market also carries inclusive
    strike fields (``custom`` / ``between``) they must agree with the title.
    """
    text = (title or "").strip().lower()
    if m := _BELOW.match(text):
        lo, hi = 0, int(m.group(1)) - 1
    elif m := _ABOVE.match(text):
        lo, hi = int(m.group(1)) + 1, size
    elif m := _RANGE.match(text):
        lo, hi = int(m.group(1)), int(m.group(2))
    elif m := _EXACT.match(text):
        lo = hi = int(m.group(1))
    elif strike_type == "less" and cap is not None:
        lo, hi = 0, int(cap) - 1
    elif strike_type == "greater" and floor is not None:
        lo, hi = int(floor) + 1, size
    elif floor is not None and cap is not None:
        lo, hi = int(floor), int(cap)
    else:
        raise ValueError(f"cannot parse seat bucket {title!r}")

    if strike_type in ("custom", "between") and floor is not None and cap is not None:
        strike_hi = min(int(cap), size)
        if (int(floor), strike_hi) != (lo, min(hi, size)):
            raise ValueError(
                f"bucket {title!r}: title gives {lo}-{hi}, strikes give {floor}-{cap}"
            )
    return lo, min(hi, size)


def build_seat_market(event_ticker: str, event: dict, quotes: dict[str, Quote], *, size: int) -> SeatMarket:
    buckets = []
    for m in event["markets"]:
        lo, hi = parse_bucket(
            m["yes_sub_title"], m["strike_type"], m["floor_strike"], m["cap_strike"], size=size
        )
        buckets.append(Bucket(lo, hi, quotes[m["ticker"]]))
    buckets.sort(key=lambda b: b.lo)
    _validate_tiling(event_ticker, buckets, size)
    return SeatMarket(event_ticker, tuple(buckets))


def _validate_tiling(event: str, buckets: list[Bucket], size: int) -> None:
    if not buckets or buckets[0].lo != 0 or buckets[-1].hi != size:
        raise ValueError(f"{event}: buckets do not span 0..{size}")
    for prev, nxt in zip(buckets, buckets[1:]):
        if nxt.lo != prev.hi + 1:
            raise ValueError(f"{event}: gap or overlap between {prev.hi} and {nxt.lo}")


# --- All aggregate markets ---------------------------------------------------


@dataclass(frozen=True)
class ControlMarket:
    dem: Quote
    rep: Quote

    def p_dem(self) -> float | None:
        """P(Democratic control): the D and R midpoints normalized."""
        d, r = self.dem.mid, self.rep.mid
        if d is not None and r is not None and d + r > 0:
            return d / (d + r)
        if d is not None:
            return d
        if r is not None:
            return 1.0 - r
        return None


@dataclass(frozen=True)
class AggregateMarkets:
    combo: dict[str, Quote]  # "DD", "DR", "RD", "RR"
    house_control: ControlMarket
    senate_control: ControlMarket
    same_party: Quote | None
    house_seats: SeatMarket | None
    senate_seats: SeatMarket | None

    def combo_mids(self) -> dict[str, float | None]:
        return {code: q.mid for code, q in self.combo.items()}


def fetch_markets(client: Client) -> AggregateMarkets:
    """Fetch every aggregate market the checks and the calibration need."""
    combo_event = client.fetch_event(constants.COMBO_EVENT)
    combo = {
        code: load_quote(client, ticker, combo_event)
        for code, ticker in constants.COMBO_MARKETS.items()
    }
    return AggregateMarkets(
        combo=combo,
        house_control=_control(client, constants.CONTROL_HOUSE_EVENT),
        senate_control=_control(client, constants.CONTROL_SENATE_EVENT),
        same_party=_single(client, constants.SAME_PARTY_MARKET),
        house_seats=_seats(client, constants.HOUSE_SEATS_EVENT, constants.HOUSE_TOTAL),
        senate_seats=_seats(client, constants.SENATE_SEATS_EVENT, constants.SENATE_TOTAL),
    )


def _control(client: Client, event_ticker: str) -> ControlMarket:
    event = client.fetch_event(event_ticker)
    return ControlMarket(
        dem=load_quote(client, f"{event_ticker}-D", event),
        rep=load_quote(client, f"{event_ticker}-R", event),
    )


def _single(client: Client, ticker: str) -> Quote | None:
    event = client.fetch_event(ticker)
    if event is None:
        return None
    return load_quote(client, ticker, event)


def _seats(client: Client, event_ticker: str, size: int) -> SeatMarket | None:
    event = client.fetch_event(event_ticker)
    if event is None or not event["markets"]:
        return None
    quotes = {m["ticker"]: load_quote(client, m["ticker"], event) for m in event["markets"]}
    return build_seat_market(event_ticker, event, quotes, size=size)


def traded_series(markets: AggregateMarkets) -> set[str]:
    """Series tickers of every market the checks can trade."""
    quotes = list(markets.combo.values())
    quotes += [markets.house_control.dem, markets.house_control.rep]
    quotes += [markets.senate_control.dem, markets.senate_control.rep]
    if markets.same_party is not None:
        quotes.append(markets.same_party)
    for seats in (markets.house_seats, markets.senate_seats):
        if seats is not None:
            quotes += [b.quote for b in seats.buckets]
    return {q.series for q in quotes}
