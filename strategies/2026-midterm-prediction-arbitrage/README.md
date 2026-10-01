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
strategy's bets get scored: the edge exists only if independence is right.

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
   a second scan, size within the caps, and fill on paper.

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
at the repository root (gitignored) with `ledger.jsonl` (the accounting record:
one flat row per fill, settlement, or mark, the same thirteen columns in every
row), `events.jsonl` (scans and signals, diagnostics), `run.json`, and
`run.log`. The schema is in `src/strategy/ledger.py` and the root README.
**Paper mode is the default and the only mode: nothing is ever sent.** Live
orders are issue #5.

- **Signals**: guaranteed arbitrage baskets, and combo legs where the model's
  edge after fees beats `--min-edge` plus three Monte Carlo standard errors.
  Conditional baskets, near-identities, and thin books are recorded and
  rejected with the reason.
- **Sizing**: the largest quantity the caps allow: resting depth, free cash
  (cost plus entry fees reserved), and `--max-position`; one open position per
  basket; overlapping baskets never reuse the same depth. Each signal records
  what set its quantity (`depth`, `cash`, or `max position`). Edges and fees
  are evaluated at the quantity that fills.
- **Confirmation**: candidates are re-priced on a second scan of their legs'
  books and filled at that scan's prices.
- **Settlement**: a later run settles any position whose markets have resolved,
  at $1 per winning contract.
- **Marks**: at the end of each run every open leg gets a `mark` row at what a
  buyer pays for it now (the bid for a YES, one minus the ask for a NO), so the
  ledger carries a value series for the open book.

Reporting across runs and strategies is `scripts/` at the repo root
(`uv run pnl csv | report | plot`); the strategy itself does not aggregate.

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
Bankroll $1,000.00; free cash $866.04 after this run; 2 open position(s), entry capital $133.96, marked at $125.30 (P&L -$8.66)
Signals: 2 accepted, 3 rejected
  + 20260923T170000Z-01 model:sell RR: 37 x (capped by depth), edge +7.9c/set > threshold +1.0c
  + 20260923T170000Z-02 model:buy DR: 352 x (capped by max position), edge +4.8c/set > threshold +1.4c
  - model:buy DD: edge +1.17c/set at 152 sets does not exceed threshold 1.45c
  ...
  mark 20260923T170000Z-01 KXBALANCEPOWERCOMBO-27FEB-RR NO 37 @ 0.9130
  mark 20260923T170000Z-02 KXBALANCEPOWERCOMBO-27FEB-DR YES 352 @ 0.2600
```

The book is marked at the bid right after it was bought at the ask, so the
first mark shows the spread and fees as a loss.

The RR sale is capped by the book's depth (37 contracts resting at the bid);
the DR purchase by `--max-position`.

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
| `ledger.py`, `trading.py`, `pnl.py` | run directories and the ledger schema; paper trading; positions and cash from the ledger |
| `output.py` | shared rendering: checks, EV, paper section |
| `simulation.py` | the independent model |
| `report.py`, `runner.py`, `cli.py` | this strategy's report, pipeline, command line |

Every module except `report.py` and `runner.py` is shared with
`2026-midterm-dependence-arbitrage` and kept identical (the repository copies
code between strategies instead of sharing a library).

## References

- Kalshi API: https://docs.kalshi.com; fee rounding:
  https://docs.kalshi.com/getting_started/fee_rounding
- CONTROL contract terms: https://assets.kalshi.com/contract_terms/CONTROL.pdf
