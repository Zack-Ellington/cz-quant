# cross-market-polymarket

The sum check across two prediction markets. Exactly one team wins a game, so one
contract on each team pays $1 whatever happens. Kalshi and Polymarket both list
moneylines on the same games. When buying each team on the venue where it's cheapest
costs less than $1 in total after both venues' fees, the difference is locked in:

    edge per set = 1 - sum over teams of min(Kalshi YES ask + fee, Polymarket token ask + fee)

[kalshi_sum_check](../kalshi_sum_check/) runs this check within Kalshi, and
[cross-market-sportbook](../cross-market-sportbook/) runs it against DraftKings.
This strategy prices each team on Kalshi and on Polymarket and, by default, trades
only baskets with a leg on each venue (`--any-venue` also allows one-venue baskets).
**Paper only:** nothing here can place a real order on either venue.

## How it works

| Step | What happens |
|---|---|
| Discover | Every 10 minutes: the open Kalshi game events and Polymarket game events of each league enabled in `leagues.json`, and each Kalshi series' fee schedule. |
| Pair | Each Kalshi game is paired with exactly one Polymarket moneyline ([matching.py](matching.py)). |
| Price | Both venues' books for every paired game, walked to size with each venue's fee ([sum_check.py](sum_check.py)). The best basket must beat `--min-edge`, `--min-net` and the 5% risk-free hurdle. |
| Execute | Highest APR first, within the capital limits. Each basket is re-checked on fresh books, then its legs go out one at a time as IOC orders, starting with the thinnest book. Each later leg asks for no more than the smallest fill so far. Contracts beyond the complete sets are sold back (reduce-only IOC) no lower than cost minus `--unwind-slippage`. |

Settlement belongs to the monitors: `czquant monitor market` settles the Kalshi legs
and `czquant monitor polymarket` settles the Polymarket tokens. A fully filled basket
that settles at a loss halts new trades, also after a restart. A partial basket is
recorded but doesn't halt, because it's a directional position.

### Pairing

A Kalshi game event gives its date and its teams' abbreviations
(`KXNBAGAME-26NOV21BOSSAC-BOS`). A Polymarket game event lists its teams with their
abbreviations and names, its local date (`eventDate`) and its start time. Its
moneyline has one outcome token per team, named after the team. A pair needs:

- the same date, and a start within 90 minutes when Kalshi gives a time (baseball
  doubleheaders);
- the same two abbreviations;
- each team named by exactly one of the moneyline's outcomes: its full name (NBA,
  NHL) or the end of it (NFL outcomes are nicknames, "Bills" for "Buffalo Bills").

Anything else stays unpaired. Abbreviations spelled differently on the two venues
go in `leagues.json` under `abbr_aliases`, as Kalshi's spelling to Polymarket's. The
NHL needs four (`MTL→mon`, `LA→lak`, `VGK→las`, `CGY→cal`), the NFL one (`JAC→jax`). `pairs.json` can pin a pair
or block a Kalshi event:

```json
{"pairs": {"KXNBAGAME-26NOV21BOSSAC": {"slug": "nba-bos-sac-2026-11-21",
           "legs": {"KXNBAGAME-26NOV21BOSSAC-BOS": "bos", "KXNBAGAME-26NOV21BOSSAC-SAC": "sac"}}},
 "blocked": []}
```

On 2026-11-07, 75 of 78 open NBA and NHL Kalshi games paired. The other three were
Utah games that Polymarket didn't list. None had an edge after fees; the closest was
$0.97 at the top of the book before fees.

### Fees

| Venue | Taker fee per order |
|---|---|
| Kalshi | `0.07 × series multiplier × Σ q·p·(1−p)`, rounded up to the cent |
| Polymarket | `Σ q × rate × p·(1−p)`, rounded to 5 decimals, with the market's own `feeSchedule.rate` (0.05 on sports moneylines) |

Polymarket charges only takers. Its schedule also has an `exponent`. Every market
seen so far uses 1, which matches the formula in Polymarket's docs; other values are
applied as `(p·(1−p))^exponent`.

### Rules risk

The basket is riskless only if both venues settle the same game the same way. Read
`leagues.json`'s `rules` for each league. Postponed, suspended and tied games are
where the venues can differ. A Kalshi game with a tie market (NFL, soccer) still pairs
on its two team markets; the tie market is never bought, so a tie can resolve both
Kalshi legs No while Polymarket's moneyline resolves 50-50. That risk is accepted for
paper benchmarking and each pair records `kalshi_tie_market` in its evidence.

## Odds monitor

`odds_monitor.py` serves an interactive page for watching one game at a time on both
venues (NFL by default; `--league nba` and the others work the same way):

```bash
export STRATEGY_RUNS_DIR=C:/Users/zaell/czquant-runs CZQUANT_PORTFOLIO=cross-market-polymarket
uv run python odds_monitor.py       # open http://127.0.0.1:8765
```

