# Example experiment

This example uses the cross-market strategy to demonstrate separate portfolio
management and strategy execution. Portfolio operations use packages/czquant;
only discovery and execution use the strategy environment.
portfolio.example.yaml creates paper pools. config.example.yaml selects their
portfolio and one reviewed pair from candidates.json. run.sh calls the CLI to
create and list the portfolio, run the configured strategy, and inspect P&L.
It does not register accounts, supervise monitors, or retire portfolios.

From the repository root, after exporting .env as described in the
[shared setup](../../README.md#set-up-the-shared-cli-and-environment):

```bash
uv sync --locked --project packages/czquant
uv sync --locked --project strategies/cross-market-polymarket
cp -R experiments/example experiments/my-paper-run
uv run --project strategies/cross-market-polymarket python scripts/cross-market-pairs/prepare.py \
  --venue polymarket-us --leagues nba --out experiments/my-paper-run/candidates.json
```

Review the selected pair and both venues' settlement rules. Set pair_index in
config.example.yaml to that pair's zero-based index. Use matching portfolio IDs
in both configs and distinct pool IDs for each independent experiment. Then run:

```bash
bash experiments/my-paper-run/run.sh
```

The default is one paper scan. No qualifying basket is a valid result. Creation
refuses reused IDs; to resume the same portfolio, run only the strategy:

```bash
uv run --project strategies/cross-market-polymarket python strategies/cross-market-polymarket/runner.py \
  --config experiments/my-paper-run/config.example.yaml
```

For live allocations, portfolio-live.example.yaml uses registered accounts and
$10 per venue. Follow the [shared portfolio setup](../../README.md#create-a-portfolio) and
[strategy-run example](../../README.md#run-a-strategy-cross-market-example)
to create that portfolio, start both monitors and run a reviewed live config.
The JSON game examples show inline pairs with placeholder markets; replace them
with reviewed inputs before running.

Only experiments/example is tracked. Other experiment directories and the
example's generated candidates.json are ignored. Keep private configs and
prepared inputs when removing a checkout; Git does not preserve them. The ledger
and execution logs remain under STRATEGY_RUNS_DIR.
