"""Static facts about the 2026 U.S. midterm and the Kalshi markets that price it.

Nothing here is a live quote. These are seat counts, control thresholds, the set
of races on the ballot, and the Kalshi tickers that carry them. The live prices
come from ``api.py`` at run time.

Where the numbers come from
---------------------------
* Chamber sizes and the majority thresholds are fixed by law.
* The Senate that convenes in January 2027 is split 53 R / 47 D-caucus going in.
  Of those 100 seats, 35 are on the 2026 ballot (33 Class II seats plus the
  Florida and Ohio special elections). That leaves 65 seats not up: 34 held by
  the Democratic caucus and 31 by Republicans. Those two numbers are the Senate
  baseline the simulation starts from.
* Every House seat is up every two years, so the House has no "not up" baseline.
  Kalshi lists a market only for the competitive districts; the rest are treated
  as safe at their 2024 result. ``HOUSE_SAFE_DEM`` and ``HOUSE_SAFE_REP`` are
  those safe seats, and they plus the market districts sum to 435.

The Senate ticker set was obtained by enumerating Kalshi's ``Elections`` series
(tag ``Senate``) and keeping the ``-26`` events. Two traps live in that data and
are handled here, not in the caller:

* ``SENATELA-26`` is titled "Kentucky Senate winner?" -- the LA-suffixed event
  actually holds the Kentucky race, and there is no ``SENATEKY-26``.
* Louisiana has no market at all, so it carries ``event=None`` and is scored from
  its prior.
* The Florida and Ohio specials hide under an ``S`` suffix (``SENATEFLS-26``,
  ``SENATEOHS-26``).
"""

from __future__ import annotations

from dataclasses import dataclass

# --- Chamber sizes and control thresholds -------------------------------------

SENATE_TOTAL = 100
HOUSE_TOTAL = 435

# Democrats need an outright 218 for the House. In the Senate a 50-50 tie is
# broken by the Vice President; the VP seated in January 2027 is a Republican, so
# Democrats need 51 seats to control and 50 is not enough.
HOUSE_MAJORITY = 218
VP_PARTY = "R"
SENATE_DEM_CONTROL = 51  # 100 // 2 + 1, given a Republican tie-breaking VP

# --- Senate baseline (seats not on the 2026 ballot) ---------------------------

SENATE_NOT_UP_DEM = 34
SENATE_NOT_UP_REP = 31

# --- House baseline (seats with no Kalshi market, held at their 2024 result) ---
# Democrats won 215 House seats and Republicans 220 in 2024. Kalshi prices only
# the competitive districts; every other seat is treated as safe at its 2024
# result. The safe counts are therefore the 2024 totals minus the districts that
# have a market (by their current, 119th-Congress holder), so the full 435-seat
# chamber is anchored to the real 2024 outcome rather than a guess. They are
# derived below from the ``held`` field on each market district, not hand-set.

HOUSE_2024_DEM = 215
HOUSE_2024_REP = 220

# --- Kalshi API -------------------------------------------------------------

# Public market-data host. No authentication is needed to read events or prices.
DEFAULT_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

# The combo market: one mutually-exclusive event with the four control outcomes.
COMBO_EVENT = "KXBALANCEPOWERCOMBO-27FEB"
# outcome code -> combo market ticker
COMBO_MARKETS = {
    "DD": f"{COMBO_EVENT}-DD",  # Democrats sweep
    "DR": f"{COMBO_EVENT}-DR",  # D House, R Senate
    "RD": f"{COMBO_EVENT}-RD",  # R House, D Senate
    "RR": f"{COMBO_EVENT}-RR",  # Republicans sweep
}
# Single-chamber control markets, used only as a cross-check in the output.
CONTROL_HOUSE_EVENT = "CONTROLH-2026"
CONTROL_SENATE_EVENT = "CONTROLS-2026"


@dataclass(frozen=True)
class SenateSeat:
    """A Senate seat on the 2026 ballot.

    ``event`` is the Kalshi event ticker whose ``-D`` / ``-R`` markets price the
    race, or ``None`` when Kalshi lists no market (Louisiana). ``prior`` is the
    Democratic win probability used when the market is missing or unpriced.
    ``lean`` is the party the seat is scored to if everything else fails.
    """

    state: str
    event: str | None
    lean: str  # "D" or "R"
    prior: float


