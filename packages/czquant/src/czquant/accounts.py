"""Virtual subaccounts: venue-independent allocations of one monitored account.

A registered account is one real venue account (a Kalshi subaccount on one exchange,
the Polymarket US account) or one hypothetical paper account. Registration creates
its reserve pool, which holds the account's unallocated cash. Every other pool on the
account is a virtual subaccount: an ordinary cash pool that a broker binds to, funded
from the reserve (or another sub) by allocation. Nothing here moves money at a venue;
only recorded ownership changes, so the same operations work at every venue and in
paper mode, and loading or unloading a sub never touches the venue.

The account monitor reconciles the venue account against the sum of its pools and
books venue-level facts it cannot attribute to one pool (transfers, deposits) to the
reserve. Within one account:

    venue cash      == reserve cash + sum of sub cash
    venue positions == net of every pool's lots
"""
from __future__ import annotations

import json
import uuid
from decimal import Decimal
from typing import Any

from .accounting import portfolio_state, venue_of
from .store import Store, decimal, encode, utc

ZERO = Decimal(0)
VENUES = ("kalshi", "polymarket_us", "polymarket")


def reserve_id(account: str) -> str:
    return f"{account}:reserve"


def accounts(store: Store) -> list[dict[str, Any]]:
    return [{**dict(r), "metadata": json.loads(r["metadata"])}
            for r in store.db.execute("SELECT * FROM accounts ORDER BY id")]


def account(store: Store, identifier: str) -> dict[str, Any]:
    found = next((a for a in accounts(store) if a["id"] == identifier), None)
    if found is None:
        raise ValueError(f"no registered account {identifier}")
    return found


def active_pools(store: Store) -> list[dict[str, Any]]:
    return [p for p in store.pools() if not p["metadata"].get("retired_at")]


def pool(store: Store, identifier: str) -> dict[str, Any]:
    found = next((p for p in active_pools(store) if p["id"] == identifier), None)
    if found is None:
        raise ValueError(f"{identifier} is not an active cash pool")
    return found


def members(store: Store, identifier: str) -> list[dict[str, Any]]:
    """Active pools that hold a share of the account, the reserve first."""
    key = account(store, identifier)
    found = [p for p in active_pools(store) if store.account_key(p) == store.account_key(
        {"venue": key["venue"], "mode": key["mode"], "metadata": {**key["metadata"], "account": identifier}})]
    return sorted(found, key=lambda p: (not p["metadata"].get("reserve"), p["id"]))


def _register(store: Store, identifier: str, venue: str, mode: str, cash: Decimal,
              evidence: dict[str, Any], **scope: Any) -> None:
    if not identifier or ":" in identifier:
        raise ValueError("an account ID is required and cannot contain ':'")
    if venue not in VENUES:
        raise ValueError(f"unknown venue {venue}")
    if store.db.execute("SELECT 1 FROM accounts WHERE id=?", (identifier,)).fetchone():
        raise ValueError(f"account {identifier} is already registered")
    probe = {"venue": venue, "mode": mode, "metadata": {**scope, "account": identifier}}
    if mode == "live" and any(store.account_key({"venue": a["venue"], "mode": a["mode"], "metadata": a["metadata"]})
                              == store.account_key(probe) for a in accounts(store)):
        raise ValueError("that venue account is already registered")
    reserve = reserve_id(identifier)
    store.db.execute("INSERT INTO accounts VALUES(?,?,?,?,?)", (identifier, venue, mode, utc(), encode(scope)))
    store.create_pool(reserve, venue, "0", mode, account=identifier, reserve=True, fund=False, **scope)
    store.append("transaction", {"reason": "funding", "purpose": "account_opening", "venue": venue,
                 "cash_delta": str(cash), "external_delta": str(cash), "fee": "0", "legs": [],
                 "evidence": evidence}, reserve, source=f"account-opening:{identifier}")


def register_paper(store: Store, identifier: str, venue: str, cash: Any) -> None:
    """A hypothetical account: its reserve opens with `cash`, for subs to draw on."""
    amount = decimal(cash)
    if amount < 0:
        raise ValueError("paper account cash cannot be negative")
    with store.transaction():
        _register(store, identifier, venue, "paper", amount, {"hypothetical": True})


