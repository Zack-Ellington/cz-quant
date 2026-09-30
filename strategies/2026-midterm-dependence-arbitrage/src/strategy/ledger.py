"""Paper ledger: one directory per run, and an append-only event log.

Every ``uv run strategy`` creates ``runs/<strategy-id>/<YYYYMMDDTHHMMSSZ>/`` under
the repository root (UTC; a suffix is added if two runs start in the same
second). It holds:

* ``run.json`` -- strategy id, start and end time, git commit and dirty flag,
  command line, API base URL, mode, input and output snapshot paths, the paper
  policy, and how the run ended.
* ``ledger.jsonl`` -- one JSON object per line, appended and fsync'd as each
  operation happens, so a killed run keeps every record it wrote.
* ``run.log`` -- everything the run printed.

Each event has ``ts``, ``run_id``, ``strategy``, ``type``, and ``position_id``.
Types: ``scan`` (model and market prices), ``signal`` (a basket or trade the
strategy wants, accepted or rejected with a reason), ``fill`` (one leg: ticker,
side, price, quantity, fee, cash delta; ``simulated`` in paper mode),
``settlement`` (position, receipt, date of cash receipt), ``valuation`` (the
simulated P&L of the open book under one view), and ``note``.

A run can carry tags that are written into every one of its events: a run that
is one pass of a ``--duration`` session has ``session`` (the session id), and
its ``run.json`` records the session, the pass number, and the deadline.

Money is written as decimal strings and read back as ``Decimal``, so the ledger
never rounds. ``runs/`` is gitignored. The directory is found by walking up
from the strategy to the repository root; ``STRATEGY_RUNS_DIR`` or
``--runs-dir`` override it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable

EVENT_TYPES = ("scan", "signal", "fill", "settlement", "valuation", "note")
PROJECT_DIR = Path(__file__).resolve().parents[2]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_time(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)


def repo_root(start: Path = PROJECT_DIR) -> Path:
    """The nearest directory at or above ``start`` holding ``.git``."""
    for candidate in (start, *start.parents):
        if (candidate / ".git").exists():
            return candidate
    return start


def default_runs_dir() -> Path:
    override = os.environ.get("STRATEGY_RUNS_DIR")
    return Path(override) if override else repo_root() / "runs"


def git_state(cwd: Path = PROJECT_DIR) -> dict:
    """Commit and dirty flag of the checkout, or nulls outside a git tree."""
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=cwd, capture_output=True, text=True, check=True
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"], cwd=cwd, capture_output=True, text=True, check=True
        ).stdout.strip()
        return {"commit": commit, "dirty": bool(dirty)}
    except (OSError, subprocess.CalledProcessError):
        return {"commit": None, "dirty": None}


def _encode(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"cannot serialize {type(value).__name__}")


@dataclass
class Run:
    """An open run directory. Use ``Run.start`` to create one."""

    strategy: str
    run_id: str
    directory: Path
    clock: Callable[[], datetime] = utc_now
    meta: dict = field(default_factory=dict)
    tags: dict = field(default_factory=dict)  # written into every event, e.g. {"session": id}

    @property
    def ledger_path(self) -> Path:
        return self.directory / "ledger.jsonl"

    @property
    def log_path(self) -> Path:
        return self.directory / "run.log"

    @property
    def label(self) -> str:
        """``runs/<strategy>/<run id>``: how reports name the run directory."""
        return f"{self.directory.parent.parent.name}/{self.strategy}/{self.run_id}"

    @classmethod
    def start(
        cls,
        runs_dir: Path,
        strategy: str,
        meta: dict,
        clock: Callable[[], datetime] = utc_now,
        tags: dict | None = None,
    ) -> "Run":
        started = clock()
        base = started.strftime("%Y%m%dT%H%M%SZ")
        parent = Path(runs_dir) / strategy
        parent.mkdir(parents=True, exist_ok=True)
        run_id, n = base, 1
        while True:
            try:
                (parent / run_id).mkdir()
                break
            except FileExistsError:
                n += 1
                run_id = f"{base}-{n}"
        run = cls(strategy, run_id, parent / run_id, clock, tags=dict(tags or {}))
        run.meta = {
            "strategy": strategy,
            "run_id": run_id,
            "started_at": iso(started),
            "ended_at": None,
            "status": "running",
            "git": git_state(),
            "command": sys.argv,
            **meta,
        }
        run._write_meta()
        run.ledger_path.touch()
        return run

    def event(self, type: str, position_id: str | None = None, **fields) -> dict:
        """Append one event and flush it to disk before returning."""
        if type not in EVENT_TYPES:
            raise ValueError(f"unknown event type {type!r}")
        record = {
            "ts": iso(self.clock()),
            "run_id": self.run_id,
            "strategy": self.strategy,
            "type": type,
            "position_id": position_id,
            **self.tags,
            **fields,
        }
        line = json.dumps(record, default=_encode, sort_keys=True)
        with self.ledger_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return json.loads(line)

    def finish(self, status: str = "ok", **extra) -> None:
        self.meta.update({"ended_at": iso(self.clock()), "status": status, **extra})
        self._write_meta()

    def _write_meta(self) -> None:
        text = json.dumps(self.meta, default=_encode, indent=2, sort_keys=True)
        (self.directory / "run.json").write_text(text + "\n", encoding="utf-8", newline="\n")


class Tee:
    """Write to a stream and to ``run.log`` at once; to ``run.log`` only if the stream is None."""

    def __init__(self, stream, path: Path) -> None:
        self._stream = stream
        self._file = path.open("a", encoding="utf-8", newline="\n")

    def write(self, text: str) -> int:
        self._file.write(text)
        self._file.flush()
        return len(text) if self._stream is None else self._stream.write(text)

    def flush(self) -> None:
        self._file.flush()
        if self._stream is not None:
            self._stream.flush()

    def close(self) -> None:
        self._file.close()


def read_runs(runs_dir: Path, strategy: str) -> list[dict]:
    """``run.json`` of every run of ``strategy``, oldest first."""
    parent = Path(runs_dir) / strategy
    if not parent.is_dir():
        return []
    metas = []
    for path in sorted(parent.glob("*/run.json")):
        metas.append(json.loads(path.read_text(encoding="utf-8")))
    return metas


def read_events(runs_dir: Path, strategy: str) -> list[dict]:
    """Every event of every run of ``strategy``, in the order it was written."""
    parent = Path(runs_dir) / strategy
    if not parent.is_dir():
        return []
    events = []
    for path in sorted(parent.glob("*/ledger.jsonl")):
        with path.open(encoding="utf-8") as handle:
            events.extend(json.loads(line) for line in handle if line.strip())
    events.sort(key=lambda e: (e["ts"], e["run_id"]))
    return events
