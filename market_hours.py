"""
Market hours detection and expiration finder for SPX/SPXW options.
Handles trading-hour vs off-hour logic and finds the next available 0DTE expiration.
"""

from datetime import datetime, date, timedelta, time
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo
from config import RTH_OPEN, RTH_CLOSE, SPXW_CEASE, SPX_OPT_GAP_START, SPX_OPT_GAP_END, FOMC_DATES

ET = ZoneInfo("US/Eastern")

# Close-settled (PM) SPX trading classes. AM-settled series are excluded.
PM_SETTLE_CLASSES = {"SPX", "SPXW"}

# Cache the NYSE calendar handle (building it is expensive).
_NYSE_CAL = None


def _get_nyse_calendar():
    global _NYSE_CAL
    if _NYSE_CAL is None:
        import exchange_calendars as xcals
        _NYSE_CAL = xcals.get_calendar("XNYS")
    return _NYSE_CAL


def now_et() -> datetime:
    """Current datetime in US/Eastern."""
    return datetime.now(ET)


def is_weekday(dt: date) -> bool:
    return dt.weekday() < 5  # Mon=0 .. Fri=4


def is_short_trading_day(ref: Optional[date] = None) -> bool:
    """True when ``ref`` (or today) is an early-close (half) session.

    Detected via the NYSE exchange calendar: a session whose market close is
    earlier than the normal 4:00 PM SPX close. Non-sessions (weekends/holidays)
    return False.
    """
    d = ref or now_et().date()
    cal = _get_nyse_calendar()
    try:
        if not cal.is_session(d):
            return False
        close_et = cal.session_close(d).astimezone(ET)
        return close_et.time() < SPXW_CEASE
    except Exception:
        return False


def is_nfp_day(ref: Optional[date] = None) -> bool:
    """True when ``ref`` (or today) is the first Friday of its month (NFP / jobs report)."""
    d = ref or now_et().date()
    first = date(d.year, d.month, 1)
    offset = (4 - first.weekday()) % 7  # 4 = Friday
    first_friday = first + timedelta(days=offset)
    return d == first_friday


def is_fomc_day(ref: Optional[date] = None) -> bool:
    """True when ``ref`` (or today) is a scheduled FOMC meeting date."""
    d = ref or now_et().date()
    return d.isoformat() in FOMC_DATES


def is_pm_settle(trading_class: str) -> bool:
    """True only for close-settled (PM) SPX classes; AM-settled series return False."""
    return trading_class in PM_SETTLE_CLASSES


def resolve_trading_expiration(state, ref: Optional[date] = None) -> Optional[Tuple[str, str]]:
    """Pick today's tradeable PM expiration, preferring SPXW then SPX (monthly).

    Returns (expiration, trading_class) or None when today has no expiring option
    (i.e. a market holiday / no 0DTE), so callers can skip the day entirely.
    """
    d = ref or now_et().date()
    today_str = d.strftime("%Y%m%d")
    if today_str in (getattr(state, "expirations", None) or []):
        return (today_str, "SPXW")
    if today_str in (getattr(state, "monthly_expirations", None) or []):
        return (today_str, "SPX")
    return None


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
