"""The full set of races the simulation runs over.

``discover_races()`` returns every contest that moves a chamber: 35 Senate seats
and all 435 House seats. It reads no live data -- it assembles the canonical set
from ``constants`` -- so the race list is deterministic and the ticker traps are
resolved in one place.

Two things are worth calling out:

* The Kentucky Senate race carries the Kalshi event ``SENATELA-26`` (the trap),
  and Louisiana carries no event at all. Both come straight from
  ``constants.SENATE_RACES``; nothing downstream has to special-case them.
* Kalshi prices only competitive House districts. The remaining seats are
  emitted here as "safe" races with a fixed prior (1.0 for a safe Democratic
  seat, 0.0 for a safe Republican one) so the House always has 435 races and the
  seat math is closed.
"""

from __future__ import annotations

from dataclasses import dataclass

from strategy import constants


@dataclass(frozen=True)
class Race:
    """One contest.

    ``race_id`` is prefixed ``senate:`` or ``house:`` so the simulation can split
    chambers from the probability dict alone. ``d_ticker`` is the Kalshi market
    for the Democratic outcome, or ``None`` for a race with no market (Louisiana,
    and every safe House seat). ``prior`` is the fallback Democratic win
    probability used when there is no market or the market has no quote.
    """

    race_id: str
    chamber: str  # "senate" or "house"
    label: str
    event: str | None
    d_ticker: str | None
    r_ticker: str | None
    prior: float
    is_safe: bool = False

    @property
    def has_market(self) -> bool:
        return self.event is not None


def discover_races() -> list[Race]:
    """Return the 35 Senate races and 435 House races, Senate first."""
    return _senate_races() + _house_races()


def _senate_races() -> list[Race]:
    races: list[Race] = []
    for seat in constants.SENATE_RACES:
        d_ticker = f"{seat.event}-D" if seat.event else None
        r_ticker = f"{seat.event}-R" if seat.event else None
        races.append(
            Race(
                race_id=f"senate:{seat.state}",
                chamber="senate",
                label=f"{seat.state} Senate",
                event=seat.event,
                d_ticker=d_ticker,
                r_ticker=r_ticker,
                prior=seat.prior,
                is_safe=seat.event is None,
            )
        )
    return races


def _house_races() -> list[Race]:
    races: list[Race] = []
    for district in constants.HOUSE_MARKET_DISTRICTS:
        races.append(
            Race(
                race_id=f"house:{district.code}",
                chamber="house",
                label=f"{district.code} House",
                event=district.event,
                d_ticker=f"{district.event}-D",
                r_ticker=f"{district.event}-R",
                prior=district.prior,
            )
        )
    for i in range(constants.HOUSE_SAFE_DEM):
        races.append(_safe_house_seat("D", i))
    for i in range(constants.HOUSE_SAFE_REP):
        races.append(_safe_house_seat("R", i))
    return races


def _safe_house_seat(party: str, index: int) -> Race:
    prior = 1.0 if party == "D" else 0.0
    return Race(
        race_id=f"house:SAFE-{party}-{index:03d}",
        chamber="house",
        label=f"Safe {party} House seat",
        event=None,
        d_ticker=None,
        r_ticker=None,
        prior=prior,
        is_safe=True,
    )
