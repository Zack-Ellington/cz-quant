# Cross-market Polymarket experiments

Each run trades one prepared market pair. Its portfolio selects two funded cash
pools, one per venue. The strategy prices the pair, sizes a complete set and
submits every entry and repair through the broker for that venue's pool.
Multiple independent experiments use distinct pools and run as separate processes.

A complete set has an estimated $1 payout only when the venues resolve equivalent
outcomes. Review deadlines, ties, postponements and settlement rules before
selecting the pair. Matching names alone does not establish equivalent payouts.

## Separation of responsibilities

[Pair preprocessing](../../scripts/cross-market-pairs/) discovers markets, matches
teams or questions, applies aliases and reviewed overrides, and exports outcome
mappings. It does not open cash pools or execute orders. The runtime does not
import these tools, enumerate markets or refresh its selection. It reads metadata
and prices only for the pair in its configuration.

Cash pools persist independently of runs. Brokers enforce venue, mode, ownership,
market scope, reservations and available cash. Startup never creates or funds a
pool. A new experiment ID does not reset cash, losses or positions. The
czquant portfolios create command creates a reporting group and can explicitly
fund new paper pools. The strategy resolves that group by ID to its two venue
pools. The group has no additional buying power.

```mermaid
flowchart LR
    D[Discovery and review] --> P[Prepared pair]
    P --> S[One strategy run]
    S --> K[Kalshi broker]
    S --> U[Polymarket broker]
    K --> KC[Funded Kalshi cash pool]
    U --> UC[Funded Polymarket cash pool]
    K --> L[(Economic log)]
    U --> L
    L --> R[Independent reporting]
```

The [design principles](../../docs/design-principles.md) define the broker contract.
The [Kalshi](../../docs/services/kalshi-broker.md) and
[Polymarket](../../docs/services/polymarket-broker.md) service guides cover account
mapping, execution capabilities and explicit capital operations.

## Prepare configuration

