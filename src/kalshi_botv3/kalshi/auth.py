import base64
import time
from functools import lru_cache
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

from kalshi_botv3.config.settings import get_settings


class KalshiSigner:
    """Signs Kalshi API requests with RSA-PSS SHA-256."""

    def __init__(self, key_path: Path, api_key_id: str = "") -> None:
        pem = key_path.read_bytes()
        key = serialization.load_pem_private_key(pem, password=None)
        if not isinstance(key, RSAPrivateKey):
            raise TypeError(f"Expected RSA private key, got {type(key).__name__}")
        self._key: RSAPrivateKey = key
        self._api_key_id = api_key_id

    def sign(self, timestamp_ms: int, method: str, path: str) -> str:
        """Return base64-encoded RSA-PSS SHA-256 signature.

        Kalshi signs: f"{timestamp_ms}{METHOD}{path_without_query}"
        PSS padding: SHA-256, MGF1(SHA-256), salt_length=DIGEST_LENGTH (32).
        """
        message = f"{timestamp_ms}{method.upper()}{path}".encode()
        sig = self._key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=hashes.SHA256.digest_size,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(sig).decode()

    def auth_headers(self, method: str, path: str) -> dict[str, str]:
        """Return the three Kalshi auth headers for a request."""
        ts = int(time.time() * 1000)
        return {
            "KALSHI-ACCESS-KEY": self._api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": str(ts),
            "KALSHI-ACCESS-SIGNATURE": self.sign(ts, method, path),
        }


@lru_cache(maxsize=1)
def get_signer() -> KalshiSigner:
    s = get_settings()
    return KalshiSigner(s.kalshi_private_key_path, s.kalshi_api_key_id)
