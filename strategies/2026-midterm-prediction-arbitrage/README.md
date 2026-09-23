# 2026-midterm-prediction-arbitrage

Price the four ways the 2026 U.S. midterms can split control of Congress, and
compare them to Kalshi's combo market. The four outcomes are mutually exclusive
and exhaustive:

| Code | Meaning            |
| ---- | ------------------ |
| DD   | Democrats sweep    |
| DR   | Democratic House, Republican Senate |
| RD   | Republican House, Democratic Senate |
| RR   | Republicans sweep  |

Kalshi lists one combo market, `KXBALANCEPOWERCOMBO`, with a leg for each
outcome. The strategy tests it two ways: with **model-free checks** against the
markets it must agree with by construction, and with a **bottom-up model** built
from the individual House and Senate race markets, where races move together
through a calibrated latent swing.

**Findings on the committed snapshot:** [reports/dependence.md](reports/dependence.md).
In short, the independent model is badly wrong, one latent swing fitted to the
seat-count and control markets reproduces the held-out combo within 0.4 points,
no model trade survives fees, and one small model-free arbitrage exists
(200 sets at +3.3¢).

## Pipeline

1. **Read quotes.** Every leg of every race (35 Senate, 67 priced House
   districts), the combo, House and Senate control, the same-party market, the
   Democratic seat-count buckets, and each traded series' fee schedule. Prices
   come from order books: best bid, best ask, and the contracts resting at each.
2. **Model-free checks.** Baskets that must pay at least $1 in every outcome:
   combo sums, the combo against control and same-party (exact settlement
   identities), Fréchet bounds, and the seat buckets. Each is priced as a taker,
   with Kalshi's fees on the size actually available.
3. **Race probabilities.** Each race's legs (D, R, and any independent) become
   P(D nominee wins) and P(independent wins) with a chosen estimator.
4. **Calibrate.** Fit the swing to the seat-count and control markets, along
   with the flip rates of the House seats Kalshi does not price and the share of
   independent winners who caucus with Democrats. The combo is held out. Keep
   one factor unless it misses a control market by more than 0.01 and two
   factors do better.
5. **Compare.** The independent model, the calibrated factor model, and the
   combo side by side, with the edge and the expected value per contract after
   the spread and the taker fee.

### The dependence model

Each race has a latent Democratic margin
`M = sqrt(1 + σ²) · Φ⁻¹(p) + σ · U + ε`, where `p` is the race's market
probability, `U` a shared swing, and `ε` race-specific noise. The scaling keeps
every race at exactly its market price for any σ; σ only controls how much races
move together (σ = 0 is the independent model). The seat distribution is
computed exactly given the swing and integrated with Gauss-Hermite quadrature.
Details, fit, and the one- vs two-factor test are in the report.

### Control rules

- **House:** Democrats need 218 of 435. 217 leaves Republicans in control.
- **Senate:** a 50-50 tie goes to the Vice President, a Republican in the Senate
  seated in January 2027, so Democrats need **51**.

### Data traps handled in code

- **Kentucky is filed under `SENATELA-26`** (titled "Kentucky Senate winner?");
  there is no `SENATEKY-26`, and **Louisiana has no market**, so it uses its
  prior. The FL and OH specials are `SENATEFLS-26` and `SENATEOHS-26`.
- **Independent candidates** (Osborn NE, Bodnar MT, Achilles ID, Bengs SD, Hill
  AK-AL) have their own legs. Every leg is normalized together; normalizing only
  D against R overstates both parties and understates the Democratic caucus.
