# cz-quant

Quantitative trading strategies. The first venue is the Kalshi prediction
market.

The repo is a monorepo of strategies. Each strategy is a standalone project:
it has its own install, its own secrets, and one command that runs it. There is
no shared library and no root install, so a strategy can be deployed, pinned,
or retired without an effect on the others.

New here? Read [QUICKSTART.md](QUICKSTART.md).

## Layout

```
cz-quant/
├── README.md
├── QUICKSTART.md
├── scripts/
│   └── new-strategy.sh          # creates a strategy from the template
└── strategies/
    ├── _template/               # the template that every strategy starts from
    └── <strategy-id>/
        ├── pyproject.toml       # standalone install
        ├── uv.lock              # pinned dependencies, committed
        ├── .env.example         # every variable the strategy reads, committed
        ├── .env                 # real secrets, gitignored
        ├── README.md            # the market, the edge, the risks, how to run
        └── src/strategy/
            ├── cli.py           # the single entry point, the `strategy` command
            └── runner.py        # run(), the strategy itself
```

## Strategies

- [2026-midterm-prediction-arbitrage](strategies/2026-midterm-prediction-arbitrage/): arbitrage on Kalshi markets for the 2026 US midterm elections. Scaffold only.

## How a strategy runs

Every strategy has the same interface. From the strategy directory:

```bash
uv sync
uv run strategy
```

`uv sync` creates `.venv/` in the strategy directory from `pyproject.toml` and
`uv.lock`. The `strategy` command is `main()` in `src/strategy/cli.py`. It
parses the command line, loads `.env` from the strategy directory, then calls
`run()` in `src/strategy/runner.py`. A new strategy starts with an empty `run()`.

The Python package is named `strategy` in every strategy. This is safe because
each strategy installs into its own virtual environment, and it keeps the
template free of names that must be changed.

## Create a new strategy

1. Pick a strategy id. Use lowercase letters, digits, and hyphens, for example `2026-midterm-prediction-arbitrage`.
2. Run `scripts/new-strategy.sh <strategy-id>`. The script copies `strategies/_template/` to `strategies/<strategy-id>/` and sets the project name.
3. In the new directory, run `uv sync`, then `uv run strategy`. The command must exit with no output and no error.
4. Write the strategy in `src/strategy/runner.py`. Add modules under `src/strategy/` as the strategy grows. Add options to the parser in `cli.py` and pass them to `run()`.
5. Add dependencies with `uv add <package>`. Commit `uv.lock`.
6. List every environment variable the strategy reads in `.env.example`, with a comment that says where to get the value.
7. Fill in the strategy `README.md`: the market, the edge, the risks, and how to run it.
8. Add the strategy to the list in this README.

Do not import code from one strategy into another. If two strategies need the
same code, copy it first. Propose a shared package only when a second real use
makes the duplication a problem.

## Secrets

Never commit `.env`, API keys, or private key files. The root `.gitignore`
excludes `.env`, `*.pem`, and `*.key` in every directory. Point new strategies
at a demo or paper-trading environment by default, and make the switch to
production an explicit change in `.env`.

## Requirements

- [uv](https://docs.astral.sh/uv/). uv installs the correct Python version for each strategy.
- git

## License

Apache 2.0. See [LICENSE](LICENSE).
