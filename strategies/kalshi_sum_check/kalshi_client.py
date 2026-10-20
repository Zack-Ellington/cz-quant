"""Minimal Kalshi Trade API v2 client (stdlib only).

Market-data endpoints are public, so no key is required. With a key ID and its RSA
private key (see load_credentials), requests are signed with RSA-PSS / SHA-256 over
timestamp_ms + METHOD + path, sent as KALSHI-ACCESS-KEY / -TIMESTAMP / -SIGNATURE.
That gets your account's rate-limit tier and is required for the WebSocket.
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


ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
# Accepted names, first match wins. KALSHI-ACCESS-KEY mirrors the header name; a shell
# can't export a name with hyphens, so it only works from the .env file.
KEY_ID_NAMES = ("KALSHI_API_KEY_ID", "KALSHI_ACCESS_KEY", "KALSHI-ACCESS-KEY")
KEY_PATH_NAMES = ("KALSHI_PRIVATE_KEY_PATH", "KALSHI-PRIVATE-KEY-PATH")
KEY_PEM_NAMES = ("KALSHI_PRIVATE_KEY", "KALSHI-PRIVATE-KEY")  # the PEM itself, newlines as \n


def read_env_file(path: str = ENV_FILE) -> dict:
    """KEY=value lines from the strategy's .env (comments and blanks skipped). Never logged."""
    out = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip().removeprefix("export ").strip()
                out[k] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return out


def _first(names, *sources):
    for src in sources:
        for n in names:
            if src.get(n):
                return n, src[n]
    return None, None


def credential_status(env_file: str = ENV_FILE) -> dict:
    """Which credential names were found, and where. Values are never returned."""
    file_vars = read_env_file(env_file)
    out = {}
    for label, names in (("key_id", KEY_ID_NAMES), ("key_path", KEY_PATH_NAMES), ("key_pem", KEY_PEM_NAMES)):
        n_env, _ = _first(names, os.environ)
        n_file, _ = _first(names, file_vars)
        out[label] = f"environment {n_env}" if n_env else (f".env {n_file}" if n_file else None)
    return out


def load_credentials(env_file: str = ENV_FILE):
    """(key_id, private_key) for signing, or (None, None).

    Looks in the environment first, then the strategy's .env. The key ID is any of
    KEY_ID_NAMES; its private key (RSA or Ed25519) is either a PEM file
    (KEY_PATH_NAMES) or the PEM text itself (KEY_PEM_NAMES).
    """
    file_vars = read_env_file(env_file)
    _, key_id = _first(KEY_ID_NAMES, os.environ, file_vars)
    _, key_path = _first(KEY_PATH_NAMES, os.environ, file_vars)
    _, key_pem = _first(KEY_PEM_NAMES, os.environ, file_vars)
    if not key_id or not (key_path or key_pem):
        return None, None
    from cryptography.hazmat.primitives import serialization

    if key_path:
        with open(os.path.expanduser(key_path), "rb") as f:
            data = f.read()
    else:
        data = key_pem.replace("\\n", "\n").encode()
    key = serialization.load_pem_private_key(data, password=None)
    from cryptography.hazmat.primitives.asymmetric import ed25519, rsa

    if not isinstance(key, (rsa.RSAPrivateKey, ed25519.Ed25519PrivateKey)):
        raise ValueError(f"The private key is {type(key).__name__.removesuffix('PrivateKey').lstrip('_')}. "
                         "Kalshi verifies RSA (RSA-PSS/SHA-256) or Ed25519 keys only.")
    return key_id, key


def key_type(private_key) -> str:
    from cryptography.hazmat.primitives.asymmetric import ed25519

    return "Ed25519" if isinstance(private_key, ed25519.Ed25519PrivateKey) else "RSA"


def signed_headers(key_id, private_key, method: str, path: str) -> dict:
    """Kalshi auth headers over timestamp_ms + METHOD + path (no query).

    RSA keys sign with RSA-PSS / SHA-256; Ed25519 keys sign the same message with
    plain Ed25519 (it takes no padding or hash argument).
    """
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ed25519, padding

    ts = str(int(time.time() * 1000))
    message = (ts + method + path).encode()
    if isinstance(private_key, ed25519.Ed25519PrivateKey):
        sig = private_key.sign(message)
    else:
        sig = private_key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
    return {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
    }


class KalshiClient:
    def __init__(self, base_url: str = BASE_URL, max_rps: float = 10.0, retries: int = 5):
        self.base_url = base_url.rstrip("/")
        self.min_interval = 1.0 / max_rps
        self.retries = retries
        self._last = 0.0
        self._key_id, self._private_key = load_credentials()

    # -- transport ---------------------------------------------------------

    def _auth_headers(self, method: str, path: str) -> dict:
        if not self._private_key:
            return {}
        sign_path = urllib.parse.urlparse(self.base_url + path).path  # no query string
        return signed_headers(self._key_id, self._private_key, method, sign_path)

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
