"""Is exactly one outcome of a mutually exclusive event guaranteed to win?

Kalshi's `mutually_exclusive` (and `collateral_return_type = MECNET`) only promise
AT MOST one YES. The sum check needs AT LEAST one YES too, and the API has no field
for that, so it's inferred from the markets:

  EXHAUSTIVE      provably covers every outcome (numeric strikes tile the line,
                  or there is a catch-all leg such as "Other")
  GAP             numeric strikes leave part of the line uncovered
  NOT_EXHAUSTIVE  the rules say a tie or "no one" resolves every leg NO
  RISKY           party-based / "sworn in" legs, or decades-long events that may
                  never happen: an outcome outside the list is possible but unlikely
  UNKNOWN         none of the above; fall back to the market-implied 1 - sum(mid)

Only EXHAUSTIVE events are arbitrage. RISKY edges are a premium for a real risk.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone

EXHAUSTIVE, GAP, NOT_EXHAUSTIVE, RISKY, UNKNOWN = "EXHAUSTIVE", "GAP", "NOT_EXHAUSTIVE", "RISKY", "UNKNOWN"

NUMERIC = {"less", "less_or_equal", "between", "greater", "greater_or_equal"}

# Rules text that makes every leg lose.
_NO_WINNER = [
    re.compile(r"\btie[ds]?\b[^.]*\bresolves? to no\b", re.I),
    re.compile(r"\bif (?:no|none of)\b[^.]*\bresolves? to no\b", re.I),
    re.compile(r"\bif no (?:one|candidate|party|team|winner)\b", re.I),
]
# A leg that catches everything not listed.
_CATCH_ALL = re.compile(r"^(?:any )?(?:other|others|another|field|the field|none|no one|nobody|neither|tie|draw)\b", re.I)
# Legs that pay on a party or on someone being sworn in: a third party or vacancy loses them all.
# Catch-alls that also cover "nothing happens"; "Other" alone only covers unlisted names.
_COVERS_NONE = re.compile(r"^(?:none|no one|nobody|neither|tie|draw)\b", re.I)
# A deadline means the event can end with no one qualifying ("first to hit X before Jan 1").
_DEADLINE = re.compile(r"\b(?:before|by|prior to)\s+(?:the end of\s+)?(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|\d{1,2}/|\d{4})", re.I)
_PARTY = re.compile(r"\b(?:representative|member) of the \w+ part(?:y|ies)\b|\bsworn in\b|\bwins the [\w ]*(?:house|senate|chamber)\b", re.I)
LONG_DATED_YEARS = 5


def _num(x):
    return None if x in (None, "") else float(x)


def _resolution(values):
    """Smallest unit the strikes are quoted in (1 for integers, 0.1 for one decimal, ...)."""
    decimals = 0
    for v in values:
        s = repr(float(v)).rstrip("0").rstrip(".")
        if "." in s:
            decimals = max(decimals, len(s.split(".")[1]))
    return 10.0 ** -decimals


def numeric_coverage(markets) -> bool:
    """True if the markets' [floor, cap] intervals tile the whole real line.

    `between` is inclusive at both ends, and outcomes are assumed to be reported at
    the strikes' own resolution, so "70-71" followed by "72-73" leaves no gap.
    """
    ivs = []
    for m in markets:
        st, lo, hi = m.get("strike_type"), _num(m.get("floor_strike")), _num(m.get("cap_strike"))
        if st in ("less", "less_or_equal"):
            ivs.append((-math.inf, hi))
        elif st in ("greater", "greater_or_equal"):
            ivs.append((lo, math.inf))
        elif st == "between":
            ivs.append((lo, hi))
        if None in ivs[-1]:
            return False
    if not ivs:
        return False
    step = _resolution([v for iv in ivs for v in iv if math.isfinite(v)])
    ivs.sort()
    if ivs[0][0] != -math.inf:
        return False
    reach = ivs[0][1]
    for lo, hi in ivs[1:]:
        if lo - reach > step + 1e-9:
            return False
        reach = max(reach, hi)
    return reach == math.inf


def classify(event: dict, now: datetime | None = None) -> tuple[str, str]:
    """Returns (class, reason) for an event with nested markets."""
    markets = [m for m in event.get("markets") or [] if m.get("result") != "no"]
    if not markets:
        return UNKNOWN, "no markets"
    types = {m.get("strike_type") for m in markets}

    if types <= NUMERIC:
        return (EXHAUSTIVE, "numeric strikes cover every value") if numeric_coverage(markets) else (GAP, "numeric strikes leave a gap")

    # A listed "Tie" / "Other" leg wins whenever the named legs don't, so check it
    # before the rules text (which often describes exactly that leg).
    rules = " ".join(f"{m.get('rules_primary') or ''} {m.get('rules_secondary') or ''}" for m in markets[:3])
    labels = [(m.get("yes_sub_title") or "").strip() for m in markets]
    catch = [l for l in labels if _CATCH_ALL.match(l)]
    if catch:
        if not any(_COVERS_NONE.match(l) for l in catch) and _DEADLINE.search(rules):
            return RISKY, f"'{catch[0]}' covers unlisted names, but the deadline means no one may qualify"
        return EXHAUSTIVE, f"catch-all leg '{catch[0]}'"

    for pat in _NO_WINNER:
        if pat.search(rules):
            return NOT_EXHAUSTIVE, "rules: " + pat.search(rules).group(0)[:80]

    if _PARTY.search(rules):
        return RISKY, "party or sworn-in legs: a third party or vacancy loses every leg"

    now = now or datetime.now(timezone.utc)
    exp = max((m.get("latest_expiration_time") or m.get("close_time") or "" for m in markets), default="")
    if exp:
        years = (datetime.fromisoformat(exp.replace("Z", "+00:00")) - now).days / 365
        if years > LONG_DATED_YEARS:
            return RISKY, f"settles in {years:.0f} years: the event may never happen"

    return UNKNOWN, "no structural signal"
