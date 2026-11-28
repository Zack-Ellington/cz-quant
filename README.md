# cz-quant

## Design and shared accounting

The [design principles](docs/design-principles.md) are the authoritative
reference for development and review: independent strategies, all trades
through a broker scoped to one venue and cash pool, and common local logging
for fair comparison. Rewrites and code duplication support experimentation.
Strategies own their market readers, models, and schedules; using the shared
market or account monitors is not a required strategy interface.

Quantitative trading strategies. The first venue is the Kalshi prediction
market.

The repo is a monorepo of separately installed strategy projects. There is no
root install. The shared packages/czquant library provides execution accounting
and reporting infrastructure. Each strategy owns its research implementation
and entry point, so it can be deployed, pinned, rewritten, or retired
independently.

## Strategies

- [2026-midterm-prediction-arbitrage](strategies/2026-midterm-prediction-arbitrage/): 2026 Congress control (DD/DR/RD/RR) from Kalshi's House and Senate race markets drawn independently, nine-state model-free checks, and paper trading into `runs/`, against the `KXBALANCEPOWERCOMBO` combo market.

- [2026-midterm-dependence-arbitrage](strategies/2026-midterm-dependence-arbitrage/): 2026 Congress control (DD/DR/RD/RR) from Kalshi's House and Senate race markets with a calibrated latent-swing model, nine-state model-free checks, and paper trading into `runs/`, against the `KXBALANCEPOWERCOMBO` combo market.

- [cross-market-polymarket](strategies/cross-market-polymarket/): the sum check across Kalshi and Polymarket. A separate [preprocessing bundle](scripts/cross-market-pairs/) discovers and prepares pairs. Each run prices one selected pair and operates on its funded venue cash pools through separate brokers. Polymarket US supports paper and live execution; international Polymarket supports paper execution.

- [kalshi-sum-check-unwind](strategies/kalshi-sum-check-unwind/): the Kalshi sum check, but when a basket fills only some legs it keeps the complete sets and sells the extra contracts back within a slippage limit.

- [kalshi-sum-check-retry](strategies/kalshi-sum-check-retry/): the Kalshi sum check, but when a basket fills only some legs it buys the missing legs again, as long as the completed basket still breaks even (or loses no more than a set amount).

## Research

- [DraftKings odds](research/draftkings/): independent, read-only odds and score collection.
- [Kalshi calibration](research/kalshi-calibration/): empirical price calibration.

## Quickstart

