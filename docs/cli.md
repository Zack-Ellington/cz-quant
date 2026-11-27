# Operator CLI

The shared czquant CLI manages portfolios independently of strategy execution.
Portfolio commands use the portfolios create/list/inspect/delete subcommands.
Run it from packages/czquant after uv sync, or from an installed broker-enabled
strategy environment. Export STRATEGY_RUNS_DIR first.

```bash
uv run czquant portfolios create --config portfolio.yaml
uv run czquant portfolios list
uv run czquant portfolios inspect game-a --events 20
```

## Create

The YAML or JSON creation config has id, optional label, and a pools list. Each
item is either an existing pool ID or a new paper pool with id, venue and cash:

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

Creation and initial paper funding share one transaction. Duplicate IDs or an
invalid member roll back all changes. Repeating creation never adds funding.
For live portfolios, register accounts and allocate pools with the existing
account and open-sub commands, then list those IDs as strings under pools.
The creation command cannot create live capital or mix paper and live pools.

## List and inspect

portfolios list shows active pool, portfolio and legacy rows with their IDs, mode
and member pools. portfolios inspect ID returns open FIFO lots with their owning
pool, current
cash, contributed capital, reservations, fees, realized and unrealized P&L,
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
uv run czquant portfolios delete game-a
uv run czquant portfolios list --all
uv run czquant portfolios inspect game-a --events 20
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
reports, dashboard, export and backup. Run uv run czquant --help for all commands.
