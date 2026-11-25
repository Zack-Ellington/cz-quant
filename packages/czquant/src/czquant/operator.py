"""Explicit account administration. No allocation daemon or implicit transfers.

A Kalshi account (one subaccount on one exchange) can back several live portfolios.
Each portfolio records its own share of the account's cash, and the account monitor
reconciles the account against their sum. Cash moves between portfolios on one account
only through `allocate`; a retired portfolio releases its cash back to the account,
where the next portfolio mapped there opens with it.
"""
from __future__ import annotations
import json
import uuid
from decimal import Decimal
from typing import Any
from .accounting import portfolio_state, venue_of
from .broker import PRIMARY, scope
from .store import Store, decimal, encode, utc

ZERO = Decimal(0)


def active_live(store: Store) -> list[dict[str, Any]]:
    """Live portfolios that still own their accounts (retired ones are history only)."""
    return [p for p in store.portfolios() if p['mode'] == 'live' and not p['metadata'].get('retired_at')]


def account_of(metadata: dict[str, Any]) -> tuple[int, int]:
    return metadata['subaccount'], metadata.get('exchange_index', 0)


def kalshi_cash(store: Store, p: dict[str, Any]) -> Decimal:
    """A live portfolio's recorded share of its Kalshi account's cash."""
    state = portfolio_state(store, p['id'])
    return state.venue_cash.get('kalshi', ZERO) if p['metadata'].get('venues') else state.cash


def kalshi_positions(store: Store, portfolios: list[dict[str, Any]]) -> dict[str, Decimal]:
    """Net YES position per Kalshi market across portfolios, as the account would show it."""
    net: dict[str, Decimal] = {}
    for p in portfolios:
        for lot in portfolio_state(store, p['id']).lots.values():
            if venue_of(lot['market']) == 'kalshi':
                sign = 1 if lot['side'] == 'yes' else -1
                net[lot['market']] = net.get(lot['market'], ZERO) + lot['quantity'] * sign
    return {k: v for k, v in net.items() if v}


def _balance(api: Any, params: dict[str, Any]) -> tuple[dict[str, Any], Decimal]:
    balance = api.get('/portfolio/balance', params)
    cash = decimal(balance['balance_dollars']) if 'balance_dollars' in balance else decimal(balance['balance']) / 100
    return balance, cash


def _check_account(subaccount: int, primary: bool) -> None:
    if subaccount == PRIMARY and not primary:
        raise ValueError('Subaccount 0 is the primary account: pass --primary-account to trade it from this portfolio')
    if subaccount != PRIMARY and not 1 <= subaccount <= 63:
        raise ValueError('Use a dedicated numbered subaccount (1-63), or 0 with --primary-account')


def _sharers(store: Store, subaccount: int, exchange: int, exclude: str | None = None) -> list[dict[str, Any]]:
    return [p for p in active_live(store)
            if account_of(p['metadata']) == (subaccount, exchange) and p['id'] != exclude]


def _opening_kalshi(store: Store, api: Any, subaccount: int, exchange: int, share: bool,
                    exclude: str | None = None) -> tuple[Decimal, dict[str, Any]]:
    """The Kalshi cash a portfolio opens with: all of a flat account, or on a shared
    account the cash none of its other portfolios records."""
    sharers = _sharers(store, subaccount, exchange, exclude)
    if sharers and not share:
        names = ', '.join(p['id'] for p in sharers)
        raise ValueError(f'Kalshi subaccount {subaccount} is already mapped to {names}: '
                         'pass --share-account to trade it from this portfolio too')
    params = {**scope({'subaccount': subaccount}), 'exchange_index': exchange}
    positions = list(api.pages('/portfolio/positions', 'market_positions', **params))
    orders = list(api.pages('/portfolio/orders', 'orders', status='resting', **params))
    held = {r['ticker']: decimal(r.get('position_fp', '0')) for r in positions if decimal(r.get('position_fp', '0'))}
    if not sharers and (held or orders):
        raise ValueError('Map a flat Kalshi subaccount with no resting orders; '
                         'existing inventory needs an explicit opening ledger')
    known = {r[0] for r in store.db.execute('SELECT venue_id FROM orders WHERE venue_id IS NOT NULL')}
    if held != kalshi_positions(store, sharers) or any(o.get('order_id') not in known for o in orders):
        raise ValueError('The shared account holds positions or resting orders its portfolios do not record: '
                         'reconcile them first')
    balance, cash = _balance(api, params)
    free = cash - sum((kalshi_cash(store, p) for p in sharers), ZERO)
    if free < 0:
        raise ValueError(f'Kalshi holds ${cash} but its portfolios record more: reconcile them first')
    return free, {'balance': balance, 'positions': positions, 'shared_with': [p['id'] for p in sharers]}