def register_kalshi(store: Store, api: Any, identifier: str, subaccount: int, exchange: int = 0,
                    primary: bool = False) -> None:
    """Register a Kalshi account. Its reserve opens with the cash no pool there records:
    all of a flat account, or the remainder beside pools mapped before registration
    (which become members of it)."""
    from .operator import _check_account, _opening_kalshi
    with store.transaction():
        _check_account(subaccount, primary)
        cash, evidence = _opening_kalshi(store, api, subaccount, exchange, share=True)
        _register(store, identifier, "kalshi", "live", cash, evidence,
                  subaccount=subaccount, exchange_index=exchange)


def _pmus_cash(balance: dict[str, Any]) -> Decimal:
    return decimal(balance["currentBalance"]) - decimal(balance.get("depositReservation") or 0)


def pending_advances(store: Store) -> Decimal:
    """Advances the monitor booked on Polymarket US deposits that are still clearing:
    neither completed (then booked in full) nor reversed."""
    total = ZERO
    for row in store.db.execute("SELECT source_key, data FROM events WHERE source_key LIKE 'pmus-advance:%' "
                                "AND source_key NOT LIKE '%:reversed'"):
        txid = row["source_key"].split(":", 1)[1]
        settled = store.db.execute(
            "SELECT 1 FROM events WHERE source_key=? OR source_key LIKE ?",
            (row["source_key"] + ":reversed", f"pmus-activity:%:{txid}")).fetchone()
        if not settled:
            total += decimal(json.loads(row["data"])["cash_delta"])
    return total


def register_polymarket_us(store: Store, api: Any, identifier: str) -> None:
    """Register the Polymarket US account. A pool mapped to it before registration
    becomes a member; the reserve opens with the cash none of its pools records."""
    from .operator import active_live
    from .polymarket_us import position_quantity, slug_of
    if not api.authenticated:
        raise ValueError("Polymarket US credentials are required")
    with store.transaction():
        legacy = [p for p in active_live(store) if p["venue"] == "polymarket_us"]
        held = {slug: position_quantity(v) for slug, v in api.positions().items() if position_quantity(v)}
        recorded: dict[str, Decimal] = {}
        for p in legacy:
            for lot in portfolio_state(store, p["id"]).lots.values():
                slug = slug_of(lot["market"])
                recorded[slug] = recorded.get(slug, ZERO) + lot["quantity"] * (1 if lot["side"] == "yes" else -1)
        if held != {k: v for k, v in recorded.items() if v}:
            raise ValueError("the Polymarket US account holds positions its pools do not record: reconcile first")
        known = {r[0] for r in store.db.execute("SELECT venue_id FROM orders WHERE venue_id IS NOT NULL")}
        if any(str(o.get("id") or o.get("orderId")) not in known for o in api.open_orders()):
            raise ValueError("the Polymarket US account has open orders its pools do not record")
        balance = api.balance()
        free = _pmus_cash(balance) - sum((portfolio_state(store, p["id"]).venue_cash.get("polymarket_us", ZERO)
                                         for p in legacy), ZERO)
        if free < 0:
            raise ValueError("Polymarket US holds less than its pools record: reconcile first")
        _register(store, identifier, "polymarket_us", "live", free,
                  {"balance": balance, "shared_with": [p["id"] for p in legacy]})


def open_sub(store: Store, identifier: str, account_id: str, cash: Any = "0",
             markets: list[str] | None = None, source: str | None = None, *, api: Any = None) -> None:
    """Load a virtual subaccount: a new pool on the account, funded by allocation from
    the reserve (or `source`, another pool on the account). Works while other pools on
    the account trade, since nothing changes at the venue.

    `markets` optionally limits what its broker may trade: exact tickers, or prefixes
    ending in '*'. It is an assertion, not routing: orders outside it are refused."""
    acct = account(store, account_id)
    amount = decimal(cash)
    if amount < 0:
        raise ValueError("cash cannot be negative")
    if markets is not None and not all(isinstance(m, str) and m for m in markets):
        raise ValueError("markets are non-empty ticker strings or prefixes ending in '*'")
    with store.transaction():
        parent = source or reserve_id(account_id)
        extra = {"markets": list(markets)} if markets else {}
        store.create_pool(identifier, acct["venue"], "0", acct["mode"], account=account_id, parent=parent,
                          fund=False, **acct["metadata"], **extra)
        store.append("observation", {"kind": "sub_opened", "account": account_id, "parent": parent,
                                     "cash": str(amount), **extra}, identifier)
        if amount:
            allocate(store, parent, identifier, amount, api=api)


