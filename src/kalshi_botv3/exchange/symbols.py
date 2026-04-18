"""Exchange symbol mappings for the 7 supported coins.

HYPE availability (verified 2026-04-17):
  Coinbase: HYPE-USD  (listed Feb 2025)
  Binance:  HYPEUSDT  (spot, active)
No fallback to other exchanges needed — both primary feeds available.
"""

COIN_TO_COINBASE: dict[str, str] = {
    "BTC": "BTC-USD",
    "ETH": "ETH-USD",
    "XRP": "XRP-USD",
    "SOL": "SOL-USD",
    "DOGE": "DOGE-USD",
    "HYPE": "HYPE-USD",
    "BNB": "BNB-USD",
}

COIN_TO_BINANCE: dict[str, str] = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
    "XRP": "XRPUSDT",
    "SOL": "SOLUSDT",
    "DOGE": "DOGEUSDT",
    "HYPE": "HYPEUSDT",
    "BNB": "BNBUSDT",
}

_COINBASE_TO_COIN: dict[str, str] = {v: k for k, v in COIN_TO_COINBASE.items()}
_BINANCE_TO_COIN: dict[str, str] = {v: k for k, v in COIN_TO_BINANCE.items()}


def to_coinbase(coin: str) -> str:
    return COIN_TO_COINBASE[coin.upper()]


def to_binance(coin: str) -> str:
    return COIN_TO_BINANCE[coin.upper()]


def coinbase_to_coin(product_id: str) -> str | None:
    return _COINBASE_TO_COIN.get(product_id)


def binance_to_coin(symbol: str) -> str | None:
    return _BINANCE_TO_COIN.get(symbol.upper())