def map_live(store: Store, api: Any, portfolio: str, subaccount: int, exchange: int = 0,
             primary: bool = False, share: bool = False) -> None:
    """Map a Kalshi account to a new live portfolio.

    On a flat account the portfolio opens at the account's cash. With `share=True` it can
    join an account other live portfolios already trade; it opens with the cash none of
    them records (often $0: use `allocate` to give it some). Subaccount 0, the primary
    account, needs `primary=True`.
    """
    _check_account(subaccount, primary)
    cash, evidence = _opening_kalshi(store, api, subaccount, exchange, share)
    with store.transaction():
        store.create(portfolio, mode='live', subaccount=subaccount, exchange_index=exchange)
        store.append('transaction', {'reason': 'funding', 'venue': 'kalshi', 'cash_delta': str(cash),
            'external_delta': str(cash), 'fee': '0', 'legs': [], 'evidence': evidence}, portfolio,
            source=f'opening-live:{portfolio}')


def transfer(store: Store, api: Any, identifier: str, source: int, target: int, cents: int, exchange: int=0) -> dict[str, Any]:
    uuid.UUID(identifier)
    if cents <= 0 or source==target:
        raise ValueError('positive amount and distinct subaccounts required')
    request={'client_transfer_id':identifier,'from_subaccount':source,'to_subaccount':target,
             'amount_cents':cents,'exchange_index':exchange}
    store.append('transfer_intent',request,source='transfer-intent:'+identifier)
    # Stable client ID makes an explicit operator retry safe; no automatic write retries.
    result=api.request('POST','/portfolio/subaccounts/transfer',request)
    store.append('transfer_ack',{'request':request,'response':result},source='transfer-ack:'+identifier)
    return result


def map_live_cross(store: Store, kalshi: Any, polymarket_us: Any, portfolio: str, subaccount: int,
                   exchange: int = 0, primary: bool = False, share: bool = False) -> None:
    """Map a Kalshi account and a flat Polymarket US account to one new live portfolio,
    opening it at both accounts' cash (each booked to its own venue). The Kalshi side
    follows `map_live`'s rules, sharing included.

    Called on an existing live portfolio, it moves the portfolio's Kalshi side to another
    Kalshi account instead (see `remap_kalshi`); its Polymarket US side is unchanged.

    The Polymarket US account has no subaccounts, so the whole account belongs to this
    portfolio: manual trades, deposits the monitor can't attribute, or another program
    trading it show up as mismatches and halt the trader.
    """
    _check_account(subaccount, primary)
    if any(p['id'] == portfolio for p in store.portfolios()):
        remap_kalshi(store, kalshi, portfolio, subaccount, exchange, share)
        return
    if any('polymarket_us' in (p['metadata'].get('venues') or []) for p in active_live(store)):
        raise ValueError('the Polymarket US account is already mapped to a live portfolio')
    if not polymarket_us.authenticated:
        raise ValueError('Polymarket US credentials are required (POLYMARKET_US_KEY_ID, POLYMARKET_US_SECRET_KEY_PATH)')
    opening, evidence = _opening_kalshi(store, kalshi, subaccount, exchange, share)
    from .polymarket_us import position_quantity
    pm_positions = {k: v for k, v in polymarket_us.positions().items() if position_quantity(v)}
    if pm_positions or polymarket_us.open_orders():
        raise ValueError('Map a flat Polymarket US account with no open orders')
    pm_balance = polymarket_us.balance()
    pm_cash = decimal(pm_balance['currentBalance'])
    with store.transaction():
        store.create(portfolio, mode='live', subaccount=subaccount, exchange_index=exchange,
                     venues=['kalshi', 'polymarket_us'])
        store.append('transaction', {'reason': 'funding', 'venue': 'kalshi', 'cash_delta': str(opening),
            'external_delta': str(opening), 'fee': '0', 'legs': [], 'evidence': evidence}, portfolio,
            source=f'opening-live:{portfolio}')
        store.append('transaction', {'reason': 'funding', 'venue': 'polymarket_us', 'cash_delta': str(pm_cash),
            'external_delta': str(pm_cash), 'fee': '0', 'legs': [], 'evidence': {'balance': pm_balance}},
            portfolio, source=f'opening-live-polymarket-us:{portfolio}')


def _active(store: Store, portfolio: str) -> dict[str, Any]:
    p = next((p for p in active_live(store) if p['id'] == portfolio), None)
    if p is None:
        raise ValueError(f'{portfolio} is not an active live portfolio')
    return p


def _kalshi_reserved(store: Store, portfolio: str) -> Decimal:
    return sum((decimal(r['reserved']) for r in store.db.execute(
        'SELECT request,reserved FROM orders WHERE portfolio_id=?', (portfolio,))
        if venue_of(json.loads(r['request'])['ticker']) == 'kalshi'), ZERO)