def free_cash(store: Store, p: dict[str, Any]) -> Decimal:
    state = portfolio_state(store, p["id"])
    return state.venue_cash.get(p["venue"], ZERO) - store.reserved(p["id"])


def _check_live_cash(store: Store, p: dict[str, Any], api: Any = None) -> dict[str, Any] | None:
    """Read current account backing without making monitor health a strategy API.

    Run under the allocation transaction. Unrecorded cash is not allocated here;
    a shortfall must be reconciled before any recorded ownership is reassigned.
    """
    if p["mode"] != "live":
        return
    group = [q for q in active_pools(store) if store.account_key(q) == store.account_key(p)]
    balances = [portfolio_state(store, q["id"]).venue_cash.get(p["venue"], ZERO) for q in group]
    if p["venue"] == "kalshi":
        from .broker import scope
        from .kalshi import Kalshi
        from .operator import _balance
        api = api or Kalshi()
        if not api.key_id:
            raise ValueError("live allocation requires Kalshi credentials")
        balance, observed = _balance(api, {**scope(p["metadata"]),
                                          "exchange_index": p["metadata"].get("exchange_index", 0)})
    elif p["venue"] == "polymarket_us":
        from .polymarket_us import PolymarketUS
        api = api or PolymarketUS.from_env(authenticated=True)
        balance = api.balance()
        # Cash still clearing backs the pools only as far as the monitor booked it as an
        # advance (buying power), and never beyond what Polymarket holds in reservation.
        clearing = decimal(balance.get("depositReservation") or 0)
        observed = _pmus_cash(balance) + min(clearing, pending_advances(store))
    else:
        raise ValueError(f"live allocation is unsupported for {p['venue']}")
    recorded = sum(balances, ZERO)
    if observed < recorded or any(cash < 0 for cash in balances):
        raise ValueError(f"account cash ${observed} does not back its pools (${recorded}): reconcile first")
    return {"balance": balance, "recorded_cash": str(recorded), "observed_cash": str(observed),
            "pools": [q["id"] for q in group], "observed_at": utc()}


def allocate(store: Store, source: str, target: str, dollars: Any, *, api: Any = None) -> None:
    """Move recorded cash between two pools on one account (any venue, either mode).
    No money moves at the venue; only each pool's share of the account changes."""
    dollars = decimal(dollars)
    if dollars <= 0 or source == target:
        raise ValueError("a positive amount between two pools is required")
    with store.transaction():
        a, b = pool(store, source), pool(store, target)
        key = store.account_key(a)
        if key is None or key != store.account_key(b):
            raise ValueError(f"{source} and {target} trade different accounts: use `transfer` to move money")
        free = free_cash(store, a)
        if dollars > free:
            label = "Kalshi " if a["venue"] == "kalshi" else ""
            raise ValueError(f"{source} has ${free} of free {label}cash")
        backing = _check_live_cash(store, a, api)
        tag, at = uuid.uuid4().hex, utc()
        for pid, delta in ((source, -dollars), (target, dollars)):
            store.append("transaction", {"reason": "funding", "purpose": "allocation", "venue": a["venue"],
                "cash_delta": str(delta), "external_delta": str(delta), "fee": "0", "legs": [],
                "evidence": {"from": source, "to": target, **({"backing": backing} if backing else {})}},
                pid, source=f"allocate:{tag}:{pid}", at=at)


