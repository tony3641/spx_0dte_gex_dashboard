import os, pytest
from strategy_models import Strategy, Condition, ExitRules
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
