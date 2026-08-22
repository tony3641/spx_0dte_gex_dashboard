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


# ---------------------------------------------------------------------------
# Schedule fields: run_days, short_day_enabled, run_on_fomc, run_on_nfp
# ---------------------------------------------------------------------------

def _base_dict(**overrides):
    d = {"name": "x", "direction": "bull_put", "conditions": [],
         "exit_rules": {"take_profit": None, "stop_loss": None, "hold_to_expire": False}}
    d.update(overrides)
    return d


def test_schedule_fields_roundtrip():
    s = Strategy(name="x", direction="bull_put", conditions=[],
                 run_days=[0, 2, 4], short_day_enabled=True, run_on_fomc=False, run_on_nfp=False)
    d = s.to_dict()
    assert d["run_days"] == [0, 2, 4]
    assert d["short_day_enabled"] is True
    assert d["run_on_fomc"] is False
    assert d["run_on_nfp"] is False
    s2 = Strategy.from_dict(d)
    assert s2.run_days == [0, 2, 4]
    assert s2.short_day_enabled is True
    assert s2.run_on_fomc is False
    assert s2.run_on_nfp is False


def test_schedule_fields_default_when_absent():
    s = Strategy.from_dict(_base_dict())
    assert s.run_days == [0, 1, 2, 3, 4]
    assert s.short_day_enabled is False
    assert s.run_on_fomc is True
    assert s.run_on_nfp is True


def test_run_days_list_not_mutated():
    """Default run_days must be a fresh list, not a shared constant."""
    a = Strategy(name="a", direction="bull_put", conditions=[])
    a.run_days.clear()
    b = Strategy(name="b", direction="bull_put", conditions=[])
    assert b.run_days == [0, 1, 2, 3, 4]


def test_run_days_invalid_rejected():
    with pytest.raises(ValueError):
        Strategy.from_dict(_base_dict(run_days=[]))
    with pytest.raises(ValueError):
        Strategy.from_dict(_base_dict(run_days=[1, 6]))  # 6 = Sunday, out of range
    with pytest.raises(ValueError):
        Strategy.from_dict(_base_dict(run_days=["a"]))


from strategy_models import TriggerSpec, RuntimeState, TRIGGER_KINDS, TRIGGER_LOGIC


def test_trigger_spec_roundtrips():
    t = TriggerSpec(kind="time_of_day", enabled=True, params={"start": "13:00", "end": "15:00"})
    assert TriggerSpec.from_dict(t.to_dict()).to_dict() == t.to_dict()


def test_child_strategy_roundtrip_with_triggers():
    s = Strategy(
        name="followup", direction="bull_put", conditions=[],
        parent_name="master",
        subsequent_triggers=[TriggerSpec(kind="parent_exit_reason", params={"reason": "stop_loss"})],
        trigger_logic="any",
    )
    d = s.to_dict()
    assert d["parent_name"] == "master"
    assert d["subsequent_triggers"][0]["kind"] == "parent_exit_reason"
    assert d["trigger_logic"] == "any"
    s2 = Strategy.from_dict(d)
    assert s2.parent_name == "master"
    assert s2.subsequent_triggers[0].kind == "parent_exit_reason"
    assert s2.trigger_logic == "any"


def test_child_strategy_defaults_when_absent():
    d = {"name": "x", "direction": "bull_put", "conditions": [],
         "exit_rules": {"take_profit": None, "stop_loss": None, "hold_to_expire": False}}
    s = Strategy.from_dict(d)
    assert s.parent_name == ""
    assert s.subsequent_triggers == []
    assert s.trigger_logic == "any"


def test_invalid_trigger_logic_rejected():
    d = {"name": "x", "direction": "bull_put", "conditions": [],
         "exit_rules": {"take_profit": None, "stop_loss": None, "hold_to_expire": False},
         "trigger_logic": "or"}
    with pytest.raises(ValueError):
        Strategy.from_dict(d)


def test_invalid_trigger_kind_rejected():
    d = {"name": "x", "direction": "bull_put", "conditions": [],
         "exit_rules": {"take_profit": None, "stop_loss": None, "hold_to_expire": False},
         "subsequent_triggers": [{"kind": "parent_price", "enabled": True, "params": {}}]}
    with pytest.raises(ValueError):
        Strategy.from_dict(d)


def test_runtime_state_defaults():
    rt = RuntimeState()
    assert rt.cycle == 0
    assert rt.entered is False
    assert rt.done is False
    assert rt.trade is None
    assert rt.time_met is False
    assert rt.parent_cycle == 0
