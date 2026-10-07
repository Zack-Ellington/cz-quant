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

## Live monitor and dashboard

`monitor.py` runs the scan repeatedly and keeps running statistics in
`runs/kalshi_sum_check/` at the repo root (gitignored). For each event it tracks:
- how often the walked Σask was under 1, and how often Σbid was over 1
- the lowest and highest Σask and Σbid seen so far
- the latest taker edge, ROI and APR

After each scan it writes `dash_summary.json` and `dash_history.json` for the
dashboard.

```bash
python monitor.py --once            # one scan
python monitor.py --interval 900    # loop every 15 minutes
```

The dashboard is the artifact at https://claude.ai/artifact/1U6uskGmH6vbEbm6HDbSGx.
Its source is `dashboard/index.html`. The page can't call Kalshi itself, so a
Claude desktop scheduled task named `kalshi-sum-monitor` runs
`monitor.py --once` every 15 minutes and writes the two JSON files into the
artifact's database (`dash/summary`, `dash/history`). Scheduled tasks only run
while the Claude app is open. A missed run happens at the next launch, and the
page shows "Stale" when the last scan is more than 40 minutes old.

The estimated return is the sum of taker edge over every exhaustive event with a
positive edge after fees in the latest scan. It's shown three ways: before fees
(`gross`), Kalshi's taker fees (`fees`), and after fees (`net`). The dashboard
also counts events whose edge was positive before fees but wiped out by them.
All ROI and APR figures are after fees unless labeled otherwise. It's a snapshot, not a running total,
because the same opportunity usually persists across scans.

## Caveats (read before trading anything this flags)

1. **Mutually exclusive is not the same as exhaustive.** The API has no exhaustiveness
   field. "Which country becomes the 51st state?" is mutually exclusive, but every leg
   can resolve NO, and then a long "arb" loses everything. Events whose mid prices sum
   below `1 − 0.10` are flagged `likely_non_exhaustive` and hidden from the console
   table, though they stay in the CSV. Even unflagged two-party races (Dem vs Rep)
   lose if a third party wins. Read the rules for each event.
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
- `monitor.py`: repeated scans, running statistics, dashboard JSON
- `dashboard/index.html`: the dashboard artifact's source
- `tests/`: unit tests on synthetic books and monitor state
