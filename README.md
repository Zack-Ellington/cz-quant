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

You need only [uv](https://docs.astral.sh/uv/) and git. uv installs the correct
Python version for each strategy.

For authenticated monitoring and live account mapping, see the
[Kalshi broker guide](docs/services/kalshi-broker.md). Public-data paper runs
do not require account credentials.

```bash
git clone git@github.com:Zack-Ellington/cz-quant.git
cd cz-quant
```

### Run a cross-market paper experiment

A portfolio groups existing venue cash pools. The operator CLI creates the
portfolio and its paper funding once. The strategy selects that portfolio by ID;
restarting a strategy preserves cash, positions, losses and pending orders.
Discovery produces a candidate file before execution. Reporting reads the same
ledger independently of the strategy.

1. Copy the root environment example. It sets STRATEGY_RUNS_DIR to
   "$HOME/.czquant/runs", which is shared across working directories. Public-data paper runs can leave
   the four credential variables empty. Live account monitoring and execution
   require the corresponding venue keys.

   ```bash
   cp .env.example .env
   ```

   After editing .env, export it in each shell. No Python command loads .env.

   ```bash
   set -a; source .env; set +a
   mkdir -p "$STRATEGY_RUNS_DIR/experiments"
   cd strategies/cross-market-polymarket
   uv sync --locked
   ```

   All remaining commands in this quickstart run from this strategy directory.
   Its environment includes the shared czquant CLI.

2. Create a portfolio config. The example creates game-a with $60 in a Kalshi
   paper pool and $40 in a Polymarket US paper pool. Edit IDs and funding before
   creation if needed. Creating the same ID again fails without adding cash.

   ```bash
   cp experiments/portfolio.example.yaml "$STRATEGY_RUNS_DIR/experiments/portfolio.yaml"
   uv run czquant portfolios create --config "$STRATEGY_RUNS_DIR/experiments/portfolio.yaml"
   uv run czquant portfolios list
   ```

   Creation is atomic. Existing pool IDs can be listed in the config instead of
   new paper pool definitions. For live allocations, specify account and cash
   per pool; the command allocates from registered account reserves. See the
   [live config example](strategies/cross-market-polymarket/experiments/portfolio-live.example.yaml).
   A portfolio cannot mix paper and live.

3. Identify a market pair using the reusable discovery CLI. This requests
   public market data and writes candidates; it does not trade.

   ```bash
   uv run python ../../scripts/cross-market-pairs/prepare.py \
     --venue polymarket-us --leagues nba \
     --out "$STRATEGY_RUNS_DIR/experiments/candidates.json"
   ```

   Inspect the pairs array in candidates.json. Review the selected event,
   outcome mappings, deadlines, ties and postponement rules on both venues.
   Matching names alone does not establish equivalent payouts. If no suitable
   pair is returned, select another supported league using the
   [discovery guide](scripts/cross-market-pairs/README.md).

4. Copy the runner config. Set portfolio to the ID created above. Set pair_index
   to the zero-based index of the reviewed pair in candidates.json. pair_file
   resolves relative to the config, so these two files can move together.

   ```bash
   cp experiments/config.example.yaml "$STRATEGY_RUNS_DIR/experiments/config.yaml"
   ```

   The example uses mode: paper and once: true. After reviewing and editing it,
   run one scan. The config supplies the portfolio, pair, timing and sizing;
   no strategy funding arguments are needed.

   ```bash
   uv run python runner.py --config "$STRATEGY_RUNS_DIR/experiments/config.yaml"
   ```

   A scan can legitimately make no trades when no basket meets the configured
   return hurdle. For repeated scans, set once to false and set
   parameters.duration_minutes, or leave duration null to run until Ctrl-C.
   Repeat only the runner command on restart. Use a new portfolio and distinct
   pool IDs for an independent experiment.

5. Inspect positions, P&L and the latest ledger events. The command returns JSON
   with pool ownership, contributed capital, cash, fees, realized and unrealized
   P&L. Portfolio totals overlap pool rows; do not add them together.

   ```bash
   uv run czquant portfolios inspect game-a --events 20
   ```

   Open positions need fresh recorded market data for a complete valuation.
   Missing or stale marks produce null net P&L and a list of missing markets;
   they do not imply zero profit or loss. Refresh paper marks and settlement
   evidence with the independent collectors, then inspect again:

   ```bash
   uv run czquant monitor market --once
   uv run czquant monitor polymarket-us --once
   uv run czquant portfolios inspect game-a
   ```

   For a frozen time-window report, use czquant report as documented in the
   [reporting guide](docs/services/reporting.md). Run czquant dashboard for a
   browser view of the same ledger. The [strategy guide](strategies/cross-market-polymarket/README.md)
   covers live preparation, monitor requirements and recovery. The
   [operator CLI guide](docs/cli.md) covers retiring a flat portfolio with
   czquant portfolios delete game-a.

The older election strategy has its own [quickstart](strategies/2026-midterm-prediction-arbitrage/README.md).

## Shared services

Install the shared CLI separately from strategy environments. From the
repository root:

```bash
cd packages/czquant
uv sync
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
