"""All shared venue transports retain response diagnostics and retry semantics."""

import httpx
import pytest

from czquant.kalshi import Kalshi
from czquant.polymarket import Polymarket
from czquant.polymarket_us import PolymarketUS


@pytest.mark.parametrize("venue", ["Kalshi", "Polymarket", "Polymarket US"])
@pytest.mark.parametrize("statuses", [[403], [503, 503, 503, 503, 503], [503, 200]])
def test_venue_error_logging(venue, statuses, monkeypatch, caplog):
    monkeypatch.setattr("time.sleep", lambda _: None)
    monkeypatch.setattr("czquant.kalshi.ratelimit.acquire", lambda **kwargs: None)
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(statuses[len(requests) - 1], json={"message": "venue explanation"})

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        if venue == "Kalshi":
            client = Kalshi(authenticated=False)
            client.http.close()
            client.http = http
            call = lambda: client.get("/markets", params={"secret": "private-query"})
        elif venue == "Polymarket":
            client = Polymarket(http=http, min_interval=0)
            call = lambda: client._request("GET", client.gamma + "/markets", params={"secret": "private-query"})
        else:
            client = PolymarketUS(http=http, min_interval=0)
            call = lambda: client.public("/markets", params={"secret": "private-query"})
        if statuses[-1] == 200:
            assert call() == {"message": "venue explanation"}
            assert "HTTP error" not in caplog.text
        else:
            with pytest.raises(httpx.HTTPStatusError) as caught:
                call()
            assert caught.value.response.status_code == statuses[-1]
            assert len(caplog.records) == 1
            assert f"{venue} HTTP error: method=GET" in caplog.text
            assert "venue explanation" in caplog.text
            assert "private-query" not in caplog.text
    assert len(requests) == len(statuses)


def test_kalshi_failed_write_is_not_retried(monkeypatch, caplog):
    monkeypatch.setattr("czquant.kalshi.ratelimit.acquire", lambda **kwargs: None)
    requests = []

    def reject(request):
        requests.append(request)
        return httpx.Response(503, text="Unavailable")

    client = Kalshi(authenticated=False)
    client.http.close()
    with httpx.Client(transport=httpx.MockTransport(reject)) as http:
        client.http = http
        with pytest.raises(httpx.HTTPStatusError):
            client.create_order({"private": "request-body"})
    assert len(requests) == 1
    assert "Unavailable" in caplog.text
    assert "request-body" not in caplog.text