You need [uv](https://docs.astral.sh/uv/) and git. uv installs the required Python
version. Run the commands below from the repository root:

```bash
git clone git@github.com:Zack-Ellington/cz-quant.git
cd cz-quant
```

The shared packages/czquant environment owns account registration, portfolio
allocation, monitoring and reporting. Strategies have separate environments and
operate on existing allocations. Only the strategy example below requires the
cross-market environment.

### Set up the shared CLI and environment

Install the shared package and copy the environment template:

```bash
uv sync --locked --project packages/czquant
cp .env.example .env
```

Edit .env, then export it in every shell used for a strategy or service. Public
paper runs need no credentials. Live account operations need the keys for their
venues. The example stores the shared ledger and logs in "$HOME/.czquant/runs".
No Python command loads .env automatically.

```bash
set -a; source .env; set +a
mkdir -p "$STRATEGY_RUNS_DIR"
uv run --project packages/czquant czquant --help
```

Keep experiment configs and reviewed inputs together in a private directory:

```bash
export EXPERIMENT_DIR="$PWD/experiments/my-run"
mkdir -p "$EXPERIMENT_DIR"
```

Only experiments/example is tracked. Every other experiment directory is ignored
by Git. Preserve those files when removing a checkout. Ledger state and execution
logs remain under STRATEGY_RUNS_DIR, independently of experiment directories.

### Create a portfolio

A portfolio groups venue cash pools. Each pool has its own cash and cannot spend
another pool's allocation. Portfolio configs select accounts or paper venues and
cash amounts, not strategies or markets. Choose one of the following funding
options, then create the portfolio with the common command below.

For paper trading, copy the paper example. It creates game-a with $60 of
hypothetical Kalshi cash and $40 of hypothetical Polymarket US cash. Independent
paper pools need neither credentials nor registered accounts.

```bash
cp experiments/example/portfolio.example.yaml "$EXPERIMENT_DIR/portfolio.yaml"
```

For live trading, credentials authenticate venue API requests; registration
establishes ownership in the local ledger. It records the venue account scope
and creates a reserve for cash not already allocated to pools. This lets brokers
and monitors reconcile all allocations against one account without counting the
same money twice. Registration is local bookkeeping, not exchange signup, a new
API key, or a deposit. It is required for the account allocations in this example
and is implemented in the shared package, independently of any strategy.

First inspect accounts already registered in this ledger:

```bash
uv run --project packages/czquant czquant accounts
```

If the venue accounts are not registered, register them once. The IDs below are
local names; Kalshi's scope is its primary account on the default exchange.
Reuse existing registered IDs when present. Registration reads the venue and
refuses unexplained holdings or orders; resolve those through the
[account setup guide](docs/services/virtual-subaccounts.md).

```bash
uv run --project packages/czquant czquant account main-kalshi \
  --venue kalshi --subaccount 0 --primary-account
uv run --project packages/czquant czquant account main-polymarket-us \
  --venue polymarket-us
```

Copy the live example instead of the paper example. It creates game-live by
allocating $10 from each registered account reserve. Edit account IDs to match
your ledger and ensure each reserve has the requested cash.

```bash
cp experiments/example/portfolio-live.example.yaml "$EXPERIMENT_DIR/portfolio.yaml"
```

For either option, edit portfolio and pool IDs before creating an independent
experiment. Creation is atomic and refuses reused IDs without adding cash:

```bash
uv run --project packages/czquant czquant portfolios create --config "$EXPERIMENT_DIR/portfolio.yaml"
uv run --project packages/czquant czquant portfolios list
```

Create the portfolio once. Restarting a strategy preserves its cash, positions,
losses and pending orders. Account registration is also once per venue account
in this ledger, not once per portfolio or strategy. See the
[operator CLI guide](docs/cli.md) for lifecycle details.

### Start live account monitors

Skip this step for a public paper run. Account monitors are shared services that
record fills and reconcile cash and positions across all allocations at their
venue. Start the components for the venues you use. They run independently of
strategies and use the same STRATEGY_RUNS_DIR.

For Kalshi, open a second terminal at the repository root:

```bash
set -a; source .env; set +a
uv run --project packages/czquant czquant monitor account --interval 60
```

For Polymarket US, open another terminal at the repository root:

```bash
set -a; source .env; set +a
uv run --project packages/czquant czquant monitor polymarket-us --interval 60
```

Wait for status healthy with an empty discrepancies list from each component
(account and polymarket_us). Resolve degraded or failed checks using the
[account monitor guide](docs/services/account-monitor.md). Keep the collectors
running during live trading and through reconciliation afterward. Each strategy
sets its own readiness policy; the cross-market example below pauses live
entries when either account monitor is stale, missing or discrepant.

### Run a strategy: cross-market example

This example selects one reviewed pair and an existing portfolio containing one
Kalshi pool and one Polymarket US pool. Other strategies use their own inputs and
entry points; account and portfolio management remain the shared CLI's job.
Install this strategy's environment and discover candidate pairs:

```bash
uv sync --locked --project strategies/cross-market-polymarket
uv run --project strategies/cross-market-polymarket python scripts/cross-market-pairs/prepare.py \
  --venue polymarket-us --leagues nba --out "$EXPERIMENT_DIR/candidates.json"
cp experiments/example/config.example.yaml "$EXPERIMENT_DIR/config.yaml"
```

Discovery reads public data and does not trade. Review the pairs array, including
outcome mappings, deadlines, ties and postponement rules at both venues. Matching
names alone do not establish equivalent payouts. Use another supported league
if no suitable pair is returned; see the [discovery guide](scripts/cross-market-pairs/README.md).

In config.yaml, set portfolio to the ID created above and pair_index to the
reviewed pair's zero-based index. pair_file resolves relative to the config;
keep candidates.json beside it and unchanged for reproducible restarts.

For the paper example, the defaults select game-a, mode: paper and once: true.
For the live example, set id and portfolio to game-live, mode to live, and once
to false. Set parameters.max_basket_dollars and parameters.max_total_dollars to
20 for the $20 allocation. Set parameters.max_capital_frac to 1 to make the full
allocation available; the default 0.1 limits outstanding cost to 10% of
contributed capital. Both live monitors must be healthy before starting.

```bash
uv run --project strategies/cross-market-polymarket python strategies/cross-market-polymarket/runner.py \
  --config "$EXPERIMENT_DIR/config.yaml"
```

Live mode can submit real orders. Setting parameters.dry_run to true builds
orders without sending them, but skips monitor freshness checks; a dry run does
not establish monitor readiness. A scan can make no trades when no basket meets
the configured return hurdle. For repeated scans, set once to false and
parameters.duration_minutes to a time limit, or null to run until Ctrl-C.
Restart with only the runner command. See the
[strategy guide](strategies/cross-market-polymarket/README.md) for execution and recovery.

The [example directory](experiments/example/) also contains a shell script that
calls the shared CLI and this runner for a new paper portfolio after candidate
review. Do not use it to recreate a portfolio that already exists.

### Inspect results and retire a portfolio

These commands work independently of the strategy. Use your portfolio ID;
replace game-a with game-live for the live example:

```bash
uv run --project packages/czquant czquant portfolios inspect game-a --events 20
```

Inspection reports positions, cash, fees, realized and unrealized P&L, and ledger
events. Portfolio totals overlap pool rows; do not add them together. Open
positions need fresh recorded marks for complete valuation. Missing or stale
marks produce null net P&L, not zero profit. For paper portfolios, refresh the
references for their venues before inspecting again:

```bash
uv run --project packages/czquant czquant monitor market --once
uv run --project packages/czquant czquant monitor polymarket-us --once
uv run --project packages/czquant czquant portfolios inspect game-a
```

The market component covers Kalshi; polymarket-us covers Polymarket US. For
international Polymarket, use monitor polymarket. Live collectors already running
continue recording account evidence. For frozen reports and a browser dashboard,
see the [reporting guide](docs/services/reporting.md).

Stop the strategy with Ctrl-C. Keep live monitors running until outstanding
orders, fills and settlements reconcile. Stopping does not close positions.
Once flat with no unresolved orders, retire the portfolio:

```bash
uv run --project packages/czquant czquant portfolios delete game-a
uv run --project packages/czquant czquant portfolios inspect game-a --events 20
```

Registered allocations return their remaining cash to their account reserves.
Independent paper pools record a capital withdrawal. Ledger history and logs
remain available. Stop monitors only when no other live strategy needs them.

The older election strategy has its own [quickstart](strategies/2026-midterm-prediction-arbitrage/README.md).

## Shared services

Install the shared CLI separately from strategy environments. From the
repository root:

```bash
uv sync --locked --project packages/czquant
uv run --project packages/czquant czquant --help
```

Use the suggested state directory outside the checkout, and export the same
value in every strategy, monitor, and reporting shell:

```bash
export STRATEGY_RUNS_DIR="$HOME/.czquant/runs"
mkdir -p "$STRATEGY_RUNS_DIR"
```

The directory holds accounting.sqlite3 and supporting artifacts. Services do
not load .env files; export their variables explicitly. The --runs-dir option
on the shared CLI can select a different root for inspection or restore.

- [Operator CLI](docs/cli.md): portfolio creation, listing, inspection, retirement,
  and state-directory setup.
- [Kalshi broker](docs/services/kalshi-broker.md): paper orders, authentication,
  live mapping, allocation operations, and recovery.
- [Polymarket broker](docs/services/polymarket-broker.md): international outcome
  tokens, US paper/live orders, and settlement collection.
- [Market monitor](docs/services/market-monitor.md): public reference data,
  paper settlements, coverage, and process operation.
- [Account monitor](docs/services/account-monitor.md): authenticated evidence,
  live fill recovery, reconciliation, and halt inspection.
- [Virtual subaccounts](docs/services/virtual-subaccounts.md): registered accounts,
  reserves, and loading or unloading cash pools on one account while it trades.
- [Reporting](docs/services/reporting.md): comparisons, dashboard, export,
  valuation policy, and backup/restore.

The guides describe available commands. The design contract can require
boundaries that existing implementations have not yet enforced; capability
limits are stated in the relevant service guide. All new strategy trades
must use the broker contract.

## Runs and reporting

Reporting lives in packages/czquant, outside strategies. Its dashboard and
frozen comparison reports read the shared accounting ledger and recorded
market and account evidence. Strategies record executions through brokers
and can attach diagnostics; they do not supply reporting implementations.
See [reporting](docs/services/reporting.md) for commands and portfolio lifetime.

The [cross-market strategy](strategies/cross-market-polymarket/#prepare-configuration)
selects one pair and an existing portfolio with runner.py --config FILE.yaml.
Each experiment uses two venue cash pools and two brokers in both
paper and live mode. A reporting portfolio groups those pools without merging
their spending authority. Reusing a pool preserves its cash, positions, and
losses across runs. Paper funding can be arbitrarily large but remains bound
to its venue. Reports show pool results and portfolio totals; historical pooled
records remain readable with a deprecation label.

## Create a new strategy

1. From the repo root, run the script with a new strategy id. Use lowercase letters, digits, and hyphens, for example `2026-midterm-prediction-arbitrage`. The script copies `strategies/_template/` to `strategies/<strategy-id>/` and sets the project name.

   ```bash
   scripts/new-strategy.sh my-new-strategy
   ```

2. Install and run the empty strategy to check the scaffold. The command must exit with no output and no error.

   ```bash
   cd strategies/my-new-strategy
   uv sync
   cp .env.example .env
   set -a; source .env; set +a
   uv run strategy
   ```

3. Write the strategy in `src/strategy/runner.py`, or replace the scaffold with the structure the experiment needs. Route every trade through a broker bound to its venue and cash pool; see the [design principles](docs/design-principles.md) and [broker example](docs/services/kalshi-broker.md#paper-execution). Read secrets with `os.environ`. The code must not read `.env`; the user exports the variables. The scaffold's options go in `cli.py` and pass to `run()`.

   ```python
   import os


   def run() -> None:
       api_key = os.environ["EXAMPLE_API_KEY"]
       ...
   ```

4. Add dependencies with `uv add <package>`.

5. List every environment variable the strategy reads in `.env.example`, with a comment that says where to get the value.

6. Fill in the strategy `README.md`: the market, the edge, the risks, and how to run it.

7. Add the strategy to the Strategies list in this README.

8. Commit these files with `pyproject.toml` and `uv.lock`. Do not commit `.env`.

## Layout

```
cz-quant/
├── README.md
├── scripts/
│   ├── new-strategy.sh          # creates a strategy from the template
│   └── cross-market-pairs/      # discovery, matching and prepared pair export
├── experiments/                 # tracked example; private experiment folders are ignored
│   └── example/                 # configs and a shell example calling the CLI and runner
├── packages/czquant/            # broker, accounting, monitors, reports
├── research/                    # independent data collection and analysis
├── docs/design-principles.md    # authoritative development contract
├── runs/                        # legacy JSONL output; shared state lives outside worktrees
│   └── <strategy-id>/<UTC time>/
│       ├── ledger.jsonl         # flat accounting rows: fill, settlement, mark
│       ├── events.jsonl         # scans and signals, diagnostics
│       ├── run.json             # run metadata
│       └── run.log              # what the run printed
└── strategies/
    ├── _template/               # optional starting scaffold
    └── <strategy-id>/
        ├── pyproject.toml       # standalone install
        ├── uv.lock              # pinned dependencies, committed
        ├── .env.example         # every variable the strategy reads, committed
        ├── .env                 # real secrets, gitignored, exported by the user
        ├── README.md            # the market, the edge, the risks, how to run
        └── src/strategy/
            ├── cli.py           # the single entry point, the `strategy` command
            └── runner.py        # run(), the strategy itself
```

## How a strategy runs

The template and election strategies use `uv sync`, then `uv run strategy`,
from the strategy directory. Other strategies use their documented script
entry points. A common command or engine is not a strategy requirement.

`uv sync` creates `.venv/` in the strategy directory from `pyproject.toml` and
`uv.lock`. The `strategy` command is `main()` in `src/strategy/cli.py`. It
parses the command line, then calls `run()` in `src/strategy/runner.py`. A new
strategy starts with an empty `run()`.

Secrets and API endpoint overrides come from exported environment variables.
Strategy parameters use each strategy's documented CLI or configuration file;
the cross-market strategy uses runner.py --config FILE.yaml. No code reads .env files,
and no strategy depends on a dotenv library. An .env file is a shell convenience;
export it before a run with `set -a; source .env; set +a`.

The template and election strategies use a Python package named strategy. Each
strategy installs into its own virtual environment. Script-based strategies
have their own documented entry points.

Do not import code from one strategy into another. If two strategies need the
same research code, copy it so each can change independently. Shared execution
accounting is the required boundary; a shared strategy utility or framework
must not become an additional integration requirement.

## Secrets

Never commit `.env`, API keys, or private key files. The root `.gitignore`
excludes `.env`, `*.pem`, and `*.key` in every directory. Point new strategies
at a demo or paper-trading environment by default, and make the switch to
production an explicit change in `.env`.

## License

Apache 2.0. See [LICENSE](LICENSE).
