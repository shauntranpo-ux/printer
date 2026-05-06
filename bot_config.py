"""bot_config.py — Config read/write and atomic JSON helper."""
import json
import logging
import os
import tempfile

import bot_state

log = logging.getLogger("bot")


def atomic_write_json(data: dict, path: str) -> None:
    """
    Write JSON atomically: serialize to a sibling temp file, fsync, then
    os.replace() which is atomic on POSIX and near-atomic on Windows (NTFS
    guarantees no partial-read exposure because replace is a rename).
    Cleans up the temp file on any failure so no orphaned .tmp files linger.
    """
    dir_ = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp_path = tempfile.mkstemp(dir=dir_, suffix=".json.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def read_config() -> dict:
    """Read and return the contents of the config file.
    Falls back to bot_state._last_good_config if the file is transiently corrupt
    (e.g. a partial write in progress). Raises only on startup failure
    when no cached config is available yet.
    """
    try:
        with open(bot_state._CONFIG_FILE, "r") as fh:
            cfg = json.load(fh)
        cfg.setdefault("enabled_assets", ["ETH", "SOL", "XRP"])
        bot_state._last_good_config = cfg
        return cfg
    except json.JSONDecodeError as exc:
        log.warning(f"Config JSON decode error: {exc}. Using last known good config.")
        if bot_state._last_good_config is not None:
            return bot_state._last_good_config
        raise
    except Exception as exc:
        log.warning(f"Config read error: {exc}. Using last known good config.")
        if bot_state._last_good_config is not None:
            return bot_state._last_good_config
        raise


def write_config(data: dict) -> None:
    """Write a dict to the config file atomically (temp+replace)."""
    atomic_write_json(data, bot_state._CONFIG_FILE)


def get_asset_config(config: dict, asset: str, field: str, default=None):
    """Get config value — asset override if present, else global value, else default."""
    overrides = config.get("asset_overrides", {}).get(asset, {})
    if field in overrides:
        return overrides[field]
    return config.get(field, default)


def _init_config() -> None:
    """
    Create config.json on startup if missing, and apply Railway env var overrides.

    Set BOT_MODE=live and BOT_ENABLED=true in Railway environment variables
    so live mode survives every redeploy without manual editing.
    Daily loss limits still work — they set bot_state.limit_triggered in memory which
    is checked independently of the mode flag.
    """
    # Build defaults
    defaults = {
        "bot_enabled": False,
        "trade_amount_dollars": 25,
        "mode": "paper",
        "confidence_threshold": 0,   # Supertrend win_prob is fixed 0.70; real gate is EV
        "daily_loss_limit_dollars": 50,          # 2× trade size — real guard, not $5M decoration
        "daily_profit_target_dollars": 200,
        "max_consecutive_losses": 5,             # pause 15 min after this many losses in a row
        "enable_reversal_signal": False,         # disabled by default — no backtested evidence yet
        "min_ev_base": 8,                        # EV gate; fee formula fix may allow lower — tune via backtest
        "kalshi_fee_per_contract_cents": 7,      # Kalshi platform fee; update if pricing changes
        "preflight_override": False,             # set true ONLY to bypass pre-flight hard stop — not recommended
    }

    # Load existing config or start from defaults
    if os.path.exists(bot_state._CONFIG_FILE):
        try:
            with open(bot_state._CONFIG_FILE) as fh:
                cfg = json.load(fh)
        except Exception:
            cfg = defaults.copy()
    else:
        cfg = defaults.copy()

    # Persistent bot_enabled from Railway volume — survives redeploys
    # (written by server.py whenever the dashboard toggle changes)
    _data_dir = os.path.dirname(os.path.abspath(bot_state._DB_FILE))
    _be_state = os.path.join(_data_dir, "bot_enabled.state")
    if os.path.exists(_be_state):
        try:
            cfg["bot_enabled"] = open(_be_state).read().strip() == "1"
        except Exception:
            pass

    # Railway env var overrides — highest priority, set once, persist forever
    if "BOT_MODE" in os.environ:
        cfg["mode"] = os.environ["BOT_MODE"].strip().lower()
    if "BOT_ENABLED" in os.environ:
        cfg["bot_enabled"] = os.environ["BOT_ENABLED"].strip().lower() in ("1", "true", "yes")

    # Fill in any missing keys with defaults
    for k, v in defaults.items():
        cfg.setdefault(k, v)

    write_config(cfg)
    log.info(f"Config ready: mode={cfg['mode']} enabled={cfg['bot_enabled']}")
