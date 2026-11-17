# Cross-market sportsbook sum check

The [Kalshi sum check](../kalshi_sum_check/), run across venues. Exactly one team
wins a game. Buying one contract on every outcome therefore pays $1 whatever happens.
If buying each outcome where it is cheapest costs less than $1 in total, after fees,
the difference is locked in. This strategy prices each outcome on Kalshi's game
markets and on DraftKings' moneyline, and trades only baskets with at least one leg
on each venue:

```
edge per set = 1 − Σ over outcomes of min(Kalshi YES ask + taker fee, 1 / DraftKings decimal odds)
```

| Game | Kalshi | DraftKings | Basket |
|---|---|---|---|
| BKN @ BOS | BOS YES ask 0.70, fee ≈ 0.015 | BKN 4.00 → 0.25 | 0.965 per $1: edge 0.035 |

It is paper only. Kalshi legs and DraftKings bets both go through the shared
[broker](../../docs/broker-and-reporting.md), into one portfolio with one cash
balance. The shared market and DraftKings monitors settle them, and the shared
report measures them.

## DraftKings data

DraftKings publishes no sportsbook API. The
[documentation the work started from](https://github.com/SeanDrum/Draft-Kings-API-Documentation)
covers DraftKings' fantasy (DFS) endpoints, contests and draftables, not sportsbook
odds, so that data flow does not transfer. A separate one is added to the shared
package as `czquant.draftkings`, with two sources:

| `DRAFTKINGS_SOURCE` | What it reads | Results | Status |
|---|---|---|---|
| `oddsapi` (default) | [The Odds API](https://the-odds-api.com), bookmaker `draftkings`, `h2h` market, decimal odds | `/scores` settles bets automatically | Documented and keyed (`ODDS_API_KEY`). Odds update about once a minute; each odds read costs one credit per league |
| `nash` | `sportsbook-nash.draftkings.com/api/sportscontent/{site}/v1/leagues/{id}`, the JSON DraftKings' website reads | None. The operator settles each bet | Undocumented. Field names follow community reverse-engineering. DraftKings' CDN returns 403 to many non-browser clients, including this one when tested |

The `nash` client sends an ordinary request and reports a 403 as `Blocked`. It
does not imitate a browser's TLS fingerprint or headers to get past DraftKings'
bot protection. If DraftKings serves your host, it works; otherwise use `oddsapi`.
Its parser is tested against the payload shape described in public scrapers, not
against a live response, so check its first run.

A DraftKings selection is a ticker `DK:<source>:<league>:<event>:<ML2|ML3>:<role>`.
A bet at decimal odds d is recorded as a YES contract bought at 1/d (rounded up to
6 places). The stake is count × 1/d and the payout is count. The bookmaker's margin
is in the price, so there is no fee. A bet cannot be sold, so its book has an ask
and no bid. The shared report therefore values open bets in the range [0, 1/d]
with no midpoint. A portfolio holding open bets has no point valuation until they
settle, and the report ranks it as incomplete for those minutes.

## Run

You need [uv](https://docs.astral.sh/uv/) and an Odds API key. Kalshi market data
is public.

```bash
cd strategies/cross-market-sportbook
uv sync
cp .env.example .env        # set ODDS_API_KEY, STRATEGY_RUNS_DIR (absolute)
set -a; source .env; set +a
```

From `packages/czquant`, create the portfolio once, then start the monitors in
separate shells:

```bash
uv run czquant portfolio cross-market-sportbook --cash 1000
uv run czquant monitor market        # Kalshi legs: reference books and settlement
uv run czquant monitor draftkings    # bets: reference odds, results and settlement
```

Then, from this directory:

```bash
uv run python trader.py --once --leagues nba     # one scan
uv run python trader.py --duration 4:00
```

### Odds credits

The Odds API's free plan gives 500 credits a month. An odds read costs 1 credit per
league, and a results read costs 2. Only two things spend credits:

| Who | What it pays for | When |
|---|---|---|
| `trader.py` | One league's odds | When the next read is due, for one league with a game starting within `--horizon` hours (12). The league read longest ago goes first |
| `trader.py` (fill) | The bet's own odds check | Once per basket it actually tries to place |
| `czquant monitor draftkings` | Results, all held games of a league at once | At most every 30 minutes per league, and only 2.5 hours after a held game started |

Everything else is free. The monitor marks held bets from the newest read any process
cached in `$STRATEGY_RUNS_DIR/odds_cache/`, dated by when that read was made. The
dashboard reads files and the store only.

The trader spreads reads so the credits last the trial. The next read waits
(trial end − now) ÷ (credits − reserve), never less than `--interval`. With 499
credits, a reserve of 40, and a 7-day trial, that is one read about every 22 minutes,
and unused hours (no upcoming games) leave more credits for later. The trial end is
saved in `odds_cache/trial.json`, so a restart keeps it. `--trial-days N` starts a new
one. Odds reads stop at `--odds-reserve` (40) credits. Those are kept for results
reads, so held bets can still settle. Each results read uses 2 of the 40, so the
reserve covers about 20. The dashboard shows the credits remaining and the trial end.

```bash
uv run python trader.py --trial-days 7 --leagues nba,nhl     # fewer leagues, more reads per league
```

## What it trades

Every check below must pass:

- **Leagues** come from `leagues.json`: NBA, NHL and MLB are enabled. NFL is
  disabled. A tie pushes DraftKings' two-way moneyline (the stake is returned) while
  both Kalshi team markets resolve No, so the basket can lose.
- **Pairing** (`matching.py`). A Kalshi game pairs with a DraftKings game when it
  has the same Eastern game date and, for MLB tickers that carry it, a start within
  90 minutes. There must also be exactly one way to assign Kalshi's team markets
  (by city and ticker abbreviation) to DraftKings' home and away teams. Ambiguous or
  unmatched games are counted and never traded. `pairs.json` can pin a pair (with
  explicit roles) or block a Kalshi event.
- **Pre-game only.** A game that starts within `--min-lead` minutes (10) is skipped.
  So is a game with suspended odds, or odds DraftKings last updated more than
  `--max-odds-age` (1800 s) ago according to the source.
- **Edge.** The basket must clear `--min-edge` (0.01 per $1 set) and `--min-net`
  ($0.25), and beat the risk-free hurdle `--risk-free` (5%) compounded to the game's
  expected end. Kalshi legs are walked through the book. Each pays the series taker
  fee, rounded up to the cent, as the paper adapter charges it.
- **Size.** The basket is sized at each depth where a leg's price changes, up to
  `--max-sets` (500). It is also capped by `--max-event-frac` of the bankroll per
  game (10%), `--max-total-frac`, `--max-open-events`, cash, and `--max-stake` per
  DraftKings selection ($500). The last stands in for DraftKings' unpublished
  account limits.
- **Execution.** After a re-check on fresh Kalshi books (the odds are the read the
  scan just paid for), the DraftKings legs go first. Each is a fill-or-kill bet, declined whole if the odds shortened, the game
  started, or the quote went stale. Better odds are taken at the better price.
  Only when every bet is accepted are the Kalshi legs bought: IOC through the shared
  depth policy, concurrently, for the same number of sets. A declined bet costs
  nothing. A Kalshi leg that fills partially leaves a partial basket, which is
  recorded with its intended and actual legs.
- **Halts.** A fully filled basket that settles at a loss halts new trades. The
  halt survives restart until an operator records `halt_cleared` against it. A file
  named `KILL` in `$STRATEGY_RUNS_DIR/diagnostics/<portfolio>/` also halts.

## Settlement

`czquant monitor draftkings` records each held selection's odds, then its result:
1 for the winner, 0 for the others. A tie on a two-way moneyline, or a void, returns
each lot's stake. With `nash`, or when a game is postponed or cancelled, settle by
hand from evidence:

```bash
uv run czquant settle-sportsbook "DK:oddsapi:basketball_nba:<event>:ML2:away" \
  --result void --note "postponed; DraftKings voided the moneyline (bet history, 2026-10-28)"
```

## Caveats

1. **The venues' rules are not identical.** Kalshi resolves on the game "originally
   scheduled" for the date. DraftKings voids postponed games under its own house
   rules. A postponed or suspended game can pay one leg and void the other.
   `leagues.json` records the rule assumptions, and each trade's context carries
   them. Read both venues' current rules before trusting a league.
2. **DraftKings limits and moves accounts.** Real books restrict accounts that bet
   stale lines. Paper fills at the quoted odds model neither limits nor latency
   beyond `--book-latency`.
3. **Odds API data is aggregated.** Its DraftKings price can lag the sportsbook by
   about a minute. The fill re-reads the odds, but a lagged quote can still be
   unavailable on DraftKings itself.
4. **Legal and terms of service.** Sports betting is legal only in some states,
   and DraftKings' terms restrict automated access. This code never places a real
   bet. Before anything here leaves paper, check the law where you are, DraftKings'
   terms, and Kalshi's.

## Files

- `matching.py`: pairs Kalshi game events with DraftKings events
- `sum_check.py`: cross-venue basket pricing and sizing
- `trader.py`: the paper trader (discovery, pairing, pricing, execution through the broker)
- `leagues.json`: league ids per source, draw handling, rule notes
- `pairs.json`: operator-pinned pairs and blocked events
- `tests/`: matching, pricing, and end-to-end paper trading against fixture venues
- `../../packages/czquant/src/czquant/draftkings.py`, `sportsbook.py`: the shared DraftKings data flow, paper adapter, venue router and operator settlement
