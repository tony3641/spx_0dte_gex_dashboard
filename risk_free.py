"""Risk-free rate helpers.

This module fetches the current 7-day yield for the SGOV ETF and exposes it as
an annualized risk-free rate for GEX calculations.
"""

import logging
import re
import urllib.request
from typing import Optional

logger = logging.getLogger("risk_free")

YAHOO_SGOV_URL = "https://finance.yahoo.com/quote/SGOV?p=SGOV"
DEFAULT_RISK_FREE_RATE = 0.043


def parse_sgov_7_day_yield(html: str) -> float:
    """Parse SGOV 7-day yield from Yahoo Finance HTML."""
    match = re.search(r"7\s*Day\s*Yield[^0-9%]*(?P<yield>[0-9]+(?:\.[0-9]+)?)%",
                      html, re.IGNORECASE)
    if match:
        return float(match.group("yield")) / 100.0
    raise ValueError("Could not parse SGOV 7 Day Yield from page content")


def fetch_sgov_7_day_yield(timeout: float = 5.0) -> float:
    """Fetch the latest SGOV 7-day yield from Yahoo Finance."""
    request = urllib.request.Request(
        YAHOO_SGOV_URL,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
        }
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        html = response.read().decode("utf-8", errors="ignore")
    return parse_sgov_7_day_yield(html)


def get_risk_free_rate(default: Optional[float] = None) -> float:
    """Return the SGOV-derived risk-free rate, falling back on a default if needed."""
    if default is None:
        default = DEFAULT_RISK_FREE_RATE
    try:
        return fetch_sgov_7_day_yield()
    except Exception as exc:
        logger.warning(
            "Unable to fetch SGOV 7 Day Yield; falling back to default risk-free rate: %s",
            exc,
        )
        return default