# The 35 seats up in 2026: 33 Class II seats plus the FL and OH specials.
# prior is P(Democratic win) and is only consulted when the market is unpriced.
SENATE_RACES: tuple[SenateSeat, ...] = (
    SenateSeat("AL", "SENATEAL-26", "R", 0.02),
    SenateSeat("AK", "SENATEAK-26", "R", 0.10),
    SenateSeat("AR", "SENATEAR-26", "R", 0.02),
    SenateSeat("CO", "SENATECO-26", "D", 0.92),
    SenateSeat("DE", "SENATEDE-26", "D", 0.97),
    SenateSeat("FL", "SENATEFLS-26", "R", 0.10),  # special (Moody seat)
    SenateSeat("GA", "SENATEGA-26", "D", 0.55),
    SenateSeat("ID", "SENATEID-26", "R", 0.02),
    SenateSeat("IL", "SENATEIL-26", "D", 0.95),
    SenateSeat("IA", "SENATEIA-26", "R", 0.18),
    SenateSeat("KS", "SENATEKS-26", "R", 0.05),
    # The trap: the Kentucky race is filed under the LA-suffixed event.
    SenateSeat("KY", "SENATELA-26", "R", 0.08),
    # Louisiana: no Kalshi market. Scored from its prior (safe Republican).
    SenateSeat("LA", None, "R", 0.03),
    SenateSeat("ME", "SENATEME-26", "R", 0.35),
    SenateSeat("MA", "SENATEMA-26", "D", 0.98),
    SenateSeat("MI", "SENATEMI-26", "D", 0.60),
    SenateSeat("MN", "SENATEMN-26", "D", 0.90),
    SenateSeat("MS", "SENATEMS-26", "R", 0.03),
    SenateSeat("MT", "SENATEMT-26", "R", 0.05),
    SenateSeat("NE", "SENATENE-26", "R", 0.10),
    SenateSeat("NH", "SENATENH-26", "D", 0.72),
    SenateSeat("NJ", "SENATENJ-26", "D", 0.93),
    SenateSeat("NM", "SENATENM-26", "D", 0.94),
    SenateSeat("NC", "SENATENC-26", "R", 0.52),
    SenateSeat("OH", "SENATEOHS-26", "R", 0.15),  # special (Husted seat)
    SenateSeat("OK", "SENATEOK-26", "R", 0.02),
    SenateSeat("OR", "SENATEOR-26", "D", 0.95),
    SenateSeat("RI", "SENATERI-26", "D", 0.95),
    SenateSeat("SC", "SENATESC-26", "R", 0.06),
    SenateSeat("SD", "SENATESD-26", "R", 0.03),
    SenateSeat("TN", "SENATETN-26", "R", 0.04),
    SenateSeat("TX", "SENATETX-26", "R", 0.28),
    SenateSeat("VA", "SENATEVA-26", "D", 0.94),
    SenateSeat("WV", "SENATEWV-26", "R", 0.02),
    SenateSeat("WY", "SENATEWY-26", "R", 0.01),
)


@dataclass(frozen=True)
class HouseDistrict:
    """A competitive House district with a Kalshi market.

    ``held`` is the party that won the district in 2024 (its current holder in
    the 119th Congress). It is used only to split the safe-seat baseline, so that
    a currently-Democratic seat that happens to have a market is not also counted
    in the safe-Democratic pool.
    """

    code: str  # e.g. "PA-07"
    event: str  # e.g. "HOUSEPA7-26"
    held: str  # "D" or "R": 2024 winner
    prior: float  # P(Democratic win), used when the market is unpriced


