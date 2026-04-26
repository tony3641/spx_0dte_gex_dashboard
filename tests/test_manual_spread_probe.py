"""Focused unit tests for the standalone manual spread probe."""

import sys
import os
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from market_hours import ET
from tests.manual_spread_probe import (
    ACCEPTED_SERVER_STATUSES,
    LegQuote,
    SpreadScenario,
    accepted_status_from_result,
    build_payload,
    next_rth_open,
)


def test_next_rth_open_from_weekday_gth_morning():
    ref = datetime(2026, 4, 24, 8, 0, tzinfo=ET)
    target = next_rth_open(ref)
    assert target == datetime(2026, 4, 24, 9, 30, tzinfo=ET)


def test_next_rth_open_from_friday_evening_rolls_to_monday():
    ref = datetime(2026, 4, 24, 18, 0, tzinfo=ET)
    target = next_rth_open(ref)
    assert target == datetime(2026, 4, 27, 9, 30, tzinfo=ET)


def test_build_payload_natural_credit_spread_uses_negative_combo_price():
    scenario = SpreadScenario(
        name="credit_vertical",
        combo_action="SELL",
        lower_strike=5200.0,
        upper_strike=5210.0,
        right="C",
    )
    lower_quote = LegQuote(bid=5.0, ask=5.2, last=5.1, reference=5.1)
    upper_quote = LegQuote(bid=3.0, ask=3.2, last=3.1, reference=3.1)

    payload = build_payload(
        scenario,
        "20260424",
        1,
        lower_quote,
        upper_quote,
        False,
        "natural",
    )

    assert payload["legs"][0]["action"] == "SELL"
    assert payload["legs"][1]["action"] == "BUY"
    assert payload["legs"][0]["lmtPrice"] == 5.0
    assert payload["legs"][1]["lmtPrice"] == 3.2
    assert payload["comboLmtPrice"] == -1.8


def test_accepted_status_from_result_only_allows_submitted_or_filled():
    result = {"data": {"status": "ApiPending"}}
    observations = [{"status": "PendingSubmit"}, {"status": "Submitted"}]
    snapshot = {"status": "Cancelled"}

    accepted = accepted_status_from_result(result, observations, snapshot)

    assert accepted == "Submitted"
    assert accepted in ACCEPTED_SERVER_STATUSES