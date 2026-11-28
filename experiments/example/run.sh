#!/usr/bin/env bash
# Run after preparing and reviewing candidates.json beside these configs.
# This creates a new paper portfolio once. Restart with runner.py directly.
set -euo pipefail

: "${STRATEGY_RUNS_DIR:?Export STRATEGY_RUNS_DIR from the repo .env first}"
experiment_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(cd -- "$experiment_dir/../.." && pwd)
cd "$repo_root/strategies/cross-market-polymarket"

portfolio_id=$(uv run --locked python -c \
  'import sys; from czquant.config import load_config; print(load_config(sys.argv[1])["id"])' \
  "$experiment_dir/portfolio.example.yaml")

uv run --locked czquant portfolios create --config "$experiment_dir/portfolio.example.yaml"
uv run --locked czquant portfolios list
uv run --locked python runner.py --config "$experiment_dir/config.example.yaml"
uv run --locked czquant portfolios inspect "$portfolio_id" --events 20
