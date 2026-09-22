# 2026-midterm-prediction-arbitrage

Price the four ways the 2026 U.S. midterms can split control of Congress, and
compare those model probabilities to Kalshi's combo market. The four outcomes
are mutually exclusive and exhaustive:

| Code | Meaning            |
| ---- | ------------------ |
| DD   | Democrats sweep    |
| DR   | Democratic House, Republican Senate |
| RD   | Republican House, Democratic Senate |
| RR   | Republicans sweep  |

Kalshi lists a single combo market, `KXBALANCEPOWERCOMBO`, with one leg for each
outcome. The edge, if any, is the gap between that market and a bottom-up
simulation built from the individual House and Senate races.

## Method

1. **Discover the races.** 35 Senate seats are on the 2026 ballot (33 Class II
   seats plus the Florida and Ohio special elections) and all 435 House seats.
   See the ticker traps below.
2. **Read a probability per race.** For each race with a Kalshi market, take the
   Democratic and Republican leg prices from the **order book** and normalize
   them against each other (the two legs rarely sum to exactly 100¢). Races with
   no market or an empty book fall back to a prior. Prices must come from the
   order book: Kalshi leaves the `yes_bid` / `yes_ask` summary fields on these
   markets empty even when a full book of resting orders exists, so reading the
   summary alone makes every market look unquoted.
3. **Simulate.** Draw every race independently, add the seats not on the ballot
   (the Senate baseline) and the safe House seats, apply the control rules, and
   bucket each run into DD / DR / RD / RR. 100,000 runs by default.
4. **Compare.** Print the model probability, the Kalshi combo price, and the edge
   (model − market, in percentage points) for each outcome.

### Control rules

- **House:** Democrats need an outright 218 of 435. 217 leaves Republicans in
  control.
- **Senate:** a 50-50 tie is broken by the Vice President, who is a Republican in
  the Senate seated in January 2027, so Democrats need **51** and 50 is not
  enough. This is the single most common way a naive model misprices Senate
  control.

### Ticker traps handled in `races.py` / `constants.py`

Kalshi's per-state Senate tickers have two traps that a suffix-to-state map gets
wrong:

- **Kentucky is filed under `SENATELA-26`** (its event is titled "Kentucky Senate
  winner?"). There is no `SENATEKY-26`.
- **Louisiana has no market at all**, so it is scored from its prior.
- The Florida and Ohio special elections use an `S` suffix: `SENATEFLS-26`,
  `SENATEOHS-26`.

### Baselines and the independence assumption

Kalshi prices only the competitive House districts (67 at the snapshot below).
The other 368 seats are treated as safe at their 2024 result: the safe counts
(`HOUSE_SAFE_DEM` = 177, `HOUSE_SAFE_REP` = 191) are the 2024 chamber result
(D 215 / R 220) minus the market districts counted by their current holder, so a
currently-Democratic seat that happens to have a market is not double-counted in
the safe pool. The Senate starts from the 65 seats not on the ballot (34
Democratic-caucus, 31 Republican).

Races are drawn **independently**. That is the model's main simplification: a
real national swing correlates races, so the true probability of a sweep (DD or
RR) is higher than independence implies. Read the table as a test of the
market's implied correlation, not as a calibrated forecast.

## Sample output

Snapshot date: **2026-09-22** · repo commit `a612666` · `--seed 12345` ·
100,000 simulations. Produced from the committed order-book snapshot in
`tests/fixtures/`, which was captured from live Kalshi data.

```
2026 Midterm - Congress balance of power
100,000 simulations - snapshot fixtures

Outcome               Model   Market      Edge
----------------------------------------------
Democrats sweep       62.8%    60.5%    +2.3pp
D House / R Senate    37.2%    29.5%    +7.7pp
R House / D Senate     0.0%     0.8%    -0.8pp
Republicans sweep      0.0%     8.5%    -8.5pp
----------------------------------------------
Democratic control:  House 100.0%  Senate 62.8%
Monte Carlo SE <= 0.15pp
```

These are real market prices. The **Senate** model (62.8% Democratic control)
tracks the market's implied 61.3% closely, because Senate control turns on a
handful of decisive races the market prices directly.

The **House** shows the model's headline limitation. Kalshi's competitive
district books price Democrats to win about 229 of 435 seats, so an *independent*
draw of those districts makes a Democratic House all but certain (≈100%). The
combo market instead prices a Democratic House at ~90%, holding back ~9% for
Republicans. That gap is the independence assumption: a correlated national swing
could flip many districts together (a red wave), which the market prices and this
model cannot produce. Read the House edges (DR +7.7pp, RR −8.5pp) as a flag that
the model and market disagree about correlation, not as a clean trade.

## Install

```bash
uv sync
cp .env.example .env
```

No API key is needed. The market-data endpoints this strategy reads are public,
and `.env.example` carries no required variables for this MVP. `KALSHI_API_BASE_URL`
is read from the environment if set, and otherwise defaults to Kalshi's public
elections host.

## Run

Live (reads current Kalshi order books; shows `n/a` only where a book is empty):

```bash
uv run strategy --seed 12345
```

Reproducible (reads the committed snapshot instead of the network):

```bash
uv run strategy --snapshot tests/fixtures --seed 12345
```

Options: `--simulations` (default 100000), `--seed` (default unseeded),
`--snapshot DIR` (read saved Kalshi responses instead of the live API).

## Test

```bash
uv run --group dev pytest
```

The suite covers the seat arithmetic, the control tie-breaks, the KY/LA ticker
mapping, price normalization and fallbacks, the simulator (determinism, a valid
distribution, Monte Carlo error under 0.002 at 100k runs), and a byte-for-byte
end-to-end check of the printed table against `tests/fixtures/expected_output.txt`.

## Refreshing the snapshot

The fixtures are three saved order-book snapshots keyed by market ticker:
`senate_snapshot.json`, `house_snapshot.json`, and `combo_snapshot.json`, plus
the golden `expected_output.txt`. To rebuild them from live data, fetch the `-D`
and `-R` order book for each event in `constants.SENATE_RACES` and
`constants.HOUSE_MARKET_DISTRICTS`, and each leg in `constants.COMBO_MARKETS`,
through `api.KalshiClient.fetch_orderbook`, write the normalized books into the
three files, and regenerate `expected_output.txt` from a seeded run. Order books
move continuously, so a refreshed snapshot changes the golden table.

## Risks and limitations

- **Independence.** The single biggest one. Races are drawn independently, so a
  correlated national swing (a wave in either direction) is invisible to the
  model. This is why the model prints a near-certain Democratic House while the
  combo market holds back ~9% for Republicans. A correlated-error model would
  close most of that gap.
- **Safe-seat baseline.** The House safe counts are the 2024 result outside the
  market districts; a wave that puts currently-safe seats in play is not captured
  until Kalshi opens markets for them.
- **Thin books.** Prices are the best bid/ask midpoint. A wide or one-sided book
  (a few districts have no NO-side orders) makes that midpoint noisy, and empty
  books fall back to priors.
- **No fees or execution.** The table is a raw probability comparison. Kalshi
  fees, the bid/ask spread, and combo-leg liquidity are not modeled, so a
  positive edge is necessary but not sufficient to trade.

## References

- Kalshi API documentation: https://docs.kalshi.com
- Public market data (no auth): `https://api.elections.kalshi.com/trade-api/v2`
- Combo market: `KXBALANCEPOWERCOMBO`; single-chamber controls: `CONTROLH-2026`,
  `CONTROLS-2026`.
