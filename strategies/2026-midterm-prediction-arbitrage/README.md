# 2026-midterm-prediction-arbitrage

Price the four ways the 2026 U.S. midterms can split control of Congress by
drawing every House and Senate race **independently** from its Kalshi market,
compare them to Kalshi's combo market, and paper-trade the differences. The four
outcomes:

| Code | Meaning            |
| ---- | ------------------ |
| DD   | Democrats sweep    |
| DR   | Democratic House, Republican Senate |
| RD   | Republican House, Democratic Senate |
| RR   | Republicans sweep  |

Its sibling, `2026-midterm-dependence-arbitrage`, runs the same machinery with
a latent-swing model in which races move together. The two are separate
strategies with separate ledgers, so their paper performance can be compared.

**What to expect from this model.** Independent races concentrate the seat
count: on the committed snapshot it gives a Democratic House 100% (control
market: 91.3%) and a Republican sweep 0% (market: 8.6%). It therefore wants to
sell RR and buy DR, trades the dependence model rejects. See
`2026-midterm-dependence-arbitrage/reports/dependence.md` for why the market's
seat-count prices contradict independence. The paper ledger is how this
strategy's bets get scored. On that snapshot the book it opens is worth +$5.23
under its own model but -$1.21 under the market's prices and -$0.98 under the
dependence model: the edge exists only if independence is right.

## Pipeline

1. **Acquire quotes**: every leg of every race (35 Senate, 67 priced House
   districts), the combo, House and Senate control, the same-party market, the
   seat-count buckets, and each traded series' fees and contract terms. Prices
   come from order books. With `--snapshot-out`, the snapshot is saved **now,
   before the model runs**.
2. **Model-free checks** over the nine settlement states (each chamber leader
   D, R, or neither). `ARB` only if the basket profits after fees in all nine;
   `cond` if only while both leaders are D or R.
3. **Independent model**: each race's legs (D, R, independents) become a
   probability with the chosen estimator; each race is drawn on its own
   (100,000 Monte Carlo runs); independent candidates count as non-Democratic.
4. **Report** the model against the market, with the EV after fees at the
   quantity the book offers.
