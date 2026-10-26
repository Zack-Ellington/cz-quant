"""Offline comparison from a consistent event-store snapshot."""
from __future__ import annotations

import csv
import hashlib
import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from .accounting import State, active_events
from .book import levels
from .store import Store, decimal, encode, utc


def timestamp(value: str) -> datetime:
    value = value.replace('Z', '+00:00')
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError('UTC offset required')
    return result


def value(state: State, marks: dict[str, dict[str, Any]], at: datetime) -> tuple[Decimal | None, Decimal | None, Decimal | None, list[str]]:
    """Midpoint plus quote envelope; missing sides use the contract bounds 0/1.

    This envelope is not executable liquidation value or a settlement guarantee.
    Stale, absent, or crossed references invalidate both midpoint and envelope.
    """
    total = lower = upper = state.cash + state.prepaid
    missing, unbounded = [], False
    prices: dict[str, tuple[Decimal, Decimal, bool] | None] = {}
    for lot in state.lots.values():
        ticker = lot['market']
        if ticker not in prices:
            event = marks.get(ticker)
            prices[ticker] = None
            if event:
                data = event['data']
                market = data.get('market', {})
                if market.get('status') in ('settled', 'finalized') and market.get('settlement_value_dollars') is not None:
                    payout = decimal(market['settlement_value_dollars'])
                    if 0 <= payout <= 1:
                        prices[ticker] = (payout, payout, True)
                elif 0 <= (at - timestamp(event['effective_at'])).total_seconds() <= 120:
                    book = levels(data.get('book', {}))
                    bid = book['yes'][0][0] if book['yes'] else Decimal(0)
                    ask = 1 - book['no'][0][0] if book['no'] else Decimal(1)
                    if 0 <= bid <= ask <= 1:
                        prices[ticker] = (bid, ask, bool(book['yes'] and book['no']))
        quote = prices[ticker]
        if quote is None:
            missing.append(ticker)
            unbounded = True
            continue
        bid, ask, two_sided = quote
        low, high = (bid, ask) if lot['side'] == 'yes' else (1 - ask, 1 - bid)
        lower += lot['quantity'] * low
        upper += lot['quantity'] * high
        if two_sided:
            total += lot['quantity'] * (low + high) / 2
        else:
            missing.append(ticker)
    return (None if missing else total, None if unbounded else lower,
            None if unbounded else upper, sorted(set(missing)))


