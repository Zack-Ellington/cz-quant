# Live cross-market launcher

The launcher funds one prepared experiment and supervises its trader and both
account monitors. It allocates ledger cash within each venue account; it does not
transfer cash between venues. The experiment selects the two pool identities,
market pair and strategy parameters. `--cash-per-venue` funds each missing pool
once. Restarting preserves existing cash, losses, positions and reservations.

From the cross-market strategy directory, with credentials and STRATEGY_RUNS_DIR
exported, launch a reviewed live experiment:

```bash
uv run python ../../scripts/cross-market-launch/launch.py \
  --experiment /absolute/private/path/game.json --cash-per-venue 20
```

The launcher reconciles both accounts before allocation and before starting the
trader. Runtime logs and the process manifest are under
STRATEGY_RUNS_DIR/experiments/cross-market/logs/EXPERIMENT_ID. Ctrl-C stops
processes; it does not close positions.

## Replacing an experiment

Add `--retire-pools` followed by the exact old pool IDs to retire. All selected
pools must be flat and have no outstanding orders or reservations. Their cash
returns to their account reserves and their economic history remains intact.
Their configuration files move to the active configuration directory's retired
subdirectory. Historical reports remain available from the ledger.
Retirements and new allocations share one transaction: a failure rolls back the
entire ownership change. Existing replacement pools are never topped up on a
restart. Use new pool identities for a new independent experiment.

Stop the previous launcher first, or supply `--stop-launcher` with its absolute
Python script path. On macOS, the launcher uses lsof and ps to identify the process
holding this ledger's launch lock, checks that its command contains that exact
script path, and sends SIGTERM. The previous supervisor stops its children before
releasing the lock. An ambiguous or unrelated process is not signaled. No
allocations change until the lock is acquired.

Use one launcher per ledger. Other processes using the same pools must also be
stopped before replacement. Completed-game inventory must settle through the
account monitor before its pool can retire; this launcher never fabricates a
settlement or clears an execution halt to bypass outstanding exposure.

## Windows

The launcher runs on Windows. The ledger lock uses a non-blocking byte-range lock
(msvcrt) in place of flock. Each child runs in its own process group and is stopped
with CTRL_BREAK_EVENT, then terminated if it has not exited within the wait.
`--stop-launcher` is refused on Windows: without lsof and ps the lock owner can't be
proven, so stop the previous launcher with Ctrl-C in its terminal before relaunching.
