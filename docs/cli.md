# Operator CLI

The shared czquant CLI manages accounts, portfolios, monitoring and reporting
independently of strategy execution. These operations live in packages/czquant;
no strategy environment is required. All shell examples in this guide run from
the repository root with STRATEGY_RUNS_DIR exported as described under
[State directory](#state-directory).

```bash
uv sync --locked --project packages/czquant
uv run --project packages/czquant czquant --help
```

Portfolio commands use the portfolios create/list/inspect/delete subcommands.
Copy a [sample portfolio config](../experiments/example/) to your own experiment
directory and edit its IDs and funding before creating it:

```bash
uv run --project packages/czquant czquant portfolios create --config experiments/my-run/portfolio.yaml
uv run --project packages/czquant czquant portfolios list
uv run --project packages/czquant czquant portfolios inspect game-a --events 20
```

## Accounts and credentials

Credentials authenticate API requests. Registration records the account in the
local ledger so allocations have an owner and a shared reserve. It reads venue
balances, checks existing holdings and orders, and records cash not already
assigned to pools in the reserve. Brokers enforce each pool's allocation;
monitors reconcile the account against all its pools together.

Registration does not create an exchange account, generate or store API keys,
deposit money, select a market, or start a strategy. It persists under
STRATEGY_RUNS_DIR and is needed once per venue account in that ledger. The local
account ID is an operator-chosen name. Kalshi registration also records the
subaccount and exchange scope. Credentials continue to come from the environment
for authenticated commands.

Check for existing registrations first:

```bash
uv run --project packages/czquant czquant accounts
```

For venue accounts not yet registered, these commands register Kalshi's primary
account and the authenticated Polymarket US account:

```bash
uv run --project packages/czquant czquant account main-kalshi \
  --venue kalshi --subaccount 0 --primary-account
uv run --project packages/czquant czquant account main-polymarket-us --venue polymarket-us
```

Use those IDs in the account fields of allocation configs. Registration and
allocation reuse the shared package's venue backing and reconciliation checks;
unexplained holdings or orders must be reconciled before registration succeeds.
See [virtual subaccounts](services/virtual-subaccounts.md) for account scopes,
reserves and recovery. Independent paper pools need no registration; hypothetical
registered accounts are also supported when testing shared-account allocations.

## Create

The YAML or JSON creation config has id, optional label, and a pools list. Each
item can be an existing pool ID, a new independent paper pool with id, venue
and cash, or a new account allocation with id, account and cash:

```yaml
id: game-a
label: Paper comparison
pools:
  - id: game-a-kalshi
    venue: kalshi
    cash: 60
  - id: game-a-us
    venue: polymarket_us
    cash: 40
```

For account-backed allocations, first register each account with czquant account.
Put the registered account IDs and allocation amounts directly in the config:

```yaml
id: game-live
pools:
  - id: game-live-kalshi
    account: main-kalshi
    cash: 10
  - id: game-live-us
    account: main-polymarket-us
    cash: 10
```

Portfolio configs select accounts and cash amounts. They do not select markets.
Venue and paper/live mode come from the registered account; do not also specify
venue. The command allocates each cash amount from that account's reserve using
the existing open-sub checks, without a market restriction. Strategies select
markets and submit orders; brokers enforce the venue and allocation constraints.
No separate open-sub commands are needed. Live allocations require credentials
and current venue cash backing; allocation does not deposit or transfer money
at a venue.

Portfolio creation, pool creation and all cash allocations share one transaction.
An unknown account, insufficient reserve, failed live backing check, duplicate
ID or invalid member rolls back every local change, including earlier allocations.
The portfolio cannot mix paper and live pools. Repeating creation fails without
topping up existing IDs. Use an existing pool ID string to group an allocation
that already exists.

## List and inspect

portfolios list shows active pool, portfolio and legacy rows with their IDs, mode
and member pools. portfolios inspect ID returns open FIFO lots with their owning
pool, current cash, contributed capital, reservations, fees, realized and
unrealized P&L,
equity and quote bounds using the dashboard's valuation policy. --events N
includes the latest N ledger events belonging to those pools, plus their total
count. Events include orders, fills, transactions and strategy observations;
they are not a market-discovery listing. An unknown ID is an error.

This is a current, lifetime operational view. Missing marks leave net P&L null;
realized P&L and unmarked cost remain visible. It does not certify live account
reconciliation or produce a frozen comparison. Use report for time-window
metrics, evidence coverage and a reproducible snapshot.

## Delete

Stop the strategy process before retiring its portfolio:

```bash
uv run --project packages/czquant czquant portfolios delete game-a
uv run --project packages/czquant czquant portfolios list --all
uv run --project packages/czquant czquant portfolios inspect game-a --events 20
```

Deletion retires the portfolio and its pools. It requires flat positions, no
prepaid capital, no unresolved orders and no cash reservations. It refuses pools
shared with another active portfolio and account reserve pools. It does not
cancel orders or liquidate holdings. Reconcile or close positions first.

Registered account allocations return their remaining cash to the account
reserve using the existing allocation checks. Live allocation checks require
venue credentials and can refuse unreconciled backing. Independent paper pools
record their remaining cash as a capital withdrawal, which preserves P&L.
Directly mapped live pools must be retired through their venue management
command first. The portfolio retirement is atomic: any failure rolls back all
local cash moves and retirement records.

Retired portfolios and pools are hidden from the default list. list --all and
inspect still show their state and economic history; reports retain their past
results. IDs cannot be reused. New runs cannot select a retired portfolio, and
brokers reject further orders on its pools, including from an already-open
broker. Repeating delete on a retired portfolio changes nothing. Deletion does
not remove database rows or files, terminate processes, or transfer venue cash.

## State directory

From the repository root, copy .env.example to .env and export it:

```bash
cp .env.example .env
set -a; source .env; set +a
mkdir -p "$STRATEGY_RUNS_DIR"
```

The example sets STRATEGY_RUNS_DIR to "$HOME/.czquant/runs". This keeps the same
ledger across working directories and worktrees. A literal ~/ path is also
expanded by the store. To choose a repo-relative directory, run the following
from the repository root before changing directories:

```bash
export STRATEGY_RUNS_DIR="$PWD/runs"
```

This resolves the relative location once. A bare runs value would instead depend
on each command's current directory. The root runs directory is gitignored;
keep it when replacing or removing the checkout. --runs-dir PATH, placed before
portfolios, overrides the environment for a command.

## Other operator commands

Account registration, allocation and pool closure are documented in
[virtual subaccounts](services/virtual-subaccounts.md). Venue mapping and recovery
are in the [Kalshi](services/kalshi-broker.md) and
[Polymarket](services/polymarket-broker.md) guides. The
[reporting guide](services/reporting.md) owns valuation policy, time-window
reports, dashboard, export and backup. Run uv run --project packages/czquant czquant --help for all commands.
