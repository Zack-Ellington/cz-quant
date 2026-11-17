"""One Kalshi request budget shared by every process on this machine.

Kalshi limits reads per account and IP, not per process. Each strategy, monitor and
paper fill reserves its slot in one file under $STRATEGY_RUNS_DIR, so running several
strategies at once slows each of them down instead of drawing HTTP 429s.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

DEFAULT_RPS = 15.0  # below Kalshi's basic-tier 20 reads/s, leaving room for retries
_LOCK_BYTES = 1


def _path() -> Path | None:
    root = os.environ.get("STRATEGY_RUNS_DIR")
    return Path(root) / "kalshi_rate" if root else None


def _lock(f):
    if os.name == "nt":
        import msvcrt
        f.seek(0)
        while True:
            try:
                msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, _LOCK_BYTES)
                return
            except OSError:  # LK_LOCK gives up after ~10 s of contention
                continue
    import fcntl
    fcntl.flock(f.fileno(), fcntl.LOCK_EX)


def _unlock(f):
    if os.name == "nt":
        import msvcrt
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, _LOCK_BYTES)
    else:
        import fcntl
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def acquire(rps: float | None = None, penalty: float = 0.0) -> None:
    """Wait for this process's turn. `penalty` pushes every process back, e.g. after a 429."""
    rps = rps or float(os.environ.get("KALSHI_SHARED_RPS", DEFAULT_RPS))
    path = _path()
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    with open(path, "r+b") as f:
        _lock(f)
        try:
            f.seek(0)
            raw = f.read().decode().strip() or "0"
            now = time.time()
            slot = max(now, float(raw)) + penalty
            f.seek(0)
            f.write(repr(slot + 1 / rps).encode().ljust(32))
            f.truncate()
            f.flush()
        finally:
            _unlock(f)
    if slot > now:
        time.sleep(slot - now)
