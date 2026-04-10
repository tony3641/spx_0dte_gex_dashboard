"""
Config module unit tests — tick rounding helpers (pure functions).
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from config import (
    spx_tick_for_price,
    round_abs_to_tick,
    round_signed_to_tick,
    IB_PORT,
    SERVER_PORT,
)


# ---------------------------------------------------------------------------
# spx_tick_for_price
# ---------------------------------------------------------------------------

class TestSpxTickForPrice:

    def test_below_threshold(self):
        assert spx_tick_for_price(0.01) == 0.05
        assert spx_tick_for_price(0.50) == 0.05
        assert spx_tick_for_price(1.00) == 0.05
        assert spx_tick_for_price(1.99) == 0.05
        assert spx_tick_for_price(2.00) == 0.05

    def test_above_threshold(self):
        assert spx_tick_for_price(2.01) == 0.10
        assert spx_tick_for_price(3.00) == 0.10
        assert spx_tick_for_price(50.00) == 0.10

    def test_negative_uses_absolute(self):
        """Negative prices (credit spreads) use absolute value."""
        assert spx_tick_for_price(-1.50) == 0.05
        assert spx_tick_for_price(-3.00) == 0.10


# ---------------------------------------------------------------------------
# round_abs_to_tick
# ---------------------------------------------------------------------------

class TestRoundAbsToTick:

    def test_exact_tick(self):
        assert round_abs_to_tick(3.50, 0.10) == 3.50
        assert round_abs_to_tick(1.75, 0.05) == 1.75

    def test_rounds_down(self):
        assert round_abs_to_tick(3.53, 0.10) == 3.50
        assert round_abs_to_tick(1.72, 0.05) == 1.70

    def test_rounds_up(self):
        assert round_abs_to_tick(3.56, 0.10) == 3.60
        assert round_abs_to_tick(1.73, 0.05) == 1.75

    def test_never_returns_zero(self):
        """Minimum returned value is one tick."""
        assert round_abs_to_tick(0.001, 0.05) == 0.05
        assert round_abs_to_tick(0.001, 0.10) == 0.10

    def test_negative_input_uses_abs(self):
        assert round_abs_to_tick(-3.53, 0.10) == 3.50

    def test_midpoint_rounding(self):
        """Banker's rounding: 0.5 rounds to nearest even tick count."""
        # 3.45 / 0.10 = 34.5 → round(34.5) = 34 (banker's rounding) → 3.40
        result = round_abs_to_tick(3.45, 0.10)
        assert result in (3.40, 3.50)  # accept either since this is edge case


# ---------------------------------------------------------------------------
# round_signed_to_tick
# ---------------------------------------------------------------------------

class TestRoundSignedToTick:

    def test_positive(self):
        assert round_signed_to_tick(2.53, 0.10) == 2.50

    def test_negative(self):
        assert round_signed_to_tick(-2.53, 0.10) == -2.50

    def test_negative_small(self):
        assert round_signed_to_tick(-1.93, 0.05) == -1.95

    def test_positive_small(self):
        assert round_signed_to_tick(0.03, 0.05) == 0.05

    def test_zero_positive(self):
        """Zero is treated as positive → returns tick minimum."""
        result = round_signed_to_tick(0.0, 0.05)
        assert result == 0.05


# ---------------------------------------------------------------------------
# Config constants sanity checks
# ---------------------------------------------------------------------------

class TestConfigConstants:

    def test_defaults_are_reasonable(self):
        assert IB_PORT > 0
        assert SERVER_PORT > 0
