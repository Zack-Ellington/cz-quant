"""Discover and prepare cross-market pairs without opening portfolios or trading."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from czquant.kalshi import Kalshi
from czquant.polymarket import Polymarket
from czquant.polymarket_us import PolymarketUS

from markets import Listing, load_markets, resolve
from matching import Pair, PolyGame, kalshi_game, load_pairs, match, poly_game, us_game

HERE = Path(__file__).resolve().parent


def load_leagues(names: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    specs = json.loads((HERE / "leagues.json").read_text())
    unknown = set(names) - specs.keys()
    if unknown:
        raise ValueError(f"unknown leagues: {sorted(unknown)}")
    return {name: spec for name, spec in specs.items() if name in names or (not names and spec["enabled"])}


def us_games(client: Any, leagues: dict[str, dict[str, Any]]) -> dict[str, list[PolyGame]]:
    now = datetime.now(timezone.utc)
    prefixes = {spec.get("polymarket_us_prefix", name) + "-": name for name, spec in leagues.items()}
    events: dict[str, tuple[str, dict[str, Any]]] = {}
    for event in client.events(active="true", closed="false",
                               startTimeMin=(now - timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                               startTimeMax=(now + timedelta(days=8)).strftime("%Y-%m-%dT%H:%M:%SZ")):
        league = next((v for k, v in prefixes.items() if event["slug"].startswith(k)), None)
        primary = [g for g in event.get("marketGroups", []) if g["id"] == "PRIMARY"]
        if league is not None and len(primary) == 1 and len(primary[0].get("marketIds", [])) == 1:
            events[str(primary[0]["marketIds"][0])] = league, event
    result: dict[str, list[PolyGame]] = {name: [] for name in leagues}
    for market in client.markets_by_id(sorted(events)):
        league, event = events[str(market["id"])]
        game = us_game(event, market)
        if game is not None:
            result[league].append(game)
    return result


def sports_pair(pair: Pair, venue: str, resolution_hours: float, rules: str) -> dict[str, Any]:
    if pair.poly.start is None:
        raise ValueError(f"{pair.poly.slug} has no start time; prepare a resolution estimate before trading")
    us = venue == "polymarket_us"
    return {"venue": venue, "kalshi_event": pair.game.event, "kalshi_series": pair.game.series,
            "polymarket_event": pair.poly.slug,
            "polymarket_market": pair.poly.market["slug"] if us else str(pair.poly.market["id"]),
            "resolves_at": (pair.poly.start + timedelta(hours=resolution_hours)).isoformat(),
            "outcomes": [{"kalshi": name, "kalshi_side": "YES",
                          "polymarket": "PMUS:" + pair.poly.market["slug"] if us else "PM:" + outcome,
                          "polymarket_side": outcome if us else "YES"} for name, outcome in pair.legs.items()],
            "evidence": {**pair.evidence, "rules": rules}}


def sports(kalshi: Any, poly: Any, venue: str, leagues: dict[str, dict[str, Any]],
           pins: dict[str, Any], resolution_hours: float) -> dict[str, Any]:
    polys = us_games(poly, leagues) if venue == "polymarket_us" else {}
    pairs, counts = [], {}
    for league, spec in leagues.items():
        if venue == "polymarket":
            polys[league] = [game for event in poly.events(tag_slug=spec["polymarket_tag"], closed="false")
                             if (game := poly_game(event)) is not None]
        aliases = (spec.get("polymarket_us_abbr_aliases") if venue == "polymarket_us" else None) or spec.get("abbr_aliases", {})
        for event in kalshi.pages("/events", "events", series_ticker=spec["kalshi_series"], status="open",
                                  with_nested_markets="true", limit=200):
            game = kalshi_game(event)
            if game is None:
                continue
            if game.event in pins["blocked"]:
                counts["blocked"] = counts.get("blocked", 0) + 1
                continue
            pair, reason = match(game, polys[league], aliases, pins["pairs"].get(game.event))
            counts[reason] = counts.get(reason, 0) + 1
            if pair is not None:
                pairs.append(sports_pair(pair, venue, resolution_hours, spec.get("rules", "")))
    return {"pairs": pairs, "counts": counts}


def questions(kalshi: Any, poly: Any, listings: list[Listing]) -> dict[str, Any]:
    pairs, counts = [], {}
    for listing in listings:
        market = kalshi.get(f"/markets/{listing.kalshi}")["market"]
        raw = poly.market_by_slug(listing.polymarket)
        pair, reason = resolve(listing, market, raw)
        counts[reason] = counts.get(reason, 0) + 1
        if pair is None:
            continue
        if pair.resolves is None:
            raise ValueError(f"{listing.kalshi} has no resolution time")
        series = kalshi.get(f"/events/{pair.event}")["event"]["series_ticker"]
        pairs.append({"venue": "polymarket", "kalshi_event": pair.event, "kalshi_series": series,
                      "polymarket_event": listing.polymarket, "polymarket_market": str(raw["id"]),
                      "resolves_at": pair.resolves.isoformat(),
                      "outcomes": [{"kalshi": listing.kalshi, "kalshi_side": side.upper(),
                                    "polymarket": "PM:" + token, "polymarket_side": "YES"}
                                   for side, token in pair.tokens.items()],
                      "evidence": {"method": listing.source, "name": listing.name, "rules": listing.rules,
                                   "invert": listing.invert, "score": listing.score}})
    return {"pairs": pairs, "counts": counts}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=("sports", "questions"), default="sports")
    parser.add_argument("--venue", choices=("polymarket-us", "polymarket"), default="polymarket-us")
    parser.add_argument("--leagues", default="", help="comma-separated league keys; default: enabled leagues")
    parser.add_argument("--pairs", help="sports pins and blocks; default: venue-specific pairs JSON in this bundle")
    parser.add_argument("--listings", default=str(HERE / "markets.json"), help="reviewed international question listings")
    parser.add_argument("--resolution-hours", type=float, default=6, help="sports: estimated hours from start to payout")
    parser.add_argument("--out", required=True, type=Path, help="candidate pair JSON; review before selecting one")
    args = parser.parse_args(argv)
    if args.source == "questions" and args.venue != "polymarket":
        parser.error("question preparation requires --venue polymarket")
    venue = args.venue.replace("-", "_")
    kalshi = Kalshi(authenticated=False)
    poly = PolymarketUS.from_env(authenticated=False) if venue == "polymarket_us" else Polymarket.from_env()
    if args.source == "sports":
        pins = load_pairs(args.pairs or str(HERE / ("pairs.us.json" if venue == "polymarket_us" else "pairs.json")))
        result = sports(kalshi, poly, venue, load_leagues(tuple(filter(None, args.leagues.split(",")))), pins, args.resolution_hours)
    else:
        result = questions(kalshi, poly, load_markets(args.listings, auto=None))
    args.out.write_text(json.dumps({"prepared_at": datetime.now(timezone.utc).isoformat(), **result}, indent=2) + "\n")
    print(f"{len(result['pairs'])} pairs -> {args.out}; {result['counts']}")


if __name__ == "__main__":
    main()
