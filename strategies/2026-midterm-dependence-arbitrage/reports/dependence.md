# Dependence between races: independent vs. factor vs. market

Snapshot `snapshots/2026-09-23.json`, captured 2026-09-23 16:25 UTC (347 quotes,
fetched over about 16 seconds). Every number below comes from replaying it:

```bash
uv run strategy --snapshot-in snapshots/2026-09-23.json --seed 12345
```

## Summary

- **Independence is wrong, and the markets say so directly.** The Democratic
  House seat-count market puts 8.8% below the 218-seat majority and 14.4% above
  249 seats; drawing the 67 priced districts independently gives a standard
  deviation of 2.9 seats and puts essentially nothing in either tail. The
  independent model puts 100% on a Democratic House; the control market says
  91.3%.
- **The market prices the two chambers as almost perfectly dependent.** Given
  the control markets, the combo's Democratic-sweep price must lie between
  55.2% (Fréchet lower bound) and 63.9% (upper bound); independence would put it
  at 58.3%. The market has it at 63.5%, 96% of the way to perfect dependence.
  P(Democratic Senate | Democratic House) is 70.6%; given a Republican House,
  7.0%.
- **One latent swing explains it.** A one-factor model, calibrated only to the
  seat-count and control markets, reproduces the held-out combo market to within
  0.37 points on average (independent model: 5.25; two factors: 2.18).
  Calibrated swing σ = 0.56, a latent correlation of 0.24 between any two races.
- **One factor is enough.** It reproduces both control markets within 0.005,
  inside the 0.01 criterion, so the second factor is not added. The two-factor
  fit is slightly better on the calibration targets but worse on the combo.
- **No model trade survives.** After the spread and taker fees, the model's only
  positive-expectation trade is selling RD for +0.2¢, and that is the one leg
  where one and two factors disagree in sign. It is not a robust edge.
- **One small model-free arbitrage exists.** DR + RD + "same party" YES costs
  93.7¢ and pays $1 in every outcome: +3.3¢ per set after fees, on 200 sets
  ($6.58 total, 3.4% over the 131 days to settlement). It is limited entirely by
  the same-party book, which has traded 190 contracts in its life.

## 1. Why the independent model fails

The independent model prices each race from its own market and adds them up as
if they were unrelated coin flips. Sums of many independent coin flips
concentrate hard around their mean. Kalshi's seat-count markets price how many
seats Democrats will hold, and they are far wider:

| Democratic House seats | Market | Independent | No swing* | One factor | Two factor |
| --- | ---: | ---: | ---: | ---: | ---: |
| below 210 | 2.6% | 0.0% | 0.3% | 2.7% | 2.8% |
| 210-213 | 2.5% | 0.0% | 0.9% | 2.3% | 2.2% |
| 214-217 | 3.7% | 0.0% | 2.4% | 3.9% | 3.7% |
| 218-221 | 6.2% | 0.5% | 5.4% | 6.3% | 5.8% |
| 222-225 | 8.5% | 9.9% | 9.8% | 9.2% | 8.4% |
| 226-229 | 10.4% | 43.9% | 14.6% | 11.7% | 10.9% |
| 230-233 | 12.4% | 39.7% | 17.7% | 12.8% | 12.4% |
| 234-237 | 12.4% | 5.9% | 17.5% | 12.0% | 12.5% |
| 238-241 | 11.4% | 0.1% | 14.1% | 10.1% | 11.2% |
| 242-245 | 9.2% | 0.0% | 9.2% | 7.8% | 9.0% |
| 246-249 | 6.4% | 0.0% | 4.9% | 5.8% | 6.8% |
| above 249 | 14.4% | 0.0% | 3.2% | 15.3% | 14.3% |

| Democratic Senate seats | Market | Independent | No swing* | One factor | Two factor |
| --- | ---: | ---: | ---: | ---: | ---: |
| below 45 | 2.4% | 0.0% | 0.0% | 1.1% | 1.4% |
| 45 | 1.4% | 0.0% | 0.0% | 1.2% | 1.4% |
| 46 | 1.8% | 0.1% | 0.1% | 2.3% | 2.5% |
| 47 | 3.3% | 0.6% | 0.7% | 4.0% | 4.2% |
| 48 | 5.8% | 2.5% | 3.0% | 6.3% | 6.4% |
| 49 | 5.8% | 7.3% | 8.4% | 9.0% | 9.0% |
| 50 | 11.9% | 14.9% | 16.6% | 11.6% | 11.3% |
| 51 | 16.0% | 21.8% | 23.1% | 13.2% | 12.8% |
| 52 | 13.9% | 22.7% | 22.5% | 13.3% | 12.9% |
| above 52 | 37.7% | 30.0% | 25.6% | 37.8% | 38.0% |

