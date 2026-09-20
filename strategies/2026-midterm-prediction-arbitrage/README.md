# 2026-midterm-prediction-arbitrage

Arbitrage on Kalshi prediction markets for the 2026 United States midterm
elections. The target is a set of related contracts whose prices are
inconsistent with each other, for example mutually exclusive outcomes whose
prices do not sum to 1 after fees.

Status: scaffold only. `run()` in `src/strategy/runner.py` is empty. The
strategy logic, the fee model, and the risk limits are not written yet.

## Install

```bash
uv sync
cp .env.example .env
```

Fill in `.env`. `.env.example` points at the Kalshi demo environment, which uses
separate API keys from production. Stay on demo until the strategy is tested.

Then export the variables into your shell. Do this in each new shell, and again
after you change `.env`.

```bash
set -a; source .env; set +a
```

## Run

```bash
uv run strategy
```

The `strategy` command calls `run()` in `src/strategy/runner.py`. The code reads
configuration from the environment only. It does not read `.env`.

## References

- Kalshi API documentation: https://docs.kalshi.com
- Authentication uses three headers: `KALSHI-ACCESS-KEY`,
  `KALSHI-ACCESS-TIMESTAMP`, and `KALSHI-ACCESS-SIGNATURE` (RSA-PSS with
  SHA-256 over timestamp + method + path).
