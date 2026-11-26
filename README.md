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

- [cross-market-polymarket](strategies/cross-market-polymarket/): the sum check across Kalshi and Polymarket. Kalshi NBA, NHL and MLB game markets are paired with Polymarket moneylines by date and team abbreviation, and baskets with a leg on each venue use separate brokers. Polymarket US supports paper and live execution; international Polymarket supports paper execution.

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

### Install and run 2026-midterm-prediction-arbitrage

1. Go to the strategy directory.

   ```bash
   cd strategies/2026-midterm-prediction-arbitrage
   ```

2. Install. This creates `.venv/` in the strategy directory.

   ```bash
   uv sync
   ```

3. Create your `.env` from the example.

   ```bash
   cp .env.example .env
   ```

4. Create a Kalshi API key. Kalshi gives you a key ID and an RSA private key file. The example file points at the demo environment, so create the key in a demo account (https://demo.kalshi.co). Production keys do not work on demo.

5. Fill in `.env`. Set `KALSHI_API_KEY_ID` to the key ID. Set `KALSHI_PRIVATE_KEY_PATH` to the path of the private key file.

6. Export the variables into your shell. The code reads the environment only. It does not read `.env`. Do this step in each new shell, and again after you change `.env`.

   ```bash
   set -a; source .env; set +a
   ```

7. Run the strategy.

   ```bash
   uv run strategy
   ```

This strategy is implemented: the command reads the live Kalshi markets, runs
the model-free checks and the independent Monte Carlo, prints the
model-vs-market table, and paper-trades into `runs/` (nothing is ever sent). See its
[README](strategies/2026-midterm-prediction-arbitrage/README.md) for the method,
a sample report, and the reproducible `--snapshot-in` run.

## Shared services

Install the shared CLI separately from strategy environments. From the
repository root:

```bash
cd packages/czquant
uv sync
```

Select an absolute state directory outside the checkout. Replace the example
path with a writable private directory, and export the same value in every
strategy, monitor, and reporting shell:

```bash
export STRATEGY_RUNS_DIR=/absolute/path/to/czquant-runs
mkdir -p "$STRATEGY_RUNS_DIR"
```

The directory holds accounting.sqlite3 and supporting artifacts. Services do
not load .env files; export their variables explicitly. The --runs-dir option
on the shared CLI can select a different root for inspection or restore.

- [Kalshi broker](docs/services/kalshi-broker.md): paper orders, authentication,
  live mapping, allocation operations, and recovery.
- [Polymarket broker](docs/services/polymarket-broker.md): international outcome
  tokens, US paper/live orders, and settlement collection.
- [Market monitor](docs/services/market-monitor.md): public reference data,
  paper settlements, coverage, and process operation.
- [Account monitor](docs/services/account-monitor.md): authenticated evidence,
  live fill recovery, reconciliation, and halt inspection.
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

The [cross-market strategy](strategies/cross-market-polymarket/#experiment-configuration)
selects one pair and two pre-created allocations with --experiment FILE.json.
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
│   └── new-strategy.sh          # creates a strategy from the template
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

Configuration comes from the environment only. No code reads `.env`, and no
strategy depends on a dotenv library. The `.env` file is a convenience for the
user, who exports it with `set -a; source .env; set +a` before a run. This keeps
the code the same on a laptop, in a container, and under a scheduler, where the
platform sets the variables.

The Python package is named `strategy` in every strategy. This is safe because
each strategy installs into its own virtual environment, and it keeps the
template free of names that must be changed.

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
