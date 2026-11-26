"""Independent public quotes for recorded cross-market experiments; never submits orders."""

from __future__ import annotations

import logging
import time
from decimal import Decimal, ROUND_CEILING
from typing import Any

from .accounting import portfolio_state
from .book import levels
from .kalshi import Kalshi
from .polymarket_us import PolymarketUS, DEFAULT_THETA, fee, normalize_market
from .store import Store, decimal, utc

log = logging.getLogger(__name__)
ZERO = Decimal(0)
FEE_TYPES = {"quadratic", "quadratic_with_maker_fees", "quadratic_with_combo_maker_fees"}


def selections(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Latest recorded configuration per experiment, deduplicated across its brokers."""
    selected = {}
    for event in sorted(events, key=lambda e: e["sequence"]):
        data = event["data"]
        config = data.get("config", {})
        experiment = config.get("experiment", {})
        if (data.get("strategy") == "cross-market-polymarket"
                and experiment.get("pair", {}).get("venue") == "polymarket_us"):
            selected[experiment["id"]] = config
    return list(selected.values())


def asks(book: dict, side: str) -> list[tuple[Decimal, Decimal]]:
    normalized = levels(book)
    if normalized['yes'] and normalized['no'] and normalized['yes'][0][0] + normalized['no'][0][0] > 1:
        raise ValueError("crossed order book")
    return sorted((1 - p, q) for p, q in normalized["no" if side.upper() == "YES" else "yes"]
                  if 0 <= p <= 1)


def walk(book: list[tuple[Decimal, Decimal]], count: int) -> list[tuple[Decimal, Decimal]] | None:
    left, fills = Decimal(count), []
    for price, quantity in book:
        take = min(left, quantity)
        if take > 0:
            fills.append((price, take))
            left -= take
        if left == 0:
            return fills
    return None


def quote_route(kbook: list, pbook: list, count: int, multiplier: Decimal, theta: Decimal) -> dict | None:
    kfills, pfills = walk(kbook, count), walk(pbook, count)
    if kfills is None or pfills is None:
        return None
    kcost = sum((p * q for p, q in kfills), ZERO)
    pcost = sum((p * q for p, q in pfills), ZERO)
    kfee = (Decimal("0.07") * multiplier * sum((p * (1 - p) * q for p, q in kfills), ZERO)).quantize(
        Decimal("0.01"), rounding=ROUND_CEILING)
    pfee = fee(pfills, theta)
    gross = count - kcost - pcost
    return {"sets": count, "kalshi_price": kcost / count, "polymarket_price": pcost / count,
            "gross_gap": gross / count, "fees": kfee + pfee, "fee_per_set": (kfee + pfee) / count,
            "net": gross - kfee - pfee, "net_per_set": (gross - kfee - pfee) / count,
            "capital": kcost + pcost + kfee + pfee, "kalshi_capital": kcost + kfee,
            "polymarket_capital": pcost + pfee}


def measure_quotes(config: dict, kbooks: dict, pbook: dict, multiplier: Decimal, theta: Decimal,
                   available: dict[str, Decimal], capital: Decimal) -> dict:
    pair = config["experiment"]["pair"]
    outcomes = pair["outcomes"]
    if len(outcomes) != 2:
        raise ValueError("quote monitor requires exactly two outcomes")
    names = {a: name for name, a in pair.get("evidence", {}).get("kalshi", [])}
    legs, kb, pb = [], [], []
    for outcome in outcomes:
        k = asks(kbooks[outcome["kalshi"]], outcome["kalshi_side"])
        p = asks(pbook, outcome["polymarket_side"])
        kb.append(k)
        pb.append(p)
        suffix = outcome["kalshi"].rsplit("-", 1)[-1]
        name = names.get(suffix, suffix)
        legs.append({"name": name, "kalshi": outcome["kalshi"], "polymarket": outcome["polymarket"],
                     "polymarket_side": outcome["polymarket_side"],
                     "kalshi_ask": k[0][0] if k else None, "polymarket_ask": p[0][0] if p else None,
                     "price_gap": k[0][0] - p[0][0] if k and p else None,
                     "kalshi_depth": sum((q for _, q in k), ZERO),
                     "polymarket_depth": sum((q for _, q in p), ZERO)})
    routes = []
    for i, j in ((0, 1), (1, 0)):
        one = quote_route(kb[i], pb[j], 1, multiplier, theta)
        feasible = []
        for size in range(1, int(config.get("max_sets", 20)) + 1):
            q = quote_route(kb[i], pb[j], size, multiplier, theta)
            if q is None:
                break
            if (q["kalshi_capital"] <= available["kalshi"]
                    and q["polymarket_capital"] <= available["polymarket_us"] and q["capital"] <= capital):
                feasible.append(q)
        best = max(feasible, key=lambda q: q["net"], default=None)
        routes.append({"id": str(i), "name": f"{legs[i]['name']} on Kalshi + {legs[j]['name']} on Polymarket US",
                       "one_set": one, "best": best, "opportunity": bool(best and best["net"] > 0)})
    return {"legs": legs, "routes": routes, "capital_limit": capital,
            "available": available, "fee_multiplier": multiplier, "fee_coefficient": theta}


def sample(store: Store, config: dict, kalshi: Any, pmus: Any) -> dict:
    started = utc()
    experiment = config["experiment"]
    pair = experiment["pair"]
    markets, kbooks = {}, {}
    for outcome in pair["outcomes"]:
        ticker = outcome["kalshi"]
        market = kalshi.market(ticker)
        if market["event_ticker"] != pair["kalshi_event"]:
            raise ValueError("Kalshi event differs from recorded selection")
        markets[ticker] = market
        kbooks[ticker] = kalshi.book(ticker) if market["status"] in ("active", "open") else {}
        store.append("market", {"ticker": ticker, "market": market, "book": kbooks[ticker],
                                "source": "cross_market_quotes"})
    series = kalshi.get(f"/series/{pair['kalshi_series']}")["series"]
    if series["fee_type"] not in FEE_TYPES:
        raise ValueError("unsupported Kalshi fee type")
    market = pmus.market_raw(pair["polymarket_market"])
    if market["slug"] != pair["polymarket_market"]:
        raise ValueError("Polymarket market differs from recorded selection")
    pbook = pmus.books([pair["polymarket_market"]])[pair["polymarket_market"]]
    store.append("market", {"ticker": "PMUS:" + pair["polymarket_market"],
                            "market": normalize_market(market), "book": pbook, "source": "cross_market_quotes"})
    states = {v: portfolio_state(store, p) for v, p in experiment["portfolio"].items()}
    available = {v: max(ZERO, s.cash - store.reserved(experiment["portfolio"][v])) for v, s in states.items()}
    exposure = sum((s.cost for s in states.values()), ZERO)
    capital = min(sum(available.values(), ZERO),
                  sum((s.contributed for s in states.values()), ZERO) * decimal(config["max_capital_frac"]) - exposure)
    if config.get("max_total_dollars") is not None:
        reserved = sum((store.reserved(p) for p in experiment["portfolio"].values()), ZERO)
        capital = min(capital, decimal(config["max_total_dollars"]) - exposure - reserved)
    if config.get("max_basket_dollars") is not None:
        capital = min(capital, decimal(config["max_basket_dollars"]))
    theta = config.get("pmus_theta")
    theta = decimal(theta if theta is not None else market.get("feeCoefficient", DEFAULT_THETA))
    measured = measure_quotes(config, kbooks, pbook, decimal(series.get("fee_multiplier", 1)), theta,
                              available, max(ZERO, capital))
    open_ = all(m["status"] in ("active", "open") for m in markets.values()) and market["status"] == "MARKET_STATUS_OPEN"
    if not open_:
        for route in measured["routes"]:
            route["opportunity"] = False
            route["best"] = None
    return {"experiment_id": experiment["id"], "status": "ok" if open_ else "closed",
            "started_at": started, "observed_at": utc(), **measured}


def run(root: str | None, interval: float = 5, once: bool = False) -> None:
    if interval <= 0:
        raise ValueError("interval must be positive")
    store = Store(root)
    kalshi, pmus = Kalshi(authenticated=False), PolymarketUS.from_env(authenticated=False)
    try:
        while True:
            start = time.monotonic()
            errors = []
            configs = selections(store.events(kinds=("run",)))
            for config in configs:
                identifier = config["experiment"]["id"]
                try:
                    result = sample(store, config, kalshi, pmus)
                except Exception as exc:
                    log.exception("quote sampling failed for %s", identifier)
                    errors.append({"experiment_id": identifier, "error": type(exc).__name__})
                    result = {"experiment_id": identifier, "status": "error", "error": str(exc)[:200],
                              "observed_at": utc(), "legs": [], "routes": []}
                store.append("observation", {"kind": "cross_market_quote", "interval": interval, **result})
            store.append("health", {"component": "cross_market_quotes", "status": "degraded" if errors else "healthy",
                                    "experiments": len(configs), "errors": errors})
            if once:
                return
            time.sleep(max(0.1, interval - (time.monotonic() - start)))
    finally:
        store.close()
