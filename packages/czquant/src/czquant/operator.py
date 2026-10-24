"""Explicit account administration. No allocation daemon or implicit transfers."""
from __future__ import annotations
import uuid
from typing import Any
from .store import Store, decimal


def map_live(store: Store, api: Any, portfolio: str, subaccount: int, exchange: int = 0) -> None:
    if not 1 <= subaccount <= 63:
        raise ValueError('Use a dedicated numbered subaccount')
    if any(p['mode']=='live' and p['metadata'].get('subaccount')==subaccount and p['metadata'].get('exchange_index',0)==exchange for p in store.portfolios()):
        raise ValueError('subaccount already mapped')
    params={'subaccount':subaccount,'exchange_index':exchange}
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
