# 2026-midterm-dependence-arbitrage

Price the four ways the 2026 U.S. midterms can split control of Congress with a
**latent-swing model** of the individual race markets, compare them to Kalshi's
combo market, and paper-trade the differences. The four outcomes:

| Code | Meaning            |
| ---- | ------------------ |
| DD   | Democrats sweep    |
| DR   | Democratic House, Republican Senate |
| RD   | Republican House, Democratic Senate |
| RR   | Republicans sweep  |

Kalshi lists one combo market, `KXBALANCEPOWERCOMBO`, with a leg for each
outcome. This strategy tests it with **model-free checks** against the markets it
must agree with by construction, and with a **bottom-up model** in which races
move together through a swing calibrated to the seat-count and control markets.
Its sibling, `2026-midterm-prediction-arbitrage`, is the same machinery with the
independent-race model.

**Findings on the committed snapshot:** [reports/dependence.md](reports/dependence.md).
One latent swing reproduces the held-out combo within 0.4 points; the independent
model misses by 5. No model trade survives fees and model uncertainty, and the
one mispriced basket is conditional on both chamber leaders being D or R.

## Pipeline

1. **Acquire quotes**: every leg of every race (35 Senate, 67 priced House
   districts), the combo, House and Senate control, the same-party market, the
   seat-count buckets, and each traded series' fees and contract terms. Prices
   come from order books: best bid, best ask, and the contracts resting at each.
   With `--snapshot-out`, the snapshot is saved **now, before any fitting**.
2. **Model-free checks**: baskets over the nine settlement states (each chamber
   leader D, R, or neither). `ARB` only if the basket profits after fees in all
   nine; `cond` if only while both leaders are D or R.
3. **Race probabilities** from each race's legs (D, R, independents) with the
   chosen estimator.
4. **Calibrate** the swing, the flip rates of the House seats Kalshi does not
   price, and the independents' caucus share to the seat-count and control
   markets; the combo is held out. Keep one factor unless it misses a control
   market by more than 0.01 and two factors do better.
5. **Report** the independent model, the factor model, and the market side by
   side.
6. **Paper trade** (issue #7): settle resolved positions, confirm candidates on
   a second scan, size within the caps, and fill on paper.

### The dependence model

Each race has a latent Democratic margin
`M = sqrt(1 + σ²) · Φ⁻¹(p) + σ · U + ε`, with `p` the race's market price, `U`
a shared swing, and `ε` race-specific noise. The scaling keeps every race at its
market price for any σ; σ only controls how much races move together (σ = 0 is
the independent model). Seat distributions are computed exactly given the swing
and integrated with Gauss-Hermite quadrature. See the report for the fit.

### Control rules and settlement

- **House:** Democrats need 218 of 435. **Senate:** 51, since the Republican VP
  breaks a 50-50 tie.
- The combo, control, and same-party markets settle on Kalshi's **CONTROL**
  rules: the party of the Speaker and of the President pro tempore. An
  independent or vacant leader is neither party, so every D/R leg loses; that is
  why baskets are checked over nine states.
- Expected expiration Feb 1, 2027; the combo's metadata allows Feb 8, and early
  determination or a review can move it. Holding periods are assumptions.

### Data traps handled in code

- Kentucky's Senate race is filed under `SENATELA-26`; Louisiana has no market;
  the FL and OH specials are `SENATEFLS-26` and `SENATEOHS-26`.
- Independent candidates (Osborn NE, Bodnar MT, Achilles ID, Bengs SD, Hill
  AK-AL) have their own legs; every leg is normalized together.
- 368 House districts have no market; they start at their 2024 holder and get
  calibrated flip rates.
- Prices are snapped to Kalshi's four-decimal grid and all money is `Decimal`:
  `1 - 0.34` is `0.66`, and three asks that cost exactly $1 never show a
  phantom edge.

## Fees

Kalshi's quadratic trade fee (`0.07 · C · P · (1 − P)`, times the series
multiplier) is rounded up to $0.000001, and then the balance change is floored
to the account's precision: **$0.01 for a non-direct (retail) account**, the one
paper results assume. So one contract at $0.055 costs $0.06 all in (fee $0.005,
Kalshi's published example). Fees are always computed at the quantity actually
filled; they are not proportional to quantity and are never scaled.

## Paper trading and the ledger

Every `uv run strategy` creates `runs/2026-midterm-dependence-arbitrage/<UTC time>/`
at the repository root (gitignored) with `run.json`, an append-only
`ledger.jsonl`, and `run.log`. **Paper mode is the default and the only mode:
nothing is ever sent.** Live orders are issue #5.

- **Signals**: guaranteed arbitrage baskets, and combo legs where the model's
  edge after fees beats `--min-edge` plus the model's own uncertainty (the gap
  between the one- and two-factor prices). Conditional baskets, near-identities,
  and thin books are recorded and rejected with the reason.
