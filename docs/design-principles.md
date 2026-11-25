# Design principles: independent strategies, uniform accounting

This is the authoritative design reference for CZ Quant. It governs new
development, reviews, and refactors. It defines the intended boundaries;
existing code is not evidence that an exception is part of the contract.
The [service guides](../README.md#shared-services) describe available
implementations and commands. This document owns the shared interface and
economic schema; service guides describe how to use them.

## Purpose

CZ Quant exists to try, replace, and discard many trading strategies. Most
experiments will not succeed. The codebase must make the next experiment
cheap, including when it requires a complete rewrite. Copying code is welcome
when it lets experiments evolve independently.

We also need to compare and aggregate their results fairly. This creates a
tension: common interfaces make measurement easier, but each interface limits
what a strategy can try. Resolve that tension with a small execution boundary
and a common economic record. Keep research choices inside each strategy.

## Core principles

1. Optimize for experimentation. Rewrites and duplication are valid design
   choices. Do not preserve an abstraction merely because several strategies
   currently resemble one another.
2. Measure strategies consistently. Record actual or simulated executions,
   capital flows, and positions in a common format. Apply the same reporting
   policy to the portfolios being compared, with differences in execution
   assumptions and evidence coverage made explicit.
3. Keep every strategy independent. A strategy owns its models, market
   readers, matching, sizing, execution sequence, configuration, dependencies,
   and schedule. It must be possible to change or retire it without changing
   another strategy. Do not import from another strategy, subclass its engine,
   or require a shared strategy framework. Copy useful code into the experiment
   that needs it. Common research utilities are not part of the required
   interface; prefer local copies when shared changes would couple experiments.
4. Make uniform trade logging the minimal integration requirement. A thin
   broker wraps one venue's execution API and records what was requested and
   what happened locally. Paper execution must produce the same economic facts
   as live execution, with its simulation assumptions identified.
5. Bind each broker to one venue and an explicitly selected cash pool or
   portfolio allocation. Every strategy must make every trade through a
   broker, including exits, repairs, and retries. Use a broker for each venue
   and allocation being traded. Shared accounts and allocation management belong
   in broker infrastructure and must preserve ownership of every cash and
   position change.
6. Keep reporting observation independent of strategy decisions. Account and
   market monitors collect references for accounting and comparison. Strategies
   may read markets directly and need not call monitors, read their storage, or
   consume their health records. A strategy may choose to use monitor outputs;
   that choice does not become a requirement for other strategies.
7. Keep reporting outside strategies. Performance reports, dashboards, and
   valuation belong in independent reporting services that read the shared
   ledger and recorded reference data. Strategies emit economic facts through
   their brokers and may attach diagnostics or arbitrary context; they do not
   maintain a second P&L ledger or implement reporting views. Reporting must
   not import strategy code or require the strategy process to remain running.

The broker is the only mandatory strategy integration surface. Installing it
may install accounting and venue dependencies internally. That does not impose
their APIs, SQL tables, or a common market reader on strategy authors. Separate
strategy installations and pinned dependencies should preserve each
experiment's ability to evolve independently. The repository's template is a
starting point, not a required runtime architecture.

## Broker boundary and cash ownership

Here, venue means an execution service such as Kalshi or Polymarket US. It
does not mean one event or contract within that service. One broker may trade
many contracts at its venue, but it does not route between venues.

A cash pool persists beyond any one process run. Reopening its broker must
not fund it again, erase losses, or discard outstanding orders and positions.
Each run records which strategy revision used which allocation. A reporting
portfolio may aggregate allocations across venues without becoming a broker
that sends orders to all of them. Cash at one venue cannot fund an order at
another without an actual, recorded transfer.

Portfolio identity and broker identity are distinct. Two brokers do not require
two portfolios. In the current cross-market implementation, one persistent
portfolio records both venues; each live broker uses that portfolio's allocation
at its own venue. Runs append to this history. Creating a new portfolio is an
explicit experiment setup action, not a side effect of starting a broker.
Reusing a portfolio continues its economic history; a fresh comparison experiment
uses a separate portfolio with explicitly recorded funding. The current paper
implementation uses one cash pool across both venues, which is a simulation
assumption rather than evidence of transferable live venue balances.

```mermaid
flowchart LR
    D[Strategy-selected data] --> S[Independent strategy]
    S -->|native orders and optional context| B[Broker bound to venue and allocation]
    B --> E[Venue API or paper execution]
    B --> L[(Shared local economic log)]
    A[Independent account monitor] -->|account evidence| L
    A -.->|recovered executions| B
    M[Independent market monitor] -->|quotes, rules and outcomes| L
    L --> R[Offline accounting and comparison]
```

A strategy may use as many broker instances as its experiment needs. For
example, a strategy trading two venues uses two brokers that write to the
same logging directory. Optional context can link their orders for analysis.
The strategy controls order sequencing and partial-fill handling. A common
trade identifier does not promise atomic execution across venues.

Several experimental allocations may share one real account. The account is
the venue's balance; an allocation is the portion assigned in our ledger.
Record each allocation change and reconcile the account against all its
allocations. Do not count the same cash twice. Moving an allocation within
one account changes recorded ownership without pretending money moved at the
venue. Independent paper pools have separately recorded hypothetical funding.
Processes using the same pool must share its reservations and accounting.

Keep broker operations close to venue actions. Logging, ownership,
reservations, execution normalization, and recovery support the contract.
Candidate selection, cross-venue coordination, target returns, and strategy
risk policies do not belong in that contract. A venue or paper adapter may
support only some order types, but this is an explicit capability limit, not
a rule that all future strategies must use those order types.

## Shared broker interface

The current Python interface is Broker(store, portfolio, adapter=None,
run_id=None, *, venue=None). Store selects the shared logging directory. The portfolio must
already exist; construction does not create or fund it. The adapter implements
execution for the chosen venue and mode. Built-in adapters declare their venue;
otherwise it defaults to Kalshi. The venue keyword explicitly binds a collector
or custom adapter. A conflicting adapter declaration is rejected. Orders, fills,
completion, cancellation, settlement, and corrections cannot cross that boundary.
An omitted adapter allows accounting access but cannot submit orders. Venue-specific examples are in the
[Kalshi](services/kalshi-broker.md) and [Polymarket](services/polymarket-broker.md)
guides. Their current order format is a binary YES-book convention, not a
requirement that future instruments share those economics.

The strategy-facing operations are:

- start(strategy, revision, config): record a run and opaque configuration.
- submit(order, context=None): persist ownership, intent, and optional context,
  then request execution. Keep an explicit client_order_id when retrying the
  same intent. Reuse for a different request or context is rejected.
- cancel(identifier) and amend(identifier, changes): act on a broker-owned
  client order ID. Availability depends on venue and execution mode. An
  acknowledgement is not evidence of a fill or completed reconciliation.
- state(): inspect the aggregate portfolio, including all its venues.
- available: inspect cash after reservations. Paper brokers sharing a portfolio
  share one hypothetical pool. Live brokers expose only their own venue allocation.
  Both state and available may lag asynchronous live
  execution evidence; they do not silently turn uncertainty into zero fills.
- observe(kind, data): attach diagnostics without changing economic state.

Infrastructure uses fill, finish, settle, and correct to ingest executions,
complete order tracking, settle supported paper positions, and apply audited
corrections. Strategies do not manufacture fills or settlement facts. Public
strategy integration must not require access to the broker's private order
helpers or database tables.

Submission context accepts arbitrary nested JSON-compatible data. The broker
persists it before submission and carries it into resulting economic records.
There are no mandatory model, basket, or signal fields. Context cannot select
another portfolio or override accounting fields. Store large inputs by immutable
artifact reference. Serialization details and optional helpers must not grow
into a common research interface.

A retry with an existing client order ID returns the stored order rather than
sending another order. A new submission returns the adapter's response; paper
responses include status and filled quantity, while live responses can remain
venue-shaped. Callers must not assume every response is a completed execution.
Unknown live writes retain their reservations for reconciliation. Exact result
normalization is an implementation concern to keep explicit when changing the
interface.

## What the common record must establish

Uniform logging must let reporting reconstruct cash and positions without
importing or running the strategy. Strategy authors submit orders and may
attach context; infrastructure produces the accounting records. They do not
implement a second ledger or write SQL to meet the contract.

### Event envelope and economic schema

Each event has schema_version, record_id, source_key, sequence, portfolio_id,
run_id, type, effective_at, recorded_at, and data. Portfolio/run identity can
be absent on shared market or account observations. Portfolio metadata records
live or paper mode and account mapping. UTC effective time says when a fact
occurred; recorded time says when reporting could know it. Sequence orders
facts with equal effective times. Money and quantities use decimal strings.

A stable source_key deduplicates evidence when supplied. Conflicting payloads
for the same source are rejected. Identity must distinguish venues and
accounts where their IDs can collide. Diagnostic events may have no source
key and receive a new record_id each time.

The current record types are:

| Type | Contents |
| --- | --- |
| run | Strategy, revision, configuration, status |
| order | Intent, request, context, acknowledgement, outcome |
| execution | Source, order_id, quantity, yes_price, fee, evidence |
| transaction | Reason, cash effects, inventory change, evidence |
| market | Ticker, metadata, book or payout |
| account_snapshot | Authoritative account responses |
| account_evidence | Individual authoritative account facts |
| snapshot | Reconstructed paper state |
| health | Component, status, counts, errors |
| observation | Arbitrary diagnostics |
| correction | Target record IDs, reason |

A transaction contains reason, cash_delta, fee, optional external_delta and
prepaid_delta, optional order_id, context, evidence, and legs. The current
binary-contract schema permits zero or one inventory leg per transaction.
A leg has market, yes/no side, quantity_delta, and price; lot_id can identify
the opening lot retired by settlement. Order and portfolio records establish
ownership. Services may add evidence records such as probes, quarantined
fills, and transfer intent without treating them as economic transactions.

Funding changes both cash_delta and external_delta, so it does not create
profit. Fees on opening fills enter cost basis. Under the current FIFO policy,
closing inventory realizes net proceeds less its basis. Rebates and rewards
use their own transaction reasons. A collateral cash release has an equal
negative prepaid_delta to avoid inventing profit. Unsupported account effects
remain reconciliation gaps.

Corrections retain the original events and exclude the referenced transactions
from economic replay. Related changes must be corrected together if removing
only one would leave unmatched inventory retirement. Correcting a paper fill
does not automatically restore its consumed simulated liquidity.

The event stream and portfolio metadata support reporting. Orders,
reservations, collector checkpoints, and consumed-liquidity tables also matter
for execution recovery. Back up the full database for recovery; a JSONL event
export is not a replacement for those operational tables.

### Broker and execution evidence

The execution boundary owns run provenance, order intent, execution receipts,
and the resulting economic transactions described above. Record intent before
submission. Preserve partial fills as individual facts; an acknowledgement
alone never creates inventory. Exits and repairs preserve the same ownership
and evidence as entries.

Optional context can hold intended baskets, model outputs, expected returns,
or immutable artifact references. These observations never determine realized
profit. Paper fills also retain the fill-policy version, source observations,
timing, depth, and fee assumptions needed to interpret the simulation.

### Account evidence

Account collectors record authoritative balances, positions, orders, fills,
settlements, and supported funding or other account activity. They retain
source evidence and reconcile it with portfolio allocations and executions.
Funding, withdrawals, internal allocations, fees, rebates, and collateral
effects must remain distinguishable. An unexplained cash difference is a
reconciliation gap, not invented trading profit.

Collectors may recover missing execution evidence and feed it through the
broker's accounting path. Execution ingestion and order reconciliation are
infrastructure responsibilities, not a second interface that a strategy must
implement. Execution evidence may arrive asynchronously; unknown outcomes
remain explicit, and retries must not silently place a second order.

Paper-account snapshots are reconstructed simulation state. They are not
evidence from a real account. Paper settlement can use confirmed public
outcomes; live cash settlement needs authoritative account evidence.

### Market evidence

Market collectors record contract identity, available rules and lifecycle
metadata, timestamped quotes or depth, and confirmed outcomes or payouts.
They discover reporting needs from recorded positions and outstanding orders,
with optional watchlists. They continue after a strategy stops.

Reference data serves independent valuation. It need not be the data used
by the strategy to make its decision. Execution adapters retain their own
fill evidence. Cached quotes retain their original observation time; reading
a cache does not make a quote fresh. Missing, stale, or invalid references
remain visible as gaps.

Collectors also record component health, collection failures, and
reconciliation discrepancies. Monitor health and valuation coverage measure
different things. A running process is not proof of complete evidence.

## Storage and fair comparison

STRATEGY_RUNS_DIR selects the shared local logging directory, outside code
worktrees. The current implementation stores economic events in
accounting.sqlite3. Diagnostics, market artifacts, operational logs, and
report archives belong under the same root. Reports may be exported elsewhere
for delivery, but their evidence comes from the shared store. Private account
evidence and credentials must not enter the public repository.

Reporting reads a consistent snapshot with a defined time window and recorded
data cutoff. It reconstructs opening holdings, cash flows, and remaining
positions. Funding is capital, not return. Fees enter the economics; actual
holdings include incomplete baskets. Corrections can restate a result, so
retain the original snapshot and report provenance.

The shared package owns the reporting implementation. The portfolio is the
current comparison unit, across all of its runs and venues. Run identifiers
retain execution provenance; restarting a process does not reset a report's
cost basis or create a new performance series. Research-specific views, if
needed, also live outside strategies and consume recorded evidence. A second
ledger or live venue reads in a report must not substitute for that evidence.

Use common accounting and valuation rules within a comparison. Record the
reporting policy and implementation revision, paper fill policies, and data
coverage so a result can be reproduced. A common log makes comparison
possible; it does not make different windows, capital constraints, execution
models, or incomplete evidence equivalent. Do not silently rank an incomplete
valuation as a complete result or treat a paper result as live execution
validation.

FIFO basis, a particular polling interval, and a particular midpoint rule
are implementation policies documented in the
[reporting guide](services/reporting.md). They can change with evidence and
explicit comparison provenance. They do not constrain
strategy research, and they are not permanent properties of the broker API.

## Applying these principles

A strategy change should be implementable without modifying another
strategy. A new strategy should require no changes to the reporting code
when its executions fit the existing economic schema. If a new instrument
cannot be represented faithfully, extend accounting and its evidence rather
than forcing the experiment into misleading binary-contract semantics.

A broker change should preserve auditable ownership and economic facts while
leaving venue-specific execution possible. A monitor change should improve
reference coverage or reconciliation without imposing a research interface.
A report change should be reproducible from stored evidence and applicable
consistently to the portfolios being compared.

Review existing code against these boundaries. Record deviations and their
evidence separately; do not broaden the strategy contract to justify an
existing dependency. Keep implementation investigations and refactor plans
out of this contract.
