"""Minimal Kalshi Trade API v2 client (stdlib only).

Market-data endpoints are public, so no key is required. If KALSHI_API_KEY_ID and
KALSHI_PRIVATE_KEY_PATH are set, requests are signed (RSA-PSS / SHA-256), which
gets you your account's rate-limit tier. Signing needs the `cryptography` package.
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
ORDERBOOK_BATCH = 100  # max tickers per GET /markets/orderbooks call


class KalshiClient:
    def __init__(self, base_url: str = BASE_URL, max_rps: float = 10.0, retries: int = 5):
        self.base_url = base_url.rstrip("/")
        self.min_interval = 1.0 / max_rps
        self.retries = retries
        self._last = 0.0
        self._key_id = os.environ.get("KALSHI_API_KEY_ID")
        self._private_key = None
        key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH")
        if self._key_id and key_path:
            from cryptography.hazmat.primitives import serialization

            with open(key_path, "rb") as f:
                self._private_key = serialization.load_pem_private_key(f.read(), password=None)

    # -- transport ---------------------------------------------------------

    def _auth_headers(self, method: str, path: str) -> dict:
        if not self._private_key:
            return {}
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        ts = str(int(time.time() * 1000))
        sign_path = urllib.parse.urlparse(self.base_url + path).path  # no query string
        sig = self._private_key.sign(
            (ts + method + sign_path).encode(),
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self._key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
        }

    def get(self, path: str, params=None) -> dict:
        query = urllib.parse.urlencode(params or [], doseq=True)
        url = self.base_url + path + ("?" + query if query else "")
        for attempt in range(self.retries + 1):
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            headers = {"Accept": "application/json", **self._auth_headers("GET", path)}
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as r:
                    return json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503, 504) and attempt < self.retries:
                    time.sleep(min(2**attempt * 0.5, 10))
                    continue
                body = e.read().decode("utf-8", "replace")[:300]
                raise RuntimeError(f"GET {path} -> HTTP {e.code}: {body}") from e
            except urllib.error.URLError:
                if attempt < self.retries:
                    time.sleep(min(2**attempt * 0.5, 10))
                    continue
                raise
        raise RuntimeError("unreachable")

    # -- endpoints ---------------------------------------------------------

    def iter_open_events(self, series_ticker: str | None = None):
        """Yields every open event with its markets nested."""
        cursor = None
        while True:
            params = {"status": "open", "with_nested_markets": "true", "limit": 200}
            if series_ticker:
                params["series_ticker"] = series_ticker
            if cursor:
                params["cursor"] = cursor
            data = self.get("/events", params)
            yield from data.get("events") or []
            cursor = data.get("cursor")
            if not cursor:
                return

    def all_series(self) -> dict:
        """series_ticker -> series dict (carries fee_type / fee_multiplier)."""
        return {s["ticker"]: s for s in self.get("/series").get("series") or []}

    def orderbooks(self, tickers: list[str]) -> dict:
        """ticker -> raw orderbook dict, fetched in batches."""
        out = {}
        for i in range(0, len(tickers), ORDERBOOK_BATCH):
            chunk = tickers[i : i + ORDERBOOK_BATCH]
            data = self.get("/markets/orderbooks", [("tickers", t) for t in chunk])
            for ob in data.get("orderbooks") or []:
                out[ob["ticker"]] = ob.get("orderbook_fp") or ob.get("orderbook") or {}
        return out
