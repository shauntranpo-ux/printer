"""
bot_state.py — Shared mutable globals and constants for the kalshi bot.

Every other module does `import bot_state` and reads/writes attributes here.
No classes, no dataclasses — plain module attributes for zero-overhead access.
"""
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
_PRICE_VAL_CSV = os.path.join(_DATA_DIR, "price_validation_log.csv")

# ── mutable runtime state ────────────────────────────────────────────────────
import asset_manager  # noqa: E402 — after os/path setup
btc_prices: deque = asset_manager._prices["BTC"]

_obi_monitor       = None   # OBIMonitor | None  — set in main()
_funding_monitor_btc = None  # FundingDispersionMonitor | None
_funding_monitor_eth = None  # FundingDispersionMonitor | None

private_key = None
api_key: str = ""

current_market: dict | None = None
current_phase: str = "DONE"
current_position: dict | None = None
_order_attempted_tickers: set = set()
_asset_states: dict = {}

_market_cache: dict | None = None
_market_cache_ts: float = 0.0
_all_markets_cache: list = []
_all_markets_cache_ts: float = 0.0

limit_triggered: bool = False
limit_reason: str = ""
pre_limit_mode: str | None = None
daily_reset_date = None

_price_val_count: int    = 0
_price_val_gap_n: int    = 0
_price_val_sim_sum: float  = 0.0
_price_val_real_sum: float = 0.0
_price_val_gap_sum: float  = 0.0

last_confidence_score: int      = 0
last_confidence_breakdown: dict = {}
last_action: str       = ""
last_skip_reason: str  = ""

_asset_eval: dict             = {}
_contract_price_history: dict = {}

_CAL_DEFAULTS: dict = {
    "last_count": 0, "prob_scale": 1.0, "min_edge_override": None,
    "confidence_bonus": 0, "reward_tier": 0, "overall_wr": 0.0,
    "condition_wr": {}, "bullish_wr": 0.5, "bearish_wr": 0.5,
}
_brain_cal_s1: dict = {**_CAL_DEFAULTS}
_brain_cal_s2: dict = {**_CAL_DEFAULTS}

_last_good_config: dict | None = None
_consecutive_losses: int       = 0
_consecutive_price_skips: int  = 0

_S2_SINGLETONS: dict  = {}
_config_mtime: float  = 0.0
_current_window: str  = ""

_S1_VERSION = "bv3-2026-05-06"
_S2_VERSION = "d3-hybrid-2026-05-06"

_S1_ASSET_VOL_RATIO: dict = {
    "BTC": 1.00, "ETH": 1.10, "SOL": 2.20, "XRP": 1.80, "DOGE": 2.60,
}
