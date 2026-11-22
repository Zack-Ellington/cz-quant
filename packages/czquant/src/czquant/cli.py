"""Operator commands; no strategy discovery or execution framework."""
from __future__ import annotations
import argparse
import logging
from pathlib import Path
from .store import Store, encode


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--runs-dir', default=None)
    commands = parser.add_subparsers(dest='command', required=True)
    create = commands.add_parser('portfolio')
    create.add_argument('id')
    create.add_argument('--cash', default='1000')
    create.add_argument('--label', default='')
    monitor = commands.add_parser('monitor')
    monitor.add_argument('component', choices=['market','account','draftkings','polymarket','polymarket-us'])
    monitor.add_argument('--once', action='store_true')
    monitor.add_argument('--interval', type=float, default=60)
    settle = commands.add_parser('settle-sportsbook')
    settle.add_argument('ticker')
    settle.add_argument('--result', choices=['yes','no','void'], required=True)
    settle.add_argument('--note', required=True, help='the evidence: source and final score')
    dashboard = commands.add_parser('dashboard', help='live portfolio comparison in the browser (read-only)')
    dashboard.add_argument('--host', default='127.0.0.1')
    dashboard.add_argument('--port', type=int, default=8790)
    dashboard.add_argument('--interval', type=float, default=5, help='seconds between updates')
    dashboard.add_argument('--step', type=float, default=300, help='seconds between chart points')
    report = commands.add_parser('report')
    report.add_argument('--since', required=True)
    report.add_argument('--until', required=True)
    report.add_argument('--output', required=True)
    export = commands.add_parser('export')
    export.add_argument('--output', required=True)
    halts = commands.add_parser('halts', help="list a portfolio's standing halts")
    halts.add_argument('id')
    clear = commands.add_parser('clear-halt', help='clear one reviewed halt; the record stays')
    clear.add_argument('id')
    clear.add_argument('--halt-id', required=True, help='record id from `czquant halts`')
    clear.add_argument('--reason', required=True, help='what was checked and resolved')
    backup = commands.add_parser('backup')
    backup.add_argument('--output', required=True)
    setup = commands.add_parser('setup')
    setup.add_argument('--upgrade', action='store_true')
    mapping = commands.add_parser('map-live')
    mapping.add_argument('id')
    mapping.add_argument('--subaccount',type=int,required=True)
    mapping.add_argument('--exchange-index',type=int,default=0)
    mapping.add_argument('--primary-account', action='store_true',
                         help='with --subaccount 0: dedicate the primary account to this portfolio')
    cross = commands.add_parser('map-live-cross', help='map a Kalshi subaccount and the Polymarket US account to one live portfolio')
    cross.add_argument('id')
    cross.add_argument('--subaccount', type=int, required=True)
    cross.add_argument('--exchange-index', type=int, default=0)
    cross.add_argument('--primary-account', action='store_true',
                       help='with --subaccount 0: dedicate the primary account to this portfolio')
    commands.add_parser('check-polymarket-us', help='authenticated read of the Polymarket US balance (no orders)')
    commands.add_parser('create-subaccount')
    transfer = commands.add_parser('transfer')
    transfer.add_argument('--client-id',required=True)
    transfer.add_argument('--from-subaccount',type=int,required=True)
    transfer.add_argument('--to-subaccount',type=int,required=True)
    transfer.add_argument('--amount-cents',type=int,required=True)
    transfer.add_argument('--exchange-index',type=int,default=0)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    store = Store(args.runs_dir)
    if args.command == 'portfolio':
        store.create(args.id, args.cash, label=args.label)
    elif args.command == 'monitor':
        from .monitor import run_monitor
        run_monitor(str(store.root), args.component, args.interval, args.once)
    elif args.command == 'settle-sportsbook':
        from .sportsbook import operator_settle
        print(encode({'settled_lots': operator_settle(store, args.ticker, args.result, args.note)}))
    elif args.command == 'dashboard':
        import asyncio
        from .dashboard import serve
        try:
            asyncio.run(serve(str(store.root), args.host, args.port, args.interval, args.step))
        except KeyboardInterrupt:
            pass
    elif args.command == 'report':
        from .report import generate
        result = generate(str(store.root),args.since,args.until,args.output)
        print(encode({'metrics':result['metrics'],'output':args.output}))
    elif args.command == 'export':
        with Path(args.output).open('w') as f:
            for event in store.events():
                f.write(encode(event)+'\n')
    elif args.command == 'backup':
        store.backup(Path(args.output))
    elif args.command == 'map-live-cross':
        from .kalshi import Kalshi
        from .operator import map_live_cross
        from .polymarket_us import PolymarketUS
        map_live_cross(store, Kalshi(), PolymarketUS.from_env(), args.id, args.subaccount,
                       args.exchange_index, args.primary_account)
        print(f'Mapped {args.id} to Kalshi subaccount {args.subaccount} and the Polymarket US account.')
    elif args.command == 'check-polymarket-us':
        from .polymarket_us import PolymarketUS
        client = PolymarketUS.from_env()
        balance = client.balance()
        print(encode({'currentBalance': balance.get('currentBalance'), 'buyingPower': balance.get('buyingPower'),
                      'open_positions': len(client.positions())}))
        print('Authenticated Polymarket US read succeeded.')
        if balance.get('unfunded'):
            print('The account lists no balance: it has not been funded yet. Deposit in the Polymarket US app before mapping it.')
    elif args.command in ('map-live','create-subaccount','transfer'):
        from .kalshi import Kalshi
        from .operator import map_live, transfer
        api = Kalshi()
        if args.command == 'map-live':
            map_live(store,api,args.id,args.subaccount,args.exchange_index,args.primary_account)
        elif args.command == 'create-subaccount':
            print(encode(api.request('POST','/portfolio/subaccounts',{})))
        else:
            print(encode(transfer(store,api,args.client_id,args.from_subaccount,args.to_subaccount,args.amount_cents,args.exchange_index)))
    elif args.command in ('halts', 'clear-halt'):
        events = store.events(args.id, kinds=('observation',), observations=('halt', 'halt_cleared'))
        cleared = {e['data'].get('halt_id') for e in events if e['data']['kind'] == 'halt_cleared'}
        standing = {e['record_id']: e for e in events if e['data']['kind'] == 'halt' and e['record_id'] not in cleared}
        if args.command == 'halts':
            for record_id, e in standing.items():
                print(record_id, e['effective_at'], e['data'].get('reason'))
        elif args.halt_id not in standing:
            raise SystemExit(f'{args.halt_id} is not a standing halt of {args.id}')
        else:
            store.append('observation', {'kind': 'halt_cleared', 'halt_id': args.halt_id, 'reason': args.reason}, args.id)
            print(f'Cleared {args.halt_id}.')
    elif args.command == 'setup':
        from .kalshi import Kalshi
        api = Kalshi()
        print(encode(api.get('/account/limits')))
        if args.upgrade:
            print(encode(api.request('POST','/account/api_usage_level/upgrade',{})))
            print(encode(api.get('/account/limits')))
        api.get('/portfolio/balance')
        print('Authenticated account read succeeded.')
    store.close()
