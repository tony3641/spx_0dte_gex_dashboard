from app_state import create_app_state
from strategy_models import Strategy, Condition
from discord_bot import (
    is_authorized, account_view, positions_view, orders_view, status_view,
    strategies_view, candidates_view, arm_strategy, disarm_strategy,
    set_kill_switch, place_refusal,
)


def _cand_state():
    state = create_app_state()
    state.chain_quotes_cache = {"strikes": [
        {"strike": 5100, "put_bid": 1.0, "put_ask": 1.4, "put_delta": -0.20,
         "put_iv": 21.0},
        {"strike": 5200, "put_bid": 3.0, "put_ask": 3.4, "put_delta": -0.30,
         "put_iv": 19.0},
        {"strike": 5300, "put_bid": 6.0, "put_ask": 6.6, "put_delta": -0.80,
         "put_iv": 23.0},
    ], "spot_price": 5200.0}
    strat = Strategy(name="bp", direction="bull_put", budget=10000, conditions=[
        Condition(kind="short_delta", params={"min": 0.1, "max": 0.4}),
        Condition(kind="spread_width", params={"min": 50, "max": 200}),
    ])
    strat.armed = True
    state.strategies["bp"] = strat
    return state, strat


def test_is_authorized_user_ids():
    assert is_authorized(123, [123, 456], "", [], []) is True
    assert is_authorized(999, [123, 456], "", [], []) is False


def test_is_authorized_role_name_case_insensitive():
    role_names = ["Trader", "Mod"]
    assert is_authorized(999, [], "trader", role_names, []) is True
    assert is_authorized(999, [], "admin", role_names, []) is False


def test_is_authorized_role_id():
    # allowed_role "77" matches role id 77 (as str)
    assert is_authorized(999, [], "77", [], [77]) is True


def test_account_view_builds_payload():
    state = create_app_state()
    state.account_summary = {"NetLiquidation": 100000.0}
    state.positions = [{"contract": {"symbol": "SPX"}, "position": -1}]
    state.open_orders = []
    state.executions = []
    view = account_view(state)
    assert view["summary"]["NetLiquidation"] == 100000.0
    assert len(view["positions"]) == 1


def test_positions_view_filter():
    state = create_app_state()
    state.positions = [
        {"contract": {"symbol": "SPX", "localSymbol": "SPXW"}, "position": -1},
        {"contract": {"symbol": "ES", "localSymbol": ""}, "position": 2},
    ]
    assert positions_view(state)["count"] == 2
    assert positions_view(state, "spx")["count"] == 1


def test_orders_view():
    state = create_app_state()
    state.open_orders = [{"orderId": 1, "status": "Submitted"}]
    assert orders_view(state)["count"] == 1


def test_status_view_keys():
    state = create_app_state()
    state.connected = True
    state.spx_price = 6100.5
    state.vix = 14.2
    view = status_view(state)
    for k in ("connected", "market_status", "expiration", "data_mode", "gex_mode", "spot", "vix"):
        assert k in view
    assert view["spot"] == 6100.5


def test_strategies_view_single_and_all():
    state, strat = _cand_state()
    single = strategies_view(state, "bp")
    assert single["ok"] is True and single["name"] == "bp"
    allv = strategies_view(state)
    assert allv["ok"] is True and len(allv["strategies"]) == 1


def test_candidates_view_returns_sized_rows():
    state, strat = _cand_state()
    from strategy_engine import build_candidate_views
    state.strategy_candidates["bp"] = build_candidate_views(strat, state)
    view = candidates_view(state, "bp")
    assert view["ok"] is True and view["count"] > 0
    assert view["rows"][0]["direction"] == "bull_put"
    assert all("size" in r and "total_credit" in r for r in view["rows"])


def test_arm_disarm_and_killswitch():
    state, strat = _cand_state()
    armed = arm_strategy(state, "bp")
    assert armed["ok"] is True and armed["armed"] is True and armed["auto_execute"] is False
    state.strategies["bp"].armed = True
    dis = disarm_strategy(state, "bp")
    assert dis["ok"] is True and dis["armed"] is False
    assert set_kill_switch(state, True) is True
    assert state.auto_trade_kill_switch is True
    assert set_kill_switch(state, False) is False


def test_arm_missing_strategy():
    state = create_app_state()
    out = arm_strategy(state, "nope")
    assert out["ok"] is False


def test_place_refusal_never_orders():
    state, strat = _cand_state()
    out = place_refusal(state, "bp", 0)
    assert out["ok"] is False
    assert "web" in out["message"].lower()
