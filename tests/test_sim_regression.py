# tests/test_sim_regression.py
"""Bit-identical chain for the smile-dynamics work (spec 2026-09-05 §8).

Chains: legacy -> A(off) -> AB(gamma=0) -> ABC(budget off). Each gate task adds its
npz + test; every link must hold with np.array_equal (exact float equality), not
allclose. Baselines are captured by tests/fixtures/generate_sim_baseline.py.
"""
import os

import numpy as np

from sim_calibrate import calibrate
from sim_config import SimRunConfig
from sim_data import load_bars
from sim_engine import run_entry, run_exits
from sim_paths import simulate_chunk
from strategy_models import Condition, ExitRules, StopLoss, Strategy

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "sim_bars_5m.csv")
LADDER = np.arange(5100.0, 6900.0 + 2.5, 5.0)

LEGACY = {}   # neutral dials are the dataclass defaults; extended by gate tasks


def _strategy():
    return Strategy(
        name="T", direction="bull_put",
        conditions=[
            Condition(kind="short_delta", params={"min": 0.05, "max": 0.60}),
            Condition(kind="spread_width", params={"min": 5, "max": 50}),
            Condition(kind="credit", params={"min": 0.05}),
            Condition(kind="entry_window", params={"start": "09:35", "end": "14:00"}),
        ],
        exit_rules=ExitRules(stop_loss=StopLoss(multiplier=6.0)), budget=None)


def _cell(dials=None):
    """Deterministic pipeline slice: calibrate -> paths -> entry -> exits."""
    cfg = SimRunConfig(strategy_name="T", source="csv", csv_path=FIXTURE,
                       bar_size="5m", n_paths=60, seed=42)
    for k, v in (dials or {}).items():
        setattr(cfg, k, v)
    cfg.validate()
    bars = load_bars(cfg)
    model = calibrate(bars, cfg)
    spot0 = float(bars.closes[-1])
    paths = simulate_chunk(model, cfg, spot0, cfg.n_paths,
                           np.random.SeedSequence(entropy=cfg.seed))
    entry = run_entry(model, cfg, _strategy(), paths, LADDER)
    trials = run_exits(model, cfg, _strategy(), paths, LADDER, entry)
    return entry, trials


def _npz(tag):
    return np.load(os.path.join(os.path.dirname(__file__), "fixtures",
                                f"sim_baseline_{tag}.npz"))


def _assert_cell_matches(z, entry, trials):
    assert np.array_equal(entry.entered, z["entered"])
    assert np.array_equal(entry.entry_minute, z["entry_minute"])
    assert np.array_equal(entry.short_idx, z["short_idx"])
    assert np.array_equal(entry.long_idx, z["long_idx"])
    assert np.array_equal(entry.fill_credit, z["fill"])
    assert np.array_equal(np.array([t.pnl for t in trials]), z["pnl"])
    if not np.isnan(z["mtm0"]).all():
        mtm = next(t.mtm for t in trials if t.mtm is not None)
        assert np.array_equal(mtm, z["mtm0"])


def test_legacy_baseline_unchanged():
    _assert_cell_matches(_npz("legacy"), *_cell(LEGACY))


A_STATE = {"skew_beta": 1.0, "skew_t_gamma": 0.0, "atm_budget": False}


def test_phase_A_activated_baseline_unchanged():
    """Gate A: skew_beta=1.0 outputs are pinned; Task 8 re-links to this npz."""
    _assert_cell_matches(_npz("A"), *_cell(A_STATE))


AB_STATE = {"skew_beta": 1.0, "skew_t_gamma": 0.4, "atm_budget": False}


def test_phase_B_gamma0_matches_phase_A():
    """Gate B chain link: gamma=0 must reproduce the Phase-A baseline exactly."""
    _assert_cell_matches(_npz("A"),
                         *_cell({"skew_beta": 1.0, "skew_t_gamma": 0.0,
                                 "atm_budget": False}))


def test_phase_B_activated_baseline_unchanged():
    _assert_cell_matches(_npz("AB"), *_cell(AB_STATE))