def move_lots(store: Store, source: str, target: str, market: str, side: str,
              quantity: Any = None) -> Decimal:
    """Hand open contracts from one pool to another on the same account, at cost basis.

    Each moved lot is a pair of position_transfer transactions: capital leaves the
    source and enters the target at the lot's basis, without cash or realized P&L.
    The venue position is unchanged. Returns the quantity moved."""
    if side not in ("yes", "no"):
        raise ValueError("side is yes or no")
    with store.transaction():
        a, b = pool(store, source), pool(store, target)
        if source == target or store.account_key(a) is None or store.account_key(a) != store.account_key(b):
            raise ValueError("lots move only between two pools on one account")
        if b["metadata"].get("reserve"):
            raise ValueError("the reserve holds cash only")
        busy = [r for r in store.db.execute("SELECT request FROM orders WHERE portfolio_id IN (?,?) AND "
                                            "status IN ('pending','unknown','acknowledged')", (source, target))
                if json.loads(r["request"])["ticker"] == market]
        if busy:
            raise ValueError(f"{market} has orders open in {source} or {target}")
        opposite = "no" if side == "yes" else "yes"
        if any(l["market"] == market and l["side"] == opposite for l in portfolio_state(store, target).lots.values()):
            raise ValueError(f"{target} holds the opposite side of {market}")
        lots = [l for l in portfolio_state(store, source).lots.values() if l["market"] == market and l["side"] == side]
        held = sum((l["quantity"] for l in lots), ZERO)
        want = held if quantity is None else decimal(quantity)
        if want <= 0 or want > held:
            raise ValueError(f"{source} holds {held} {side} of {market}")
        remaining, tag, at, moved = want, uuid.uuid4().hex, utc(), ZERO
        for lot in [dict(l) for l in lots]:
            if not remaining:
                break
            take = min(remaining, lot["quantity"])
            basis = lot["cost"] * take / lot["quantity"]
            price = str(basis / take)
            common = {"reason": "position_transfer", "venue": a["venue"], "cash_delta": "0", "fee": "0",
                      "context": lot["context"], "evidence": {"from": source, "to": target, "lot_id": lot["record_id"]}}
            store.append("transaction", {**common, "external_delta": str(-basis), "legs": [
                {"market": market, "side": side, "quantity_delta": str(-take), "price": price,
                 "lot_id": lot["record_id"]}]}, source, source=f"move:{tag}:{lot['record_id']}:out", at=at)
            store.append("transaction", {**common, "external_delta": str(basis), "legs": [
                {"market": market, "side": side, "quantity_delta": str(take), "price": price}]},
                target, source=f"move:{tag}:{lot['record_id']}:in", at=at)
            remaining -= take
            moved += take
        return moved


def close_sub(store: Store, identifier: str, to: str | None = None, *, api: Any = None) -> Decimal:
    """Unload a virtual subaccount: it must have no orders open and hold nothing (let
    lots settle, or move them with `move_lots`). Its cash is allocated to `to`, its
    parent, or the reserve, and it is retired. Its records stay. Returns the cash moved."""
    with store.transaction():
        p = pool(store, identifier)
        if p["metadata"].get("reserve"):
            raise ValueError("the reserve is the account itself; it is not closed")
        acct_id = p["metadata"].get("account")
        if not acct_id:
            raise ValueError(f"{identifier} is not a virtual subaccount")
        pending = store.db.execute("SELECT 1 FROM orders WHERE portfolio_id=? AND "
                                   "status IN ('pending','unknown','acknowledged') LIMIT 1", (identifier,)).fetchone()
        if pending or store.reserved(identifier):
            raise ValueError(f"{identifier} has orders open")
        held = sorted({l["market"] for l in portfolio_state(store, identifier).lots.values()})
        if held:
            raise ValueError(f"{identifier} still holds {held}: let them settle or move them with move-lots")
        target = to or p["metadata"].get("parent") or reserve_id(acct_id)
        if not any(q["id"] == target for q in active_pools(store)):
            target = reserve_id(acct_id)
        cash = portfolio_state(store, identifier).venue_cash.get(p["venue"], ZERO)
        if cash < 0:
            raise ValueError(f"{identifier} records negative cash ${cash}: reconcile first")
        if cash:
            allocate(store, identifier, target, cash, api=api)
        metadata = {**p["metadata"], "retired_at": utc()}
        store.db.execute("UPDATE portfolios SET metadata=? WHERE id=?", (encode(metadata), identifier))
        store.append("observation", {"kind": "sub_closed", "to": target, "cash": str(cash)}, identifier)
        return cash


