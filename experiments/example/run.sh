#!/usr/bin/env bash
# Run after preparing and reviewing candidates.json beside these configs.
# This creates a new paper portfolio once. Restart with runner.py directly.
set -euo pipefail

: "${STRATEGY_RUNS_DIR:?Export STRATEGY_RUNS_DIR from the repo .env first}"
experiment_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(cd -- "$experiment_dir/../.." && pwd)
cd "$repo_root"

portfolio_id=$(uv run --locked --project packages/czquant python -c \
  'import sys; from czquant.config import load_config; print(load_config(sys.argv[1])["id"])' \
  "$experiment_dir/portfolio.example.yaml")

uv run --locked --project packages/czquant czquant portfolios create --config "$experiment_dir/portfolio.example.yaml"
uv run --locked --project packages/czquant czquant portfolios list
uv run --locked --project strategies/cross-market-polymarket python strategies/cross-market-polymarket/runner.py \
  --config "$experiment_dir/config.example.yaml"
uv run --locked --project packages/czquant czquant portfolios inspect "$portfolio_id" --events 20
