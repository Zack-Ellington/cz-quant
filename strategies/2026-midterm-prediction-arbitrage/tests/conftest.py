"""Shared test fixtures.

Adds ``src/`` to the import path so the tests run under a plain ``pytest`` as
well as under ``uv run pytest`` (where the package is already installed).

Two kinds of data:

* ``tests/fixtures/aggregate.json`` -- a small synthetic snapshot of the
  aggregate markets (combo, control, same-party, seat buckets) with round,
  readable prices, for unit tests.
* ``snapshots/`` -- the committed snapshot of every real quote a run reads, for
  the reproducibility test.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))

from strategy.snapshot import SnapshotClient  # noqa: E402  (after sys.path tweak)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SNAPSHOTS = _ROOT / "snapshots"


def committed_snapshot() -> Path:
    """The most recent committed snapshot (files are named by capture date)."""
    snapshots = sorted(SNAPSHOTS.glob("*.json"))
    if not snapshots:
        raise FileNotFoundError(f"no snapshot in {SNAPSHOTS}")
    return snapshots[-1]


@pytest.fixture
def aggregate_client() -> SnapshotClient:
    return SnapshotClient(FIXTURES / "aggregate.json")
