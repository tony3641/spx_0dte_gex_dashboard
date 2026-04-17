"""Risk-free rate helpers.

This module fetches SGOV yield data from yfinance and exposes an annualized
no-risk rate for GEX calculations.
"""

import logging
from typing import Optional
from config import (
    DEFAULT_RISK_FREE_RATE,
    SGOV_TICKER,
)

try:
    import yfinance as yf
except Exception:  # pragma: no cover - optional dependency at runtime
    yf = None

logger = logging.getLogger("risk_free")


def _normalize_rate(raw: float) -> float:
    """Normalize a raw yield value into decimal form (e.g. 0.046 for 4.6%)."""
    value = float(raw)
    if value <= 0:
        raise ValueError("Yield value must be positive")
    return value / 100.0 if value > 1.0 else value


def _normalize_expense_ratio(raw: float) -> float:
    """Normalize ETF expense ratio into decimal form (e.g. 0.0009 for 0.09%)."""
    value = float(raw)
    if value <= 0:
        raise ValueError("Expense ratio must be positive")
    if value >= 1.0:
        return value / 100.0
    # Yahoo commonly returns netExpenseRatio as percentage points for ETFs (e.g. 0.09 => 0.09%).
    if value > 0.02:
        return value / 100.0
    return value


def fetch_sgov_no_risk_yield() -> float:
    """Fetch SGOV no-risk yield using yfinance yield plus ETF expense ratio."""
    if yf is None:
        raise RuntimeError("yfinance is not available")

    info = yf.Ticker(SGOV_TICKER).info or {}

    raw_yield = info.get("yield")
    if raw_yield is None:
        raise ValueError("SGOV 'yield' is missing from yfinance info")

    rate = _normalize_rate(raw_yield)

    expense_raw = info.get("netExpenseRatio")
    if expense_raw is None:
        raise ValueError("SGOV 'netExpenseRatio' is missing from yfinance info")

    rate += _normalize_expense_ratio(expense_raw)
    return rate


def get_risk_free_rate(default: Optional[float] = None) -> float:
    """Return the SGOV-derived risk-free rate, falling back on a default if needed."""
    if default is None:
        default = DEFAULT_RISK_FREE_RATE
    try:
        return fetch_sgov_no_risk_yield()
    except Exception as exc:
        logger.warning(
            "Unable to fetch SGOV no-risk yield; falling back to default risk-free rate: %s",
            exc,
        )
        return default