- **Search** any open Kalshi game by team, city, abbreviation or ticker. Each result
  says whether it pairs with a Polymarket game (the trader's own matching).
- **Chart**: each team's Kalshi price (solid) and Polymarket price (dashed), with mid, ask
  or last trade selectable, and ranges 1H, 6H, 1D, 1W and All. History comes from Kalshi's
  candlesticks and Polymarket's price history (1-minute for the last day, hourly before).
  Once a game is loaded, both books are read every `--interval` seconds (10).
- **Arbitrage**: the lower panel plots the best cross-venue edge per $1 set, after both
  venues' taker fees, and green bands shade every stretch where it's at least **Min
  edge** (1¢ by default, the trader's `--min-edge`; the trader also needs `--min-net`,
  the return hurdle and depth, which the monitor doesn't check). Polymarket publishes no ask history, only a traded price, so edges before the
  monitor started watching are estimates: faint bands, hidden from the table unless
  **Show estimates from history** is ticked. Edges are measured at the top of the book only
  and depth isn't checked, so a band is a lead rather than a basket the trader could fill.
- **Trades**: the portfolio's baskets (▼) and the trader's logged opportunities (◆) in
  the game are marked on the chart and listed under it.
- **Recent arbitrage opportunities**: windows on live asks in every paired game of the
  league, whether opened or not (all their books are read every `--interval` seconds,
  one request per venue), plus every opportunity the trader logged in the league.
  When a game closes on Kalshi it stops being polled, but its windows stay in the table
  for 3 days, across restarts (each game's pairing is saved beside its samples as
  `<event>.meta.json`). Click a row to open it.

Every read is public and free. Live samples are kept in
`$STRATEGY_RUNS_DIR/diagnostics/<portfolio>/odds_monitor/<event>.jsonl`, so a restart
keeps them.
The monitor reads the store and never trades.

## Run

```bash
uv sync
cp .env.example .env        # fill in STRATEGY_RUNS_DIR; then: set -a; source .env; set +a
uv run czquant portfolio cross-market-polymarket --cash 1000
uv run python trader.py --once --leagues nba        # one scan
uv run python trader.py --duration 4:00
```

Run the monitors in their own shells, with the same `STRATEGY_RUNS_DIR`:

```bash
uv run czquant monitor market
uv run czquant monitor polymarket
```

The portfolio shows up on `uv run czquant dashboard` with no setup. Polymarket tokens
are marked from their CLOB books, in the same layout as Kalshi's, so the report's
valuation rules apply unchanged.

| Flag | Default | Meaning |
|---|---|---|
| `--leagues` | every enabled | comma-separated `leagues.json` keys |
| `--interval` | 15 | seconds between scans |
| `--discover` | 10 | minutes between event refreshes |
| `--max-event-frac` | 0.10 | cap per game, as a share of the bankroll |
| `--max-sets` | 500 | contract sets ($1 payouts) per basket |
| `--min-edge` | 0.01 | minimum profit per $1 set: the margin for rules and settlement differences |
| `--min-net` | 0.25 | minimum profit per basket, dollars |
| `--any-venue` | off | also trade one-venue baskets |
| `--no-markets` | off | skip the reviewed non-sports pairs in `markets.json` |
| `--min-lead` | 10 | skip games starting within this many minutes |
| `--resolution-hours` | 6 | hours from the start to payout, for the hurdle |
| `--unwind-slippage` | 0.05 | worst price per contract below cost when selling back |
| `--cooldown` | 300 | seconds before trading a game again |

A file named `KILL` in `$STRATEGY_RUNS_DIR/diagnostics/<portfolio>/` halts new trades.

## Non-sports pairs

Beyond sports, the trader scans any yes/no question listed in `markets.json`: one
Kalshi market against one Polymarket market. A basket buys one answer on each venue,
Kalshi YES with Polymarket No or Kalshi NO with Polymarket Yes, and pays $1 however
the question resolves, **if both venues resolve it the same way**. Kalshi NO is bought
through the YES book (an ask at 1 − price), the same way the shared broker records it.

Questions don't pair themselves the way games do: the wording, deadline, data source
and edge cases differ between venues. So pairs are listed by hand, from leads that
`find_pairs.py` suggests (it writes `candidates.json`, git-ignored).

**With an Oddpool key** (`ODDPOOL_API_KEY`, the default source when set), it uses
[Oddpool's series search](https://docs.oddpool.com/search/series), which lists each
topic's Kalshi and Polymarket series side by side ([oddpool.py](oddpool.py)):

```bash
uv run python find_pairs.py --topics fed,cpi,inflation,bitcoin
uv run python find_pairs.py                     # every active non-sports series
```

1. `GET /search/series?q=<topic>` for each topic; sports series are dropped.
2. Kalshi series are paired with Polymarket series whose titles share at least 30% of
   their words (`--series-score`), up to three per Kalshi series.
3. `GET /search/markets?series_id=` lists each paired series' open markets, and
   markets are compared only within a series pair, so a Fed market is only ever
   compared with Fed markets.
4. Each surviving pair is re-read from both venues for resolution dates, rules and
   live quotes, and dropped if they resolve more than `--days` apart.

About 15% of events belong to no series (one-off news and political markets), so this
misses them. **Without a key**, or with `--source scan`, it reads every open yes/no
market on both venues instead, which finds those too but takes about 90 s:

```bash
uv run python find_pairs.py --source scan
```

Either way a pair needs most of its words shared, exactly the same numbers, and
resolutions within 7 days. Each candidate lists the words only one side uses, both
venues' rules and a rough cost before fees. On 2026-11-08 the scan read 78,313
Kalshi and 30,305 Polymarket markets and suggested 252 pairs. The 41 with identical
wording were mostly arrest and IPO questions, priced within about 2¢ of each other.

**A cheap candidate is usually a rules mismatch, not an edge.** The cheapest that day,
Godzilla Minus Zero's Rotten Tomatoes score at $0.36 for both answers, was Kalshi's
"above 90" against Polymarket's "at least 90": a score of exactly 90 loses both legs.
Before listing a pair, read both rules for the deadline, the source, "above" against
"at least", and what happens if the data is late or the event is cancelled.

To trade a pair, copy its entry from `candidates.json` into `markets.json`:

```json
{"markets": [{"name": "Anthropic IPO in 2026", "kalshi": "KXIPO-26-ANTHROPIC",
              "polymarket": "anthropic-ipo-before-2027", "invert": false, "enabled": true,
              "rules": "both: IPO by Dec 31 2026; reviewed 2026-11-08"}]}
```

**Auto-approval, for paper benchmarking.** `--auto-approve 0.6` also writes every
candidate scoring 0.6 or more to `markets.auto.json` (git-ignored): the best match per
Kalshi market, each Polymarket market used once, all `invert: false`. The trader
trades these without review, next to `markets.json`. A reviewed entry wins over an
auto one for the same Kalshi market, and a reviewed entry with `"enabled": false`
keeps that market out of the auto list. Each trade's context records
`match.method` (`reviewed` or `auto`) and `match.score`, so settled P&L can later be
split by method and score to measure how often a word match settles the same way on
both venues. On 2026-11-09, 0.6 let 139 pairs through, including the Rotten Tomatoes
and measles strikes whose "above" against "at least" can lose both legs: that is
what the benchmark measures. Keep auto-approval to paper; the trader is paper only.

```bash
while true; do uv run python find_pairs.py --auto-approve 0.6; sleep 1800; done
```

`invert` is true when Polymarket's Yes is Kalshi's No. A running trader re-reads
`markets.json` at each discovery (every `--discover` minutes), so added, changed and
disabled pairs take effect without a restart; a file it can't parse (caught mid-save)
keeps the previous list. Positions in a removed pair stay held until they settle.
`find_pairs.py` never writes `markets.json`: running it on a loop refreshes
`candidates.json` for review, and only pairs you copy over are traded. Each discovery
also re-checks every
listing (both markets open, Polymarket's outcomes Yes/No) and skips one that fails.
The hurdle uses the later of the two resolution dates, so long-dated questions need a
larger edge. `--no-markets` scans sports only. The monitors settle these legs like the
others.

## Keys

`.env.example` lists every key, grouped by venue. Paper trading needs only
`STRATEGY_RUNS_DIR` and `CZQUANT_PORTFOLIO`, because both venues' market data is
public. The Kalshi key is for the account monitor and live trading. The Polymarket
trading credentials are listed but unused:

| Variable | What it is |
|---|---|
| `POLYMARKET_PRIVATE_KEY_PATH` | file holding the Polygon wallet key that signs orders |
| `POLYMARKET_FUNDER_ADDRESS` | the address holding your funds (your profile's proxy wallet, or the signer) |
| `POLYMARKET_SIGNATURE_TYPE` | 0 for the key's own wallet, 1 for an email/Magic proxy, 2 for a browser-wallet proxy |
| `POLYMARKET_API_KEY`, `_SECRET`, `_PASSPHRASE` | CLOB API credentials, created or derived once from the wallet |
| `POLYMARKET_CHAIN_ID` | 137 (Polygon) |
| `ODDPOOL_API_KEY` | Oddpool search key, for `find_pairs.py` only; the trader never needs it |

Live Polymarket trading would need an order-signing adapter, which this repo doesn't
have yet. Check that Polymarket permits trading from your jurisdiction before adding
one.

## Tests

```bash
uv run pytest -q
```
