# strategy-template

TODO: describe the strategy. State the market, the edge, and the risks.

## Install

```bash
uv sync
cp .env.example .env
```

Fill in `.env`. Then export the variables into your shell. Do this in each new
shell, and again after you change `.env`.

```bash
set -a; source .env; set +a
```

## Run

```bash
uv run strategy
```

The `strategy` command calls `run()` in `src/strategy/runner.py`. The code reads
configuration from the environment only. It does not read `.env`.