The runtime interface is `uv run python runner.py --config FILE.yaml` from this
strategy directory. The file specifies an ID, an existing portfolio ID, mode,
one prepared pair, once and parameters. JSON is also accepted. All run options
can live in the file. Secrets come from exported environment variables; no
dotenv file is loaded. The root [quickstart](../../README.md#run-a-strategy-cross-market-example)
walks through the complete paper workflow.

Copy [config.example.yaml](../../experiments/example/config.example.yaml) to your private
experiment directory. Its pair_file points to a saved discovery candidate file,
relative to the config. pair_index explicitly selects one element of its pairs
array, starting at zero. Review that pair and the venue rules before running;
the runner never discovers or changes pairs. Do not overwrite the candidate
file for an experiment you intend to reproduce.

```yaml
id: game-a
portfolio: game-a
mode: paper
pair_file: candidates.json
pair_index: 0
once: true
parameters:
  max_sets: 20
  max_basket_dollars: 25
  duration_minutes: null
```

The portfolio must already exist, contain exactly one active pool at each
selected venue, and match the configured mode. A missing or invalid portfolio
fails before the engine starts. For an independent experiment, create another
portfolio with distinct pools; changing only the experiment ID does not isolate
capital. The resolved pool IDs and full selected pair are recorded with each run.

An inline pair object can replace pair_file and pair_index. The complete
[game-a JSON example](../../experiments/example/game-a.example.json) shows that shape, including
the alternative portfolio mapping from venue names to existing pool IDs.
Use runner.py --config for both YAML and JSON; once in the config requests a single scan.
For YAML inline pairs, quote resolves_at so it remains a string.

Scan diagnostics retain the books used for decisions. The independent
[experiment companion](../../scripts/cross-market-monitor/) reads them without
making exchange requests. See [Streaming execution quotes](#streaming-execution-quotes)
for capture timing, freshness evidence and diagnostic limitations.

The pair object has venue, kalshi_event, kalshi_series, polymarket_event,
polymarket_market, resolves_at, outcomes and optional evidence. Preprocessing
resolves aliases and question inversion into the two explicit outcome mappings.
For US markets, the mappings use YES and NO on one PMUS: market slug. For
international markets, they use two PM: token IDs with side YES; only paper
execution is supported. resolves_at is a timezone-aware estimate used for the
return hurdle. evidence retains the matching method and reviewed rules.

Parameters omitted from the file use [Config defaults](trader.py):

- Streaming runs evaluate on book updates. interval_seconds defaults to 0.1 seconds
  and controls the recovery watchdog; it does not delay stream updates. In explicit
  REST mode it is the polling interval, subject to request duration and venue limits.
  A null duration_minutes runs until stopped. There is no trade cooldown.
- max_capital_frac limits outstanding cost relative to contributed capital.
  max_sets limits basket size. max_basket_dollars and max_total_dollars impose
  absolute limits; null disables the respective dollar cap.
- risk_free, min_net, min_edge and max_apr control the return hurdle.
  min_edge defaults to $0.01 net profit per matched pair after fees. min_net
  defaults to zero, so there is no separate basket-profit floor unless specified.
- require_cross defaults to true. False permits both outcomes at one venue.
  Opposite sides of the same market are refused because their fills would net.
- kalshi_latency and polymarket_latency specify paper execution delays.
  Repairs sell at visible bids, with no entry-price loss limit.
- pmus_theta overrides US pricing and paper fees. Null uses the market coefficient;
  this parameter does not change live venue fees.
- dry_run requires live mode and builds orders without submitting them.
  monitor_max_age limits the age of live monitor evidence.

Both brokers record the full resolved configuration and code revision at run
start. Each order includes the experiment ID, selected pair and shared trade ID.
Reporting reads the economic log without importing the strategy.

## Create a portfolio and run

The [operator CLI guide](../../docs/cli.md) documents portfolio commands and
retirement after positions and orders are cleared.

Copy [portfolio.example.yaml](../../experiments/example/portfolio.example.yaml), edit its IDs
and funding, and create it once through the operator CLI. Copy the example directory
to an ignored experiments/my-paper-run folder at the repository root, then prepare
and review its candidates as shown in the root quickstart. From the repository root:

```bash
uv sync --locked --project packages/czquant
uv sync --locked --project strategies/cross-market-polymarket
uv run --project packages/czquant czquant portfolios create --config experiments/my-paper-run/portfolio.example.yaml
uv run --project packages/czquant czquant portfolios list
uv run --project strategies/cross-market-polymarket python strategies/cross-market-polymarket/runner.py \
  --config experiments/my-paper-run/config.example.yaml
uv run --project packages/czquant czquant portfolios inspect game-a --events 20
```

Repeat only the runner command on restart. Cash, positions, losses and
reservations remain in the same pools. The pools can be used by a later run;
unresolved orders or incompatible holdings must be reconciled first. Each run
considers one pair. Use one process per experiment. Run additional processes
with separate configs and distinct portfolios for independent experiments.

The strategy owns execution only. Create, allocate, inspect and report using
czquant commands in separate steps. For longer runs, set once to false; a null
parameters.duration_minutes runs until Ctrl-C. Stopping does not close positions.

## Live preparation and recovery

Live execution requires Polymarket US and pre-created live allocations. Follow
the [virtual-subaccount guide](../../docs/services/virtual-subaccounts.md):
register each venue account with czquant account, then specify account and cash
for each pool in a [live portfolio config](../../experiments/example/portfolio-live.example.yaml).
czquant portfolios create --config FILE.yaml allocates from both reserves and
creates the portfolio atomically. The portfolio selects accounts and cash only.
The runner config selects the portfolio ID and the market pair; brokers enforce
venue ownership and available cash. Several US pools require account-wide reconciliation;
repeating map-live cannot prepare these allocations. Registered accounts also
prevent experiments from opening opposite sides of the same market.
Set mode to live, select the live portfolio ID and set parameters.dry_run to true
for a dry run. Export the credentials listed in .env.example. The runtime does
not create accounts, move funds or manage allocation ownership.

The [shared monitor setup](../../README.md#start-live-account-monitors) gives
commands for both monitors and the healthy-status check. Portfolio setup and
retirement use the shared CLI, independently of this strategy. Run the independent market and account collectors documented in the
service guides against the same logging root. Live runs wait for fresh account and
Polymarket US monitor evidence. Dry runs skip monitor freshness checks while
retaining allocation and unresolved-order checks.

Basket sizing uses the available cash in each selected portfolio's broker pool,
including existing reservations. Candidate sizes must cover both the broker's
fee allowance and the quoted fees at each leg's limit price. The search reduces
quantity to fit each venue before choosing a profitable basket; it cannot use
another game's allocation or the account reserve. Aggregate capital and profit
thresholds still apply.

Before submitting either leg, the strategy reprices the selected pair and calls
Broker.check for both intended orders. It also checks aggregate basket cost and
existing holdings. Broker.submit repeats its checks and reserves funds atomically
for each order. Separate venue orders are not atomic; a preflight result does
not reserve cash or guarantee a fill. Insufficient funding, opposite inventory
or a reconciliation pause records a rejection and leaves the process running.
Invalid pool, scope or pair configuration raises an error before submission.

The thinnest leg goes first as an IOC. Later legs use the smallest known fill.
Known partial fills or failures trigger reduce-only repair of excess holdings.
Unknown results stop further submissions and retain reservations for reconciliation.
Execution failures leave a recoverable entry halt and keep the recovery loop alive.
After all outstanding orders reconcile and both account monitors are healthy,
the strategy compares the two outcomes' total quantities across this experiment's
pools. It retains matched pairs and sells only excess contracts. This comparison
uses actual holdings, so FIFO lot attribution cannot cause duplicate repairs.

Each repair reads fresh bids and sends a reduce-only IOC with a limit at the worst
visible bid needed for the offered quantity. It can realize a loss greater than
5 cents per contract. No bids or a closed market leave recovery pending. Partial
exits retry on later scans with a fresh book and the reconciled remainder. A lost
repair acknowledgement blocks further submissions until the monitor resolves it;
restarting does not duplicate an uncertain sell. At most one recovery order is
submitted per scan. Matched inventory partway through settlement is retained.

Restart reads the existing economic records. It does not retry
uncertain orders with new IDs. Acknowledged orders pause entries without a
persistent halt until the monitor books their fills and releases reservations.
Pending or unknown writes still halt entries. Execution-recovery halts clear
automatically only after reconciliation leaves no excess holdings. Settlement
losses, missing provenance, monitor discrepancies, manual halts and KILL files
remain hard stops, including for recovery. Inspect and resolve those conditions
before explicitly clearing their halt:

```bash
uv run czquant halts game-a-kalshi
uv run czquant clear-halt game-a-kalshi --halt-id HALT_RECORD_ID --reason "Reviewed reconciliation and remaining exposure"
```

Clearing a halt does not erase orders or inventory; unresolved conditions halt
again. Keep the ledger and pool identities during recovery. The independent
[reporting service](../../docs/services/reporting.md) provides performance views,
export and backup.

## Validation

Offline tests use fake venues, including fake live adapters:

```bash
uv run pytest -q
uv run basedpyright
```

These checks cover preprocessing exports, one-pair runtime isolation, independent
cash pools, whole-basket preflight, retained balances and reservations, restarts,
partial-fill repairs and unknown execution outcomes. Type checking includes the
preprocessing bundle.

### Profit-bounded slippage

Set `parameters.profit_slippage` to `true` for a Kalshi/Polymarket US
experiment to let IOC orders cross beyond the observed book prices. The default
is `false`. The strategy splits the remaining after-fee profit allowance equally
between the legs, then reallocates unused allowance in submission order when
price increments or pool caps leave room. It preserves `min_edge` at the limit
prices and respects
broker reservations and the basket capital cap. Limits use the market price
increment (a conservative one-cent increment for Kalshi).

After the first fill, the broker submit result supplies execution quantity,
YES notional, fees, and price bounds for the second limit. Paper and Polymarket
US adapters summarize their exact receipts. Kalshi supplies validated V2 response
averages for cost and fees, and uses the submitted limit as a conservative bound
on individual fills. An average alone cannot bound a partially matched basket.
The account monitor still books Kalshi receipts and releases filled reservations;
the response summary does not create spendable cash or duplicate accounting.
A confirmed terminal empty order releases its reservation immediately.

The calculation allocates first-leg fees proportionally to matched contracts and
bounds second-leg fees for each possible integer partial fill. Missing or invalid
execution evidence creates a recoverable halt. If no second limit meets the
floor, the existing automatic repair sells the unmatched first leg. Repair losses
and a tied game are outside the completed-basket profit floor. Slippage does not
guarantee execution. Rechecks price only the originally selected contracts, using
the same configured thresholds as entry; there is no lower recheck threshold.

Slippage observations record the second limit, first-leg price bound, fees, and
matched target quantity. These diagnostics are batched after execution. Quote
checks are attached to each order's durable submission context, without separate
observation commits between the quote check and submit. The original expected
profit remains the book-price estimate; requests record the submitted limits.

Each new attempt requires current quotes, funding, and resolved prior orders.
There is no additional wait after a successful or confirmed empty IOC. Unknown
writes and unmatched inventory retain their reconciliation and recovery gates.

### Streaming execution quotes

Set parameters.execution_websocket to true to use authenticated Kalshi and
Polymarket US order books for entry pricing, rechecks, and repair bids. Both
venue credentials are required. Stream messages wake the execution loop immediately;
updates arriving during execution are coalesced into the next evaluation. The
100 ms default watchdog checks recovery without repeating unchanged quote scans.
Heartbeats are written every five seconds. Venue rate limits still apply, and a
filled Kalshi order still pauses new entries until accounting reconciles it.
This scheduling policy is not a guarantee of venue execution latency.

REST remains the source of market metadata and fee schedules. A strategy-owned
worker refreshes them every five seconds, outside the quote-to-order path, using
separate read clients. A failed refresh invalidates the reference snapshot; data
older than 15 seconds from the start of its request is also unavailable. These
reference intervals do not control quote sampling. Request failures pause
execution and retry. Unexpected worker errors are logged with a traceback and
propagated to metadata readers. Paper execution uses the refreshed fee schedule
for each order.

Streaming execution never falls back to REST books. Explicit REST mode requires
a complete, open Polymarket book with exchange and receipt ages within
quote_max_age; missing timestamps and cached old books visibly block execution.
No cache-busting workaround silently makes an old book current. Kalshi REST
snapshots record request duration and reject reads slower than quote_max_age;
they do not provide the sequence/liveness evidence of streaming books. Streaming
is the execution mode for low-latency runs. Paper fills use the same selected
readers and freshness checks as pricing, including checks after simulated latency.

Kalshi starts with full snapshots for both selected tickers and applies deltas
in subscription sequence order. A missing or repeated sequence number, invalid
delta, subscription error, or disconnect invalidates both books. Reconnection
signs a new handshake and requires both snapshots before execution resumes.
The stream uses immediate updates, without conflation. See Kalshi's
[order-book protocol](https://docs.kalshi.com/websockets/orderbook-updates).

Kalshi book age and connection health are separate. A quiet book remains usable
while sequence continuity holds and client-initiated ping/pong checks succeed.
quote_max_age (default 2 seconds) bounds time since a confirmed pong and the
transmission age of timestamped incoming messages. Pings run at half that
interval; an unanswered ping reconnects after half that interval. A missing
server timestamp is retained as unknown, never invented. Snapshot and delta
receipt ages remain in the log even when a quiet book is valid. Kalshi documents
client pings in its [keep-alive protocol](https://docs.kalshi.com/websockets/connection-keep-alive).

Polymarket US sends full snapshots without debouncing. Its existing conservative
policy requires exchange and receipt ages within quote_max_age (default two
seconds); duplicate or older snapshots do not renew freshness. The published
[full-book protocol](https://docs.polymarket.us/api-reference/websocket/markets) does
not establish timestamp behavior during quiet periods. A quiet Polymarket book
can therefore pause execution until a fresh timestamp arrives. Ping/pong alone
does not override this policy. This is an explicit availability limitation until
venue evidence supports a stronger freshness contract. Both connections clear
cached books on disconnect and reconnect automatically.

This can suppress opportunities caused by Kalshi price changes while the
Polymarket book is unchanged. Expiry between basket legs can prevent the second
submission, and an expired Polymarket book also blocks Polymarket repair orders.
Recovery retains the exposure and retries; it cannot bound the waiting time.

Both feeds are checked immediately before each basket submission. If a feed
fails after the first fill, reconciliation and recovery retain unmatched
inventory until usable repair bids are available. Missing data records a visible
pause rather than destroying the run; current data permits another evaluation.
Order context records each venue's source and freshness evidence before submission.
Kalshi records subscription ID, sequence, connection age, and last book receipt age.

Every evaluated scan records its input books, source timestamps, fee parameters, four raw
buy prices, and both fee-adjusted cross-venue gaps in the scan_complete quote
payload. Gaps use the execution pricer and broker funding checks across integer
sizes up to max_sets; max_gap selects profit per pair, best selects total profit.
Positive gaps do not bypass the configured execution thresholds. Missing, stale,
or closed quotes record an empty gap sample and a reason. These observations
are read-only reporting inputs for an independent dashboard, not fills. Captures
are written after the execution attempt, including failed scans. A crash can lose
that diagnostic capture, but order intent and its execution quote context are
persisted before submission. Full input depth is retained for reproducibility.

The experiment companion reads these scan captures and the existing broker and
account-monitor records. It makes no exchange connections and does not recompute
gaps from reference marks. Generic market collectors remain independent sources
for valuation and settlement; their marks are not substituted for decision data.
