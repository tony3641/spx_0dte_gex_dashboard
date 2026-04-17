"""
Centralized configuration constants and tick-rounding helpers.

Load order for each setting:
1) Environment variable
2) config/params.yaml
3) hardcoded default
"""

import os
from datetime import time
from pathlib import Path
from typing import Any, Callable, Dict

try:
    import yaml
except Exception:  # pragma: no cover
    yaml = None


PARAMS_YAML_PATH = Path(__file__).parent / "config" / "params.yaml"


def _load_yaml_params() -> Dict[str, Any]:
    if yaml is None or not PARAMS_YAML_PATH.exists():
        return {}
    try:
        with PARAMS_YAML_PATH.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


_YAML_PARAMS = _load_yaml_params()


def _get_setting(name: str, default: Any, cast: Callable[[Any], Any]) -> Any:
    env_val = os.getenv(name)
    if env_val is not None:
        try:
            return cast(env_val)
        except Exception:
            return default

    yaml_val = _YAML_PARAMS.get(name, default)
    try:
        return cast(yaml_val)
    except Exception:
        return default


def _parse_hhmm(value: Any) -> time:
    text = str(value).strip()
    hh, mm = text.split(":", 1)
    return time(int(hh), int(mm))


def _get_time_setting(name: str, default_hhmm: str) -> time:
    env_val = os.getenv(name)
    if env_val is not None:
        try:
            return _parse_hhmm(env_val)
        except Exception:
            return _parse_hhmm(default_hhmm)

    yaml_val = _YAML_PARAMS.get(name, default_hhmm)
    try:
        return _parse_hhmm(yaml_val)
    except Exception:
        return _parse_hhmm(default_hhmm)

# ---------------------------------------------------------------------------
# IB connection
# ---------------------------------------------------------------------------
IB_HOST = _get_setting("IB_HOST", "127.0.0.1", str)
IB_PORT = _get_setting("IB_PORT", 7497, int)
IB_CLIENT_ID = _get_setting("IB_CLIENT_ID", 1, int)

# ---------------------------------------------------------------------------
# Refresh cadences (seconds)
# ---------------------------------------------------------------------------
CHAIN_REFRESH_SECONDS = _get_setting("CHAIN_REFRESH_SECONDS", 10, int)
DASHBOARD_CHAIN_REFRESH_SECONDS = _get_setting("DASHBOARD_CHAIN_REFRESH_SECONDS", 300, int)
CHAIN_TAB_FULL_REFRESH_SECONDS = _get_setting("CHAIN_TAB_FULL_REFRESH_SECONDS", 300, int)
SNAPSHOT_REFRESH_SECONDS = _get_setting("SNAPSHOT_REFRESH_SECONDS", 300, int)
PRICE_PUSH_INTERVAL = _get_setting("PRICE_PUSH_INTERVAL", 1.0, float)

# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------
SERVER_HOST = _get_setting("SERVER_HOST", "0.0.0.0", str)
SERVER_PORT = _get_setting("SERVER_PORT", 8000, int)

# ---------------------------------------------------------------------------
# Chain streaming
# ---------------------------------------------------------------------------
CHAIN_STREAM_MAX_LINES = _get_setting("CHAIN_STREAM_MAX_LINES", 96, int)
CHAIN_STREAM_UPDATE_INTERVAL = _get_setting("CHAIN_STREAM_UPDATE_INTERVAL", 0.5, float)
VIEWPORT_CENTER_MIN_INTERVAL = _get_setting("VIEWPORT_CENTER_MIN_INTERVAL", 0.2, float)

# ---------------------------------------------------------------------------
# Chain fetch internals
# ---------------------------------------------------------------------------
BATCH_SIZE = _get_setting("BATCH_SIZE", 200, int)
QUALIFY_BATCH_SIZE = _get_setting("QUALIFY_BATCH_SIZE", 150, int)
QUAL_CACHE_REQUALIFY_MOVE = _get_setting("QUAL_CACHE_REQUALIFY_MOVE", 20.0, float)
DEFAULT_ANNUAL_VOL = _get_setting("DEFAULT_ANNUAL_VOL", 0.20, float)
TRADING_DAYS_PER_YEAR = _get_setting("TRADING_DAYS_PER_YEAR", 252, int)

# ---------------------------------------------------------------------------
# Monthly/account/risk-free
# ---------------------------------------------------------------------------
MONTHLY_CACHE_TTL = _get_setting("MONTHLY_CACHE_TTL", 600, int)
FORCE_REFRESH_INTERVAL = _get_setting("FORCE_REFRESH_INTERVAL", 10.0, float)
SGOV_TICKER = _get_setting("SGOV_TICKER", "SGOV", str)
DEFAULT_RISK_FREE_RATE = _get_setting("DEFAULT_RISK_FREE_RATE", 0.043, float)

# ---------------------------------------------------------------------------
# Market-hours session windows (ET)
# ---------------------------------------------------------------------------
RTH_OPEN = _get_time_setting("RTH_OPEN", "09:30")
RTH_CLOSE = _get_time_setting("RTH_CLOSE", "16:15")
SPXW_CEASE = _get_time_setting("SPXW_CEASE", "16:00")
SPX_OPT_GAP_START = _get_time_setting("SPX_OPT_GAP_START", "17:00")
SPX_OPT_GAP_END = _get_time_setting("SPX_OPT_GAP_END", "20:15")

# ---------------------------------------------------------------------------
# SPX tick-rounding helpers (pure functions)
# ---------------------------------------------------------------------------

def spx_tick_for_price(price: float) -> float:
    """SPX/SPXW price increment rule: > $2.00 uses 0.10, else 0.05."""
    return 0.10 if abs(float(price)) > 2.0 else 0.05


def round_abs_to_tick(price: float, tick: float) -> float:
    """Round an absolute price to the nearest tick increment (always positive)."""
    p = abs(float(price))
    t = max(0.0001, float(tick))
    ticks = round(p / t)
    rounded = ticks * t
    return max(t, round(rounded, 2))


def round_signed_to_tick(price: float, tick: float) -> float:
    """Round price to nearest tick, preserving sign (for credit/debit combos)."""
    sign = -1.0 if float(price) < 0 else 1.0
    return sign * round_abs_to_tick(price, tick)