5. **Paper trade** (issue #7): settle resolved positions, confirm candidates on
   a second scan, size by fractional Kelly within the caps, fill on paper,
   value the open book under the model, the market, and the sibling's model.

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
- 368 House districts have no market; they are held at their 2024 holder.
- Prices are snapped to Kalshi's four-decimal grid and all money is `Decimal`.

## Fees

Kalshi's quadratic trade fee (`0.07 · C · P · (1 − P)`, times the series
multiplier) is rounded up to $0.000001, and then the balance change is floored
to the account's precision: **$0.01 for a non-direct (retail) account**, the one
paper results assume. One contract at $0.055 costs $0.06 all in (fee $0.005,
Kalshi's published example). Fees are computed at the quantity actually filled.

## Paper trading and the ledger

Every `uv run strategy` creates `runs/2026-midterm-prediction-arbitrage/<UTC time>/`
at the repository root (gitignored) with `run.json`, an append-only
`ledger.jsonl`, and `run.log`. **Paper mode is the default and the only mode:
nothing is ever sent.** Live orders are issue #5.

- **Signals**: guaranteed arbitrage baskets, and combo legs where the model's
  edge after fees beats `--min-edge` plus three Monte Carlo standard errors.
  Conditional baskets, near-identities, and thin books are recorded and
  rejected with the reason.
- **Sizing**: fractional Kelly within hard caps. The caps are resting depth,
  free cash (cost plus entry fees reserved), and `--max-position`; one open
  position per basket; overlapping baskets never reuse the same depth. Within
  them, the quantity maximizes expected CRRA utility of terminal wealth with
  risk aversion `1 / --kelly-fraction` (0.25 by default: about a quarter of the
  Kelly stake), across the nine settlement states and counting every open
  position, so a trade correlated with the book is sized against it. The
  model's probability of the trade paying is first lowered by the model's
  uncertainty on that outcome, so Kelly is applied to the edge left after the
  uncertainty. A guaranteed basket has no losing state, so only the caps limit
  it. Each signal records what set its quantity (`kelly`, `depth`, `cash`, or
  `max position`). Edges and fees are evaluated at the quantity that fills.
- **Confirmation**: candidates are re-priced on a second scan of their legs'
  books and filled at that scan's prices.
- **Settlement**: a later run settles any position whose markets have resolved,
  at $1 per winning contract.
- **Simulated P&L under three views**: each run values the open book by
  drawing settlement states from (1) the strategy's model, (2) the market -- the
  combo's midpoints, normalized to sum to one -- and (3) the sibling strategy's
  model (`2026-midterm-dependence-arbitrage`), read from the latest scan in its own
  ledger, preferring a scan of the same quotes and labeled when it is not. A
  model always likes the trades it chose, so a book that is only worth money
  under its own model shows up as a gap between the rows. The ledger report
  does the same with the latest scan. It is a valuation, kept apart from earned
  P&L and from the worst-case projected minimum.

```bash
uv run strategy ledger
uv run strategy ledger --since 2026-10-01 --until 2026-11-30
```

The report joins runs by position id and prints, per entry run and in total:
volume, capital committed (open / settled), projected minimum profit on open
positions, settled net P&L, return on committed capital, each position's status,
and the simulated P&L of the open positions under the model, the market, and
the sibling strategy's model.

## Install

```bash
uv sync
```

No API key is needed. uv installs by copying (`link-mode = "copy"` in
`pyproject.toml`): OneDrive rejects the hardlinks uv uses by default, which
leaves a half-built `.venv`. If a `.venv` is already broken, delete it and run
`uv sync` again.

## Run

```bash
uv run strategy                                          # live quotes, paper trading
uv run strategy --snapshot-out snapshots/2026-09-29.json # record what the run reads
uv run strategy --snapshot-in snapshots/2026-09-23.json --seed 12345   # replay
```

| Option | Default | |
| --- | --- | --- |
| `--estimator` | `midpoint` | `midpoint`, `width`, `last`, `shrunk` |
| `--snapshot-in FILE` | live API | replay a saved snapshot |
| `--snapshot-out FILE` | none | save every quote read (before the model, and again at the end) |
| `--mode` | `paper` | `live` is refused (issue #5) |
| `--no-trade` | off | scan and report only |
| `--bankroll` | 1000 | paper bankroll, dollars |
| `--max-position` | 100 | entry capital cap per position, dollars |
| `--min-edge` | 0.01 | edge per set after fees, dollars |
| `--kelly-fraction` | 0.25 | fraction of the Kelly stake, in (0, 1]; 1 is full Kelly |
| `--contracts` | 100 | largest order shown in checks and EV |
| `--runs-dir` | `<repo>/runs` | where run directories go (`STRATEGY_RUNS_DIR` too) |
| `--confirm-delay` | 5 live, 0 replay | seconds before the confirmation scan |
| `--simulations`, `--seed` | 100000, none | Monte Carlo runs and seed |

`--model` accepts only `independent`. The paper limits are placeholders until
the capital and position limits in issue #5 are decided.

## Sample output

`uv run strategy --snapshot-in snapshots/2026-09-23.json --seed 12345` (the
golden output in `tests/expected_output.txt` fixes the clock):

```
Outcome              Model  Market      Bid/Ask     Edge      EV after fees
---------------------------------------------------------------------------
Democrats sweep      66.8%   63.5%    63.0/64.0   +3.3pp     buy +1.2c x100
D House / R Senate   33.2%   26.5%    26.0/27.0   +6.7pp     buy +4.8c x100
R House / D Senate    0.0%    0.7%      0.6/0.7   -0.6pp    sell +0.5c x100
Republicans sweep     0.0%    8.6%      8.5/8.7   -8.6pp     sell +7.9c x37
---------------------------------------------------------------------------
D House control     100.0%   91.3%    91.2/91.3
D Senate control     66.8%   64.5%    64.0/65.0

Paper trading - runs/2026-midterm-prediction-arbitrage/20260923T170000Z (paper mode: simulated fills, nothing sent)
Bankroll $1,000.00; free cash $952.60 after this run; 2 open position(s), entry capital $47.40
Signals: 2 accepted, 3 rejected
  + 20260923T170000Z-01 model:sell RR: 37 x (capped by depth), edge +7.9c/set > threshold +1.0c
  + 20260923T170000Z-02 model:buy DR: 47 x (fractional Kelly; caps allow 352), edge +4.8c/set > threshold +1.4c
  - model:buy DD: edge +1.17c/set at 152 sets does not exceed threshold 1.45c
  ...
Open book: projected minimum profit -$47.40 (worst settlement state)
Simulated P&L of the open book (100,000 draws, seed 12345), a valuation, not earned P&L:
  under independent model (this run): mean $5.23, 5%-95% -$10.40 to $36.60, P(loss) 66.7%
  under the market (combo midpoints, normalized): mean -$1.21, 5%-95% -$47.40 to $36.60, P(loss) 73.5%
  under 2026-midterm-dependence-arbitrage: not available (no scan in runs/2026-midterm-dependence-arbitrage)
```

The golden run has no sibling ledger. After the dependence strategy has run on
the same snapshot, the last row reads `under one factor model of
2026-midterm-dependence-arbitrage (run ...): mean -$0.98, ..., P(loss) 73.1%`.

The RR sale stays at the book's depth: this model puts exactly 0% on a
Republican sweep, with no Monte Carlo error at 0, so Kelly sees no losing state
to size against. Only a model uncertainty that reflects model error, not just
simulation noise, would limit it.

## Test

```bash
uv run --group dev pytest
```

Unit tests run on small synthetic files in `tests/fixtures/` (an aggregate-market
snapshot and a two-run fixture ledger). The integration test replays the
committed snapshot: golden output, the run directory, live mode refused, a
replayable snapshot after a forced failure, and a position opened in one run and
settled in the next (issue #7's acceptance test).

## Layout

| Module | |
| --- | --- |
| `api.py`, `snapshot.py` | public Kalshi client; record and strictly replay every quote |
| `races.py`, `constants.py`, `control.py` | races, tickers, seat baselines, control rules |
| `markets.py`, `estimators.py`, `probabilities.py` | aggregate markets; race books to probabilities |
| `money.py`, `fees.py`, `checks.py` | exact money; Kalshi fees; nine-state checks |
| `ledger.py`, `trading.py`, `pnl.py` | run directories; paper trading; aggregates and simulated P&L |
| `output.py` | shared rendering: checks, EV, paper section, ledger report |
| `simulation.py` | the independent model |
| `report.py`, `runner.py`, `cli.py` | this strategy's report, pipeline, command line |

Every module except `report.py` and `runner.py` is shared with
`2026-midterm-dependence-arbitrage` and kept identical (the repository copies
code between strategies instead of sharing a library).

## References

- Kalshi API: https://docs.kalshi.com; fee rounding:
  https://docs.kalshi.com/getting_started/fee_rounding
- CONTROL contract terms: https://assets.kalshi.com/contract_terms/CONTROL.pdf
