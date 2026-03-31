"""
Market hours detection and expiration finder for SPX/SPXW options.
Handles trading-hour vs off-hour logic and finds the next available 0DTE expiration.
"""

from datetime import datetime, date, timedelta, time
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo

ET = ZoneInfo("US/Eastern")

# Regular Trading Hours for SPX options
RTH_OPEN = time(9, 30)
RTH_CLOSE = time(16, 15)

# SPXW 0DTE options cease trading at 4:00 PM ET
SPXW_CEASE = time(16, 0)


def now_et() -> datetime:
    """Current datetime in US/Eastern."""
    return datetime.now(ET)


def is_weekday(dt: date) -> bool:
    return dt.weekday() < 5  # Mon=0 .. Fri=4


def is_within_rth(dt: Optional[datetime] = None) -> bool:
    """Check if the given datetime (or now) is within Regular Trading Hours."""
    if dt is None:
        dt = now_et()
    else:
        dt = dt.astimezone(ET)
    return is_weekday(dt.date()) and RTH_OPEN <= dt.time() <= RTH_CLOSE


def market_status(dt: Optional[datetime] = None) -> str:
    """
    Return a human-readable market status string.
    Returns one of: 'RTH', 'GTH', 'CURB', 'CLOSED'
    """
    if dt is None:
        dt = now_et()
    else:
        dt = dt.astimezone(ET)

    wd = dt.weekday()
    t = dt.time()

    # Saturday — always closed
    if wd == 5:
        return "CLOSED"

    # Sunday — GTH opens at 8:15 PM
    if wd == 6:
        return "GTH" if t >= time(20, 15) else "CLOSED"

    # Weekdays
    if time(9, 30) <= t <= time(16, 15):
        return "RTH"
    elif time(16, 15) < t < time(17, 0):
        return "CURB"
    elif time(17, 0) <= t < time(20, 15):
        return "CLOSED"
    else:
        # Before 9:30 AM or at/after 8:15 PM
        return "GTH"


def find_next_expiration(expirations: List[str], ref_date: Optional[date] = None) -> Optional[str]:
    """
    Given a sorted list of SPXW expiration strings ('YYYYMMDD'),
    find the current or next available expiration.

    During RTH on a trading day with that day in the list → returns today.
    After SPXW cease time (4:00 PM ET) on expiration day → returns next expiration.
    On weekends / holidays → returns the next available date.

    Args:
        expirations: List of 'YYYYMMDD' strings from ib.reqSecDefOptParams().
        ref_date: Override date for testing (defaults to now).

    Returns:
        The target expiration string, or None if no valid expiration found.
    """
    if not expirations:
        return None

    sorted_exps = sorted(expirations)
    now = now_et()

    if ref_date is None:
        today_str = now.date().strftime("%Y%m%d")
    else:
        today_str = ref_date.strftime("%Y%m%d")

    # If current time is past the SPXW cease time, today's expiration is dead
    # Move to the next day
    if ref_date is None and now.time() > SPXW_CEASE and today_str in sorted_exps:
        # Today's expiration is done, find the next one after today
        for exp in sorted_exps:
            if exp > today_str:
                return exp
        return None

    # Otherwise, find the first expiration >= today
    for exp in sorted_exps:
        if exp >= today_str:
            return exp

    return None


def get_expiration_display(expiration: str) -> str:
    """Format expiration string for display: '2026-03-30 (0DTE)' or '2026-03-31 (1DTE)'."""
    exp_date = datetime.strptime(expiration, "%Y%m%d").date()
    today = now_et().date()
    dte = (exp_date - today).days
    if dte == 0:
        return f"{exp_date.isoformat()} (0DTE)"
    elif dte == 1:
        return f"{exp_date.isoformat()} (1DTE)"
    else:
        return f"{exp_date.isoformat()} ({dte}DTE)"


# -----------------------------------------------------------------------
# SPX Options trading sessions (ET) — nearly 24-hour
# -----------------------------------------------------------------------
#   GTH (Global Trading Hours): 8:15 PM  → 9:25 AM next day
#   RTH (Regular Trading Hours): 9:30 AM → 4:15 PM
#   Curb Session:                4:15 PM → 5:00 PM
#   Daily gap / maintenance:     5:00 PM → 8:15 PM  (closed)
#   Sunday open at 8:15 PM ET → Friday close at 5:00 PM ET
#   Expiration day (SPXW): closes at 4:00 PM

# The only closed window on a weekday is 5:00 PM – 8:15 PM ET.
# On Friday the market closes at 5:00 PM; reopens Sunday 8:15 PM.
SPX_OPT_GAP_START = time(17, 0)   # 5:00 PM – daily close
SPX_OPT_GAP_END   = time(20, 15)  # 8:15 PM – GTH reopen


def is_spx_options_open(dt: Optional[datetime] = None) -> bool:
    """
    Return True if SPX options are currently trading.

    Schedule (ET, Mon–Fri):
        GTH  8:15 PM (prev day) → 9:25 AM
        RTH  9:30 AM → 4:15 PM
        Curb 4:15 PM → 5:00 PM
        GAP  5:00 PM → 8:15 PM  ← only closed window
    Sunday: opens at 8:15 PM.
    Saturday: closed all day.
    """
    if dt is None:
        dt = now_et()
    else:
        dt = dt.astimezone(ET)

    wd = dt.weekday()   # Mon=0 … Sun=6
    t  = dt.time()

    # Saturday — always closed
    if wd == 5:
        return False

    # Sunday — open only at/after 8:15 PM
    if wd == 6:
        return t >= SPX_OPT_GAP_END

    # Monday–Friday: closed only during the daily gap 5:00 PM – 8:15 PM
    if SPX_OPT_GAP_START <= t < SPX_OPT_GAP_END:
        return False

    return True


# Keep backward-compatible alias used in server.py
is_cboe_options_open = is_spx_options_open


def last_trading_date(ref: Optional[date] = None) -> date:
    """
    Return the most recent regular-session trading date (Mon–Fri, excluding
    the current day if the session hasn't started yet).
    Doesn't account for market holidays — just weekdays.
    """
    if ref is None:
        now = now_et()
        d = now.date()
        # If before market open today, treat yesterday as last session
        if now.time() < RTH_OPEN:
            d -= timedelta(days=1)
    else:
        d = ref

    # Walk backwards to the last weekday
    while d.weekday() >= 5:   # Sat=5, Sun=6
        d -= timedelta(days=1)

    return d
