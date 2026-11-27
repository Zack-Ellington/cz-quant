"""Explicit portfolio creation and offline inspection of the common ledger."""
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from .accounting import active_events, combine_states, portfolio_state
from .dashboard import measure
from .report import reporting_units
from .store import Store, decimal


def create(store: Store, config: dict[str, Any]) -> str:
    """Atomically create paper pools or group existing pools; never top up on retry."""
    if set(config) - {'id', 'label', 'pools'}:
        raise ValueError('portfolio config accepts only id, label and pools')
    identifier, pools = config.get('id'), config.get('pools')
    if not isinstance(identifier, str) or not identifier.strip():
        raise ValueError('portfolio id must be a nonempty string')
    if not isinstance(config.get('label', ''), str):
        raise ValueError('portfolio label must be a string')
    if not isinstance(pools, list) or not pools:
        raise ValueError('pools must be a nonempty list of existing IDs or new paper pool definitions')
    with store.transaction():
        if any(p['id'] == identifier for p in reporting_units(store)):
            raise ValueError(f'portfolio or pool already exists: {identifier}')
        members = []
        for item in pools:
            if isinstance(item, str):
                members.append(item)
                continue
            if not isinstance(item, dict) or set(item) != {'id', 'venue', 'cash'}:
                raise ValueError('new paper pools require exactly id, venue and cash; use an existing ID for live pools')
            if not isinstance(item['id'], str) or not item['id'].strip():
                raise ValueError('pool id must be a nonempty string')
            if item['venue'] not in ('kalshi', 'polymarket', 'polymarket_us', 'polymarket-us'):
                raise ValueError('unknown pool venue')
            cash = decimal(str(item['cash']))
            if not cash.is_finite() or cash < 0:
                raise ValueError('paper cash must be a finite nonnegative amount')
            store.create_pool(item['id'], item['venue'].replace('-', '_'), str(cash))
            members.append(item['id'])
        store.create_portfolio(identifier, members, label=config.get('label', ''))
    return identifier


def details(store: Store, identifier: str, event_limit: int = 0) -> dict[str, Any]:
    """Read one consistent snapshot; positions stay attributed to their owning pools."""
    with store.transaction():
        unit = next((p for p in reporting_units(store) if p['id'] == identifier), None)
        if unit is None:
            raise ValueError(f'unknown portfolio or pool: {identifier}')
        now = datetime.now(timezone.utc)
        events = store.events(until=now.isoformat())
        marks = {e['data']['ticker']: e for e in active_events(events) if e['type'] == 'market'}
        states = {pid: portfolio_state(store, pid) for pid in unit['pools']}
        state = combine_states(list(states.values()))
        pnl = measure(state, marks, now)
        reserved = sum((store.reserved(pid) for pid in states), Decimal(0))
        pnl.update(reserved=reserved, available=state.cash - reserved if unit['kind'] == 'pool' else None)
        positions = [{'pool_id': pid, **lot} for pid, own in states.items() for lot in own.lots.values()]
        result = {**unit, 'pnl': pnl, 'positions': positions}
        if event_limit:
            own_events = [e for e in events if e['portfolio_id'] in states]
            result.update(events=own_events[-event_limit:], event_count=len(own_events))
        return result