# The competitive House districts Kalshi prices (its ``Elections`` series tagged
# ``House`` with a ``-26`` event). Priors are rough 2026 ratings and are only
# used when a market has no quote. Every other district is a safe seat counted in
# HOUSE_SAFE_DEM / HOUSE_SAFE_REP.
HOUSE_MARKET_DISTRICTS: tuple[HouseDistrict, ...] = (
    HouseDistrict("AK-AL", "HOUSEAKAL-26", "R", 0.10),
    HouseDistrict("AZ-01", "HOUSEAZ1-26", "R", 0.45),
    HouseDistrict("AZ-02", "HOUSEAZ2-26", "R", 0.30),
    HouseDistrict("AZ-06", "HOUSEAZ6-26", "R", 0.48),
    HouseDistrict("CA-03", "HOUSECA3-26", "R", 0.40),
    HouseDistrict("CA-09", "HOUSECA9-26", "D", 0.62),
    HouseDistrict("CA-13", "HOUSECA13-26", "D", 0.55),
    HouseDistrict("CA-21", "HOUSECA21-26", "D", 0.58),
    HouseDistrict("CA-22", "HOUSECA22-26", "R", 0.50),
    HouseDistrict("CA-27", "HOUSECA27-26", "D", 0.55),
    HouseDistrict("CA-40", "HOUSECA40-26", "R", 0.42),
    HouseDistrict("CA-41", "HOUSECA41-26", "R", 0.45),
    HouseDistrict("CA-45", "HOUSECA45-26", "D", 0.55),
    HouseDistrict("CA-47", "HOUSECA47-26", "D", 0.58),
    HouseDistrict("CA-49", "HOUSECA49-26", "D", 0.60),
    HouseDistrict("CO-03", "HOUSECO3-26", "R", 0.42),
    HouseDistrict("CO-08", "HOUSECO8-26", "R", 0.55),
    HouseDistrict("CT-05", "HOUSECT5-26", "D", 0.62),
    HouseDistrict("FL-13", "HOUSEFL13-26", "R", 0.30),
    HouseDistrict("FL-23", "HOUSEFL23-26", "D", 0.48),
    HouseDistrict("IA-01", "HOUSEIA1-26", "R", 0.45),
    HouseDistrict("IA-03", "HOUSEIA3-26", "R", 0.45),
    HouseDistrict("IL-17", "HOUSEIL17-26", "D", 0.55),
    HouseDistrict("IN-01", "HOUSEIN1-26", "D", 0.52),
    HouseDistrict("ME-02", "HOUSEME2-26", "D", 0.55),
    HouseDistrict("MI-03", "HOUSEMI3-26", "D", 0.52),
    HouseDistrict("MI-04", "HOUSEMI4-26", "R", 0.30),
    HouseDistrict("MI-07", "HOUSEMI7-26", "R", 0.50),
    HouseDistrict("MI-08", "HOUSEMI8-26", "D", 0.52),
    HouseDistrict("MI-10", "HOUSEMI10-26", "R", 0.50),
    HouseDistrict("MN-02", "HOUSEMN2-26", "D", 0.55),
    HouseDistrict("MT-01", "HOUSEMT1-26", "R", 0.40),
    HouseDistrict("NC-01", "HOUSENC1-26", "D", 0.52),
    HouseDistrict("NE-02", "HOUSENE2-26", "R", 0.55),
    HouseDistrict("NH-01", "HOUSENH1-26", "D", 0.58),
    HouseDistrict("NH-02", "HOUSENH2-26", "D", 0.60),
    HouseDistrict("NJ-05", "HOUSENJ5-26", "D", 0.58),
    HouseDistrict("NJ-07", "HOUSENJ7-26", "R", 0.50),
    HouseDistrict("NJ-09", "HOUSENJ9-26", "D", 0.60),
    HouseDistrict("NM-02", "HOUSENM2-26", "D", 0.55),
    HouseDistrict("NV-01", "HOUSENV1-26", "D", 0.55),
    HouseDistrict("NV-03", "HOUSENV3-26", "D", 0.55),
    HouseDistrict("NV-04", "HOUSENV4-26", "D", 0.58),
    HouseDistrict("NY-03", "HOUSENY3-26", "D", 0.55),
    HouseDistrict("NY-04", "HOUSENY4-26", "D", 0.58),
    HouseDistrict("NY-17", "HOUSENY17-26", "R", 0.50),
    HouseDistrict("NY-18", "HOUSENY18-26", "D", 0.55),
    HouseDistrict("NY-19", "HOUSENY19-26", "D", 0.48),
    HouseDistrict("NY-22", "HOUSENY22-26", "D", 0.50),
    HouseDistrict("OH-01", "HOUSEOH1-26", "D", 0.50),
    HouseDistrict("OH-09", "HOUSEOH9-26", "D", 0.52),
    HouseDistrict("OH-13", "HOUSEOH13-26", "D", 0.53),
    HouseDistrict("OR-05", "HOUSEOR5-26", "D", 0.55),
    HouseDistrict("PA-01", "HOUSEPA1-26", "R", 0.42),
    HouseDistrict("PA-07", "HOUSEPA7-26", "R", 0.52),
    HouseDistrict("PA-08", "HOUSEPA8-26", "R", 0.50),
    HouseDistrict("PA-10", "HOUSEPA10-26", "R", 0.42),
    HouseDistrict("PA-17", "HOUSEPA17-26", "D", 0.52),
    HouseDistrict("TX-15", "HOUSETX15-26", "R", 0.40),
    HouseDistrict("TX-28", "HOUSETX28-26", "D", 0.52),
    HouseDistrict("TX-34", "HOUSETX34-26", "D", 0.50),
    HouseDistrict("VA-01", "HOUSEVA1-26", "R", 0.48),
    HouseDistrict("VA-02", "HOUSEVA2-26", "R", 0.55),
    HouseDistrict("VA-07", "HOUSEVA7-26", "D", 0.55),
    HouseDistrict("WA-03", "HOUSEWA3-26", "D", 0.55),
    HouseDistrict("WI-01", "HOUSEWI1-26", "R", 0.42),
    HouseDistrict("WI-03", "HOUSEWI3-26", "R", 0.48),
)

# Safe seats = the 2024 chamber result minus the districts that now have a
# market (counted by their current holder). Anchors the full 435 to 2024.
HOUSE_SAFE_DEM = HOUSE_2024_DEM - sum(1 for d in HOUSE_MARKET_DISTRICTS if d.held == "D")
HOUSE_SAFE_REP = HOUSE_2024_REP - sum(1 for d in HOUSE_MARKET_DISTRICTS if d.held == "R")
