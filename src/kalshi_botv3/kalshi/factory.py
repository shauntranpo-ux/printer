from kalshi_botv3.kalshi.auth import get_signer
from kalshi_botv3.kalshi.client import HttpKalshiClient, MockKalshiClient


def build_kalshi_client() -> HttpKalshiClient | MockKalshiClient:
    """Return HttpKalshiClient for live mode, MockKalshiClient otherwise."""
    from kalshi_botv3.config.settings import get_settings

    settings = get_settings()
    if settings.mode == "live":
        return HttpKalshiClient(get_signer(), settings.kalshi_env)
    return MockKalshiClient()
