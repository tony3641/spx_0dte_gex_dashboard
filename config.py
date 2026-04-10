"""
Centralized configuration constants and tick-rounding helpers.

All environment-variable–driven settings live here so every module
imports from one place.
"""

import os

# ---------------------------------------------------------------------------
# IB connection
# ---------------------------------------------------------------------------
IB_HOST = os.getenv("IB_HOST", "127.0.0.1")
IB_PORT = int(os.getenv("IB_PORT", "7497"))
IB_CLIENT_ID = int(os.getenv("IB_CLIENT_ID", "1"))

# ---------------------------------------------------------------------------
# Refresh cadences (seconds)
# ---------------------------------------------------------------------------
CHAIN_REFRESH_SECONDS = int(os.getenv("CHAIN_REFRESH_SECONDS", "10"))
DASHBOARD_CHAIN_REFRESH_SECONDS = int(os.getenv("DASHBOARD_CHAIN_REFRESH_SECONDS", "300"))
CHAIN_TAB_FULL_REFRESH_SECONDS = int(os.getenv("CHAIN_TAB_FULL_REFRESH_SECONDS", "300"))
SNAPSHOT_REFRESH_SECONDS = int(os.getenv("SNAPSHOT_REFRESH_SECONDS", "300"))
PRICE_PUSH_INTERVAL = float(os.getenv("PRICE_PUSH_INTERVAL", "1.0"))

# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------
SERVER_HOST = os.getenv("SERVER_HOST", "0.0.0.0")
SERVER_PORT = int(os.getenv("SERVER_PORT", "8000"))

# ---------------------------------------------------------------------------
# Chain streaming
# ---------------------------------------------------------------------------
CHAIN_STREAM_MAX_LINES = int(os.getenv("CHAIN_STREAM_MAX_LINES", "96"))
CHAIN_STREAM_UPDATE_INTERVAL = float(os.getenv("CHAIN_STREAM_UPDATE_INTERVAL", "0.5"))
VIEWPORT_CENTER_MIN_INTERVAL = float(os.getenv("VIEWPORT_CENTER_MIN_INTERVAL", "0.2"))

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
