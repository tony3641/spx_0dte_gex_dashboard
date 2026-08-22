import pytest
from condition_helpers import (
    wilder_rsi, percent_change, atm_iv, nearest_row,
    spread_width, spread_margin, combo_credit,
)


def test_wilder_rsi_all_gains_is_100():
    closes = [100 + i for i in range(20)]      # strictly rising
    assert wilder_rsi(closes, 14) == 100.0


def test_wilder_rsi_not_enough_data():
    assert wilder_rsi([1.0, 2.0], 14) is None


def test_percent_change():
    closes = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0]
    assert round(percent_change(closes, 5), 4) == 5.0   # 105 vs 100


def test_percent_change_short():
    assert percent_change([10.0, 11.0], 5) is None


def test_nearest_row_and_atm_iv():
    rows = [
        {"strike": 5100, "call_iv": 20.0, "put_iv": 21.0},
        {"strike": 5200, "call_iv": 18.0, "put_iv": 19.0},
        {"strike": 5300, "call_iv": 22.0, "put_iv": 23.0},
    ]
    assert nearest_row(rows, 5210)["strike"] == 5200
    assert atm_iv(rows, 5210) == 18.5


def test_width_and_margin():
    assert spread_width("bull_put", 5200, 5100) == 100.0
    assert spread_width("bear_call", 5200, 5300) == 100.0
    assert spread_margin(25) == 2500.0


def test_combo_credit():
    short = {"strike": 5200, "put_bid": 3.0, "put_ask": 3.4}
    long = {"strike": 5100, "put_bid": 1.0, "put_ask": 1.4}
    c = combo_credit(short, long, "P")
    assert c["mid"] == pytest.approx((3.2 - 1.2), abs=1e-9)   # 2.0
    assert c["bid"] == pytest.approx(3.0 - 1.4, abs=1e-9)     # 1.6 conservative
