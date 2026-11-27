import base64
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa, padding
from cryptography.hazmat.primitives import hashes
import pytest
from czquant.kalshi import Kalshi


@pytest.mark.parametrize('kind', ['rsa', 'ed25519'])
def test_rest_and_websocket_sign_paths_without_query(kind, monkeypatch):
    monkeypatch.setattr('czquant.kalshi.time.time', lambda: 1234.5)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048) if kind == 'rsa' else ed25519.Ed25519PrivateKey.generate()
    c = Kalshi(authenticated=False)
    try:
        c.key, c.key_id = key, 'test-key'
        for path in ['/trade-api/ws/v2', '/trade-api/v2/markets?cursor=abc']:
            h = c.headers('GET', path)
            message = ('1234500GET' + path.split('?')[0]).encode()
            signature = base64.b64decode(h['KALSHI-ACCESS-SIGNATURE'])
            if kind == 'rsa':
                key.public_key().verify(signature, message, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
            else:
                key.public_key().verify(signature, message)
            assert h['KALSHI-ACCESS-KEY'] == 'test-key'
    finally: c.http.close()
