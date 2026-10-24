"""Check the Kalshi key before a WebSocket run. Read-only: it places no orders.

1. Reports which credential names were found (never their values).
2. Signed REST call: GET /portfolio/balance must return 200.
3. WebSocket: connects with signed headers, subscribes to one open market's
   orderbook_delta, and waits for its snapshot.

Usage: uv run python check_auth.py
"""

from __future__ import annotations

import asyncio
import json
import sys

from feeds import WS_PATH, WS_URL
from kalshi_client import KalshiClient, credential_status, key_type, load_credentials, signed_headers


async def ws_check(key_id, key, ticker):
    import websockets

    headers = signed_headers(key_id, key, "GET", WS_PATH)
    async with websockets.connect(WS_URL, additional_headers=headers, open_timeout=15) as ws:
        await ws.send(json.dumps({"id": 1, "cmd": "subscribe",
                                  "params": {"channels": ["orderbook_delta"], "market_tickers": [ticker]}}))
        for _ in range(10):
            m = json.loads(await asyncio.wait_for(ws.recv(), 15))
            if m.get("type") == "orderbook_snapshot":
                return m["msg"].get("market_ticker")
            if m.get("type") == "error":
                raise RuntimeError(m.get("msg"))
    raise RuntimeError("no snapshot received")


def main():
    status = credential_status()
    for k, v in status.items():
        print(f"  {k:9} {v or 'not found'}")
    try:
        key_id, key = load_credentials()
    except ValueError as e:
        sys.exit(f"FAIL: {e}")
    if not key:
        missing = "the key ID" if not status["key_id"] else "the private key (KALSHI_PRIVATE_KEY_PATH or KALSHI_PRIVATE_KEY)"
        sys.exit(f"FAIL: {missing} is missing. It is the private key (RSA or Ed25519) that belongs to this API key ID.")
    print(f"  private key loaded ({key_type(key)})")

    client = KalshiClient()
    try:
        client.get("/portfolio/balance")
        print("OK   signed REST: GET /portfolio/balance returned 200")
    except RuntimeError as e:
        sys.exit(f"FAIL signed REST: {e}  (401 = wrong key/ID pair or a demo key; check the system clock too)")

    ev = next(e for e in client.iter_open_events() if e.get("markets"))
    ticker = ev["markets"][0]["ticker"]
    try:
        got = asyncio.run(ws_check(key_id, key, ticker))
        print(f"OK   websocket: snapshot received for {got}")
    except Exception as e:
        sys.exit(f"FAIL websocket: {e}")
    print("Ready: `uv run python paper_ws.py` will use the WebSocket feed.")


if __name__ == "__main__":
    main()