Market columns are the bucket midpoints normalized to sum to one.
\*"No swing" is the independent model with the unpriced-seat flip rates and the
caucus share still fitted (section 2). Even with its flip rates pushed to the
25% bound it cannot reach the tails: the width has to come from races moving
together, not from more races being uncertain.

| Mean, SD of Democratic seats | House | Senate |
| --- | --- | --- |
| Independent | 229.1, 2.9 | 51.6, 1.7 |
| No swing | 233.3, 8.8 | 51.4, 1.7 |
| One factor | 235.5, 15.6 | 51.6, 3.1 |
| Two factor | 235.3, 14.1 | 51.6, 3.2 |

## 2. The model: latent margins

Each race gets a latent Democratic margin

    M_i = sqrt(1 + σ²) · Φ⁻¹(p_i) + σ · U + ε_i,     Democrat wins iff M_i > 0,

with p_i the race's market probability, U ~ N(0,1) a shared swing, and
ε_i ~ N(0,1) race-specific noise. The sqrt(1 + σ²) scale keeps P(M_i > 0) = p_i
for every σ: the swing changes how races move together and nothing else. Every
race stays priced at its own market. (Checked: across 100,000 simulated
elections every race's win rate is within 0.01 of its market price.) σ = 0 is
the independent model; two races share latent correlation σ² / (1 + σ²).

Three baseline parameters are fitted alongside σ. They are not dependence, but
without them the fit would force σ to absorb problems that are not about
dependence:

- **Unpriced House seats.** Kalshi prices 67 districts. The other 368 are held
  at their 2024 result, which caps the model near 250 seats, while the market
  puts 14.4% above 249. So each unpriced seat also gets a latent margin with a
  small baseline flip chance: 3.5% for a Republican seat, 0.17% for a
  Democratic one. The swing moves them like any other race, so a wave reaches
  into seats nobody quotes. The asymmetry lifts the House mean by about 6 seats
  over holding those seats at their 2024 result, consistent with mid-decade
  redistricting and a Democratic-leaning year.
- **Independent candidates.** Four Senate races have serious independents: Dan
  Osborn in Nebraska (31.3%), Seth Bodnar in Montana (9.8%), Todd Achilles in
  Idaho (7.9%), and Brian Bengs in South Dakota (6.5%). Bill Hill in AK-AL is the
  House equivalent. Race legs are now normalized across every leg, not D against
  R, and a caucus share κ is the fraction of winning independents that count as
  Democratic seats, which is how the seat-count markets settle. Fitted: κ = 0.77.
  Section 7 shows what happens without this.

Given the swing, races are independent, so the seat distribution is computed
exactly: a Poisson-binomial over the priced races, convolved with binomials for
the unpriced seats. The swing is then integrated out with 64-node Gauss-Hermite
quadrature. A Monte Carlo of the same model agrees with it to within 0.01 on
every outcome.

## 3. Calibration

The targets are four markets: the Democratic House seat buckets, the Democratic
Senate seat buckets, House control, and Senate control. The loss is the
Kullback-Leibler divergence from each market's distribution to the model's,
summed with equal weight. **The combo market is not a target.** It is what the
model is tested against, so fitting to it would be circular.

| Model | σ House | σ Senate | Latent corr. H / S | Cross-chamber corr. | κ | Flip R / D | Loss | House ctl | Senate ctl | Largest control miss |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| No swing | 0 | 0 | 0 / 0 | – | 0.42 | 25.0% / 24.6% | 0.549 | 96.4% | 71.2% | 0.073 |
| **One factor** | **0.560** | **0.560** | **0.24 / 0.24** | **1.00** | **0.77** | **3.5% / 0.17%** | **0.0211** | **91.1%** | **64.4%** | **0.005** |
| Two factor | 0.384 | 0.607 | 0.13 / 0.27 | 0.63 | 0.74 | 4.3% / 1.15% | 0.0170 | 91.2% | 63.7% | 0.002 |
| Control markets | | | | | | | | 91.3% | 63.9% | |

Adding the swing cuts the loss 26-fold. The one-factor model tracks the House
buckets to within 1.4 points everywhere. Its weakest fit is the center of the
Senate distribution: the market puts more on exactly 51 and 52 and less on 49
than any Gaussian swing does.

## 4. One factor or two

The two-factor model lets each chamber have its own swing size. The chamber
with the larger σ gets an extra chamber-only factor for the excess, which keeps
as much of the swing national as the data allow. Single-chamber markets cannot
say how the swing splits between national and chamber-only; that split is
exactly what the combo market prices.

**Rule, judged on the calibration targets only, never on the combo:** keep one
factor if it reproduces both chamber-control markets (the most liquid markets
here: spreads of 0.1¢ and 1¢, and 4 to 22 million contracts traded per leg)
within 0.01. Add the second only if one factor misses and two factors miss by
less.

This rule replaced a first version, which added the second factor whenever it
changed any reported probability by more than 1¢. On this snapshot that first
rule chose two factors (they differ by 2.6 points), and it was dropped after
seeing that result. It asks the wrong question: it measures how much the answer
depends on the unidentified national-vs-chamber split, not whether the data
need a second factor. A rule like that would adopt any extra parameter that
moves the output. The replacement asks whether one factor fits the markets it
was calibrated to. Both rules are stated here so the choice can be judged.

**Result:** one factor misses by 0.005, so it is kept. The two-factor model does
fit the calibration targets a little better (loss 0.0170 vs 0.0211). But on the
held-out combo it is clearly worse:

| Mean absolute error vs. combo midpoints | |
| --- | ---: |
| Independent | 5.25 pp |
| No swing | 3.97 pp |
| **One factor** | **0.37 pp** |
| Two factor | 2.18 pp |

The two-factor model's weaker cross-chamber link (swing correlation 0.63) makes
split outcomes too likely: P(Democratic Senate | Republican House) is 24.7%,
against the market's 7.0% and one factor's 4.0%. The combo prices the House and
Senate as one national wave. A second factor would have fit the targets slightly
better while moving the model away from the market it is tested on, which is
why a better calibration loss alone was not the criterion.

## 5. Independent vs. factor vs. market

| Outcome | Independent | One factor | Two factor | Market mid | Bid / ask |
| --- | ---: | ---: | ---: | ---: | ---: |
| Democrats sweep (DD) | 74.4% | 64.0% | 61.5% | 63.5% | 63.0 / 64.0 |
| D House, R Senate (DR) | 25.6% | 27.1% | 29.7% | 26.5% | 26.0 / 27.0 |
| R House, D Senate (RD) | 0.0% | 0.4% | 2.2% | 0.65% | 0.6 / 0.7 |
| Republicans sweep (RR) | 0.0% | 8.5% | 6.6% | 8.6% | 8.5 / 8.7 |
| Democratic House | 100.0% | 91.1% | 91.2% | 91.3% | control market |
| Democratic Senate | 74.4% | 64.4% | 63.7% | 63.9% | control market |

The independent model is not a slightly worse forecast. It is a different
world: no path to a Republican House, and a Democratic sweep 11 points too
likely. Trading on it would have meant selling RR at 8.5¢ believing it was
worth zero.

After the spread and the taker fee, the one-factor model shows positive
expected value only on RD (sell at 0.6¢ against a model 0.4%: +0.2¢). That is
the leg where the factor assumption matters most. Two factors put RD at 2.2%,
the opposite side of the market. There is no model trade here that survives
the model's own uncertainty.

## 6. Model-free checks

These need no model. Each is a basket of contracts guaranteed to pay at least
$1 (or $n) in every outcome; the payout is computed by enumerating the
settlement states. The combo, the control markets, and the same-party market
settle on the same CONTROL rules, so their identities are exact. Legs are bought
as a taker. Fees are charged on the executable order size, up to 100 sets.

| Check | Kind | Cost | Raw edge | After fees | Sets available | Verdict |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| combo: buy all four legs | sum | 1.004 | -0.4¢ | -4.0¢ | 10,464 | ok |
| combo: sell all four legs | sum | 3.019 | -1.9¢ | -5.5¢ | 37 | ok |
| DD + DR vs House-D (short) | marginal | 0.997 | +0.3¢ | -3.3¢ | 5,779 | fees |
| DD + RD vs Senate-D (long) | marginal | 0.997 | +0.3¢ | -3.3¢ | 478 | fees |
| **DD + RR vs same-party (long)** | **marginal** | **0.937** | **+6.3¢** | **+3.3¢** | **200** | **ARB** |
| DD ≥ House-D + Senate-D − 1 | Fréchet | 1.087 | -8.7¢ | -12.5¢ | 315 | ok |
| DR ≥ House-D − Senate-D | Fréchet | 0.997 | +0.3¢ | -3.3¢ | 478 | fees |
| Senate seats ≥ 51 vs Senate-D (long) | seats* | 0.984 | +1.6¢ | -7.4¢ | 0.15 | fees |

(22 checks run; the full list prints with every run. \*Seat-count vs control
checks are near-identities: a vacancy on Feb 1 or a failed leadership vote could
separate them, so they are never labeled arbitrage.)

- **Sums and marginals hold within the spread.** Three exact baskets are
  positive by 0.3¢ before fees, and roughly 3.6¢ of fees turns each negative.
  The combo, House control, and Senate control are priced consistently with one
  another.
- **The same-party market is the outlier.** It bids 59¢ and offers 66¢ for
  "same party controls both chambers", while the combo prices DD + RR at 72.1%.
  Buying DR at 27¢, RD at 0.7¢ and same-party YES at 66¢ costs 93.7¢ and pays $1
  in all four outcomes. After 3.0¢ of fees that is +3.3¢ per set on the 200 sets
  offered: $6.58, or 3.4% on the capital (9.5% annualized, simple) locked until
  settlement on Feb 1, 2027. The quotes were read about 16 seconds apart, so the
  prices should be re-checked before trading.
- **The Senate seat buckets are more Democratic than the control market.** They
  imply 67.6% for 51 or more, against 63.9%. But their midpoints sum to only
  96.9%, and the tail buckets have a fraction of a contract behind them. Not
  tradable.

## 7. Robustness

**Estimators.** How the race books become probabilities (see
`src/strategy/estimators.py`):

| Estimator | Model selected | σ H / S | κ | Flip R | Loss | DD | DR | RD | RR | House | Senate |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| midpoint | one | 0.560 | 0.77 | 3.5% | 0.0211 | 64.0% | 27.1% | 0.4% | 8.5% | 91.1% | 64.4% |
| last trade | one | 0.545 | 0.77 | 3.6% | 0.0208 | 64.0% | 27.2% | 0.4% | 8.5% | 91.2% | 64.3% |
| width | one | 0.525 | 0.90 | 3.7% | 0.0215 | 64.1% | 26.8% | 0.4% | 8.6% | 90.9% | 64.6% |
| shrunk | two | 0.309 / 0.581 | 0.83 | 5.5% | 0.0165 | 61.0% | 30.2% | 2.8% | 6.0% | 91.2% | 63.8% |

The three market-based estimators agree to within 0.4 points on every outcome.
The shrunk estimator pulls thin House books toward the hand-set seat ratings in
`constants.py`. That moves the House enough that one factor misses a control
market by more than 0.01, and the rule adds the second factor. This is a
statement about the priors, which are rough, not about the market.

**Independent candidates.** Dropping the independent legs, the old D-vs-R
normalization, pulls the Senate mean down: one factor then misses Senate control
by 0.030 (60.9% vs 63.9%), neither model meets the criterion, and DD falls to
60.6%. Counting them with a fitted caucus share closes that gap.

## 8. Limitations

- **Gaussian swing.** One normal factor fits the House well but not the Senate's
  shape: the market is more concentrated on 51-52 seats. A fatter-tailed or
  state-level (regional) factor might fit better, but nothing here requires it.
- **Unpriced seats are exchangeable.** All 191 unpriced Republican seats share
  one flip rate; in reality some are far more vulnerable. The seat-count market
  constrains their total, not which ones.
- **The caucus share is one number.** Osborn and Bodnar have not committed to a
  caucus. κ = 0.77 is what the Senate seat and control markets imply on
  average, not a statement about any candidate.
- **Quotes are not simultaneous.** A snapshot is read over about 16 seconds.
  Cross-market checks can show edges that were never available at one instant.
- **Carry and execution.** Everything settles Feb 1, 2027. A few cents of edge
  on capital locked for four months has to beat the cost of that capital, and
  taker fills on thin books will move prices.

## Reproduce

```bash
cd strategies/2026-midterm-prediction-arbitrage
uv sync
uv run strategy --snapshot-in snapshots/2026-09-23.json --seed 12345
uv run strategy --snapshot-in snapshots/2026-09-23.json --seed 12345 --model two-factor
uv run strategy --snapshot-in snapshots/2026-09-23.json --seed 12345 --estimator width
uv run --group dev pytest
```

A fresh live run with `--snapshot-out snapshots/<date>.json` records a new
snapshot; replaying it gives the same report every time.
