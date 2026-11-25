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
It trades against **Polymarket US** (polymarket.us, the CFTC-regulated exchange) by
default, on paper or live; `--venue polymarket` paper trades the international
exchange instead. See [Live trading](#live-trading).

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

Run the monitors in their own shells, with the same `STRATEGY_RUNS_DIR`. On Polymarket
US (the default venue), `czquant monitor polymarket-us` settles the Polymarket legs: it
needs no key for paper portfolios. On the international venue it's `monitor polymarket`:

```bash
uv run czquant monitor market
uv run czquant monitor polymarket-us     # or: monitor polymarket, for --venue polymarket
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
public. Live trading needs both venues' keys:

| Variable | What it is |
|---|---|
| `KALSHI_API_KEY_ID`, `KALSHI_PRIVATE_KEY_PATH` | Kalshi production key and its `.pem` file |
| `POLYMARKET_US_KEY_ID` | Polymarket US Key ID (a UUID), from polymarket.us/developer |
| `POLYMARKET_US_SECRET_KEY_PATH` | file holding the base64 Secret Key, outside the repo |
| `ODDPOOL_API_KEY` | Oddpool search key, for `find_pairs.py` only; the trader never needs it |

Polymarket US signs each request with Ed25519 (`X-PM-Access-Key`, `X-PM-Timestamp`,
`X-PM-Signature` over timestamp + method + path). It has no passphrase, API secret,
wallet or signature type: those belong to the international exchange, which blocks
US users.

## Polymarket US

A US game moneyline is **one** market (`aec-nba-bos-sac-2026-11-21`): its long side
(YES) is one team and its short side (NO) the other. Every price on the venue is the
YES price, as on Kalshi, so a team on the short side is bought with an ask at 1 − its
price (`ORDER_INTENT_BUY_SHORT`). Markets are tickers `PMUS:<slug>`. The trader reads
game events starting within eight days and takes each event's PRIMARY market group as
its moneyline. Team abbreviations that differ from Kalshi's go in `leagues.json` under
`polymarket_us_abbr_aliases` (they differ from the international ones: `NYK→ny`,
`GSW→gs`, `VGK→veg`, …), and pins and blocks in `pairs.us.json`. On 2026-11-21, 71 of 86
open NBA and NHL Kalshi games paired; the rest weren't listed yet.

**Fees.** Takers pay θ × C × p × (1 − p) per fill, rounded to the cent half-to-even.
θ is each market's `feeCoefficient` (0.095, the lowest volume tier), nearly twice the
international 0.05. `--pmus-theta` overrides it for pricing and paper fills, for
example `--pmus-theta 0.0695` at the top tier. The API promotion (θ 0.0695 and a 70%
rebate through 2026-12-31) charges the full fee on the fill and pays the rebate later.
Price with the rate actually charged on the fill, not the rebated one.

## Live trading

Live trading uses one **live portfolio mapped to two accounts**: a Kalshi subaccount
and the whole Polymarket US account (it has no subaccounts). The ledger keeps each
venue's cash apart (`State.venue_cash`), and a basket trades only if each venue's own
free cash pays its legs.

1. Put both venues' keys in `.env` and check them (both are reads only):

   ```bash
   uv run czquant setup                  # Kalshi
   uv run czquant check-polymarket-us    # Polymarket US
   ```

2. With both accounts flat (no positions, no open orders), map them:

   ```bash
   uv run czquant map-live-cross cross-market-polymarket-live --subaccount 1
   ```

   It opens the portfolio at each account's current cash. From then on, nothing else
   should trade either account: the monitors flag anything they can't attribute.

   An account without subaccount access trades from the primary account instead:
   `--subaccount 0 --primary-account`. Running the command again on an existing live
   portfolio with nothing open on Kalshi moves its Kalshi side to the named account.

   One Kalshi account can back several live portfolios. Add `--share-account` to map
   beside the portfolios already there. The new portfolio opens with the cash none of
   them records, and `czquant allocate --from A --to B --dollars N` moves recorded cash
   between them. The account monitor reconciles the account against their combined
   ledger. A cash difference on a shared account halts every portfolio on it. A
   position difference halts only the portfolios holding that market.

   `czquant retire-live <id>` frees an account a finished portfolio held. It books
   that portfolio's settled Kalshi lots from Kalshi's records and refuses if any are
   still open. It also refuses on an unexplained cash difference unless you pass
   `--book-difference`, which books the difference as an external adjustment, never
   as profit. Then it releases the cash to the account; with `--to <id>`, it hands the
   cash to a portfolio sharing the account.

3. Run both account monitors in their own shells:

   ```bash
   uv run czquant monitor account          # Kalshi fills, settlements, cash
   uv run czquant monitor polymarket-us    # Polymarket US orders, deposits, settlements, cash
   ```

4. Dry run first: every order is built and signed, and none is sent:

   ```bash
   export CZQUANT_PORTFOLIO=cross-market-polymarket-live
   uv run python trader.py --live --dry-run --leagues nba --duration 1:00
   ```

5. Then live, with small caps:

   ```bash
   uv run python trader.py --live --leagues nba --max-basket-dollars 25 --max-total-dollars 100
   ```

Live defaults are $25 per basket, $100 at risk and 20 sets per basket, unless set.
Orders are taker IOC. Kalshi fills reach the ledger through the account monitor, so
open orders' reservations count against the total cap until then. Polymarket US fills
are booked from the order's synchronous execution reports.

**It halts** (and stays halted across restarts until `czquant clear-halt`) when:
- an order's outcome is unknown: the venue didn't confirm a final state. Polymarket US
  has no client order ID, so a lost reply can't be looked up by ID: check the account
  before clearing;
- either monitor reports a discrepancy for the portfolio: cash, inventory, an
  unbooked rebate or an order it can't find;
- a fully filled basket settles at a loss, or the `KILL` file exists.

**It pauses** (no new trades, no halt) while a monitor hasn't reported in
`monitor_max_age` (5 minutes). Dry runs skip that check.

**Not yet verified against the live venue:** the docs don't show a short-side order
or its execution report, so the first live trades should be watched. Neither do they
show the promotion rebate's activity record (the monitor reports a rebate it can't read
as a discrepancy, which halts the trader), nor whether a fee shows in the execution
report as `commissionNotionalCollected`. Non-sports pairs (`markets.json`) name
international markets and are skipped on Polymarket US.

## Tests

```bash
uv run pytest -q
```
