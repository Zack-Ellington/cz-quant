# Kalshi sum check

Scans every open **mutually-exclusive** Kalshi event and checks whether the YES
prices across its markets add up to 1. In an exhaustive mutually-exclusive event
exactly one YES pays $1, so any gap after fees is an edge:

| Trade | Legs | Payout per set | Edge per set |
|---|---|---|---|
| **long** | buy YES on every leg | $1 | `1 − Σask − fees` |
| **short** | buy NO on every leg (= sell YES at the bid) | $(n−1) | `Σbid − 1 − fees` |

For each event it reports:

- **Σask / Σbid walked to a target size.** Each leg's book is walked level by level
  until `--size` contracts fill, so the sums are VWAPs, not top of book. If a leg is
  too thin, the size drops to the thinnest leg's depth and the event gets an
  `ask_depth_only_X` / `bid_depth_only_X` flag. Top-of-book and mid sums are in the CSV too.
- **Net edge as a taker and as a maker, using the event's own fee series.** The fee
  schedule comes from `GET /series`. Taker fee is `⌈0.07 × mult × C × P(1−P)⌉`, rounded
  up to the cent per leg. Maker fee is `⌈0.0175 × mult × C × P(1−P)⌉` only when
  `fee_type = quadratic_with_maker_fees` and zero otherwise. Maker prices rest one tick
  inside the spread, or join the quote when the spread is one tick. Tick size comes
  from each market's `price_ranges`.
- **Time to settlement and annualized return.** Settlement is the latest
  `expected_expiration_time` across the legs. ROI is net ÷ capital deployed. Simple
  annualization (`apr`) and compounded annualization (`apy`) are both reported.

## Usage

Python 3.10+ with the standard library only. No API key is needed because Kalshi
market data is public.

```bash
python sum_check.py --size 100 --out results.csv --legs-out legs.csv
```

| Flag | Meaning |
|---|---|
| `--size N` | contract sets to walk each book to (default 100) |
| `--series T` / `--category C` | restrict the scan |
| `--max-events N` | stop early, for quick runs |
| `--prescreen X` | skip events whose top-of-book sums are more than X from 1 on both sides. Safe for taker arbs, because walking only makes sums worse. |
| `--min-dev X` | report only events whose walked sum deviates from 1 by at least X |
| `--non-exhaustive-gap X` / `--show-non-exhaustive` | see caveats below |
| `--rps` | request rate cap (default 10/s) |

To sign requests with your API key for a higher rate-limit tier, set
`KALSHI_API_KEY_ID` and `KALSHI_PRIVATE_KEY_PATH`, then `pip install cryptography`.

A full scan of about 5,000 events and 38,000 legs takes about 2 minutes. Order books
are fetched 100 tickers per call through `GET /markets/orderbooks`.

```bash
python -m unittest discover -s tests
```

## WebSocket paper trader

**This branch's settings:** trades must have an APR of at most 2000% (`--max-apr 20`), with no lower APR limit beyond the 5% risk-free hurdle; 30% of equity is held for priority trades (settling within 7 days at 100% APR or more). Every limit is a flag, so
`--min-apr`, `--max-apr`, `--reserve-frac` and `--reserve-min-apr` override them.

`paper_ws.py` trades the sum check on paper in real time. **It never sends an
order.**

```bash
uv sync                                            # websockets + cryptography
uv run python paper_ws.py                          # WebSocket if a key is set, else REST polling
uv run python paper_ws.py --feed rest --duration 1:00
uv run python ledger_dash.py                       # dashboard at http://localhost:8780
```

**Credentials.** Put the key ID and its private key (RSA or Ed25519) in this folder's `.env`
(see `.env.example`). The key ID can be `KALSHI_API_KEY_ID` or `KALSHI-ACCESS-KEY`.
The private key can be a file path (`KALSHI_PRIVATE_KEY_PATH`) or the PEM text with
`
` for newlines (`KALSHI_PRIVATE_KEY`). Environment variables take precedence.
`uv run python check_auth.py` reports which names it found (never values), makes a
signed `GET /portfolio/balance`, and opens a signed WebSocket subscription. It
places no orders.

**Feed.** Kalshi's `orderbook_delta` WebSocket rejects connections without an API
key. Set `KALSHI_API_KEY_ID` and `KALSHI_PRIVATE_KEY_PATH` to a **production** key;
demo keys only see demo markets. Without a key, `--feed rest` streams the same
books by polling `GET /markets/orderbooks`, which takes a few seconds per sweep
instead of milliseconds.

**What it trades.** Every check below must pass:
- **Eligible events:** open, mutually exclusive, and classed EXHAUSTIVE or UNKNOWN.
  Unknowns are treated as capable for now.
- **Discovery:** the universe is refreshed every 10 minutes, keeping events whose
  top-of-book sums are within `--band` (0.03) of $1, plus every event already held.
- **Both trades are checked:** buy YES on every leg, or buy NO on every leg. Each is
  walked through the books with Kalshi's taker fee on every leg, and sized to
  maximize profit above the hurdle.
- **Risk-free hurdle:** `net > capital × ((1 + 0.05)^(days/365) − 1)`, where
  `days` runs to the latest expected settlement of any leg. In other words, the
  compounded APR to payout must beat 5% (`--risk-free`).
- **Minimum profit:** at least `--min-net` ($0.10) after fees.
- **Priority:** candidates are taken highest APR first.
- **Limits:** per event `--max-event-frac` (20% of bankroll), total
  `--max-total-frac`, `--max-open-events` (25), cash on hand, and a 30 s per-event
  cooldown.
