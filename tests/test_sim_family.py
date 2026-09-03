# tests/test_sim_family.py
import numpy as np

from sim_calibrate import CalibratedModel, DEFAULT_SMILE, GarchParams
from sim_config import SimRunConfig
from sim_engine import run_family
from sim_paths import SimPaths
from strategy_models import (Condition, ExitRules, StopLoss, Strategy, TriggerSpec)


def _model():
    return CalibratedModel(
        garch=GarchParams(omega=2e-10, alpha=0.05, gamma=0.10, beta=0.85, nu=6.0, converged=True),
        ushape=np.ones(78), sigma0=0.0005, smile=DEFAULT_SMILE, vix0=15.0, source="test")


def _parent(mult: float = 6.0):
    # credit is MIN-ONLY {min:3.0}: engine-mode combo credits for a 40-65pt bull put on the
    # 5100-6900 ladder at spot 6000 have an engine-only scale ~9-18pts (Conflict K-b); a
    # {min:0.30,max:0.45} band admits none. mult parameterized so the stop test can use 2.0
    # (K-c: stop_level = fill_credit*2 ~ 37 < 65pt width, so a crash stops the parent).
    return Strategy(name="P", direction="bull_put",
                    conditions=[
                        Condition(kind="short_delta", params={"min": 0.30, "max": 0.45}),
                        Condition(kind="spread_width", params={"min": 40, "max": 65}),
                        Condition(kind="credit", params={"min": 3.0}),
                        Condition(kind="entry_window", params={"start": "09:35", "end": "10:00"}),
                    ],
                    exit_rules=ExitRules(stop_loss=StopLoss(multiplier=mult)), budget=None)


def _child(trigger):
    return Strategy(name="C", direction="bull_put",
                    conditions=[
                        Condition(kind="short_delta", params={"min": 0.05, "max": 0.45}),
                        Condition(kind="spread_width", params={"min": 40, "max": 65}),
                        Condition(kind="credit", params={"min": 0.10}),
                    ],
                    exit_rules=ExitRules(stop_loss=StopLoss(multiplier=6.0)), budget=None,
                    parent_name="P",
                    subsequent_triggers=[trigger])


def _paths(n=6, crash_after=4):
    """Paths 0-1 crash to 5800 (K-g): below the parent's entry strikes (so the parent stops when
    both legs go ITM, mark=width=65 > stop_level 36.6) but still inside the ladder, so the child
    finds an OTM short with delta in [0.05,0.45] when it re-enters. Paths 2-5 stay quiet."""
    rng = np.random.default_rng(1)
    spots = np.full((n, 78), 6000.0)
    spots[:2, crash_after:] = 5800.0
    spots[2:] += rng.normal(0, 1.0, (n - 2, 78)).cumsum(axis=1) * 0.1
    return SimPaths(spots=spots, sigmas=np.full((n, 78), 0.0005))


def test_family_reenters_after_parent_stop():
    cfg = SimRunConfig(strategy_name="P", bar_size="5m")
    ladder = np.arange(5100.0, 6900.0 + 2.5, 5.0)
    child = _child(TriggerSpec(kind="parent_exit_reason",
                               params={"reason": "stop_loss"}))
    results, total = run_family(_model(), cfg, _parent(mult=2.0), [child], _paths(), ladder)
    parent = results["P"]
    stopped = [p for p, r in enumerate(parent) if r.entered and r.exit_reason == "stop"]
    assert stopped == [0, 1]
    child_res = results["C"]
    for p in stopped:
        c = child_res[p]
        assert c.entered, "child must re-enter after a parent stop"
        assert c.entry_minute > parent[p].exit_minute
        assert c.fill_credit > 0
    for p in range(2, 6):
        assert not child_res[p].entered     # no trigger fired on quiet paths


def test_family_total_pnl_sums_all_legs():
    cfg = SimRunConfig(strategy_name="P", bar_size="5m")
    ladder = np.arange(5100.0, 6900.0 + 2.5, 5.0)
    child = _child(TriggerSpec(kind="parent_exit_reason", params={"reason": "stop_loss"}))
    results, total = run_family(_model(), cfg, _parent(), [child], _paths(), ladder)
    # total is per-PATH (n,); expected must be per-path (K-f), NOT a scalar
    n = len(results["P"])
    expected = np.zeros(n)
    for name in results:
        for i, r in enumerate(results[name]):
            expected[i] += r.pnl if r.entered else 0.0
    np.testing.assert_allclose(total, expected, atol=1e-6)


def test_family_unrealized_pnl_trigger():
    cfg = SimRunConfig(strategy_name="P", bar_size="5m")
    ladder = np.arange(5100.0, 6900.0 + 2.5, 5.0)
    child = _child(TriggerSpec(kind="parent_unrealized_pnl", params={"loss_multiple": 0.05}))
    results, total = run_family(_model(), cfg, _parent(), [child], _paths(), ladder)
    # _parent() keeps mult 6.0, so the parent carries the loss through the crash (no stop);
    # the unrealized trigger sees the deeply-negative MTM bar and re-enters the child (K-e)
    assert results["C"][0].entered and results["C"][1].entered


def test_family_all_logic_raises():
    cfg = SimRunConfig(strategy_name="P", bar_size="5m")
    ladder = np.arange(5100.0, 6900.0 + 2.5, 5.0)
    child = _child(TriggerSpec(kind="parent_exit_reason", params={"reason": "stop_loss"}))
    child.trigger_logic = "all"
    import pytest
    with pytest.raises(ValueError):
        run_family(_model(), cfg, _parent(), [child], _paths(), ladder)
