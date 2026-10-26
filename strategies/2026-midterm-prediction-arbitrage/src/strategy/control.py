"""Who controls each chamber for a given number of Democratic seats.

The rules are deliberately isolated here because the tie cases are where control
markets get mispriced: 50-50 in the Senate goes to the party of the Vice
President, and 217-218 is the House knife's edge.
"""

from __future__ import annotations

from strategy.constants import HOUSE_MAJORITY, SENATE_DEM_CONTROL


def house_control(dem_seats: int) -> str:
    """Return "D" or "R" for the party that controls the House.

    Democrats need an outright majority (218 of 435). 217 leaves Republicans in
    control.
    """
    return "D" if dem_seats >= HOUSE_MAJORITY else "R"


def senate_control(dem_seats: int) -> str:
    """Return "D" or "R" for the party that controls the Senate.

    A 50-50 Senate is broken by the Vice President, who is a Republican in the
    2027 Senate, so Democrats need 51 and 50 is not enough.
    """
    return "D" if dem_seats >= SENATE_DEM_CONTROL else "R"
