# DraftKings odds research

This project collects read-only moneyline odds for research. It has its own
environment and does not import czquant or any strategy. It provides no broker,
execution simulator, portfolio, or settlement service.

api.py contains two source clients. OddsApi reads The Odds API's bookmaker odds
and game scores. Nash parses the sportscontent feed used by the existing
DraftKings reader. Its response format is undocumented; a refusal is reported
without retrying or attempting to bypass it. Tests use recorded response shapes
and mock HTTP, not live account access.

## Collect a snapshot

Run from research/draftkings. Export STRATEGY_RUNS_DIR to the same persistent
logging root used elsewhere in the repository. Export ODDS_API_KEY for the
oddsapi source. Environment files are not loaded automatically.

```bash
uv sync --locked
uv run python collect.py --source oddsapi --league basketball_nba
```

The command makes one odds request and prints the saved snapshot path. Reads
may consume the data provider's credits. There is no background polling or
automatic refresh. Snapshots live under STRATEGY_RUNS_DIR/research/draftkings
and contain the raw payload, normalized selections, observation time, source,
league, and reported credit balance. They do not write accounting.sqlite3.

The nash source accepts its own league ID. DRAFTKINGS_SITE selects its site
identifier, defaulting to the client's dkusnj value:

```bash
uv run python collect.py --source nash --league 42648
```

## Use the reader

The source clients expose fetch and parse separately, so an analysis can replay
saved raw responses without making another request. Each selection retains
decimal odds, home/away/draw identity, source update time, collection time,
and source evidence. Reciprocal odds include the bookmaker's margin; they
are not calibrated probabilities or executable prediction-market quotes.

```python
import json
from pathlib import Path

from api import parse_oddsapi

snapshot = json.loads(Path("/path/to/snapshot.json").read_text())
selections = parse_oddsapi(snapshot["raw"], snapshot["league"],
                          fetched_at=snapshot["observed_at"])
for selection in selections:
    print(selection.name, selection.odds, selection.implied_probability)
```

OddsApi.results reads reported game scores for supplied event IDs. Those
results are research observations; this project does not infer bet payouts
or apply venue-specific void, tie, or settlement rules.

## Validate

```bash
uv run pytest -q
uv run basedpyright
```

Tests cover source normalization, refusals, credential redaction, and snapshot
storage. The trading package and strategy have no dependency on this project.
