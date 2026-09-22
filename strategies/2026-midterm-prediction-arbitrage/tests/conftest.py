"""Shared test fixtures.

Adds ``src/`` to the import path so the tests run under a plain ``pytest`` as
well as under ``uv run pytest`` (where the package is already installed), and
exposes the saved-snapshot directory and a client that reads from it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))

from strategy.api import FixtureClient  # noqa: E402  (after sys.path tweak)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture
def fixture_client() -> FixtureClient:
    return FixtureClient(FIXTURES)
