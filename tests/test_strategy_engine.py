import pytest
from strategy_models import Strategy, Condition
from strategy_engine import generate_candidates, evaluate_conditions


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


def _closes():
    return [100.0 + 0.2 * i for i in range(20)]  # gently rising


def _state(vix=15.0, spot=5200.0):
    rows = {"strikes": [
        {"strike": 5100, "put_bid": 1.0, "put_ask": 1.4, "put_delta": -0.70, "put_iv": 21.0,
         "call_bid": 9.0, "call_ask": 9.6, "call_delta": 0.30, "call_iv": 20.0},
        {"strike": 5200, "put_bid": 3.0, "put_ask": 3.4, "put_delta": -0.30, "put_iv": 19.0,
         "call_bid": 4.0, "call_ask": 4.6, "call_delta": 0.30, "call_iv": 18.0},
        {"strike": 5300, "put_bid": 6.0, "put_ask": 6.6, "put_delta": -0.10, "put_iv": 23.0,
         "call_bid": 2.0, "call_ask": 2.6, "call_delta": 0.10, "call_iv": 22.0},
    ], "spot_price": spot}
    class S: pass
    s = S()
    s.chain_quotes_cache = rows
    s.spx_price = spot
    s.vix = vix
    s.price_history = [{"close": c} for c in _closes()]
    s.account_summary = {}
    return s


def _swe(d="bull_put"):
    return Strategy(name="t", direction=d, conditions=[
        Condition(kind="entry_window", params={"start": "09:32", "end": "11:00"}),
        Condition(kind="volatility", params={"vix_enabled": True, "vix_op": "above", "vix_value": 14.0,
                                             "atm_iv_enabled": False}),
        Condition(kind="spread_width", params={"min": 50, "max": 150}),
        Condition(kind="short_delta", params={"min": 0.2, "max": 0.4}),
    ])


def test_fail_window_blocker(monkeypatch):
    import datetime as dt
    from strategy_engine import evaluate_conditions
    now = dt.datetime(2026, 8, 21, 12, 30)
    ev = evaluate_conditions(_swe(), _state(), now=now)
    assert ev.status == "blocked"
    assert ev.blocker == "entry_window"


def test_all_pass_ready():
    import datetime as dt
    now = dt.datetime(2026, 8, 21, 10, 0)
    ev = evaluate_conditions(_swe(), _state(vix=20.0), now=now)
    assert ev.status == "ready"
    assert ev.candidates


def test_vix_blocks_before_candidates():
    import datetime as dt
    now = dt.datetime(2026, 8, 21, 10, 0)
    ev = evaluate_conditions(_swe(), _state(vix=10.0), now=now)
    assert ev.blocker == "volatility"
    assert ev.candidates == []


import pytest, asyncio
from strategy_engine import place_strategy_entry, _build_entry_payload


def _state_t8(spot=5200.0):
    rows = {"strikes": [
        {"strike": 5100, "put_bid": 1.0, "put_ask": 1.4, "put_delta": -0.70, "put_iv": 21.0,
         "call_bid": 9.0, "call_ask": 9.6, "call_delta": 0.30, "call_iv": 20.0},
        {"strike": 5200, "put_bid": 3.0, "put_ask": 3.4, "put_delta": -0.30, "put_iv": 19.0,
         "call_bid": 4.0, "call_ask": 4.6, "call_delta": 0.30, "call_iv": 18.0},
        {"strike": 5300, "put_bid": 6.0, "put_ask": 6.6, "put_delta": -0.10, "put_iv": 23.0,
         "call_bid": 2.0, "call_ask": 2.6, "call_delta": 0.10, "call_iv": 22.0},
    ], "spot_price": spot}
    class S: pass
    s = S()
    s.chain_quotes_cache = rows
    s.spx_price = spot
    s.expiration = "20260821"
    s.positions = []
    return s


def test_build_entry_payload_credit_negative():
    strat = Strategy(name="t", direction="bear_call", conditions=[Condition(kind="short_delta", params={"min": 0.2, "max": 0.4})])
    cand = type("C", (), {"direction": "bear_call", "short_strike": 5200.0, "long_strike": 5300.0,
                          "credit_mid": 2.0})()
    p = _build_entry_payload(strat, cand, _state_t8())
    assert p["comboLmtPrice"] < 0
    assert p["legs"][0]["action"] == "SELL"
    assert p["legs"][1]["action"] == "BUY"


@pytest.mark.asyncio
async def test_place_strategy_entry_via_mock(mock_ib, app_state):
    from strategy_engine import _build_entry_payload, place_strategy_entry, Candidate
    app_state.expiration = "20260821"
    strat = Strategy(name="t", direction="bear_call", conditions=[Condition(kind="short_delta", params={"min": 0.2, "max": 0.4})])
    cand = Candidate(direction="bear_call", short_strike=5200.0, long_strike=5300.0, width_points=100.0,
                     margin=10000.0, credit_bid=1.8, credit_ask=2.2, credit_mid=2.0,
                     short_delta=0.3, long_delta=0.1, atm_iv=18.0)
    payload = _build_entry_payload(strat, cand, app_state)
    resp = await place_strategy_entry(mock_ib, app_state, strat, cand)
    assert resp is not None
    assert len(app_state.strategy_log) == 1
    assert len(mock_ib.get_placed_orders()) == 1
