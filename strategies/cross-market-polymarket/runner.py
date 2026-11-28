"""Run one configured cross-market experiment on existing portfolio allocations."""
from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

from czquant import Store
from czquant.config import load_config
from experiments import Experiment, Outcome, SelectedPair
from trader import Config, Engine

log = logging.getLogger("cross_market_polymarket")


def load_experiment(path: str) -> Config:
    data = load_config(path)
    once = data.pop('once', False)
    if 'pair_file' in data:
        if 'pair' in data:
            raise ValueError('select pair or pair_file, not both')
        pair_path = Path(path).resolve().parent / data.pop('pair_file')
        candidates = load_config(pair_path)
        index = data.pop('pair_index')
        pairs = candidates.get('pairs')
        if not isinstance(index, int) or isinstance(index, bool) or not isinstance(pairs, list) or not 0 <= index < len(pairs):
            raise ValueError('pair_index must select an existing entry in the candidate pairs array')
        data['pair'] = pairs[index]
    selected = dict(data.pop("pair"))
    selected["outcomes"] = tuple(Outcome(**o) for o in selected["outcomes"])
    parameters = data.pop("parameters", {})
    portfolio_id = data.get('portfolio') if isinstance(data.get('portfolio'), str) else None
    if portfolio_id is not None:
        if not os.environ.get('STRATEGY_RUNS_DIR'):
            raise ValueError('export STRATEGY_RUNS_DIR before selecting a portfolio')
        store = Store()
        try:
            group = next((p for p in store.reporting_portfolios() if p['id'] == portfolio_id), None)
            if group is None or group.get('retired_at'):
                raise ValueError(f'unknown or retired portfolio {portfolio_id}; select an active portfolio or use czquant portfolios create')
            pools = {p['id']: p for p in store.pools()}
            members = [pools[pid] for pid in group['pools']]
            if len(members) != 2 or {p['venue'] for p in members} != {'kalshi', selected['venue']}:
                raise ValueError('cross-market portfolio requires exactly one pool for each selected venue')
            if any(p['mode'] != data['mode'] or p['metadata'].get('retired_at') for p in members):
                raise ValueError('portfolio pools must be active and match the configured mode')
            data['portfolio'] = {p['venue']: p['id'] for p in members}
        finally:
            store.close()
    experiment = Experiment(pair=SelectedPair(**selected), **data)
    return Config(experiment=experiment, once=once, portfolio_id=portfolio_id, **parameters)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--config', required=True, metavar='FILE.yaml',
                   help='portfolio ID, prepared pair, mode and run parameters (YAML or JSON)')
    a = p.parse_args(argv)
    if not os.environ.get("STRATEGY_RUNS_DIR"):
        p.error("export STRATEGY_RUNS_DIR to the persistent shared logging directory before starting")
    try:
        cfg = load_experiment(a.config)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        p.error(f"invalid experiment: {exc}")
    engine = Engine(cfg)
    run_dir = os.path.join(engine.diagnostics, engine.run_id)
    os.makedirs(run_dir, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(os.path.join(run_dir, "run.log"))])
    if cfg.experiment.mode == "live":
        log.warning("LIVE%s: portfolio %s, caps $%s per basket, $%s total, %d sets; Kalshi $%s and Polymarket US $%s free",
                    " DRY RUN (nothing sent)" if cfg.dry_run else "", list(b.pool_id for b in engine.brokers.values()), cfg.max_basket_dollars,
                    cfg.max_total_dollars, cfg.max_sets, engine.kalshi_broker.available,
                    engine.poly_broker.available)
    try:
        engine.run(once=cfg.once)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
