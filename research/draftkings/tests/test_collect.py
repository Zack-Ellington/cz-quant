"""Research collection saves evidence without creating an execution ledger."""
import json
import logging

import httpx
import pytest

from api import OddsApi, OutOfCredits
from collect import main
from tests.test_api import oddsapi_event


def test_snapshot_keeps_raw_observation_and_redacts_credentials(tmp_path, monkeypatch, capsys, caplog):
    monkeypatch.setenv("STRATEGY_RUNS_DIR", str(tmp_path))
    payload = [oddsapi_event()]
    calls = []

    def handler(request):
        assert request.method == "GET" and request.url.path.endswith("/odds")
        calls.append(request)
        return httpx.Response(200, json=payload, headers={"x-requests-remaining": "49"})

    source = OddsApi("research-secret", httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr("collect.OddsApi", lambda: source)
    with caplog.at_level(logging.INFO, logger="httpx"):
        main(["--league", "basketball_nba"])
    paths = list((tmp_path / "research" / "draftkings").glob("*.json"))
    assert len(paths) == len(calls) == 1
    snapshot = json.loads(paths[0].read_text())
    assert snapshot["raw"] == payload and snapshot["remaining_credits"] == 49
    assert len(snapshot["selections"]) == 2
    assert snapshot["selections"][0]["fetched_at"] == snapshot["observed_at"]
    assert snapshot["selections"][0]["odds"] == "1.25"
    assert str(paths[0]) in capsys.readouterr().out
    assert "research-secret" not in caplog.text + paths[0].read_text()
    assert "REDACTED" in caplog.text
    assert not list(tmp_path.rglob("*.sqlite3"))


def test_a_failed_read_does_not_write_a_snapshot_or_print_a_key(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("STRATEGY_RUNS_DIR", str(tmp_path))
    source = OddsApi("research-secret", httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(500))))
    monkeypatch.setattr("collect.OddsApi", lambda: source)
    with pytest.raises(SystemExit) as error:
        main(["--league", "basketball_nba"])
    assert error.value.code == 1
    assert not list(tmp_path.rglob("*.json"))
    output = capsys.readouterr()
    assert "HTTPStatusError" in output.err and "research-secret" not in output.err


def test_missing_storage_root_stops_before_request(monkeypatch):
    monkeypatch.delenv("STRATEGY_RUNS_DIR", raising=False)
    monkeypatch.setattr("collect.OddsApi", lambda: pytest.fail("must not create a client"))
    with pytest.raises(SystemExit) as error:
        main(["--league", "basketball_nba"])
    assert error.value.code == 2


def test_quota_exhaustion_keeps_the_reported_credit_balance():
    source = OddsApi("fixture", httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(429, headers={"x-requests-remaining": "0"}))))
    with pytest.raises(OutOfCredits):
        source.fetch("basketball_nba")
    assert source.remaining == 0
