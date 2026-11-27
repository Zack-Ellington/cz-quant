"""Explicit portfolio creation and offline inspection of the common ledger."""
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from .accounting import active_events, combine_states, portfolio_state
from .dashboard import measure
from .report import reporting_units
from .store import Store, decimal, encode, utc


def create(store: Store, config: dict[str, Any]) -> str:
    """Atomically fund pools and group them; never top up existing IDs on retry."""
    from .accounts import open_sub

    if set(config) - {'id', 'label', 'pools'}:
        raise ValueError('portfolio config accepts only id, label and pools')
    identifier, pools = config.get('id'), config.get('pools')
    if not isinstance(identifier, str) or not identifier.strip():
        raise ValueError('portfolio id must be a nonempty string')
    if not isinstance(config.get('label', ''), str):
        raise ValueError('portfolio label must be a string')
    if not isinstance(pools, list) or not pools:
        raise ValueError('pools must be a nonempty list of existing IDs or new pool definitions')
    with store.transaction():
        if any(p['id'] == identifier for p in reporting_units(store)):
            raise ValueError(f'portfolio or pool already exists: {identifier}')
        members = []
        for item in pools:
            if isinstance(item, str):
                pool = next((p for p in store.pools() if p['id'] == item), None)
                if pool is not None and pool['metadata'].get('retired_at'):
                    raise ValueError(f'cash pool is retired: {item}')
                members.append(item)
                continue
            if not isinstance(item, dict):
                raise ValueError('a pool must be an existing ID or a mapping')
            account_backed = 'account' in item
            required = {'id', 'account', 'cash'} if account_backed else {'id', 'venue', 'cash'}
            if set(item) != required:
                raise ValueError('new pools require exactly id, cash and either account or venue (paper only)')
            if not isinstance(item['id'], str) or not item['id'].strip():
                raise ValueError('pool id must be a nonempty string')
            cash = decimal(str(item['cash']))
            if cash < 0:
                raise ValueError('cash must be a nonnegative amount')
            if account_backed:
                if not isinstance(item['account'], str) or not item['account'].strip():
                    raise ValueError('account must be a registered account ID')
                open_sub(store, item['id'], item['account'], str(cash))
            else:
                if item['venue'] not in ('kalshi', 'polymarket', 'polymarket_us', 'polymarket-us'):
                    raise ValueError('unknown pool venue')
                store.create_pool(item['id'], item['venue'].replace('-', '_'), str(cash))
            members.append(item['id'])
        store.create_portfolio(identifier, members, label=config.get('label', ''))
    return identifier


def delete(store: Store, identifier: str) -> None:
    """Retire flat, unshared allocations atomically; retain all economic history."""
    from .accounts import close_sub, reserve_id

    with store.transaction():
        groups = store.reporting_portfolios()
        group = next((p for p in groups if p['id'] == identifier), None)
        if group is None:
            raise ValueError(f'unknown portfolio: {identifier}; delete requires a portfolio, not a pool ID')
        if group.get('retired_at'):
            return
        members = set(group['pools'])
        shared = [p['id'] for p in groups if p['id'] != identifier and not p.get('retired_at')
                  and members.intersection(p['pools'])]
        if shared:
            raise ValueError(f'cash pools are shared with active portfolios: {", ".join(shared)}')
        pools = [p for p in store.pools() if p['id'] in members]
        for pool in pools:
            pid, metadata = pool['id'], pool['metadata']
            state = portfolio_state(store, pid)
            pending = store.db.execute("SELECT 1 FROM orders WHERE portfolio_id=? AND "
                                       "status IN ('pending','unknown','acknowledged') LIMIT 1", (pid,)).fetchone()
            if state.lots or state.prepaid or pending or store.reserved(pid):
                raise ValueError(f'{pid} has positions, prepaid capital or unresolved orders; reconcile and flatten before deletion')
            if state.cash < 0:
                raise ValueError(f'{pid} has negative cash; reconcile before deletion')
            if metadata.get('reserve'):
                raise ValueError(f'{pid} is an account reserve and cannot be retired')
            if pool['mode'] == 'live' and not metadata.get('account') and not metadata.get('retired_at'):
                raise ValueError(f'{pid} is a directly mapped live pool; retire its venue mapping before deleting the portfolio')
        for pool in pools:
            pid, metadata = pool['id'], pool['metadata']
            if metadata.get('retired_at'):
                continue
            if metadata.get('account'):
                close_sub(store, pid, to=reserve_id(metadata['account']))
            else:
                cash = portfolio_state(store, pid).cash
                if cash:
                    store.append('transaction', {'reason': 'funding', 'purpose': 'retirement',
                                 'cash_delta': str(-cash), 'external_delta': str(-cash), 'fee': '0', 'legs': []}, pid)
                store.db.execute('UPDATE portfolios SET metadata=? WHERE id=?',
                                 (encode({**metadata, 'retired_at': utc()}), pid))
                store.append('observation', {'kind': 'retired', 'portfolio': identifier}, pid)
        store.append('observation', {'kind': 'portfolio_retired', 'portfolio': identifier,
                                    'pools': group['pools']})


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
