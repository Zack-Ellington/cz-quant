"""Operator commands; no strategy discovery or execution framework."""
from __future__ import annotations
import argparse
import logging
import sqlite3
from decimal import InvalidOperation
from pathlib import Path
from .store import Store, encode


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--runs-dir', default=None)
    commands = parser.add_subparsers(dest='command', required=True)
    pool = commands.add_parser('pool', help='create an explicitly funded paper cash pool at one venue')
    pool.add_argument('id')
    pool.add_argument('--venue', required=True, choices=['kalshi', 'polymarket', 'polymarket-us'])
    pool.add_argument('--cash', required=True)
    pool.add_argument('--label', default='')
    portfolio_cli = commands.add_parser('portfolios', help='create, list, inspect or retire portfolios')
    portfolio_actions = portfolio_cli.add_subparsers(dest='portfolio_action', required=True)
    portfolio_create = portfolio_actions.add_parser('create', help='create from YAML or JSON')
    portfolio_create.add_argument('--config', required=True)
    portfolio_list = portfolio_actions.add_parser('list', help='list active portfolios and pools')
    portfolio_list.add_argument('--all', action='store_true', help='include retired portfolios and pools')
    portfolio_inspect = portfolio_actions.add_parser('inspect', help='show positions, P&L and ledger events')
    portfolio_inspect.add_argument('id')
    portfolio_inspect.add_argument('--events', type=int, default=0, metavar='N', help='include the last N ledger events')
    portfolio_delete = portfolio_actions.add_parser('delete', help='retire a flat portfolio; retain ledger history')
    portfolio_delete.add_argument('id')
    monitor = commands.add_parser('monitor')
    monitor.add_argument('component', choices=['market','account','polymarket','polymarket-us'])
    monitor.add_argument('--once', action='store_true')
    monitor.add_argument('--interval', type=float, default=60)
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
    mapping.add_argument('--venue', choices=['kalshi', 'polymarket-us'], default='kalshi')
    mapping.add_argument('--subaccount', type=int)
    mapping.add_argument('--exchange-index',type=int,default=0)
    mapping.add_argument('--primary-account', action='store_true',
                         help='with --subaccount 0: trade the primary account from this cash pool')
    mapping.add_argument('--share-account', action='store_true', help='join a Kalshi account other live pools already trade (opens with the cash none of them records)')
    remap = commands.add_parser('remap-kalshi', help='move a flat Kalshi cash pool to another account')
    remap.add_argument('id')
    remap.add_argument('--subaccount', type=int, required=True)
    remap.add_argument('--exchange-index', type=int, default=0)
    remap.add_argument('--share-account', action='store_true')
    retire = commands.add_parser('retire-live', help='retire a live Kalshi pool: settle, reconcile and release its cash')
    retire.add_argument('id')
    retire.add_argument('--book-difference', action='store_true',
                        help='book an unexplained Kalshi cash difference as an external adjustment')
    retire.add_argument('--to', help='on a shared account: the pool that receives its Kalshi cash')
    register = commands.add_parser('account', help='register a venue (or paper) account; creates its reserve pool')
    register.add_argument('id')
    register.add_argument('--venue', required=True, choices=['kalshi', 'polymarket', 'polymarket-us'])
    register.add_argument('--paper-cash', help='a hypothetical paper account opening with this cash')
    register.add_argument('--subaccount', type=int, help='live Kalshi: the subaccount (0 with --primary-account)')
    register.add_argument('--exchange-index', type=int, default=0)
    register.add_argument('--primary-account', action='store_true')
    show = commands.add_parser('accounts', help="show registered accounts and each pool's recorded share")
    show.add_argument('id', nargs='?')
    sub_open = commands.add_parser('open-sub', help='load a virtual subaccount: a new pool funded from the reserve')
    sub_open.add_argument('id')
    sub_open.add_argument('--account', required=True)
    sub_open.add_argument('--cash', default='0')
    sub_open.add_argument('--from', dest='source', help='fund it from this pool instead of the reserve')
    sub_open.add_argument('--markets', nargs='+', help="limit it to these tickers, or prefixes ending in '*'")
    sub_close = commands.add_parser('close-sub', help='unload a flat virtual subaccount; its cash goes back')
    sub_close.add_argument('id')
    sub_close.add_argument('--to', help='the pool receiving its cash (default: its parent)')
    move = commands.add_parser('move-lots', help='hand open contracts to another pool on the account, at cost')
    move.add_argument('--from', dest='source', required=True)
    move.add_argument('--to', dest='target', required=True)
    move.add_argument('--market', required=True)
    move.add_argument('--side', required=True, choices=['yes', 'no'])
    move.add_argument('--quantity', help='default: all of them')
    difference = commands.add_parser('book-deposit', help="book a confirmed Kalshi deposit/withdrawal to the account's reserve")
    difference.add_argument('account')
    difference.add_argument('--note', required=True, help='what the difference is, e.g. the deposit reference')
    join = commands.add_parser('portfolio-add', help='add new, funding-only pools to a reporting portfolio')
    join.add_argument('id')
    join.add_argument('--pools', nargs='+', required=True)
    allocation = commands.add_parser('allocate', help='move recorded cash between pools on one account')
    allocation.add_argument('--from', dest='source', required=True)
    allocation.add_argument('--to', dest='target', required=True)
    allocation.add_argument('--dollars', required=True)
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
    if args.command == 'pool':
        store.create_pool(args.id, args.venue.replace('-', '_'), args.cash, label=args.label)
    elif args.command == 'portfolios':
        from . import portfolios
        from .config import load_config
        from .report import reporting_units
        try:
            if args.portfolio_action == 'create':
                identifier = portfolios.create(store, load_config(args.config))
                print(encode(portfolios.details(store, identifier)))
            elif args.portfolio_action == 'inspect':
                if args.events < 0:
                    parser.error('--events requires a nonnegative count')
                print(encode(portfolios.details(store, args.id, args.events)))
            elif args.portfolio_action == 'delete':
                portfolios.delete(store, args.id)
                print(encode(portfolios.details(store, args.id)))
            else:
                print(encode([p for p in reporting_units(store) if args.all or not p['metadata'].get('retired_at')]))
        except (OSError, ValueError, InvalidOperation, sqlite3.IntegrityError) as exc:
            parser.error(str(exc))
        finally:
            store.close()
        return
    elif args.command == 'monitor':
        from .monitor import run_monitor
        run_monitor(str(store.root), args.component, args.interval, args.once)
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
    elif args.command == 'map-live' and args.venue == 'polymarket-us':
        if args.subaccount is not None or args.primary_account or args.share_account or args.exchange_index:
            parser.error('Polymarket US mapping does not accept Kalshi account options')
        from .operator import map_polymarket_us
        from .polymarket_us import PolymarketUS
        map_polymarket_us(store, PolymarketUS.from_env(authenticated=True), args.id)
    elif args.command == 'remap-kalshi':
        from .kalshi import Kalshi
        from .operator import remap_kalshi
        remap_kalshi(store, Kalshi(), args.id, args.subaccount, args.exchange_index, args.share_account)
    elif args.command == 'retire-live':
        from .kalshi import Kalshi
        from .operator import retire_live
        print(encode(retire_live(store, Kalshi(), args.id, args.book_difference, args.to)))
    elif args.command == 'allocate':
        from .operator import allocate
        from .store import decimal
        allocate(store, args.source, args.target, decimal(args.dollars))
        print(f'Allocated ${args.dollars} from {args.source} to {args.target}.')
    elif args.command == 'account':
        from . import accounts
        venue = args.venue.replace('-', '_')
        if args.paper_cash is not None:
            if args.subaccount is not None or args.primary_account:
                parser.error('a paper account takes no live account options')
            accounts.register_paper(store, args.id, venue, args.paper_cash)
        elif venue == 'kalshi':
            if args.subaccount is None:
                parser.error('a live Kalshi account needs --subaccount')
            from .kalshi import Kalshi
            accounts.register_kalshi(store, Kalshi(), args.id, args.subaccount, args.exchange_index, args.primary_account)
        elif venue == 'polymarket_us':
            from .polymarket_us import PolymarketUS
            accounts.register_polymarket_us(store, PolymarketUS.from_env(authenticated=True), args.id)
        else:
            parser.error('Polymarket (international) has no live execution: register it with --paper-cash')
        print(encode(accounts.summary(store, args.id)))
    elif args.command == 'accounts':
        from . import accounts
        for a in accounts.accounts(store):
            if args.id in (None, a['id']):
                print(encode(accounts.summary(store, a['id'])))
    elif args.command == 'open-sub':
        from .accounts import open_sub
        open_sub(store, args.id, args.account, args.cash, args.markets, args.source)
        print(f'Opened {args.id} on {args.account} with ${args.cash}.')
    elif args.command == 'close-sub':
        from .accounts import close_sub
        print(f'Closed {args.id}; ${close_sub(store, args.id, args.to)} returned.')
    elif args.command == 'move-lots':
        from .accounts import move_lots
        print(f'Moved {move_lots(store, args.source, args.target, args.market, args.side, args.quantity)} '
              f'{args.side} of {args.market} to {args.target}.')
    elif args.command == 'book-deposit':
        from .accounts import book_reserve_difference
        from .kalshi import Kalshi
        print(f'Booked ${book_reserve_difference(store, Kalshi(), args.account, args.note)} to the reserve.')
    elif args.command == 'portfolio-add':
        store.add_to_portfolio(args.id, args.pools)
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
            if args.subaccount is None:
                parser.error('Kalshi mapping requires --subaccount')
            map_live(store,api,args.id,args.subaccount,args.exchange_index,args.primary_account,args.share_account)
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