def _release(store: Store, portfolio: str, venue: str, cash: Decimal, purpose: str,
             evidence: dict[str, Any], tag: str) -> None:
    """Take cash off a portfolio's books; the money stays on the account, unowned."""
    store.append('transaction', {'reason': 'funding', 'purpose': purpose, 'venue': venue,
        'cash_delta': str(-cash), 'external_delta': str(-cash), 'fee': '0', 'legs': [], 'evidence': evidence},
        portfolio, source=f'release:{portfolio}:{venue}:{tag}')


def remap_kalshi(store: Store, api: Any, portfolio: str, subaccount: int, exchange: int = 0,
                 share: bool = False) -> None:
    """Move a live portfolio's Kalshi side to another Kalshi account.

    The portfolio must hold nothing on Kalshi and have no Kalshi orders open. Any Kalshi
    cash it records is released (the money stays on the old account, unowned), and it
    opens on the new account like a fresh mapping: all of a flat account's cash, or with
    `share` whatever the account's other portfolios don't record.
    """
    p = _active(store, portfolio)
    old = account_of(p['metadata'])
    if old == (subaccount, exchange):
        raise ValueError(f'{portfolio} already trades Kalshi subaccount {subaccount}')
    if kalshi_positions(store, [p]) or _kalshi_reserved(store, portfolio):
        raise ValueError(f'{portfolio} still holds Kalshi positions or has Kalshi orders open')
    left = kalshi_cash(store, p)
    if left and _sharers(store, *old, exclude=portfolio):
        raise ValueError(f'{portfolio} records ${left} on a shared account: allocate it to another portfolio there first')
    cash, evidence = _opening_kalshi(store, api, subaccount, exchange, share, exclude=portfolio)
    tag = uuid.uuid4().hex
    with store.transaction():
        if left:
            _release(store, portfolio, 'kalshi', left, 'remap', {'subaccount': old[0], 'exchange_index': old[1]}, tag)
        metadata = {**p['metadata'], 'subaccount': subaccount, 'exchange_index': exchange}
        store.db.execute('UPDATE portfolios SET metadata=? WHERE id=?', (encode(metadata), portfolio))
        store.append('observation', {'kind': 'remapped', 'from': list(old), 'to': [subaccount, exchange]}, portfolio)
        store.append('transaction', {'reason': 'funding', 'venue': 'kalshi', 'cash_delta': str(cash),
            'external_delta': str(cash), 'fee': '0', 'legs': [], 'evidence': evidence}, portfolio,
            source=f'opening-live:{portfolio}:{tag}')


def allocate(store: Store, source: str, target: str, dollars: Decimal) -> None:
    """Move recorded Kalshi cash between two live portfolios on the same Kalshi account.
    No money moves at Kalshi; only each portfolio's share of the account changes."""
    if dollars <= 0 or source == target:
        raise ValueError('a positive amount between two portfolios is required')
    a, b = _active(store, source), _active(store, target)
    if account_of(a['metadata']) != account_of(b['metadata']):
        raise ValueError(f'{source} and {target} trade different Kalshi accounts: use `transfer` to move money')
    free = kalshi_cash(store, a) - _kalshi_reserved(store, source)
    if dollars > free:
        raise ValueError(f'{source} has ${free} of free Kalshi cash')
    tag = uuid.uuid4().hex
    with store.transaction():
        for portfolio, delta in ((source, -dollars), (target, dollars)):
            store.append('transaction', {'reason': 'funding', 'purpose': 'allocation', 'venue': 'kalshi',
                'cash_delta': str(delta), 'external_delta': str(delta), 'fee': '0', 'legs': [],
                'evidence': {'from': source, 'to': target}}, portfolio, source=f'allocate:{tag}:{portfolio}')


