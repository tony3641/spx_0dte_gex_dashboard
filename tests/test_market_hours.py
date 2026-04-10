"""
Market hours module unit tests.
"""

import sys
import os
from datetime import datetime, date, time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from market_hours import (
    ET,
    is_within_rth,
    market_status,
    find_next_expiration,
    get_expiration_display,
    is_spx_options_open,
    is_cboe_options_open,
    last_trading_date,
    is_weekday,
)


def _et(year, month, day, hour=0, minute=0, second=0):
    """Helper to create a timezone-aware ET datetime."""
    return datetime(year, month, day, hour, minute, second, tzinfo=ET)


# ---------------------------------------------------------------------------
# is_within_rth
# ---------------------------------------------------------------------------

class TestIsWithinRTH:

    def test_during_rth(self):
        dt = _et(2026, 4, 10, 10, 0)  # Friday 10:00 AM ET
        assert is_within_rth(dt) is True

    def test_at_open(self):
        dt = _et(2026, 4, 10, 9, 30)  # 9:30 AM
        assert is_within_rth(dt) is True

    def test_at_close(self):
        dt = _et(2026, 4, 10, 16, 15)  # 4:15 PM
        assert is_within_rth(dt) is True

    def test_before_open(self):
        dt = _et(2026, 4, 10, 9, 29)  # 9:29 AM
        assert is_within_rth(dt) is False

    def test_after_close(self):
        dt = _et(2026, 4, 10, 16, 16)  # 4:16 PM
        assert is_within_rth(dt) is False

    def test_weekend(self):
        dt = _et(2026, 4, 11, 12, 0)  # Saturday
        assert is_within_rth(dt) is False


# ---------------------------------------------------------------------------
# market_status
# ---------------------------------------------------------------------------

class TestMarketStatus:

    def test_rth(self):
        dt = _et(2026, 4, 10, 12, 0)  # Friday noon
        assert market_status(dt) == "RTH"

    def test_gth_morning(self):
        dt = _et(2026, 4, 10, 8, 0)  # Friday 8:00 AM (before RTH)
        assert market_status(dt) == "GTH"

    def test_curb(self):
        dt = _et(2026, 4, 10, 16, 30)  # Friday 4:30 PM
        assert market_status(dt) == "CURB"

    def test_closed_weekday_gap(self):
        dt = _et(2026, 4, 10, 18, 0)  # Friday 6:00 PM (daily gap)
        assert market_status(dt) == "CLOSED"

    def test_gth_evening(self):
        dt = _et(2026, 4, 10, 21, 0)  # Friday 9:00 PM
        assert market_status(dt) == "GTH"

    def test_saturday_closed(self):
        dt = _et(2026, 4, 11, 12, 0)  # Saturday
        assert market_status(dt) == "CLOSED"

    def test_sunday_before_open(self):
        dt = _et(2026, 4, 12, 18, 0)  # Sunday 6:00 PM
        assert market_status(dt) == "CLOSED"

    def test_sunday_after_gth_open(self):
        dt = _et(2026, 4, 12, 20, 30)  # Sunday 8:30 PM
        assert market_status(dt) == "GTH"


# ---------------------------------------------------------------------------
# find_next_expiration
# ---------------------------------------------------------------------------

class TestFindNextExpiration:

    def test_today_available(self):
        """If today is in the list and before cease time, return today."""
        exps = ["20260409", "20260410", "20260411"]
        result = find_next_expiration(exps, ref_date=date(2026, 4, 10))
        assert result == "20260410"

    def test_today_not_available(self):
        """If today is NOT in the list, return next available."""
        exps = ["20260409", "20260411", "20260413"]
        result = find_next_expiration(exps, ref_date=date(2026, 4, 10))
        assert result == "20260411"

    def test_all_past(self):
        """If all expirations are in the past, return None."""
        exps = ["20260408", "20260409"]
        result = find_next_expiration(exps, ref_date=date(2026, 4, 10))
        assert result is None

    def test_empty_list(self):
        assert find_next_expiration([]) is None

    def test_weekend_rolls_to_monday(self):
        """On Saturday, should find next weekday expiration."""
        exps = ["20260410", "20260413"]  # Friday and Monday
        result = find_next_expiration(exps, ref_date=date(2026, 4, 11))  # Saturday
        assert result == "20260413"


# ---------------------------------------------------------------------------
# is_spx_options_open / is_cboe_options_open (alias)
# ---------------------------------------------------------------------------

class TestIsSpxOptionsOpen:

    def test_during_rth(self):
        dt = _et(2026, 4, 10, 12, 0)
        assert is_spx_options_open(dt) is True

    def test_during_gth(self):
        dt = _et(2026, 4, 10, 4, 0)  # 4 AM — GTH
        assert is_spx_options_open(dt) is True

    def test_during_curb(self):
        dt = _et(2026, 4, 10, 16, 30)  # curb
        assert is_spx_options_open(dt) is True

    def test_during_gap(self):
        dt = _et(2026, 4, 10, 18, 0)  # daily gap
        assert is_spx_options_open(dt) is False

    def test_saturday(self):
        dt = _et(2026, 4, 11, 12, 0)
        assert is_spx_options_open(dt) is False

    def test_sunday_before_open(self):
        dt = _et(2026, 4, 12, 18, 0)
        assert is_spx_options_open(dt) is False

    def test_sunday_after_open(self):
        dt = _et(2026, 4, 12, 20, 30)
        assert is_spx_options_open(dt) is True

    def test_alias(self):
        """is_cboe_options_open should be the same function."""
        dt = _et(2026, 4, 10, 12, 0)
        assert is_cboe_options_open(dt) == is_spx_options_open(dt)


# ---------------------------------------------------------------------------
# last_trading_date
# ---------------------------------------------------------------------------

class TestLastTradingDate:

    def test_weekday(self):
        result = last_trading_date(ref=date(2026, 4, 10))  # Friday
        assert result == date(2026, 4, 10)

    def test_saturday_rolls_back(self):
        result = last_trading_date(ref=date(2026, 4, 11))  # Saturday
        assert result == date(2026, 4, 10)  # Friday

    def test_sunday_rolls_back(self):
        result = last_trading_date(ref=date(2026, 4, 12))  # Sunday
        assert result == date(2026, 4, 10)  # Friday

    def test_monday(self):
        result = last_trading_date(ref=date(2026, 4, 13))  # Monday
        assert result == date(2026, 4, 13)


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------

class TestIsWeekday:

    def test_weekdays(self):
        assert is_weekday(date(2026, 4, 6)) is True   # Monday
        assert is_weekday(date(2026, 4, 10)) is True   # Friday

    def test_weekend(self):
        assert is_weekday(date(2026, 4, 11)) is False  # Saturday
        assert is_weekday(date(2026, 4, 12)) is False  # Sunday
