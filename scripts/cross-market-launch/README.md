# Live cross-market launcher

launch.py starts one independent live strategy process per prepared Kalshi / Polymarket US pair. It assigns $10 per venue per game, with a $20 exposure cap per game, and starts both account monitors. It submits real orders through the existing strategy.

Export the credentials and STRATEGY_RUNS_DIR described in the strategy README. Use the existing ledger for accounts already managed by cz-quant. Supply a reviewed candidate file from scripts/cross-market-pairs/prepare.py:

```bash
uv run --locked --project strategies/cross-market-polymarket python scripts/cross-market-launch/launch.py --pairs "$STRATEGY_RUNS_DIR/candidates.json"
```

The launcher reuses the single registered live account at each venue. With no registration, it registers Kalshi's primary account and Polymarket US. Unrecorded positions, unknown orders, insufficient reserve cash, or failed reconciliation prevent startup. It does not deposit or transfer venue funds. Existing game pools retain their balances and are never topped up on restart.

Each game gets exact market scopes, its own reporting portfolio, and its own configuration and log under STRATEGY_RUNS_DIR/experiments/cross-market. The strategy retains its normal edge, fee, monitor and repair checks. NFL settlement rules still require review; two-way NFL pairs do not guarantee a full payout on a tie.

Leave the terminal open and the computer awake. A ledger lock prevents a second instance of this launcher. Other launch methods must not use the same pools concurrently. A failed monitor stops the strategies; a failed game process leaves the other game running. Ctrl-C stops all child processes. Stopping does not liquidate positions or clear uncertain orders. Retain the ledger and run the account monitors through reconciliation and settlement.

Validate without account access or order submission:

```bash
uv run --locked --project strategies/cross-market-polymarket pytest scripts/cross-market-launch/tests -q
uv run --locked --project strategies/cross-market-polymarket basedpyright scripts/cross-market-launch/launch.py
```