- **Unpriced House seats.** 368 districts have no market. They start at their
  2024 result (177 D, 191 R, derived from each market district's current holder)
  and get calibrated flip rates so a wave can reach them.
- **Summary price fields.** Kalshi's legacy integer-cent `yes_bid` / `yes_ask`
  fields are null on these markets; the order book is the price source.

## Install

```bash
uv sync
cp .env.example .env
```

No API key is needed: every endpoint read is public. `KALSHI_API_BASE_URL` is
read from the environment if set, and otherwise defaults to Kalshi's public
elections host.

## Run

Live, reading current Kalshi order books (about 350 requests, ~20 seconds):

```bash
uv run strategy
```

Record every quote a live run reads, then replay it exactly:

```bash
uv run strategy --snapshot-out snapshots/2026-09-23.json
uv run strategy --snapshot-in snapshots/2026-09-23.json --seed 12345
```

| Option | Default | |
| --- | --- | --- |
| `--model` | `auto` | `auto`, `one-factor`, `two-factor`, or `independent` (no calibration, so independent candidates count as non-Democratic) |
| `--estimator` | `midpoint` | `midpoint`, `width`, `last`, or `shrunk` (see `estimators.py`) |
| `--snapshot-in FILE` | live API | replay a saved snapshot |
| `--snapshot-out FILE` | none | save every quote the run reads |
| `--simulations` | 100000 | Monte Carlo runs for the independent model |
| `--seed` | unseeded | seed for the independent model |
| `--contracts` | 100 | order size per leg for fee rounding |

## Sample output

`uv run strategy --snapshot-in snapshots/2026-09-23.json --seed 12345`,
snapshot captured 2026-09-23 16:25 UTC:

```
2026 Midterm - Congress balance of power
snapshot 2026-09-23.json (captured 2026-09-23T16:25:13Z) - estimator midpoint - model one factor

Model-free checks (taker; edge per set in cents after fees on up to 100 sets; size = sets available at these prices)
Check                                     Cost      Raw      Net     Size  Verdict
----------------------------------------------------------------------------------
combo: buy all four legs                 1.004    -0.4c    -4.0c   10,464  ok
combo: sell all four legs                3.019    -1.9c    -5.5c       37  ok
House control: D + R                     1.000    +0.0c    -1.1c    5,779  ok
Senate control: D + R                    1.000    +0.0c    -3.2c      315  ok
DD + DR vs House-D (short)               0.997    +0.3c    -3.3c    5,779  fees
DD + DR vs House-D (long)                1.007    -0.7c    -1.9c   10,068  ok
DD + RD vs Senate-D (short)              1.007    -0.7c    -4.0c      315  ok
DD + RD vs Senate-D (long)               0.997    +0.3c    -3.3c      478  fees
DD + RR vs same-party (short)            1.137   -13.7c   -17.6c      200  ok
DD + RR vs same-party (long)             0.937    +6.3c    +3.3c      200  ARB
DD >= House-D + Senate-D - 1             1.087    -8.7c   -12.5c      315  ok
RR >= House-R + Senate-R - 1             1.640   -64.0c   -66.7c      478  ok
DR >= House-D - Senate-D                 0.997    +0.3c    -3.3c      478  fees
RD >= Senate-D - House-D                 1.280   -28.0c   -30.2c      315  ok
House seats: buy all buckets             1.044    -4.4c   -16.4c        1  ok
House seats: sell all buckets           11.026    -2.6c   -14.6c        1  ok
House seats >= 218 vs House-D (short)    1.038    -3.8c   -13.8c        1  ok
House seats >= 218 vs House-D (long)     1.006    -0.6c    -3.1c        2  ok
Senate seats: buy all buckets            1.014    -1.4c   -12.4c     0.15  ok
Senate seats: sell all buckets           9.077    -7.7c   -15.7c        2  ok
Senate seats >= 51 vs Senate-D (short)   1.030    -3.0c    -8.1c      315  ok
Senate seats >= 51 vs Senate-D (long)    0.984    +1.6c    -7.4c     0.15  fees
1 arbitrage(s) survive fees; largest: DD + RR vs same-party (long), +3.3c on 200 sets.

Calibration: seat-count and control markets (combo held out)
Model            sigma H sigma S  caucus     flip R/D    loss   House  Senate
-----------------------------------------------------------------------------
no swing           0.000   0.000    0.42  25.0%/24.6%  0.5486   96.4%   71.2%
one factor         0.560   0.560    0.77    3.5%/0.2%  0.0211   91.1%   64.4%
two factor         0.384   0.607    0.74    4.3%/1.1%  0.0170   91.2%   63.7%
control markets                                                 91.3%   63.9%
Selected one factor: one factor matches both control markets within 0.005 <= 0.01

Outcome              Indep  Factor  Market      Bid/Ask     Edge  EV after fees
-------------------------------------------------------------------------------
Democrats sweep      74.4%   64.0%   63.5%    63.0/64.0   +0.5pp              -
D House / R Senate   25.6%   27.1%   26.5%    26.0/27.0   +0.6pp              -
R House / D Senate    0.0%    0.4%    0.7%      0.6/0.7   -0.3pp     sell +0.2c
Republicans sweep     0.0%    8.5%    8.6%      8.5/8.7   -0.1pp              -
-------------------------------------------------------------------------------
D House control     100.0%   91.1%   91.3%    91.2/91.3
D Senate control     74.4%   64.4%   64.5%    64.0/65.0
Factor model Democratic seats: House 235.5 +/- 15.6, Senate 51.6 +/- 3.1
Independent: 100,000 simulations, SE <= 0.14pp
```

Verdicts: `ARB` exact identity, positive after fees, at least one full set
available; `fees` positive only before fees; `basis` positive after fees but only
a near-identity (seat counts vs control); `thin` positive but less than one set
on offer; `ok` no edge. The "RD sell +0.2c" line is not a robust trade: a
second factor puts RD on the other side of the market (see the report).

## Test

```bash
uv run --group dev pytest
```

Unit tests use small synthetic quote files in `tests/fixtures/`. They cover the
fee formula and rounding, bucket parsing, enumerated check payouts, each
estimator, the factor model against Monte Carlo, and calibration recovering known
parameters from synthetic markets. The integration test replays the committed
snapshot and requires:

- byte-for-byte golden output (`tests/expected_output.txt`),
- one factor selected, reproducing both control markets within 0.01,
- every race's simulated win rate within 0.01 of its market price.

## Layout

| Module | |
| --- | --- |
| `api.py` | public Kalshi client; normalizes events, books, series |
| `snapshot.py` | record a live run's quotes; strict replay |
| `races.py`, `constants.py` | the 35 + 435 races, tickers, seat baselines |
| `markets.py` | combo, control, same-party, seat buckets |
| `estimators.py`, `probabilities.py` | race books to probabilities |
| `fees.py`, `checks.py` | Kalshi fee schedule; model-free checks |
| `simulation.py` | independent model (σ = 0) |
| `factor.py`, `calibration.py` | latent-swing model; fitting and factor choice |
| `output.py`, `runner.py`, `cli.py` | report, pipeline, command line |

## Risks and limitations

- **One Gaussian swing.** It fits the House well; the Senate market is more
  concentrated on 51-52 seats than any normal swing produces.
- **Baseline parameters.** The unpriced-seat flip rates and the caucus share are
  fitted averages, not seat- or candidate-level facts.
- **Non-simultaneous quotes.** A snapshot is read over about 16 seconds, so
  cross-market edges should be re-checked before trading.
- **Execution and carry.** Taker fills on thin books move prices, and capital is
  locked until settlement on Feb 1, 2027.

## References

- Kalshi API documentation: https://docs.kalshi.com
- Public market data (no auth): `https://api.elections.kalshi.com/trade-api/v2`
- Markets: combo `KXBALANCEPOWERCOMBO`; control `CONTROLH-2026`,
  `CONTROLS-2026`; same party `KXSAMEPARTYCONGRESS`; seat counts
  `KXDHOUSESEATS-27`, `KXDSENATESEATS-27`.
