# Glossary

Short definitions of terms used across CZ Quant. The
[design principles](design-principles.md) are authoritative; follow the links
for the full rules.

| Term | Meaning |
| --- | --- |
| Venue | An execution service such as Kalshi or Polymarket US, not an individual event or contract. See [broker boundary](design-principles.md#broker-boundary-and-cash-ownership). |
| Account | The cash and positions actually held at a venue. |
| Cash pool | A persistent allocation of one account's cash and positions, with one venue and one mode (paper or live). Stored in the retained `portfolio_id` column. |
| Allocation | The portion of a shared account assigned to a cash pool in the ledger. Moving it changes recorded ownership, not venue balances. |
| Registered account | A venue account (or hypothetical paper account) recorded in `accounts`, with a reserve pool for its unallocated cash. See [virtual subaccounts](services/virtual-subaccounts.md). |
| Virtual subaccount | A cash pool on a registered account, funded by allocation from its reserve; loaded and unloaded without venue actions. |
| Broker | Thin wrapper bound to exactly one cash pool; the only mandatory strategy integration surface. See [shared broker interface](design-principles.md#shared-broker-interface). |
| Adapter | The venue- and mode-specific execution implementation a broker uses. |
| Portfolio (reporting) | A flat reporting selection of cash pools. It owns no cash and executes no orders. |
| Run | One strategy revision and configuration using a pool, recorded by `start`. |
| Context | Optional, opaque JSON attached to an order and carried into its economic records. |
| Economic record | The shared event log from which reporting reconstructs cash and positions. See [event envelope](design-principles.md#event-envelope-and-economic-schema). |
| Reconciliation gap | An account difference not explained by recorded evidence; never booked as profit. |
| Quarantine | Holding an invalid or conflicting fill as evidence without accepting it into accounting. |
| Shared root | The `STRATEGY_RUNS_DIR` logging directory holding `accounting.sqlite3` and related artifacts. |
