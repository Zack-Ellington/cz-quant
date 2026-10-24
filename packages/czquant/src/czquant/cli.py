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
    monitor.add_argument('component', choices=['market','account'])
    monitor.add_argument('--once', action='store_true')
    monitor.add_argument('--interval', type=float, default=60)
    report = commands.add_parser('report')
    report.add_argument('--since', required=True)
    report.add_argument('--until', required=True)
    report.add_argument('--output', required=True)
    export = commands.add_parser('export')
    export.add_argument('--output', required=True)
    backup = commands.add_parser('backup')
    backup.add_argument('--output', required=True)
    setup = commands.add_parser('setup')
    setup.add_argument('--upgrade', action='store_true')
    mapping = commands.add_parser('map-live')
    mapping.add_argument('id')
    mapping.add_argument('--subaccount',type=int,required=True)
    mapping.add_argument('--exchange-index',type=int,default=0)
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
    elif args.command in ('map-live','create-subaccount','transfer'):
        from .kalshi import Kalshi
        from .operator import map_live, transfer
        api = Kalshi()
        if args.command == 'map-live':
            map_live(store,api,args.id,args.subaccount,args.exchange_index)
        elif args.command == 'create-subaccount':
            print(encode(api.request('POST','/portfolio/subaccounts',{})))
        else:
            print(encode(transfer(store,api,args.client_id,args.from_subaccount,args.to_subaccount,args.amount_cents,args.exchange_index)))
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
