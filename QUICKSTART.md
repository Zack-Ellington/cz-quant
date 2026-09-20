# Quickstart

Two tasks: install and run the first strategy, and create a new one. Both need
only [uv](https://docs.astral.sh/uv/) and git.

```bash
git clone git@github.com:Zack-Ellington/cz-quant.git
cd cz-quant
```

## Install and run 2026-midterm-prediction-arbitrage

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

6. Run the strategy.

   ```bash
   uv run strategy
   ```

The strategy is a scaffold, so the command exits with no output. The strategy
logic goes in `src/strategy/runner.py`.

## Create a new strategy

1. From the repo root, run the script with a new strategy id. Use lowercase letters, digits, and hyphens.

   ```bash
   scripts/new-strategy.sh my-new-strategy
   ```

2. Install and run the empty strategy to check the scaffold.

   ```bash
   cd strategies/my-new-strategy
   uv sync
   cp .env.example .env
   uv run strategy
   ```

3. Write the strategy in `src/strategy/runner.py`. The `strategy` command calls `run()` after it loads `.env`, so read secrets with `os.environ`.

   ```python
   import os


   def run() -> None:
       api_key = os.environ["EXAMPLE_API_KEY"]
       ...
   ```

4. Add dependencies with `uv add <package>`.

5. Update `.env.example`, the strategy `README.md`, and the strategy list in the root `README.md`. Commit these with `pyproject.toml` and `uv.lock`. Do not commit `.env`.

The root [README.md](README.md) has the full conventions.
