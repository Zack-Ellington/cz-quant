# cz-quant

Quantitative trading strategies. The first venue is the Kalshi prediction
market.

The repo is a monorepo of strategies. Each strategy is a standalone project:
it has its own install, its own secrets, and one command that runs it. There is
no shared library and no root install, so a strategy can be deployed, pinned,
or retired without an effect on the others.

## Strategies

- [2026-midterm-prediction-arbitrage](strategies/2026-midterm-prediction-arbitrage/): arbitrage on Kalshi markets for the 2026 US midterm elections. Scaffold only.

## Quickstart

You need only [uv](https://docs.astral.sh/uv/) and git. uv installs the correct
Python version for each strategy.

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

The strategy is a scaffold, so the command exits with no output. The strategy
logic goes in `src/strategy/runner.py`.

### Create a new strategy

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

3. Write the strategy in `src/strategy/runner.py`. Read secrets with `os.environ`. The code must not read `.env`; the user exports the variables. Add modules under `src/strategy/` as the strategy grows. Add options to the parser in `cli.py` and pass them to `run()`.

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
└── strategies/
    ├── _template/               # the template that every strategy starts from
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

Every strategy has the same interface: `uv sync`, then `uv run strategy`, from
the strategy directory.

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
same code, copy it first. Propose a shared package only when a second real use
makes the duplication a problem.

## Secrets

Never commit `.env`, API keys, or private key files. The root `.gitignore`
excludes `.env`, `*.pem`, and `*.key` in every directory. Point new strategies
at a demo or paper-trading environment by default, and make the switch to
production an explicit change in `.env`.

## License

Apache 2.0. See [LICENSE](LICENSE).
