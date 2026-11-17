"""The Kalshi budget is shared: callers in different processes take turns from one file."""
import subprocess
import sys
import threading
import time

from czquant import ratelimit

CHILD = "import sys, time; from czquant import ratelimit\nfor _ in range(10): ratelimit.acquire(rps=40)\nprint(time.time())"


def test_processes_share_one_request_rate(tmp_path, monkeypatch):
    monkeypatch.setenv("STRATEGY_RUNS_DIR", str(tmp_path))
    start = time.time()
    children = [subprocess.Popen([sys.executable, "-c", CHILD], stdout=subprocess.PIPE, text=True) for _ in range(3)]
    ends = [float(c.communicate()[0]) for c in children]
    # 30 requests at 40 per second need at least 0.725 s between the first and the last, whichever process sends them.
    assert max(ends) - start >= 0.7


def test_a_429_pushes_every_caller_back(tmp_path, monkeypatch):
    monkeypatch.setenv("STRATEGY_RUNS_DIR", str(tmp_path))
    start = time.time()
    hit = threading.Thread(target=ratelimit.acquire, kwargs={"rps": 1000, "penalty": 0.3})
    hit.start()
    time.sleep(0.05)  # the other caller arrives while the first is backing off
    ratelimit.acquire(rps=1000)
    hit.join()
    assert time.time() - start >= 0.29


def test_without_a_runs_directory_nothing_waits(monkeypatch):
    monkeypatch.delenv("STRATEGY_RUNS_DIR", raising=False)
    start = time.time()
    for _ in range(100):
        ratelimit.acquire(rps=1)
    assert time.time() - start < 0.1
