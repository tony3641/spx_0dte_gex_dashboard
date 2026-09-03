# tests/test_sim_engine.py
import numpy as np

from sim_calibrate import CalibratedModel, DEFAULT_SMILE, GarchParams
from sim_config import SimRunConfig
from sim_engine import EntryState, extract_conditions, run_entry, window_minutes
from sim_paths import SimPaths
from strategy_models import Condition, ExitRules, StopLoss, Strategy


def _strategy(window=("09:35", "10:00")):
    return Strategy(
        name="T", direction="bull_put",
        conditions=[
            Condition(kind="short_delta", params={"min": 0.30, "max": 0.45}),
            Condition(kind="spread_width", params={"min": 40, "max": 65}),
            Condition(kind="credit", params={"min": 3.0}),
            Condition(kind="entry_window", params={"start": window[0], "end": window[1]}),
        ],
        exit_rules=ExitRules(stop_loss=StopLoss(multiplier=6.0)), budget=None)


def _model():
    return CalibratedModel(
        garch=GarchParams(omega=2e-10, alpha=0.05, gamma=0.10, beta=0.85, nu=6.0, converged=True),
        ushape=np.ones(78), sigma0=0.0005, smile=DEFAULT_SMILE, vix0=15.0, source="test")


def _paths(spot=6000.0, n=30, steps=78):
    rng = np.random.default_rng(0)
    spots = np.full((n, steps), spot) + rng.normal(0, 2.0, (n, steps)).cumsum(axis=1) * 0.1
    sigmas = np.full((n, steps), 0.0005)
    return SimPaths(spots=spots, sigmas=sigmas)


def test_window_minutes_maps_bars():
    cfg = SimRunConfig(strategy_name="T", bar_size="5m")
    assert window_minutes(_strategy(), 78, 300) == (0, 5)   # 09:35..10:00 closes = bars 0..5


def test_extract_conditions_defaults():
    d = extract_conditions(_strategy())
    assert d["dmin"] == 0.30 and d["dmax"] == 0.45
    assert d["wmin"] == 40 and d["wmax"] == 65
    assert d["cmin"] == 3.0 and d["cmax"] == float("inf")   # credit has only min in JSON
    assert d["vix"] is None and d["trend"] is None


def test_run_entry_enters_in_window_with_valid_geometry():
    cfg = SimRunConfig(strategy_name="T", bar_size="5m")
    strat, model, paths = _strategy(), _model(), _paths()
    ladder = np.arange(5100.0, 6900.0 + 2.5, 5.0)
    es = run_entry(model, cfg, strat, paths, ladder)
    assert es.entered.shape == (30,)
    assert es.entered.sum() > 0
    ent = es.entered.astype(bool)
    assert (es.entry_minute[ent] >= 0).all() and (es.entry_minute[ent] <= 5).all()
    s_idx, l_idx = es.short_idx[ent], es.long_idx[ent]
    spot_at_entry = paths.spots[np.arange(30), es.entry_minute][ent]
    assert (ladder[s_idx] < spot_at_entry).all()              # shorts below spot (puts)
    widths = (s_idx - l_idx) * 5.0
    assert ((widths >= 40) & (widths <= 65)).all()
    assert ((es.theo_credit[ent] >= 0.30)).all()
    assert (es.fill_credit[ent] <= es.theo_credit[ent] + 1e-9).all()
    assert (es.fill_credit[ent] > 0).all()
    assert (es.qty[ent] == 1).all()                            # budget None -> 1


def test_run_entry_dynamic_k_mode():
    cfg = SimRunConfig(strategy_name="T", bar_size="5m", strike_mode="dynamic_k",
                       dynamic_k_values=[0.5], width_points=50.0)
    strat, model, paths = _strategy(), _model(), _paths()
    ladder = np.arange(5100.0, 6900.0 + 2.5, 5.0)
    es = run_entry(model, cfg, strat, paths, ladder, k=0.5)
    ent = es.entered.astype(bool)
    assert ent.all()                                            # dynamic mode: window is the only gate
    assert (es.entry_minute == 0).all()                         # first window minute
    assert ((es.short_idx - es.long_idx) * 5.0 == 50.0).all()


def test_run_entry_respects_per_path_start():
    cfg = SimRunConfig(strategy_name="T", bar_size="5m")
    strat, model, paths = _strategy(), _model(), _paths()
    ladder = np.arange(5100.0, 6900.0 + 2.5, 5.0)
    start = np.full(30, 4)                                      # family-style late start
    es = run_entry(model, cfg, strat, paths, ladder, per_path_start=start)
    ent = es.entered.astype(bool)
    assert ent.sum() > 0 and (es.entry_minute[ent] >= 4).all()


def test_run_entry_engine_mode_budget_gates_qty():
    cfg = SimRunConfig(strategy_name="T", bar_size="5m")
    strat, model, paths = _strategy(), _model(), _paths()
    strat.budget = 100.0                                       # below every spread margin (>= 40*100)
    ladder = np.arange(5100.0, 6900.0 + 2.5, 5.0)
    es = run_entry(model, cfg, strat, paths, ladder)
    assert es.entered.sum() == 0                               # cannot afford -> never marked entered
    assert (es.qty == 0).all()                                 # no qty==0 "entered" records