- **Sizing**: the largest quantity the caps allow: resting depth, free cash
  (cost plus entry fees reserved), and `--max-position`; one open position per
  basket; overlapping baskets never reuse the same depth. Each signal records
  what set its quantity (`depth`, `cash`, or `max position`). Edges and fees
  are evaluated at the quantity that fills.
- **Confirmation**: candidates are re-priced on a second scan of their legs'
  books and filled at that scan's prices (recorded under `confirm:` in a
  snapshot).
- **Settlement**: a later run settles any position whose markets have resolved,
  at $1 per winning contract, with the run's date as the date of cash receipt.

Reporting across runs and strategies reads the `runs/` tree directly; the
strategy itself does not aggregate.

## Install

```bash
uv sync
```

No API key is needed: every endpoint read is public. uv installs by copying
(`link-mode = "copy"` in `pyproject.toml`): OneDrive rejects the hardlinks uv
uses by default, which leaves a half-built `.venv`. If a `.venv` is already
broken, delete it and run `uv sync` again.

## Run

```bash
uv run strategy                                          # live quotes, paper trading
uv run strategy --snapshot-out snapshots/2026-09-29.json # record what the run reads
uv run strategy --snapshot-in snapshots/2026-09-23.json --seed 12345   # replay
```

| Option | Default | |
| --- | --- | --- |
| `--model` | `auto` | `auto`, `one-factor`, `two-factor`, or `independent` |
| `--estimator` | `midpoint` | `midpoint`, `width`, `last`, `shrunk` |
| `--snapshot-in FILE` | live API | replay a saved snapshot |
| `--snapshot-out FILE` | none | save every quote read (before fitting, and again at the end) |
| `--mode` | `paper` | `live` is refused (issue #5) |
| `--no-trade` | off | scan and report only |
| `--bankroll` | 1000 | paper bankroll, dollars |
| `--max-position` | 100 | entry capital cap per position, dollars |
| `--min-edge` | 0.01 | edge per set after fees, dollars |
| `--contracts` | 100 | largest order shown in checks and EV |
| `--runs-dir` | `<repo>/runs` | where run directories go (`STRATEGY_RUNS_DIR` too) |
| `--confirm-delay` | 5 live, 0 replay | seconds before the confirmation scan |
| `--simulations`, `--seed` | 100000, none | Monte Carlo runs and seed |

The paper limits are placeholders until the capital and position limits in
issue #5 are decided.

## Sample output

`uv run strategy --snapshot-in snapshots/2026-09-23.json --seed 12345` (the
integration test's golden output, `tests/expected_output.txt`, fixes the clock
so the run directory name is stable).

```
2026 Midterm - Congress balance of power
snapshot 2026-09-23.json (captured 2026-09-23T16:25:13Z) - estimator midpoint - model one factor

Model-free checks (taker; per set, in cents; fees at min(100, size) sets; Raw/Net assume D or R leaders, Worst covers independent or vacant leaders)
Check                                      Cost      Raw      Net     Worst     Size  Verdict
---------------------------------------------------------------------------------------------
combo: buy all four legs                 1.0040    -0.4c    -4.0c   -104.0c   10,464  ok
combo: sell all four legs                3.0190    -1.9c    -5.5c     -5.5c       37  ok
...
DD + RR vs same-party (long)             0.9370    +6.3c    +3.3c    -96.7c      200  cond
...
0 arbitrage(s) in every settlement state; 1 conditional on D/R leaders; 4 positive only before fees.

Calibration: seat-count and control markets (combo held out)
Model            sigma H sigma S  caucus     flip R/D    loss   House  Senate
-----------------------------------------------------------------------------
no swing           0.000   0.000    0.42  25.0%/24.6%  0.5486   96.4%   71.2%
one factor         0.560   0.560    0.77    3.5%/0.2%  0.0211   91.1%   64.4%
two factor         0.384   0.607    0.74    4.3%/1.1%  0.0170   91.2%   63.7%
control markets                                                 91.3%   63.9%
Selected one factor: one factor matches both control markets within 0.005 <= 0.01

Outcome              Indep  Factor  Market      Bid/Ask     Edge      EV after fees
-----------------------------------------------------------------------------------
Democrats sweep      74.4%   64.0%   63.5%    63.0/64.0   +0.5pp                  -
D House / R Senate   25.6%   27.1%   26.5%    26.0/27.0   +0.6pp                  -
R House / D Senate    0.0%    0.4%    0.7%      0.6/0.7   -0.3pp    sell +0.2c x100
Republicans sweep     0.0%    8.5%    8.6%      8.5/8.7   -0.1pp                  -
...

Paper trading - runs/2026-midterm-dependence-arbitrage/20260923T170000Z (paper mode: simulated fills, nothing sent)
Bankroll $1,000.00; free cash $1,000.00 after this run; 0 open position(s), entry capital $0.00
Signals: 0 accepted, 2 rejected
  - model:sell RD: edge +0.20c/set at 100 sets does not exceed threshold 2.81c
  - arbitrage:DD + RR vs same-party (long): conditional: pays nothing if a chamber leader is an independent or the office is vacant
```

## Test

```bash
uv run --group dev pytest
```

Unit tests run on small synthetic files in `tests/fixtures/`: an aggregate-market
snapshot and a two-run fixture ledger. The integration test replays the
committed snapshot and requires the golden output, one factor within 0.01 of the
control markets, every race's simulated win rate within 0.01 of its price, a
replayable snapshot after a forced calibration failure, and a position opened in
one run and settled in the next (issue #7's acceptance test).

## Layout

| Module | |
| --- | --- |
| `api.py`, `snapshot.py` | public Kalshi client; record and strictly replay every quote |
| `races.py`, `constants.py`, `control.py` | races, tickers, seat baselines, control rules |
| `markets.py`, `estimators.py`, `probabilities.py` | aggregate markets; race books to probabilities |
| `money.py`, `fees.py`, `checks.py` | exact money; Kalshi fees; nine-state checks |
| `ledger.py`, `trading.py`, `pnl.py` | run directories; paper trading; positions and cash from the ledger |
| `output.py` | shared rendering: checks, EV, paper section |
| `simulation.py`, `factor.py`, `calibration.py` | independent model; latent swing; fit |
| `report.py`, `runner.py`, `cli.py` | this strategy's report, pipeline, command line |

`api.py`, `snapshot.py`, `money.py`, `fees.py`, `markets.py`, `checks.py`,
`ledger.py`, `trading.py`, `pnl.py`, `output.py`, `cli.py` and the race modules are shared
with `2026-midterm-prediction-arbitrage` and kept identical (the repository
copies code between strategies instead of sharing a library).

## References

- Kalshi API: https://docs.kalshi.com; fee rounding:
  https://docs.kalshi.com/getting_started/fee_rounding
- CONTROL contract terms: https://assets.kalshi.com/contract_terms/CONTROL.pdf
- Markets: `KXBALANCEPOWERCOMBO`, `CONTROLH-2026`, `CONTROLS-2026`,
  `KXSAMEPARTYCONGRESS`, `KXDHOUSESEATS-27`, `KXDSENATESEATS-27`.