def _settle_for_retirement(store: Store, api: Any, portfolio: str, params: dict[str, Any], sub: int,
                           others: dict[str, Decimal]) -> list[dict[str, Any]]:
    """Book the portfolio's Kalshi lots from Kalshi's settlement records. Refuses a lot
    whose market hasn't settled or whose payout doesn't match the ledger."""
    booked: list[dict[str, Any]] = []
    lots = [lot for lot in portfolio_state(store, portfolio).lots.values() if venue_of(lot['market']) == 'kalshi']
    for ticker in sorted({lot['market'] for lot in lots}):
        if ticker in others:
            raise ValueError(f'{ticker} is also held by another portfolio on this account: let the monitor settle it')
        settlements = list(api.pages('/portfolio/settlements', 'settlements', ticker=ticker, **params))
        settlement = next((s for s in settlements if s.get('ticker') == ticker), None)
        if settlement is None:
            raise ValueError(f'{ticker} has not settled on Kalshi: close the position or wait before retiring')
        if settlement.get('value') is not None:
            yes = decimal(settlement['value']) / 100
        else:
            yes = Decimal(1) if settlement['market_result'] == 'yes' else ZERO
        mine = [lot for lot in lots if lot['market'] == ticker]
        expected = sum((lot['quantity'] * (yes if lot['side'] == 'yes' else 1 - yes) for lot in mine), ZERO)
        revenue = decimal(settlement['revenue']) / 100
        if expected != revenue:
            raise ValueError(f'{ticker} paid ${revenue} on Kalshi but {portfolio} expects ${expected}')
        source = f"settlement:{settlement.get('exchange_index', 0)}:{sub}:{ticker}:{settlement['settled_time']}"
        with store.transaction():
            for lot in mine:
                payout = yes if lot['side'] == 'yes' else 1 - yes
                store.append('transaction', {'reason': 'settlement', 'cash_delta': str(lot['quantity'] * payout),
                    'fee': '0', 'context': lot['context'], 'evidence': settlement,
                    'legs': [{'market': ticker, 'side': lot['side'], 'quantity_delta': str(-lot['quantity']),
                              'price': str(payout), 'lot_id': lot['record_id']}]},
                    portfolio, source=source + ':' + lot['record_id'], at=settlement['settled_time'])
            if not store.db.execute('SELECT 1 FROM events WHERE source_key=?', (source,)).fetchone():
                store.append('account_evidence', {'kind': 'settlement', 'raw': settlement}, portfolio, source=source)
        booked.append({'ticker': ticker, 'payout': str(revenue)})
    return booked


def retire_live(store: Store, api: Any, portfolio: str, book_difference: bool = False,
                to: str | None = None) -> dict[str, Any]:
    """Retire a live portfolio so its accounts can back another.

    Its Kalshi lots that have settled are booked from Kalshi's settlement records; any
    still open refuses. On an account it has to itself, a cash difference the ledger
    can't explain refuses unless `book_difference`, which books it as an external
    adjustment (never as profit). Its cash is then released to the account, or with `to`
    allocated to another portfolio sharing the account. Its records stay; it stops being
    reconciled or traded.
    """
    p = _active(store, portfolio)
    if store.reserved(portfolio):
        raise ValueError(f'{portfolio} has orders open')
    sub, exchange = account_of(p['metadata'])
    sharers = _sharers(store, sub, exchange, exclude=portfolio)
    if to is not None and to not in {s['id'] for s in sharers}:
        raise ValueError(f"{to} does not share {portfolio}'s Kalshi account")
    params = {**scope(p['metadata']), 'exchange_index': exchange}
    booked = _settle_for_retirement(store, api, portfolio, params, sub, kalshi_positions(store, sharers))
    held = sorted({lot['market'] for lot in portfolio_state(store, portfolio).lots.values()})
    if held:
        raise ValueError(f'{portfolio} still holds {held}')
    balance, observed = _balance(api, params)
    recorded = kalshi_cash(store, p)
    difference = observed - recorded if not sharers else ZERO
    if difference and not book_difference:
        raise ValueError(f'Kalshi holds ${observed} but {portfolio} records ${recorded}. The ${difference} '
                         'difference has no ledger record (a manual trade, deposit or withdrawal?). '
                         'Pass --book-difference to book it as an external adjustment.')
    if sharers and to is None and recorded:
        raise ValueError(f'{portfolio} shares its Kalshi account: pass --to with one of '
                         + ', '.join(s['id'] for s in sharers) + ' to hand it its cash')
    tag = uuid.uuid4().hex
    with store.transaction():
        if difference:
            store.append('transaction', {'reason': 'funding', 'purpose': 'reconciliation', 'venue': 'kalshi',
                'cash_delta': str(difference), 'external_delta': str(difference), 'fee': '0', 'legs': [],
                'evidence': {'balance': balance, 'recorded': str(recorded)}},
                portfolio, source=f'reconcile:{portfolio}:{tag}')
        released = {v: c for v, c in portfolio_state(store, portfolio).venue_cash.items() if c}
        if to is not None and released.get('kalshi'):
            allocate(store, portfolio, to, released.pop('kalshi'))
        for venue, cash in released.items():
            _release(store, portfolio, venue, cash, 'retirement', {'balance': balance} if venue == 'kalshi' else {}, tag)
        result = {'settled': booked, 'difference': str(difference),
                  'released': {k: str(v) for k, v in released.items()}, 'allocated_to': to}
        metadata = {**p['metadata'], 'retired_at': utc()}
        store.db.execute('UPDATE portfolios SET metadata=? WHERE id=?', (encode(metadata), portfolio))
        store.append('observation', {'kind': 'retired', **result}, portfolio)
    return result
