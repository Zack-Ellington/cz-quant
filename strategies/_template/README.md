# strategy-template

TODO: describe the strategy. State the market, the edge, and the risks.

## Install

```bash
uv sync
cp .env.example .env
```

Fill in `.env` before the first run.

## Run

```bash
uv run strategy
```

The `strategy` command loads `.env`, then calls `run()` in `src/strategy/runner.py`.
