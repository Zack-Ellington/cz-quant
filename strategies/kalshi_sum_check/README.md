# Kalshi sum check

## Shared broker execution

Create a paper portfolio with the shared czquant CLI. Export STRATEGY_RUNS_DIR
and CZQUANT_PORTFOLIO, then run `uv run python paper_ws.py --preset low --feed rest`
or preset high. Export credentials explicitly when using WebSockets; code does
not read .env. Both presets use independent per-leg IOC simulation. Start the
separate market monitor to settle positions. Use the
[shared report](../../docs/broker-and-reporting.md) for portfolio results.
The JSONL dashboard and void commands below apply only to historical ledgers.


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

Credentials come from exported environment variables. Use KALSHI_API_KEY_ID
and an absolute KALSHI_PRIVATE_KEY_PATH. After editing .env, explicitly export
it with `set -a; source .env; set +a`. The application does not load this file.
See [account setup](../../docs/kalshi-setup.md).

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
- Execution uses the shared broker's independent IOC depth policy. Each leg
  can partially fill. Context preserves the intended basket and actual outcomes.
- The independent market monitor settles individual held contracts using
  confirmed payout values. The strategy resumes actual inventory from the
  shared store. A fully settled losing basket halts new submissions, including
  after restart.
- Use the shared report for cash, open positions, realized and unrealized P&L,
  fees, drawdown, and coverage. Historical JSONL dashboards and void requests
  do not apply to broker portfolios.

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
