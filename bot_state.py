"""
bot_state.py — Shared mutable globals and constants for the kalshi bot.

Every other module does `import bot_state` and reads/writes attributes here.
No classes, no dataclasses — plain module attributes for zero-overhead access.
"""
__all__ = [
    # URL constants
    "KALSHI_LIVE_BASE_URL", "KALSHI_DEMO_BASE_URL", "KALSHI_BASE_URL", "KALSHI_PATH_PREFIX",
    "API_TIMEOUT", "MARKET_CACHE_TTL", "WATCH_PHASE_SECONDS", "KALSHI_FEE",
    # Env-sourced secrets / paths
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
    "_CONFIG_FILE", "_DB_FILE", "_STATE_FILE", "_DATA_DIR",
    # Runtime state
    "btc_prices",
    "_ticker_obi",
    "private_key", "api_key",
    "current_market", "current_phase", "current_position",
    "_s2_attempted_tickers", "_asset_states", "_s1_pending_trades",
    "_market_cache", "_market_cache_ts", "_all_markets_cache",
    "limit_triggered", "limit_reason", "pre_limit_mode", "daily_reset_date",
    "last_confidence_score", "last_confidence_breakdown", "last_action", "last_skip_reason",
    "_asset_eval", "_contract_price_history",
    "_CAL_DEFAULTS", "_brain_cal_s1", "_brain_cal_s2",
    "_last_good_config", "_consecutive_losses", "_s1_consecutive_losses", "_s2_consecutive_losses", "_consecutive_price_skips",
    "_s1_consec_losses_by_asset", "_s1_cooldown_until",
    "_S1_VERSION", "_S2_VERSION", "_S1_ASSET_VOL_RATIO",
]

import os
from collections import deque

# ── constants (never change at runtime) ──────────────────────────────────────
KALSHI_LIVE_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_DEMO_BASE_URL = "https://demo-api.kalshi.co/trade-api/v2"
KALSHI_BASE_URL      = KALSHI_LIVE_BASE_URL  # overwritten once in load_credentials()
KALSHI_PATH_PREFIX   = "/trade-api/v2"
API_TIMEOUT          = 10
MARKET_CACHE_TTL     = 30
WATCH_PHASE_SECONDS  = 30
KALSHI_FEE           = 0.07

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID",   "")

_CONFIG_FILE = os.environ.get("BOT_CONFIG_FILE", "config.json")
_DB_FILE     = os.environ.get("BOT_DB_FILE",     "kalshi_bot.db")
_STATE_FILE  = os.environ.get("BOT_STATE_FILE",  "bot_state.json")
_DATA_DIR    = os.path.dirname(os.path.abspath(_DB_FILE))
# ── mutable runtime state ────────────────────────────────────────────────────
import asset_manager  # noqa: E402 — after os/path setup
btc_prices: deque = asset_manager._prices["BTC"]

_ticker_obi: dict = {}

private_key = None
api_key: str = ""

current_market: dict | None = None
current_phase: str = "DONE"
current_position: dict | None = None
recovery_unverified: bool = False
_s2_attempted_tickers: set = set()
_asset_states: dict = {}
_s1_pending_trades: dict = {}  # ticker → {trade_id, side, entry_price_cents, contracts, strike, asset, mode, entry_ts, market_close_time}
_s1_asset_trade_times: dict = {}  # asset → list of float timestamps of S1 fills

_market_cache: dict | None = None
_market_cache_ts: float = 0.0
_all_markets_cache: list = []

limit_triggered: bool = False
limit_reason: str = ""
pre_limit_mode: str | None = None
daily_reset_date = None

last_confidence_score: int      = 0
last_confidence_breakdown: dict = {}
last_action: str       = ""
last_skip_reason: str  = ""

_asset_eval: dict             = {}
_contract_price_history: dict = {}

# Settlement-reference basis samples (measurement only — no correction applied yet):
# our Coinbase spot-vs-strike implied side vs Kalshi's official YES/NO result.
_settlement_basis: deque      = deque(maxlen=500)

# Per-ticker held-book path {ticker: deque[(ts, yes_ask, no_ask)]} captured DURING the hold
# phase, used to compute the maker-vs-taker counterfactual at settlement (measurement only).
# Separate from _contract_price_history so S2's (ts, price) tuple shape is untouched.
_maker_track: dict            = {}

_CAL_DEFAULTS: dict = {
    "last_count": 0, "prob_scale": 1.0, "min_edge_override": None,
    "confidence_bonus": 0, "reward_tier": 0, "overall_wr": 0.0,
    "condition_wr": {}, "bullish_wr": 0.5, "bearish_wr": 0.5,
}
_brain_cal_s1: dict = {**_CAL_DEFAULTS}
_brain_cal_s2: dict = {**_CAL_DEFAULTS}

_last_good_config: dict | None = None
_consecutive_losses: int       = 0
_s1_consecutive_losses: int   = 0
_s1_consec_losses_by_asset: dict = {}  # asset → consecutive loss count since last win
_s1_cooldown_until: dict = {}          # asset → epoch timestamp when cooldown expires
_s2_consecutive_losses: int   = 0
_consecutive_price_skips: int  = 0

_S1_VERSION = "ema-momentum-2026-05-07"
_S2_VERSION = "contract-velocity-obi-2026-05-07"

kalshi_clock_skew_ms: int = 0       # corrected by _maybe_adjust_clock_skew at startup
demo_fallback_alert: bool = False    # set when demo creds missing; Telegram fired async

_S1_ASSET_VOL_RATIO: dict = {
    "BTC": 1.00, "ETH": 1.10, "SOL": 2.20, "XRP": 1.80, "DOGE": 2.60,
}
