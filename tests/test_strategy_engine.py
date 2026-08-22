import pytest
from strategy_models import Strategy, Condition
from strategy_engine import generate_candidates


def _rows():
    return {"strikes": [
        {"strike": 5100, "put_bid": 1.0, "put_ask": 1.4, "put_delta": -0.70,
         "call_bid": 9.0, "call_ask": 9.6, "call_delta": 0.60, "call_iv": 20.0, "put_iv": 21.0},
        {"strike": 5200, "put_bid": 3.0, "put_ask": 3.4, "put_delta": -0.30,
         "call_bid": 4.0, "call_ask": 4.6, "call_delta": 0.30, "call_iv": 18.0, "put_iv": 19.0},
        {"strike": 5300, "put_bid": 6.0, "put_ask": 6.6, "put_delta": -0.10,
         "call_bid": 2.0, "call_ask": 2.6, "call_delta": 0.10, "call_iv": 22.0, "put_iv": 23.0},
    ], "spot_price": 5200.0, "annual_vol": 0.2}


def test_bear_call_candidates():
    strat = Strategy(name="bc", direction="bear_call", conditions=[
        Condition(kind="short_delta", params={"min": 0.2, "max": 0.4}),
        Condition(kind="spread_width", params={"min": 50, "max": 150}),
    ])
    state = type("S", (), {"chain_quotes_cache": _rows(), "account_summary": {}})()
    cands = generate_candidates(strat, state)
    assert cands
    c = cands[0]
    assert c.direction == "bear_call"
    assert c.short_strike == 5200 and c.long_strike == 5300
    assert c.width_points == 100 and c.margin == 10000
    assert c.credit_mid > 0
    assert c.credit_mid == pytest.approx((4.3 - 2.3), abs=0.01)
