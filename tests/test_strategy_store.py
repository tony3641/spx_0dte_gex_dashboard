import os, pytest
from strategy_models import Strategy, Condition, ExitRules, TriggerSpec
from strategy_store import load_strategies, save_strategy, delete_strategy


def _s(name="a"):
    return Strategy(name=name, direction="bull_put", conditions=[Condition(kind="short_delta", params={"min": 0.1, "max": 0.3})])


def test_save_and_load_roundtrip(tmp_path):
    p = str(tmp_path / "strategies.json")
    save_strategy(p, _s("a"))
    out = load_strategies(p)
    assert out["a"].direction == "bull_put"
    assert out["a"].conditions[0].kind == "short_delta"


def test_delete(tmp_path):
    p = str(tmp_path / "strategies.json")
    save_strategy(p, _s("a"))
    save_strategy(p, _s("b"))
    delete_strategy(p, "a")
    assert set(load_strategies(p)) == {"b"}


def test_malformed_file_is_safe(tmp_path):
    p = str(tmp_path / "strategies.json")
    with open(p, "w") as f:
        f.write("{not valid json")
    assert load_strategies(p) == {}


from strategy_store import validate_strategy_tree


def _child(name, parent, enabled=True, logic="any"):
    return Strategy(name=name, direction="bull_put", conditions=[],
                    parent_name=parent,
                    subsequent_triggers=[TriggerSpec(kind="parent_exit_reason",
                                                     params={"reason": "stop_loss"},
                                                     enabled=enabled)],
                    trigger_logic=logic)


def test_validate_accepts_clean_tree():
    strat = {"master": Strategy(name="master", direction="bull_put", conditions=[]),
             "child": _child("child", "master")}
    assert set(validate_strategy_tree(strat)) == {"master", "child"}


def test_validate_drops_orphan_child():
    strat = {"child": _child("child", "nowhere")}
    assert validate_strategy_tree(strat) == {}


def test_validate_drops_child_with_no_enabled_trigger():
    strat = {"master": Strategy(name="master", direction="bull_put", conditions=[]),
             "child": _child("child", "master", enabled=False)}
    assert set(validate_strategy_tree(strat)) == {"master"}


def test_validate_drops_cycle():
    a = _child("a", "b")
    b = _child("b", "a")
    assert validate_strategy_tree({"a": a, "b": b}) == {}


def test_load_applies_validation(tmp_path):
    # A saved child whose parent is deleted on disk should not survive load.
    p = tmp_path / "strategies.json"
    from strategy_store import save_strategies
    save_strategies(str(p), {
        "master": Strategy(name="master", direction="bull_put", conditions=[]),
        "orphan": _child("orphan", "gone"),
    })
    assert set(load_strategies(str(p))) == {"master"}
