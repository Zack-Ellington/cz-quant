"""Authenticated transport. Configuration is exported by the caller."""

from __future__ import annotations

import base64
import os
import time
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlparse

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding


class Kalshi:
    def __init__(self, authenticated: bool = True, base_url: str | None = None):
        self.base = (base_url or os.environ.get("KALSHI_API_BASE_URL", "https://external-api.kalshi.com/trade-api/v2")).rstrip("/")
        self.key_id = os.environ.get("KALSHI_API_KEY_ID") if authenticated else None
        self.key: Any = None
        if self.key_id:
            self.key = serialization.load_pem_private_key(
                Path(os.environ["KALSHI_PRIVATE_KEY_PATH"]).expanduser().read_bytes(), password=None)
        self.http = httpx.Client(timeout=30)

    def request(self, method: str, path: str, data: dict[str, Any] | None = None,
                params: Any = None) -> dict[str, Any]:
        url = self.base + path
        # Only reads are automatically retried. Uncertain writes are reconciled.
        for attempt in range(5 if method == "GET" else 1):
            headers = {}
            if self.key:
                ts = str(int(time.time() * 1000))
                message = (ts + method + urlparse(url).path).encode()
                signature = self.key.sign(message) if isinstance(self.key, ed25519.Ed25519PrivateKey) else self.key.sign(
                    message, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
                headers = {"KALSHI-ACCESS-KEY": self.key_id, "KALSHI-ACCESS-TIMESTAMP": ts,
                           "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode()}
            response = self.http.request(method, url, headers=headers, json=data, params=params)
            if method == "GET" and response.status_code in (429, 500, 502, 503, 504) and attempt < 4:
                time.sleep(0.5 * 2**attempt)
                continue
            response.raise_for_status()
            return response.json() if response.content else {}
        raise RuntimeError("read retry exhausted")

    def get(self, path: str, params: Any = None) -> dict[str, Any]:
        return self.request("GET", path, params=params)

    def pages(self, path: str, key: str, **params: Any) -> Iterator[dict[str, Any]]:
        cursor = None
        while True:
            data = self.get(path, {**params, **({"cursor": cursor} if cursor else {})})
            yield from data.get(key, [])
            cursor = data.get("cursor")
            if not cursor:
                break

    def book(self, ticker: str) -> dict[str, Any]:
        return self.get(f"/markets/{ticker}/orderbook")

    def market(self, ticker: str) -> dict[str, Any]:
        return self.get(f"/markets/{ticker}")["market"]

    def create_order(self, order: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", "/portfolio/events/orders", order)

    def cancel_order(self, venue_id: str, params: dict[str, Any]) -> dict[str, Any]:
        return self.request("DELETE", f"/portfolio/events/orders/{venue_id}", params=params)

    def amend_order(self, venue_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", f"/portfolio/events/orders/{venue_id}/amend", changes)
