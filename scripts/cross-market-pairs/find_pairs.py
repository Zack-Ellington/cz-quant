"""Suggests non-sports Kalshi/Polymarket pairs for review. It only reads, and never
edits markets.json: a suggestion is a lead, not a match.

Two sources:
  oddpool  (default when ODDPOOL_API_KEY is set) Oddpool's series search finds each
           topic's series on both venues at once (oddpool.py). Series are paired by
           title first, then markets are paired only within paired series, so a Fed
           market is only ever compared with Fed markets. --topics narrows the search
           (fed,cpi,bitcoin); without it every active non-sports series is paired.
  scan     every open yes/no market on both venues, compared with each other.

Within the pool, each Kalshi market is scored against Polymarket's by shared words,
requiring:
  - every number in either question (strikes, dates, counts) to appear in the other, and
  - expected resolutions within --days of each other.
Candidates are written to candidates.json, best first, with the words only one side
uses (where a mismatch usually shows: "vice", "impeached", a different deadline),
both venues' rules text
and a rough top-of-book cost (the cheaper of Kalshi YES + Polymarket No and Kalshi NO
+ Polymarket Yes, before fees). To trade one, read both rules, then copy it into
markets.json with "enabled": true, and "invert": true if the questions are complements.

Usage
  uv run --project strategies/cross-market-polymarket python scripts/cross-market-pairs/find_pairs.py --topics fed,cpi,inflation --out /path/to/candidates.json
  uv run --project strategies/cross-market-polymarket python scripts/cross-market-pairs/find_pairs.py --source scan --out /path/to/candidates.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from collections import defaultdict
from datetime import datetime
from typing import Any

from czquant.kalshi import Kalshi
from czquant.polymarket import Polymarket

from oddpool import Oddpool

SPORTS_CATEGORIES = {"Sports"}
SPORTS_TAGS = {"sports", "esports", "games"}
STOP = set("""a an the of in on at to by for and or be will is are was with from before after than more less
over under above below this that its it as any which who what when market resolve resolves yes no end""".split())
NUMBER = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?")
WORD = re.compile(r"[a-z]+")
NAMES = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
         "november", "december")
MONTHS: dict[str, str] = {**{n: str(i) for i, n in enumerate(NAMES, 1)}, **{n[:3]: str(i) for i, n in enumerate(NAMES, 1)},
          "sept": "9"}  # a month written either way counts as the same word


def words(text: str) -> set[str]:
    out = set()
    for w in WORD.findall(text.lower()):
        w = MONTHS.get(w) or w
        if w not in STOP and len(w) > 1:
            out.add(w)
    return out


def numbers(text: str) -> set[str]:
    out = set()
    for n in NUMBER.findall(text.replace("$", "")):
        n = n.replace(",", "")
        out.add(str(float(n)).rstrip("0").rstrip("."))
    return out


def when(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None
    except ValueError:
        return None


def _list(value: Any) -> list[Any]:
    return json.loads(value) if isinstance(value, str) else list(value or [])


def kalshi_markets(client: Kalshi, categories: set[str] | None, limit: int) -> list[dict[str, Any]]:
    out = []
    for event in client.pages("/events", "events", status="open", with_nested_markets="true", limit=200):
        category = event.get("category") or ""
        if category in SPORTS_CATEGORIES or (categories and category not in categories):
            continue
        for m in event.get("markets") or []:
            if m.get("status") not in ("active", "open"):
                continue
            sub = m.get("yes_sub_title") or ""
            title = m.get("title") or event.get("title") or ""
            text = title if sub and sub.lower() in title.lower() else f"{title} {sub}".strip()
            out.append({"ticker": m["ticker"], "event": event["event_ticker"], "category": category, "text": text,
                        "end": m.get("expected_expiration_time") or m.get("close_time"),
                        "yes_ask": m.get("yes_ask_dollars"), "no_ask": m.get("no_ask_dollars"),
                        "rules": (m.get("rules_primary") or "")[:600]})
        if len(out) >= limit:
            break
    return out


def poly_markets(client: Polymarket, limit: int) -> list[dict[str, Any]]:
    out = []
    for event in client.events(closed="false"):
        tags = {str(t.get("slug") or "").lower() for t in event.get("tags") or []}
        if tags & SPORTS_TAGS:
            continue
        for m in event.get("markets") or []:
            if m.get("closed") or not m.get("acceptingOrders", True):
                continue
            if sorted(str(o).lower() for o in _list(m.get("outcomes"))) != ["no", "yes"]:
                continue
            if not m.get("question") or str(m.get("slug", "")).startswith("untitled"):
                continue
            out.append({"slug": m.get("slug"), "event": event.get("slug"), "text": m.get("question") or "",
                        "end": m.get("endDate"), "best_ask": m.get("bestAsk"), "best_bid": m.get("bestBid"),
                        "rules": (m.get("description") or "")[:600]})
        if len(out) >= limit:
            break
    return out


def rough_cost(k: dict[str, Any], p: dict[str, Any]) -> float | None:
    """Cheaper of Kalshi YES + Polymarket No, Kalshi NO + Polymarket Yes, before fees."""
    try:
        options = []
        if k.get("yes_ask") and p.get("best_bid") is not None:
            options.append(float(k["yes_ask"]) + 1 - float(p["best_bid"]))
        if k.get("no_ask") and p.get("best_ask") is not None:
            options.append(float(k["no_ask"]) + float(p["best_ask"]))
        options = [o for o in options if 0 < o < 2]
        return round(min(options), 3) if options else None
    except (TypeError, ValueError):
        return None


def candidates(kms: list[dict[str, Any]], pms: list[dict[str, Any]], min_score: float, days: float,
               per_market: int = 3) -> list[dict[str, Any]]:
    index: dict[str, list[int]] = defaultdict(list)
    pwords = [words(p["text"]) for p in pms]
    pnums = [numbers(p["text"]) for p in pms]
    for i, ws in enumerate(pwords):
        for w in ws:
            index[w].append(i)
    out = []
    for k in kms:
        kw, kn, kend = words(k["text"]), numbers(k["text"]), when(k["end"])
        hits: dict[int, int] = defaultdict(int)
        for w in kw:
            if len(index[w]) < 400:  # very common words say nothing
                for i in index[w]:
                    hits[i] += 1
        scored = []
        for i, shared in hits.items():
            if pnums[i] != kn:
                continue
            score = shared / len(kw | pwords[i])
            if score < min_score:
                continue
            pend = when(pms[i]["end"])
            if kend and pend and abs((kend - pend).total_seconds()) > days * 86400:
                continue
            scored.append((score, i))
        for score, i in sorted(scored, reverse=True)[:per_market]:
            p = pms[i]
            pw = pwords[i]
            out.append({"score": round(score, 3), "rough_cost": rough_cost(k, p),
                        "only_kalshi": sorted(kw - pw), "only_polymarket": sorted(pw - kw),
                        "name": p["text"][:80], "kalshi": k["ticker"], "polymarket": p["slug"], "invert": False,
                        "enabled": False, "kalshi_question": k["text"], "polymarket_question": p["text"],
                        "category": k["category"], "kalshi_end": k["end"], "polymarket_end": p["end"],
                        "kalshi_rules": k["rules"], "polymarket_rules": p["rules"]})
    out.sort(key=lambda c: (-c["score"], c["rough_cost"] if c["rough_cost"] is not None else 9))
    return out


SPORTS_SERIES = {"sports", "esports"}


def series_pairs(series: list[dict[str, Any]], min_score: float, per_series: int = 3) -> list[tuple[str, str, float]]:
    """(Kalshi series_id, Polymarket series_id, score) for series whose titles share most words."""
    kalshi = [x for x in series if x.get("exchange") == "kalshi"]
    poly = [x for x in series if x.get("exchange") == "polymarket"]
    pw = [words(x.get("title") or "") for x in poly]
    out = []
    for k in kalshi:
        kw = words(k.get("title") or "")
        if not kw:
            continue
        scored = sorted(((len(kw & w) / len(kw | w), x["series_id"]) for x, w in zip(poly, pw) if kw & w), reverse=True)
        out += [(k["series_id"], sid, round(score, 3)) for score, sid in scored[:per_series] if score >= min_score]
    return out


def kalshi_row(m: dict[str, Any]) -> dict[str, Any]:
    question = m.get("question") or ""
    title = m.get("event_title") or ""
    # A Kalshi strike's question can leave out the strike its event title carries.
    text = question if numbers(question) or not numbers(title) else f"{question} {title}"
    price = m.get("last_yes_price")
    return {"ticker": m["market_id"], "event": m.get("event_id"), "category": m.get("category") or "",
            "text": text, "end": None, "yes_ask": price, "no_ask": m.get("last_no_price"), "rules": "",
            "series": m.get("series_id")}


def poly_row(m: dict[str, Any]) -> dict[str, Any]:
    price = m.get("last_yes_price")
    return {"slug": m.get("slug") or m["market_id"], "event": m.get("event_id"), "text": m.get("question") or "",
            "end": None, "best_ask": price, "best_bid": price, "rules": "", "series": m.get("series_id")}


def enrich(found: list[dict[str, Any]], kalshi: Any, poly: Polymarket, days: float) -> list[dict[str, Any]]:
    """Fill in each candidate's resolution dates, rules and live quotes from the venues,
    then drop the ones resolving more than `days` apart."""
    out = []
    for c in found:
        try:
            km = kalshi.get(f"/markets/{c['kalshi']}").get("market", {})
            pm = poly.market_by_slug(c["polymarket"])
        except Exception:
            continue  # gone from one venue, or the slug isn't a market slug
        if sorted(str(o).lower() for o in json.loads(pm.get("outcomes") or "[]")) != ["no", "yes"]:
            continue
        c["kalshi_end"] = km.get("expected_expiration_time") or km.get("close_time")
        c["polymarket_end"] = pm.get("endDate")
        c["kalshi_rules"] = (km.get("rules_primary") or "")[:600]
        c["polymarket_rules"] = (pm.get("description") or "")[:600]
        kend, pend = when(c["kalshi_end"]), when(c["polymarket_end"])
        if kend and pend and abs((kend - pend).total_seconds()) > days * 86400:
            continue
        c["rough_cost"] = rough_cost({"yes_ask": km.get("yes_ask_dollars"), "no_ask": km.get("no_ask_dollars")},
                                     {"best_ask": pm.get("bestAsk"), "best_bid": pm.get("bestBid")})
        out.append(c)
    return out


def oddpool_candidates(client: Oddpool, topics: list[str], min_score: float, series_score: float, days: float,
                       kalshi: Any, poly: Polymarket) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    series: dict[str, dict[str, Any]] = {}
    for q in topics or [None]:
        for x in client.series(q=q):
            if str(x.get("category") or "").lower() not in SPORTS_SERIES:
                series[x["series_id"]] = x
    pairs = series_pairs(list(series.values()), series_score)
    rows: dict[str, list[dict[str, Any]]] = {}
    for sid in {s for k, p, _ in pairs for s in (k, p)}:
        rows[sid] = client.markets(sid)
    found: dict[tuple[str, str], dict[str, Any]] = {}
    for ksid, psid, score in pairs:
        kms = [kalshi_row(m) for m in rows[ksid]]
        pms = [poly_row(m) for m in rows[psid]]
        for c in candidates(kms, pms, min_score, days):
            c["series"] = {"kalshi": ksid, "polymarket": psid, "score": score}
            key = (c["kalshi"], c["polymarket"])
            if key not in found or c["score"] > found[key]["score"]:
                found[key] = c
    ranked = sorted(found.values(), key=lambda c: -c["score"])
    stats = {"series": len(series), "series_pairs": len(pairs), "markets": sum(len(v) for v in rows.values())}
    return enrich(ranked, kalshi, poly, days), stats


def write_json(path: str, data: Any) -> None:
    Path(path).write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")


def auto_approve(found: list[dict[str, Any]], min_score: float) -> list[dict[str, Any]]:
    """The best-scoring Polymarket match for each Kalshi market, at or above min_score,
    each Polymarket market used once."""
    out, kalshi, poly = [], set(), set()
    for c in sorted(found, key=lambda c: -c["score"]):
        if c["score"] < min_score or c["kalshi"] in kalshi or c["polymarket"] in poly:
            continue
        kalshi.add(c["kalshi"])
        poly.add(c["polymarket"])
        out.append({"name": c["name"], "kalshi": c["kalshi"], "polymarket": c["polymarket"], "invert": False,
                    "enabled": True, "score": c["score"], "rules": "auto-approved, unreviewed",
                    "only_kalshi": c["only_kalshi"], "only_polymarket": c["only_polymarket"]})
    return out


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--source", choices=("oddpool", "scan"), default=None,
                   help="oddpool (default when ODDPOOL_API_KEY is set) or scan")
    p.add_argument("--topics", default="", help="oddpool: comma-separated series searches (default: every series)")
    p.add_argument("--series-score", type=float, default=0.3, help="oddpool: shared-word score to pair two series (0.3)")
    p.add_argument("--categories", default="", help="scan: comma-separated Kalshi categories (default: all but Sports)")
    p.add_argument("--min-score", type=float, default=0.5, help="shared-word score, 0 to 1 (0.5)")
    p.add_argument("--days", type=float, default=7, help="max gap between the venues' resolution dates (7)")
    p.add_argument("--max-kalshi", type=int, default=500000)
    p.add_argument("--max-polymarket", type=int, default=500000)
    p.add_argument("--out", required=True, help="candidate output path outside the source checkout")
    p.add_argument("--auto-approve", type=float, default=None, metavar="SCORE",
                   help="also write every candidate scoring SCORE or more to markets.auto.json, for offline pairing benchmarks "
                        "(not read by the strategy runner)")
    a = p.parse_args(argv)
    source = a.source or ("oddpool" if os.environ.get("ODDPOOL_API_KEY") else "scan")
    kalshi, poly = Kalshi(authenticated=False), Polymarket.from_env()
    if source == "oddpool":
        topics = [t.strip() for t in a.topics.split(",") if t.strip()]
        found, stats = oddpool_candidates(Oddpool.from_env(), topics, a.min_score, a.series_score, a.days, kalshi, poly)
        summary = (f"{stats['series']} series, {stats['series_pairs']} series pairs, {stats['markets']} markets; "
                   f"{len(found)} candidates")
    else:
        cats = {c.strip() for c in a.categories.split(",") if c.strip()} or None
        kms = kalshi_markets(kalshi, cats, a.max_kalshi)
        pms = poly_markets(poly, a.max_polymarket)
        found, stats = candidates(kms, pms, a.min_score, a.days), {"kalshi_markets": len(kms), "polymarket_markets": len(pms)}
        summary = f"{len(kms)} Kalshi and {len(pms)} Polymarket yes/no markets; {len(found)} candidates"
    generated = datetime.now().isoformat(timespec="seconds")
    write_json(a.out, {"generated": generated, "source": source, **stats, "candidates": found})
    print(f"{summary} -> {a.out}")
    if a.auto_approve is not None:
        approved = auto_approve(found, a.auto_approve)
        auto = str(Path(a.out).with_name("markets.auto.json"))
        write_json(auto, {"generated": generated, "min_score": a.auto_approve, "markets": approved})
        print(f"{len(approved)} pairs scoring {a.auto_approve} or more -> {auto} (paper benchmarking; unreviewed)")
    for c in found[:25]:
        print(f"{c['score']:.2f}  cost {c['rough_cost']}  {c['kalshi']}  <->  {c['polymarket']}")
        print(f"      differ: kalshi {' '.join(c['only_kalshi'])} | polymarket {' '.join(c['only_polymarket'])}")


if __name__ == "__main__":
    main()
