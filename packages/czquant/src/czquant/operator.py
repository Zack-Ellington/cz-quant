"""Explicit account administration. No allocation daemon or implicit transfers."""
from __future__ import annotations
import uuid
from typing import Any
from .broker import PRIMARY, scope
from .store import Store, decimal


def map_live(store: Store, api: Any, portfolio: str, subaccount: int, exchange: int = 0,
             primary: bool = False) -> None:
    """Map a flat Kalshi account to a new live portfolio, opening it at the account's cash.

    Subaccount 0 is the primary account. It needs `primary=True`, an explicit promise that
    nothing else trades there while the portfolio is live: the account monitor reconciles
    the whole account against this portfolio, and manual trades or new deposits show up
    as mismatches.
    """
    if subaccount == PRIMARY and not primary:
        raise ValueError('Subaccount 0 is the primary account: pass --primary-account to dedicate it to this portfolio')
    if subaccount != PRIMARY and not 1 <= subaccount <= 63:
        raise ValueError('Use a dedicated numbered subaccount (1-63), or 0 with --primary-account')
    if any(p['mode']=='live' and p['metadata'].get('subaccount')==subaccount and p['metadata'].get('exchange_index',0)==exchange for p in store.portfolios()):
        raise ValueError('subaccount already mapped')
    params={**scope({'subaccount':subaccount}),'exchange_index':exchange}
    positions=list(api.pages('/portfolio/positions','market_positions',**params))
    orders=list(api.pages('/portfolio/orders','orders',status='resting',**params))
    if any(decimal(p.get('position_fp','0')) for p in positions) or orders:
        raise ValueError('Map a flat subaccount with no resting orders; existing inventory needs an explicit opening ledger')
    balance=api.get('/portfolio/balance',params)
    cash=decimal(balance['balance_dollars']) if 'balance_dollars' in balance else decimal(balance['balance'])/100
    with store.transaction():
        store.create(portfolio,mode='live',subaccount=subaccount,exchange_index=exchange)
        store.append('transaction',{'reason':'funding','cash_delta':str(cash),'external_delta':str(cash),
            'fee':'0','legs':[], 'evidence':{'balance':balance,'positions':positions}},portfolio,
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
                   exchange: int = 0, primary: bool = False) -> None:
    """Map a flat Kalshi account and a flat Polymarket US account to one new live
    portfolio, opening it at both accounts' cash (each booked to its own venue).

    The Polymarket US account has no subaccounts, so the whole account belongs to this
    portfolio: manual trades, deposits the monitor can't attribute, or another program
    trading it show up as mismatches and halt the trader.
    """
    if subaccount == PRIMARY and not primary:
        raise ValueError('Subaccount 0 is the primary account: pass --primary-account to dedicate it to this portfolio')
    if subaccount != PRIMARY and not 1 <= subaccount <= 63:
        raise ValueError('Use a dedicated numbered subaccount (1-63), or 0 with --primary-account')
    taken = [p for p in store.portfolios() if p['mode'] == 'live']
    if any(p['metadata'].get('subaccount') == subaccount and p['metadata'].get('exchange_index', 0) == exchange for p in taken):
        raise ValueError('Kalshi subaccount already mapped')
    if any('polymarket_us' in (p['metadata'].get('venues') or []) for p in taken):
        raise ValueError('the Polymarket US account is already mapped to a live portfolio')
    if not polymarket_us.authenticated:
        raise ValueError('Polymarket US credentials are required (POLYMARKET_US_KEY_ID, POLYMARKET_US_SECRET_KEY_PATH)')
    params = {**scope({'subaccount': subaccount}), 'exchange_index': exchange}
    positions = list(kalshi.pages('/portfolio/positions', 'market_positions', **params))
    orders = list(kalshi.pages('/portfolio/orders', 'orders', status='resting', **params))
    if any(decimal(p.get('position_fp', '0')) for p in positions) or orders:
        raise ValueError('Map a flat Kalshi subaccount with no resting orders')
    from .polymarket_us import position_quantity
    pm_positions = {k: v for k, v in polymarket_us.positions().items() if position_quantity(v)}
    if pm_positions or polymarket_us.open_orders():
        raise ValueError('Map a flat Polymarket US account with no open orders')
    balance = kalshi.get('/portfolio/balance', params)
    kalshi_cash = decimal(balance['balance_dollars']) if 'balance_dollars' in balance else decimal(balance['balance']) / 100
    pm_balance = polymarket_us.balance()
    pm_cash = decimal(pm_balance['currentBalance'])
    with store.transaction():
        store.create(portfolio, mode='live', subaccount=subaccount, exchange_index=exchange,
                     venues=['kalshi', 'polymarket_us'])
        store.append('transaction', {'reason': 'funding', 'venue': 'kalshi', 'cash_delta': str(kalshi_cash),
            'external_delta': str(kalshi_cash), 'fee': '0', 'legs': [],
            'evidence': {'balance': balance, 'positions': positions}}, portfolio,
            source=f'opening-live:{portfolio}')
        store.append('transaction', {'reason': 'funding', 'venue': 'polymarket_us', 'cash_delta': str(pm_cash),
            'external_delta': str(pm_cash), 'fee': '0', 'legs': [], 'evidence': {'balance': pm_balance}},
            portfolio, source=f'opening-live-polymarket-us:{portfolio}')
