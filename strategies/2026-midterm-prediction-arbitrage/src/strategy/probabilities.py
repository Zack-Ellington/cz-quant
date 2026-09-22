"""Turn Kalshi order books into a Democratic win probability for every race.

Each race is a mutually-exclusive Kalshi event with a Democratic and a
Republican market. The price of the Democratic outcome is read from that
market's order book: the best bid comes from the top of the YES side, and the
best ask is one minus the top of the NO side (buying NO at ``p`` is selling YES
at ``1 - p``). We take the bid/ask midpoint of each leg, then normalize the two
legs against each other, because the two mid prices rarely sum to exactly 1.

When a market is missing (Louisiana, safe seats) or its book is empty, the race
falls back to its prior. In practice the 2026 books are well populated, so the
simulation runs on live prices, not priors.
"""

from __future__ import annotations

from strategy import constants
from strategy.api import Book, Client
from strategy.races import Race


def extract_probabilities(races: list[Race], client: Client) -> dict[str, float]:
    """Return ``{race_id: P(Democratic win)}`` for every race.

    Two order books are read per race that has a market (the Democratic and
    Republican legs). Races with no market are scored from their prior without a
    network call.
    """
    probs: dict[str, float] = {}
    cache: dict[str, float | None] = {}
    for race in races:
        probs[race.race_id] = _race_probability(race, client, cache)
    return probs


def _race_probability(
    race: Race, client: Client, cache: dict[str, float | None]
) -> float:
    if not race.has_market:
        return race.prior
    d_mid = _cached_mid(race.d_ticker, client, cache)
    r_mid = _cached_mid(race.r_ticker, client, cache)

    if d_mid is not None and r_mid is not None:
        total = d_mid + r_mid
        if total > 0:
            return d_mid / total
    if d_mid is not None:
        return d_mid
    if r_mid is not None:
        return 1.0 - r_mid
    return race.prior


def read_combo_prices(client: Client) -> dict[str, float | None]:
    """Return the Kalshi combo-market price for each control outcome.

    Each of the four legs of ``KXBALANCEPOWERCOMBO`` is priced from its own order
    book. A leg with no book comes back as ``None`` (shown as ``n/a``).
    """
    return {
        code: _book_mid(client.fetch_orderbook(ticker))
        for code, ticker in constants.COMBO_MARKETS.items()
    }


def _cached_mid(
    ticker: str | None, client: Client, cache: dict[str, float | None]
) -> float | None:
    if ticker is None:
        return None
    if ticker not in cache:
        cache[ticker] = _book_mid(client.fetch_orderbook(ticker))
    return cache[ticker]


def _book_mid(book: Book | None) -> float | None:
    """Best bid/ask midpoint of a market's YES side, as a 0-1 probability.

    ``yes_bid`` is the top of the YES book; ``yes_ask`` is one minus the top of
    the NO book. Returns the midpoint when both sides are present, the single
    side when only one is, and ``None`` for an empty or missing book.
    """
    if not book:
        return None
    yes_bid = _best(book.get("yes"))
    no_bid = _best(book.get("no"))
    yes_ask = (1.0 - no_bid) if no_bid is not None else None

    if yes_bid is not None and yes_ask is not None:
        return (yes_bid + yes_ask) / 2.0
    if yes_bid is not None:
        return yes_bid
    if yes_ask is not None:
        return yes_ask
    return None


def _best(levels: list[list[float]] | None) -> float | None:
    """Highest price on one side of the book (the best resting bid)."""
    if not levels:
        return None
    return max(price for price, _size in levels)
