import pytest
from strategy_models import Strategy, Condition, ExitRules, TakeProfit, StopLoss


def test_strategy_roundtrips():
    s = Strategy(
        name="opening-bull-put",
        direction="bull_put",
        conditions=[
            Condition(kind="entry_window", enabled=True, params={"start": "09:32", "end": "11:00"}),
            Condition(kind="short_delta", enabled=True, params={"min": 0.05, "max": 0.35}),
        ],
        exit_rules=ExitRules(take_profit=TakeProfit(mode="pct_credit", value=0.5),
                             stop_loss=StopLoss(multiplier=5.0),
                             hold_to_expire=False),
        auto_execute=False,
    )
    d = s.to_dict()
    s2 = Strategy.from_dict(d)
    assert s2.to_dict() == d
    assert s2.direction == "bull_put"
    assert isinstance(s2.exit_rules.take_profit, TakeProfit)
    assert s2.exit_rules.stop_loss.multiplier == 5.0


def test_invalid_direction_rejected():
    with pytest.raises(ValueError):
        Strategy.from_dict({"name": "x", "direction": "iron_condor", "conditions": [],
                            "exit_rules": {"take_profit": None, "stop_loss": None, "hold_to_expire": False}})


def test_condition_passthrough():
    c = Condition(kind="trend", params={"indicator": "rsi", "period": 14, "op": "range", "low": 30, "high": 55})
    assert Condition.from_dict(c.to_dict()).params["op"] == "range"


def test_budget_roundtrips_as_number():
    s = Strategy(name="x", direction="bull_put", conditions=[], budget=25000.0)
    d = s.to_dict()
    assert d["budget"] == 25000.0
    s2 = Strategy.from_dict(d)
    assert s2.budget == 25000.0


def test_budget_defaults_to_none():
    s = Strategy(name="x", direction="bull_put", conditions=[])
    assert s.budget is None
    assert "budget" in s.to_dict() and s.to_dict()["budget"] is None
    assert Strategy.from_dict(s.to_dict()).budget is None


def test_budget_negative_rejected():
    with pytest.raises(ValueError):
        Strategy.from_dict({"name": "x", "direction": "bull_put", "conditions": [],
                            "exit_rules": {"take_profit": None, "stop_loss": None, "hold_to_expire": False},
                            "budget": -100})
