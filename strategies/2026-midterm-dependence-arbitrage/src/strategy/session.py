"""Paper sessions: run the strategy back to back for a fixed time (``--duration``).

``uv run strategy --duration 24:00`` repeats the whole pipeline -- scan, checks,
model, paper trade -- as often as it can until the deadline. Each pass starts as
soon as the previous one ends; the only waits are the confirmation scan's delay
and Kalshi's rate limit (see ``api.py``). A pass that starts before the deadline
runs to completion, so the last one can end a little after it.

Each pass is an ordinary run with its own run directory, so the ledger treats a
session like any sequence of runs: positions are settled by later passes, one
open position per basket holds across passes, and cash carries over. What marks
a pass as part of a session:

* every event it writes has ``session`` (the session id, its start time);
* its ``run.json`` has ``session``: the id, the pass number, the duration, and
  the deadline.

``uv run strategy ledger --session <id>`` reports one session's positions.

While a session runs, each pass prints one line; its full report goes to its
``run.log``. A failed pass is recorded like any failed run and retried after a
backoff (15 s, doubling to 5 min); after ``MAX_FAILURES`` failures in a row the
session stops. Ctrl-C stops the session cleanly: the pass in progress is marked
failed, and the summary is still printed.
"""

from __future__ import annotations

import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Callable, TextIO

from strategy.ledger import iso, read_events, utc_now
from strategy.output import usd
from strategy.pnl import free_cash, positions_from_events

MAX_FAILURES = 10  # passes failed in a row before the session stops
FIRST_BACKOFF = 15.0  # seconds before retrying a failed pass; doubles each time
MAX_BACKOFF = 300.0

_DURATION = re.compile(r"^(\d+):([0-5]\d)$")


def parse_duration(text: str) -> timedelta:
    """``"H:MM"`` -> timedelta: ``"24:00"`` is a day, ``"1:30"`` an hour and a half."""
    match = _DURATION.match(text.strip())
    if match is None:
        raise ValueError(f"duration must be H:MM, e.g. 24:00 or 1:30; got {text!r}")
    duration = timedelta(hours=int(match[1]), minutes=int(match[2]))
    if duration <= timedelta(0):
        raise ValueError("duration must be longer than 0:00")
    return duration


def format_duration(duration: timedelta) -> str:
    minutes = max(int(duration.total_seconds() // 60), 0)
    return f"{minutes // 60}:{minutes % 60:02d}"


@dataclass
class Session:
    session_id: str
    started_at: datetime
    deadline: datetime
    passes: list[tuple[int, str, str]] = field(default_factory=list)  # (pass, run id, status)
    stopped: str = ""  # why the session ended

    @property
    def failed(self) -> int:
        return sum(1 for _, _, status in self.passes if status != "ok")


def run_session(
    run_once: Callable[[dict], Path],
    duration: timedelta,
    runs_dir: Path,
    strategy: str,
    bankroll: Decimal,
    clock: Callable[[], datetime] = utc_now,
    sleep: Callable[[float], None] = time.sleep,
    out: TextIO | None = None,
    max_failures: int = MAX_FAILURES,
) -> Session:
    """Call ``run_once(info)`` back to back until ``duration`` has passed.

    ``info`` is the session record for the pass's ``run.json`` (``id``,
    ``pass``, ``duration``, ``ends_at``); ``run_once`` returns the run directory.
    """
    out = out or sys.stdout
    started = clock()
    deadline = started + duration
    session = Session(started.strftime("%Y%m%dT%H%M%SZ"), started, deadline)
    print(f"Paper session {session.session_id}: running back to back for "
          f"{format_duration(duration)}, until {iso(deadline)} (paper mode: nothing is sent)",
          file=out, flush=True)
    failures, n = 0, 0
    try:
        while clock() < deadline:
            n += 1
            info = {"id": session.session_id, "pass": n, "duration": format_duration(duration),
                    "ends_at": iso(deadline)}
            try:
                directory = run_once(info)
            except KeyboardInterrupt:
                session.passes.append((n, "", "interrupted"))
                raise
            except Exception as exc:  # recorded in the pass's run.json; retry after a backoff
                failures += 1
                session.passes.append((n, "", "failed"))
                print(f"  pass {n} failed: {type(exc).__name__}: {exc}", file=out, flush=True)
                if failures >= max_failures:
                    session.stopped = f"stopped after {failures} failed passes in a row"
                    return session
                left = (deadline - clock()).total_seconds()
                wait = min(FIRST_BACKOFF * 2 ** (failures - 1), MAX_BACKOFF, max(left, 0.0))
                if wait > 0:
                    print(f"  retrying in {wait:.0f}s", file=out, flush=True)
                    sleep(wait)
                continue
            failures = 0
            run_id = Path(directory).name
            session.passes.append((n, run_id, "ok"))
            print(pass_line(n, runs_dir, strategy, run_id, bankroll, deadline - clock()),
                  file=out, flush=True)
        session.stopped = "deadline reached"
    except KeyboardInterrupt:
        session.stopped = f"interrupted during pass {n}"
    return session


def pass_line(n: int, runs_dir: Path, strategy: str, run_id: str, bankroll: Decimal,
              left: timedelta) -> str:
    """One line for a finished pass, read back from the ledger."""
    events = read_events(runs_dir, strategy)
    mine = [e for e in events if e["run_id"] == run_id]
    signals = [e for e in mine if e["type"] == "signal"]
    opened = sum(1 for e in signals if e["accepted"])
    settled = sum(1 for e in mine if e["type"] == "settlement")
    open_ = sum(1 for p in positions_from_events(events).values() if p.is_open)
    return (
        f"  pass {n} {run_id}: {opened} opened, {settled} settled, {len(signals) - opened} rejected; "
        f"{open_} open, free cash {usd(free_cash(events, bankroll))}; {format_duration(left)} left"
    )