def compare(store: Store, start: str, end: str, recorded: str | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    first, last = timestamp(start), timestamp(end)
    if last < first:
        raise ValueError('end precedes start')
    events = active_events(store.events(until=last.isoformat(timespec='microseconds'), recorded_until=recorded,
        kinds=('transaction','correction','market','execution','observation','health')))
    points = [first]
    while points[-1] + timedelta(minutes=1) < last:
        points.append(points[-1] + timedelta(minutes=1))
    if last != first:
        points.append(last)
    points = sorted(set(points + [timestamp(e['effective_at']) for e in events
        if e['type']=='transaction' and decimal(e['data'].get('external_delta','0'))
        and first < timestamp(e['effective_at']) < last]))
    states = {p['id']: State() for p in store.portfolios() if timestamp(p['created_at']) <= first}
    marks: dict[str, dict[str, Any]] = {}
    history: dict[str, list[dict[str, Any]]] = {p: [] for p in states}
    cursor = 0
    for at in points:
        while cursor < len(events) and timestamp(events[cursor]['effective_at']) <= at:
            event = events[cursor]
            if event['type'] == 'market':
                marks[event['data']['ticker']] = event
            elif event['portfolio_id'] in states:
                states[event['portfolio_id']].apply(event)
            cursor += 1
        for portfolio, state in states.items():
            equity, low, high, missing = value(state, marks, at)
            history[portfolio].append({'portfolio': portfolio, 'time': at.isoformat(), 'equity': equity,
                'equity_lower': low, 'equity_upper': high,
                'cash': state.cash, 'contributed': state.contributed, 'realized': state.realized,
                'fees': state.fees, 'rewards': state.rewards, 'notional': state.notional,
                'settlements': state.settlements, 'cost': state.cost, 'missing': ','.join(missing)})
    metrics, curves = [], []
    portfolio_metadata = {p['id']:p for p in store.portfolios()}
    for portfolio, rows in history.items():
        a, b = rows[0], rows[-1]
        complete = all(r['equity'] is not None for r in rows)
        if portfolio_metadata[portfolio]['mode']=='live':
            account_health = [e for e in events if e['type']=='health' and e['data'].get('component')=='account']
            complete = complete and bool(account_health) and account_health[-1]['data']['status']=='healthy' and (last-timestamp(account_health[-1]['effective_at'])).total_seconds()<=120
        cashflow = b['contributed'] - a['contributed']
        pnl = b['equity'] - a['equity'] - cashflow if a['equity'] is not None and b['equity'] is not None else None
        flows = any(e['portfolio_id']==portfolio and e['type']=='transaction' and decimal(e['data'].get('external_delta','0')) and first < timestamp(e['effective_at']) <= last for e in events)
        ret = pnl / a['equity'] if complete and not flows and a['equity'] else None
        peak = Decimal(1)
        growth = Decimal(1)
        drawdown = Decimal(0)
        utilization = Decimal(0)
        seconds = Decimal(str((last-first).total_seconds()))
        for previous, row in zip(rows, rows[1:]):
            if complete:
                flow = row['contributed'] - previous['contributed']
                if previous['equity'] > 0:
                    growth *= (row['equity']-flow)/previous['equity']
                peak = max(peak, growth)
                drawdown = max(drawdown, (peak-growth)/peak)
                dt = Decimal(str((timestamp(row['time'])-timestamp(previous['time'])).total_seconds()))
                utilization += (previous['cost']/previous['equity'] if previous['equity'] else Decimal(0))*dt
        fills = sum(1 for e in events if e['portfolio_id'] == portfolio and e['type']=='execution' and first < timestamp(e['effective_at']) <= last)
        own = [e for e in events if e['portfolio_id']==portfolio and first < timestamp(e['effective_at']) <= last]
        policies = sorted({e['data']['evidence'].get('policy','live') for e in own if e['type']=='execution'})
        health_coverage = {}
        for component in ('market','account'):
            records = [e for e in events if e['type']=='health' and e['data'].get('component')==component]
            healthy = 0
            for at in points:
                available = [e for e in records if timestamp(e['effective_at'])<=at]
                if available:
                    latest = available[-1]
                    healthy += latest['data']['status']=='healthy' and (at-timestamp(latest['effective_at'])).total_seconds()<=120
            health_coverage[component+'_health_coverage'] = healthy/len(points)
        metrics.append({**health_coverage, 'strategy_heartbeats':sum(e['type']=='observation' and e['data'].get('kind') in ('heartbeat','scan_complete') for e in own),
            'strategy_errors':sum(e['type']=='observation' and e['data'].get('kind')=='error' for e in own), 'portfolio':portfolio, 'complete':complete, 'net_pnl':pnl,
            'net_pnl_lower':b['equity_lower']-a['equity_upper']-cashflow if b['equity_lower'] is not None and a['equity_upper'] is not None else None,
            'net_pnl_upper':b['equity_upper']-a['equity_lower']-cashflow if b['equity_upper'] is not None and a['equity_lower'] is not None else None,
            'return_on_opening_equity':ret, 'time_weighted_return':growth-1 if complete and a['equity'] is not None and a['equity']>0 else None, 'max_drawdown':drawdown if complete else None,
            'realized_pnl':b['realized']-a['realized'],
            'unrealized_change':pnl-(b['realized']-a['realized']) if pnl is not None else None,
            'capital_utilization':utilization/seconds if complete and seconds else None,
            'fees':b['fees']-a['fees'], 'rewards':b['rewards']-a['rewards'],
            'filled_notional':b['notional']-a['notional'], 'fills':fills,
            'settlement_lots':b['settlements']-a['settlements'], 'open_lots':len(states[portfolio].lots),
            'range_coverage':sum(r['equity_lower'] is not None for r in rows)/len(rows),
            'valuation_coverage':sum(r['equity'] is not None for r in rows)/len(rows),
            'external_flows':cashflow, 'execution_policy':','.join(policies) or 'no fills'})
        curves.extend(rows)
    return metrics, curves


def generate(root: str, start: str, end: str, output: str) -> dict[str, Any]:
    destination = Path(output)
    destination.mkdir(parents=True, exist_ok=True)
    store = Store(root)
    snapshot = destination/'accounting.sqlite3'
    if store.path.resolve() == snapshot.resolve():
        raise ValueError('report must not overwrite source database')
    store.backup(snapshot)
    store.close()
    frozen = Store(destination)
    recorded = frozen.db.execute("SELECT MAX(recorded_at) FROM events").fetchone()[0] or utc()
    metrics, curves = compare(frozen, start, end, recorded)
    for name, rows in [('metrics',metrics),('equity',curves)]:
        with (destination/f'{name}.csv').open('w') as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0]) if rows else ['portfolio'])
            writer.writeheader()
            writer.writerows(rows)
    import plotly.graph_objects as go
    fig = go.Figure()
    colors = ['#087e8b','#279359','#6850ad','#df8935','#7c8798']
    for index, metric in enumerate(metrics):
        rows = [r for r in curves if r['portfolio']==metric['portfolio']]
        color = colors[index % len(colors)]
        for key, fill in [('equity_upper',None),('equity_lower','tonexty')]:
            fig.add_scatter(name=metric['portfolio']+' quote range', legendgroup=metric['portfolio'],
                x=[r['time'] for r in rows], y=[float(r[key]) if r[key] is not None else None for r in rows],
                line={'color':color,'width':0.5}, fill=fill, opacity=0.25,
                showlegend=False, connectgaps=False)
        fig.add_scatter(name=metric['portfolio'], legendgroup=metric['portfolio'], line={'color':color}, x=[r['time'] for r in rows],
                       y=[float(r['equity']) if r['equity'] is not None else None for r in rows], connectgaps=False)
    fig.update_layout(title='Paper equity · midpoint lines and quote ranges (not liquidation values)', yaxis_title='USD')
    fig.write_html(destination/'report.html', include_plotlyjs=True)
    manifest = {'schema_version':1, 'generated_at':utc(), 'start':start, 'cutoff':end, 'recorded_cutoff':recorded,
        'valuation':'midpoint-v1 plus quote-range-v1; one-minute grid; maximum book age 120 seconds',
        'cost_basis':'FIFO, fees included', 'metrics':metrics,
        'limitations':['Paper fills do not establish real execution quality.',
            'Missing midpoints exclude complete-period rankings. Fresh one-sided books retain quote ranges with 0/1 boundaries.',
            'Quote ranges are neither executable liquidation values nor guaranteed settlement values; exit fees and depth are not modeled.',
            'Fixed-capital return unavailable across funding changes; time-weighted return uses funding-time valuations.',
            'Short trials cannot establish long-run profitability.'],
        'health':[e for e in frozen.events(until=end,recorded_until=recorded,kinds=('health',)) if e['type']=='health']}
    lines = ['# Experiment comparison', '', f'Window: {start} to {end}.', '',
             'Paper portfolios; midpoint and quote-range valuation with 120-second maximum book age.', '',
             '| Portfolio | Net P&L | P&L range | Realized | Fees | Fills | Midpoint coverage | Range coverage |',
             '| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |']
    for m in metrics:
        pnl = f"${float(m['net_pnl']):.4f}" if m['net_pnl'] is not None else 'incomplete'
        envelope = f"${float(m['net_pnl_lower']):.2f} to ${float(m['net_pnl_upper']):.2f}" if m['net_pnl_lower'] is not None else 'unavailable'
        lines.append(f"| {m['portfolio']} | {pnl} | {envelope} | ${float(m['realized_pnl']):.2f} | ${float(m['fees']):.2f} | {m['fills']} | {100*m['valuation_coverage']:.1f}% | {100*m['range_coverage']:.1f}% |")
    lines += ['', 'A zero-fill result measures inactivity, not demonstrated profitability. Short trials and paper execution cannot establish durable returns.', '',
              'Ranges use fresh bid/ask quotes and 0/1 for missing sides. They do not model executable liquidation, exit fees, or guaranteed settlement. Stale or absent references remain gaps.', '',
              'Fees are included in P&L. Open positions remain valued at the cutoff; the report does not liquidate them.', '']
    (destination/'comparison.md').write_text('\n'.join(lines))
    frozen.close()
    manifest['snapshot_sha256']=hashlib.sha256(snapshot.read_bytes()).hexdigest()
    (destination/'manifest.json').write_text(encode(manifest)+'\n')
    return manifest
