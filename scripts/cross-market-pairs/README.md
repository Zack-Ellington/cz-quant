# Cross-market pair preprocessing

These tools prepare inputs for the [cross-market strategy](../../strategies/cross-market-polymarket/).
They discover and match markets without opening a cash pool, starting a strategy
run or placing orders. Run them from the repository root using the strategy's uv
environment. Write outputs to an ignored experiment folder under experiments/, alongside its
portfolio and runner configs. See the [example](../../experiments/example/).

## Sports pairs

prepare.py lists upcoming games, matches date and team identity, and exports
explicit outcome mappings. It preserves matching evidence and estimates payout
time from the game start plus --resolution-hours (default 6).

```bash
uv run --project strategies/cross-market-polymarket python scripts/cross-market-pairs/prepare.py \
  --venue polymarket-us --leagues nba,nhl --out /path/to/sports-pairs.json
```

Use --venue polymarket for international markets. leagues.json holds venue series,
tags, abbreviation aliases and rule notes. Omitting --leagues selects its enabled
leagues. Review settlement rules, including draw and cancellation treatment.

pairs.us.json and pairs.json provide venue-specific pinned mappings and blocked
Kalshi events. --pairs selects another file with that schema. Pins resolve naming
ambiguities during preprocessing; blocks exclude events from the exported list.
matching.py retains date, abbreviation, nickname and doubleheader matching.

The output contains prepared_at, pairs and rejection counts. Review a pair, then
set pair_file and its zero-based pair_index in the strategy's YAML config.
Alternatively, copy that one object into an inline pair field. Keep the candidate
file unchanged for reproducible restarts. The strategy records the resolved pair
and reads current prices; it never reloads leagues or pins or runs discovery.

## Binary questions

find_pairs.py proposes international Kalshi/Polymarket question matches by wording,
numbers and resolution dates. It supports Oddpool series search when ODDPOOL_API_KEY
is exported, or a public scan of both venues:

```bash
uv run --project strategies/cross-market-polymarket python scripts/cross-market-pairs/find_pairs.py \
  --source scan --out /path/to/question-candidates.json
```

Review wording, rules, deadlines and sources. Add selected listings to a private
file with the markets.json schema. Set invert when Polymarket Yes means Kalshi No.
Then resolve those reviewed listings into explicit outcome mappings:

```bash
uv run --project strategies/cross-market-polymarket python scripts/cross-market-pairs/prepare.py \
  --source questions --venue polymarket --listings /path/to/reviewed-questions.json \
  --out /path/to/question-pairs.json
```

An optional find_pairs.py --auto-approve SCORE writes unreviewed benchmark listings
to markets.auto.json beside the candidate output. This is a preprocessing research
utility; the strategy never loads that file. load_markets can combine explicitly
provided benchmark listings with reviewed entries; reviewed exclusions take
precedence. Question preparation uses only the selected reviewed listings file.

No account API keys or STRATEGY_RUNS_DIR are required for public preprocessing.
Oddpool requires its own key when that source is selected. Live execution
credentials belong to the separately launched strategy.
