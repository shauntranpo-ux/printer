from __future__ import annotations
"""
Cross-asset features derived from BTC outputs.

ONLY used by ETH, SOL, XRP models. BTC model sets cross_asset.enabled: false.

Graceful degradation: if BTC data is missing or raises, returns _SENTINEL with
btc_degraded=1.0. The downstream Model.should_trade() widens its edge threshold
by config['thresholds']['btc_degraded_penalty'] when it sees btc_degraded=1.0.

Jump detection uses Barndorff-Nielsen & Shephard BV-based heuristic:
  J/RV > _JUMP_RATIO_THRESHOLD is flagged as a significant jump.
"""
# BV-based jump significance: significant if jump component is >10% of total RV.
_JUMP_RATIO_THRESHOLD = 0.10

_SENTINEL: dict[str, float] = {
    "btc_ret_1m":         float("nan"),
    "btc_ret_5m":         float("nan"),
    "btc_ret_15m":        float("nan"),
    "btc_sigma_forecast": float("nan"),
    "btc_ofi_l1":         float("nan"),
    "btc_ofi_l5":         float("nan"),
    "btc_jump_flag":      float("nan"),   # NaN to match "all NaN" sentinel spec; btc_degraded flag stays 1.0
    "btc_degraded":       1.0,
}


def compute(data_window: dict) -> dict[str, float]:
    """
    data_window keys:
      btc_features: {"har_rv": {...}, "order_flow": {...}}  - output of BTC's compute()
      btc_returns:  {"1m": float, "5m": float, "15m": float}
      config: asset config dict (cross_asset section; currently unused here)
    """
    btc_features = data_window.get("btc_features")
    btc_returns  = data_window.get("btc_returns")
    if btc_features is None or btc_returns is None:
        return dict(_SENTINEL)
    try:
        har = btc_features.get("har_rv", {})
        of  = btc_features.get("order_flow", {})
        rv_15m   = har.get("15m_rv", 0.0) or 0.0
        jump_15m = har.get("15m_jump", 0.0) or 0.0
        jump_ratio = jump_15m / rv_15m if rv_15m > 1e-14 else 0.0
        return {
            "btc_ret_1m":         float(btc_returns.get("1m",  float("nan"))),
            "btc_ret_5m":         float(btc_returns.get("5m",  float("nan"))),
            "btc_ret_15m":        float(btc_returns.get("15m", float("nan"))),
            "btc_sigma_forecast": float(har.get("sigma_forecast", float("nan"))),
            "btc_ofi_l1":         float(of.get("ofi_l1", float("nan"))),
            "btc_ofi_l5":         float(of.get("ofi_l5", float("nan"))),
            "btc_jump_flag":      1.0 if jump_ratio > _JUMP_RATIO_THRESHOLD else 0.0,
            "btc_degraded":       0.0,
        }
    except Exception:
        return dict(_SENTINEL)
