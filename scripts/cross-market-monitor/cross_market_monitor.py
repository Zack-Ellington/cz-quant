"""Select active experiments from recorded run metadata."""
from typing import Any


def selections(events: list[dict[str, Any]], pools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Latest recorded configuration per experiment, deduplicated across its brokers."""
    selected = {}
    for event in sorted(events, key=lambda e: e["sequence"]):
        data = event["data"]
        config = data.get("config", {})
        experiment = config.get("experiment", {})
        if (data.get("strategy") == "cross-market-polymarket"
                and experiment.get("pair", {}).get("venue") == "polymarket_us"):
            selected[experiment["id"]] = config
    active = {p["id"] for p in pools if not p["metadata"].get("retired_at")}
    return [cfg for cfg in selected.values() if set(cfg["experiment"]["portfolio"].values()) <= active]