def book_reserve_difference(store: Store, api: Any, account_id: str, note: str) -> Decimal:
    """Book an operator-confirmed Kalshi deposit or withdrawal to the reserve.

    Kalshi lists subaccount transfers but not bank deposits, so a deposit to the primary
    account appears only as a cash difference. With every position reconciled and no
    order unresolved, the whole difference is booked as external funding (never profit),
    with the balance and the operator's note as evidence."""
    from .broker import scope
    from .operator import _balance, kalshi_cash, kalshi_positions
    if not note:
        raise ValueError("say what the difference is (the deposit or withdrawal) in the note")
    with store.transaction():
        acct = account(store, account_id)
        if acct["venue"] != "kalshi" or acct["mode"] != "live":
            raise ValueError("only live Kalshi accounts need this; other venues report their funding")
        group = members(store, account_id)
        ids = tuple(p["id"] for p in group)
        pending = store.db.execute(f"SELECT COUNT(*) FROM orders WHERE status IN ('pending','unknown','acknowledged') "
                                   f"AND portfolio_id IN ({','.join('?' for _ in ids)})", ids).fetchone()[0]
        if pending:
            raise ValueError("orders on the account are unresolved: let the monitor book them first")
        params = {**scope(acct["metadata"]), "exchange_index": acct["metadata"].get("exchange_index", 0)}
        positions = list(api.pages("/portfolio/positions", "market_positions", **params))
        held = {r["ticker"]: decimal(r.get("position_fp", "0")) for r in positions if decimal(r.get("position_fp", "0"))}
        if held != kalshi_positions(store, group):
            raise ValueError("positions do not reconcile: the difference may be a trade, not funding")
        balance, observed = _balance(api, params)
        difference = observed - sum((kalshi_cash(store, p) for p in group), ZERO)
        if difference:
            store.append("transaction", {"reason": "funding", "purpose": "deposit" if difference > 0 else "withdrawal",
                "venue": "kalshi", "cash_delta": str(difference), "external_delta": str(difference), "fee": "0",
                "legs": [], "evidence": {"balance": balance, "note": note}},
                reserve_id(account_id), source=f"reserve-difference:{account_id}:{uuid.uuid4().hex}")
        return difference


def summary(store: Store, account_id: str) -> dict[str, Any]:
    acct = account(store, account_id)
    rows = []
    for p in members(store, account_id):
        state = portfolio_state(store, p["id"])
        rows.append({"pool": p["id"], "reserve": bool(p["metadata"].get("reserve")),
                     "cash": str(state.venue_cash.get(p["venue"], ZERO)), "reserved": str(store.reserved(p["id"])),
                     "lots": len(state.lots), "markets": p["metadata"].get("markets")})
    total = sum((decimal(r["cash"]) for r in rows), ZERO)
    return {"account": account_id, "venue": acct["venue"], "mode": acct["mode"], "scope": acct["metadata"],
            "recorded_cash": str(total), "pools": rows}


def market_in_scope(markets: list[str] | None, ticker: str) -> bool:
    if not markets:
        return True
    return any(ticker.startswith(m[:-1]) if m.endswith("*") else ticker == m for m in markets)


def netting_conflict(store: Store, p: dict[str, Any], ticker: str, opening_side: str) -> str | None:
    """Another pool on the same account that holds, or may buy, the opposite side
    of `ticker`. The venue would net the two positions away, so the ledger's lots
    would no longer match the account."""
    key = store.account_key(p)
    if key is None:
        return None
    opposite = "no" if opening_side == "yes" else "yes"
    for other in active_pools(store):
        if other["id"] == p["id"] or store.account_key(other) != key:
            continue
        from .broker import Broker
        net, bids, asks = Broker(store, other["id"])._inventory(ticker)
        if (opposite == "no" and net - asks < 0) or (opposite == "yes" and net + bids > 0):
            return other["id"]
    return None


__all__ = ["accounts", "account", "members", "register_paper", "register_kalshi", "register_polymarket_us",
           "open_sub", "close_sub", "allocate", "move_lots", "book_reserve_difference", "summary",
           "reserve_id", "venue_of"]
