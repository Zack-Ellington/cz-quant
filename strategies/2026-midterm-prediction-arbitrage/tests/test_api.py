"""The API client's retries: 5xx and transport errors back off; a 429 is waited
out (``Retry-After`` when sent) without using up a retry."""

from __future__ import annotations

import httpx
import pytest

from strategy import api
from strategy.api import MAX_RATE_LIMIT_WAIT, RATE_LIMIT_WAITS, KalshiClient


def _client(responses, monkeypatch):
    """A client whose requests get ``responses`` in turn; returns (client, sleeps)."""
    queue = list(responses)
    sleeps = []
    monkeypatch.setattr(api.time, "sleep", sleeps.append)

    def handler(request):
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return KalshiClient(base_url="https://example.test", transport=httpx.MockTransport(handler)), sleeps


def test_rate_limits_wait_for_retry_after(monkeypatch):
    limited = httpx.Response(429, headers={"Retry-After": "2"})
    client, sleeps = _client([limited, limited, httpx.Response(200, json={"ok": True})], monkeypatch)
    assert client._get("/x", None) == {"ok": True}
    assert sleeps == [2.0, 2.0]


def test_rate_limits_without_retry_after_back_off(monkeypatch):
    limited = httpx.Response(429)
    client, sleeps = _client([limited, limited, httpx.Response(200, json={})], monkeypatch)
    client._get("/x", None)
    assert sleeps == [1.0, 2.0]  # 0.5 x 2^n


def test_a_long_retry_after_is_capped(monkeypatch):
    limited = httpx.Response(429, headers={"Retry-After": "3600"})
    client, sleeps = _client([limited, httpx.Response(200, json={})], monkeypatch)
    client._get("/x", None)
    assert sleeps == [MAX_RATE_LIMIT_WAIT]


def test_rate_limits_do_not_use_up_retries_but_do_run_out(monkeypatch):
    client, sleeps = _client([httpx.Response(429, headers={"Retry-After": "1"})], monkeypatch)
    with pytest.raises(httpx.HTTPStatusError):
        client._get("/x", None)
    assert len(sleeps) == RATE_LIMIT_WAITS


def test_server_errors_still_retry_with_backoff(monkeypatch):
    client, sleeps = _client([httpx.Response(503)], monkeypatch)
    with pytest.raises(httpx.HTTPStatusError):
        client._get("/x", None)
    assert sleeps == [0.5, 1.0, 2.0]


def test_not_found_is_none(monkeypatch):
    client, _ = _client([httpx.Response(404)], monkeypatch)
    assert client._get("/x", None) is None