- **Fills:** after `--latency` (0.3 s), the trade is re-checked on the newest books.
  Every leg fills at those prices or nothing does. Paper fills are netted from the
  local books and rebuilt from the ledger's open trades on every start. A filled
  level's liquidity only counts as new after the level has been gone for
  `--refill-seconds` (300), so neither a restart nor a level that flickers out of
  one snapshot lets the same resting orders be filled twice.
- **Fresh fills:** on the REST feed, every leg is fetched again in one call just
  before filling, so a fill never mixes legs read at different moments. Fills are
  rejected (`legs_out_of_sync`) if the legs' books were read more than
  `--max-leg-skew` (2 s) apart.
- **Voids:** `uv run python void_trade.py TRADE_ID --reason "..."` removes a fill
  that couldn't have happened. The engine applies it within one heartbeat (or at
  start): the trade leaves the book, its capital returns to cash, and a `void`
  record goes in the ledger. The dashboard drops voided trades from every total.
- **Reserve (R2):** `--reserve-frac` (30%) of equity is kept for priority
  trades: those settling within `--reserve-max-days` (7) with APR of at least
  `--reserve-min-apr` (100%). Priority trades can use any cash. General trades can
  only use cash beyond what the reserve still needs (the 30% minus what priority
  trades already hold). Nothing is sold to refill the reserve: priority trades
  settle quickly, and settled cash tops up the reserve before general trades can
  use it. Rejections show as `reserve_held`; trades carry `pool`.
- **Approvals:** UNKNOWN candidate lists (elections, nominations, mayors) trade
  only if their event or series ticker is in `approvals.json`; add one only after
  reading its rules. `--allow-unapproved-candidates` turns this off. Held positions
  that become ineligible are kept to settlement and get a `held_event_ineligible`
  warning.
- **Kill switch:** a file named `KILL` in `runs/kalshi_sum_check/ws/` halts new
  trades. So does any settled trade that loses money.
- **Settlement cross-check:** a trade settles only once every leg is `finalized`
  on Kalshi. The payout uses Kalshi's `settlement_value_dollars` (a NO contract
  pays 1 − value) and is compared with what the yes/no results imply. Any
  disagreement, a missing value, or a market added after the trade that won is
  written as a `settlement_mismatch` warning, and the settle record carries
  `verified: false`. Settlement keeps being
  tracked either way, and the engine warns if Kalshi adds a market to an event
  that's already held.

**Ledger.** Each run writes `runs/kalshi_sum_check/ws/<UTC>/` with `run.json`,
`ledger.jsonl` (append-only and fsync'd: run_start, universe, opportunity, trade,
reject, settle, warning, halt, heartbeat, run_stop) and `run.log`. A new run
rebuilds open positions and cash from all earlier ledgers.

**Dashboard.** `ledger_dash.py` serves `dashboard/ledger.html` and re-reads the
ledgers on every request. The page polls every 2 seconds, so it is always as
current as the ledger. No artifact or scheduled task is involved.

## Caveats (read before trading anything this flags)

1. **Mutually exclusive is not the same as exhaustive.** `classifier.py` sorts each
   event into EXHAUSTIVE (numeric strikes that cover every value, or a catch-all leg
   such as "Other", "Tie" or "No team"), GAP, NOT_EXHAUSTIVE (a tie or no-winner
   rule, including "resolves to $0", or a combat sport with no Draw leg), RISKY
   (party or sworn-in legs, decades-long events, "first to" markets with no
   "None" leg, or "Other" with a deadline), or UNKNOWN. Candidate lists are
   UNKNOWN with the reason "candidate list: trade only if approved". Only
   EXHAUSTIVE events, and UNKNOWN events whose mid prices sum to 0.90 or more,
   are eligible for the paper trader.
   The API has no exhaustiveness field, so this is inferred, and the
   rules-text patterns can miss unusual wording. Read the rules before trading.
   Excluded events are flagged `likely_non_exhaustive`, hidden from the console
   table, and kept in the CSV.
2. **Maker numbers assume every leg fills.** They show what quoting inside the spread
   would earn, not a locked-in arb. Partial fills leave you with outright directional
   risk. Rankings (`best_taker`) use taker trades only.
3. **Capital is gross.** Kalshi nets collateral on `MECNET` events, so the real
   capital tied up in the short trade is lower and its ROI is understated.
4. **Fee rounding is per leg, on the whole walked fill.** If you split one leg into
   several orders, each order is rounded up separately.
5. Book snapshots are taken across many HTTP calls, so they aren't perfectly
   simultaneous. Re-check the book before acting.

## Files

- `sum_check.py`: event analysis and the CLI
- `pricing.py`: book parsing (NO bids converted to YES asks), book walking, fees, ticks
- `kalshi_client.py`: stdlib HTTP client with pagination, batching, retry and rate limiting, plus optional signing
- `classifier.py`: decides whether exactly one outcome must win
- `paper_ws.py`: WebSocket paper trader (engine, sizing, limits, settlement, ledger)
- `feeds.py`: order-book store, Kalshi WebSocket feed, REST polling fallback
- `check_auth.py`: read-only check of the key, signed REST and the WebSocket
- `void_trade.py`: void a paper fill
- `ledger_dash.py`, `dashboard/ledger.html`: local paper-trade dashboard
- `tests/`: unit tests on synthetic books, the classifier and the paper trader
