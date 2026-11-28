# Virtual subaccounts

A virtual subaccount is a cash pool that holds a recorded share of one registered
account. Subaccounts work the same way at every venue and in paper mode, because
the operations change recorded ownership rather than moving money at the venue.
A sub can be loaded while other pools trade when current account cash backs the
recorded allocations. Closing a sub also requires that it has no unresolved orders
or positions. Each venue's own subaccount mechanism (Kalshi's numbered
subaccounts, for example) remains
optional, as a way to get isolation at the venue.

This follows [principle 5](../design-principles.md#core-principles): each broker
binds to one pool, allocations on a shared account never count the same cash
twice, and every allocation change is recorded and reconciled.

## Credentials and registration

API credentials authenticate the venue client. Account registration is a separate
local bookkeeping operation in packages/czquant: it records the venue scope,
creates the reserve pool and records unallocated cash after checking existing
positions and orders. It does not create a venue account or store credentials.
Register once in the shared ledger, then reuse the account across portfolios and
strategies. See the [operator CLI](../cli.md#accounts-and-credentials) for setup
from the repository root. Independent paper pools do not require registration.

## Model

| Piece | What it is |
| --- | --- |
| Account | One real venue account (a Kalshi subaccount on one exchange, or the Polymarket US account), or one hypothetical paper account. Stored in the `accounts` table. |
| Reserve | `<account>:reserve`, created at registration. It holds the account's unallocated cash. It receives supported venue funding. It cannot trade. |
| Sub | Any other pool on the account, funded by allocation from the reserve or from another sub. Brokers bind to subs. |

For each live account, the monitor checks:

```
venue cash      == reserve cash + sum of sub cash
venue positions == net of every pool's lots
```

Funding the monitor can identify is booked to the reserve as external funding. Any
other difference is a discrepancy (`cash_mismatch`, `inventory_mismatch`), never profit.

## Commands

From packages/czquant:

```bash
# Register an account; its reserve opens with the cash no pool there records.
uv run czquant account main --venue kalshi --subaccount 0 --primary-account
uv run czquant account pmus --venue polymarket-us
uv run czquant account lab --venue kalshi --paper-cash 5000

# Load a sub, optionally limited to some markets ('*' ends a prefix).
uv run czquant open-sub nba-game --account main --cash 200 --markets 'KXNBAGAME-26NOV26BOSSAC*'
uv run czquant allocate --from main:reserve --to nba-game --dollars 50

# Hand open contracts to another pool on the account, at cost (no P&L).
uv run czquant move-lots --from nba-game --to main-book --market KXNBAGAME-... --side yes

# Unload: the sub must be flat with no orders open; its cash returns to its parent.
uv run czquant close-sub nba-game

# A Kalshi deposit to the primary account has no transfer record. With positions
# reconciled and no orders unresolved, the operator books it to the reserve.
uv run czquant book-deposit main --note "ACH 2026-11-26"

uv run czquant accounts            # each account's pools and recorded shares
uv run czquant portfolio-add trial --pools nba-game
```

A strategy uses a sub by binding a broker to it: `Broker(store, "nba-game", adapter)`.
Which market goes to which sub is the strategy's own choice. The `markets` scope is an
assertion, like the broker's `venue` keyword: an order outside it is refused, and
nothing is routed.

## Market scope

`--markets` (`open_sub(..., markets=[...])`) limits which tickers a sub's broker may
trade. Each entry is either:

| Entry | Matches | Example |
| --- | --- | --- |
| Exact ticker (no `*`) | That ticker only | `KXNBAGAME-26NOV26BOSSAC-BOS` |
| Prefix ending in `*` | Every ticker starting with the text before `*` | `KXNBA*` (a series), `KXNBAGAME-26NOV26BOSSAC*` (one event) |

Entries can be mixed, and an order is allowed if its ticker matches any entry:

```bash
uv run czquant open-sub bos-sac --account main --markets KXNBAGAME-26NOV26BOSSAC-BOS 'KXNFL*'
```

- **Matching:** it compares ticker strings only. A "group" is just a shared prefix;
  nothing looks up the venue's event or series structure.
- **Polymarket US:** use the full `PMUS:<slug>` ticker or a prefix of it, such as
  `PMUS:aec-nba-*`.
- **Default:** without `--markets`, a sub can trade any market at its venue.
- **Refusals:** an order outside the scope raises `ValueError` before anything is
  recorded or sent. The scope is an assertion, not routing; the strategy still decides
  which broker (sub) each order goes to.
- **Fixed at opening:** the scope is stored in the pool's metadata when the sub is
  opened, and no command changes it. To change it, move the sub's lots (`move-lots`)
  or let them settle, close it, and open a new sub.
- **Moved lots:** `move-lots` doesn't check the receiving sub's scope. A sub can hold
  a moved lot outside its scope, but its broker can't trade that market, so it can't
  close the lot either; the lot can only settle or be moved again.

## Rules and limits

- **Mode and scope.** A sub has its account's venue and mode. Live Kalshi subs carry
  the account's subaccount and exchange, so their orders go to the right account.
- **Registering over existing pools.** Pools mapped before registration (`map-live`,
  including `--share-account`) become members of the account. The reserve opens with
  whatever cash they don't record.
- **Cross-pool netting.** Kalshi and Polymarket US net one market's YES and NO within
  one account. A broker refuses an order that would open the opposite side of a
  market another pool on the account holds or may buy (reason cross_pool_netting).
  Registered paper accounts enforce the same rule; independent paper accounts remain
  independent. Confirmed fills establish closing ownership before exact receipts are
  booked. Outstanding orders reserve closing quantities. Amendments expose their
  maximum possible quantity before the venue call, including unknown outcomes.
  Moving lots onto the opposite side is refused.
- **Moving lots.** `position_transfer` transactions move lots at their basis. The
  source records an external capital outflow at basis; the target records the matching
  inflow and opens the lot with that basis. Cash, prepaid collateral, and realized P&L
  are unchanged. The capital flows cancel when both pools belong to a reporting group.
- **Reporting.** A reporting portfolio can add a pool that has only funding (a newly
  loaded sub) with `portfolio-add`. Pools never leave. A closed sub keeps its realized
  P&L at zero equity, and allocations between member pools are internal.
- Live allocation. Opening a funded sub, allocating cash, and returning cash at
  closure read the current venue balance under the ledger write transaction. A cash
  shortfall or negative pool balance refuses the operation: reconcile first. These
  operations need venue credentials but do not require a monitor process or strategy
  health polling. The allocation records retain the balance evidence. Paper
  allocation checks the hypothetical account's recorded cash.
  Python callers may inject a venue client with the keyword api for these operations.
- Buying power. Each broker spends only its pool's unreserved cash. Allocation-time
  account reads do not make later external withdrawals or venue operations atomic
  with local orders. Monitors retain responsibility for ongoing reconciliation.
- Closing. A sub must have no inventory, reservations, or pending, unknown, or
  acknowledged orders. Zero reserved dollars do not establish order completion.
  Existing brokers re-read retirement before recording an intent or amendment.
- Polymarket US. Supported account funding goes to the reserve. Trading rebates
  without pool ownership evidence are quarantined as unattributed_rebate and remain
  unavailable for allocation. They remain reconciliation gaps until ownership is
  resolved; do not book them as deposits. A legacy account with one owner can retain
  its rebates directly. Settlement requires account-wide cash reconciliation. Several
  pools require a registered account (unregistered_shared_account otherwise).

## Validation

From packages/czquant, run:

```bash
uv run pytest -q tests/test_portfolio_isolation.py
```

The offline cases use fake venues and temporary stores. They cover the five $10
allocations and $12 winner, live backing checks, rebate quarantine, confirmed-fill
repairs, pending orders and amendments, lot-transfer basis, retirement, and matching
paper/live ownership constraints. No real orders or money movements are needed.
