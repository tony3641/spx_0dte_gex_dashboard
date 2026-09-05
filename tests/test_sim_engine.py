# tests/test_sim_engine.py
import numpy as np
import pytest

from sim_calibrate import CalibratedModel, DEFAULT_SMILE, GarchParams, calibrate
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


@pytest.mark.parametrize("bar_size,bar_secs,steps,w0,w1", [
    ("5s", 5, 4680, 59, 359),
    ("15s", 15, 1560, 19, 119),
    ("30s", 30, 780, 9, 59),
])
def test_window_minutes_sub_minute_bar_sizes(bar_size, bar_secs, steps, w0, w1):
    """window_minutes must not divide by zero on sub-minute bars (5s/15s/30s).

    window (09:35, 10:00); w0 = first bar closing at 09:35, w1 = bar closing at 10:00.
    """
    cfg = SimRunConfig(strategy_name="T", bar_size=bar_size)
    assert window_minutes(_strategy(), steps, bar_secs) == (w0, w1)


def test_run_entry_sub_minute_respects_window():
    cfg = SimRunConfig(strategy_name="T", bar_size="30s")
    strat, model, paths = _strategy(), _model(), _paths(n=20, steps=780)
    ladder = np.arange(5100.0, 6900.0 + 2.5, 5.0)
    es = run_entry(model, cfg, strat, paths, ladder)
    ent = es.entered.astype(bool)
    assert ent.sum() > 0
    assert (es.entry_minute[ent] >= 9).all() and (es.entry_minute[ent] <= 59).all()


def _strategy_with(*conds):
    s = _strategy()
    s.conditions = list(s.conditions) + [Condition(kind=k, params=p) for k, p in conds]
    return s


def _ladder():
    return np.arange(5100.0, 6900.0 + 2.5, 5.0)


def test_enabled_trend_condition_raises_not_silently_skipped():
    cfg = SimRunConfig(strategy_name="T", bar_size="5m")
    strat = _strategy_with(("trend", {"indicator": "rsi", "period": 14, "op": "below",
                                      "value": 30.0}))
    with pytest.raises(ValueError, match="trend"):
        run_entry(_model(), cfg, strat, _paths(), _ladder())


def test_enabled_atm_iv_volatility_condition_raises():
    cfg = SimRunConfig(strategy_name="T", bar_size="5m")
    strat = _strategy_with(("volatility", {"atm_iv_enabled": True, "atm_iv_op": "below",
                                           "atm_iv_value": 25.0}))
    with pytest.raises(ValueError, match="atm_iv"):
        run_entry(_model(), cfg, strat, _paths(), _ladder())


def test_vix_only_volatility_condition_still_validates():
    cfg = SimRunConfig(strategy_name="T", bar_size="5m")
    strat = _strategy_with(("volatility", {"vix_enabled": True, "vix_op": "below",
                                           "vix_value": 25.0}))
    es = run_entry(_model(), cfg, strat, _paths(), _ladder())   # must NOT raise
    assert es.entered.shape == (30,)


def test_bear_call_direction_rejected():
    cfg = SimRunConfig(strategy_name="T", bar_size="5m")
    strat = _strategy()
    strat.direction = "bear_call"                    # geometry is bull-put-only; reject loudly
    with pytest.raises(ValueError, match="bull_put"):
        run_entry(_model(), cfg, strat, _paths(), _ladder())


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


from sim_engine import EntryState, run_cell, run_exits


def _entry_state(paths, short_i, long_i, fill=0.30, entry_min=0):
    n = paths.spots.shape[0]
    return EntryState(
        entered=np.ones(n, dtype=bool),
        entry_minute=np.full(n, entry_min, dtype=np.int32),
        short_idx=np.full(n, short_i, dtype=np.int32),
        long_idx=np.full(n, long_i, dtype=np.int32),
        width=np.full(n, (short_i - long_i) * 5.0),
        qty=np.ones(n, dtype=np.int32),
        fill_credit=np.full(n, fill),
        theo_credit=np.full(n, fill))


def test_stop_fill_is_trigger_plus_extra():
    cfg = SimRunConfig(strategy_name="T", bar_size="5m", stop_extra=0.10)
    strat, model = _strategy(), _model()
    paths = _paths(n=2)
    paths.spots[:, :4] = 6100.0
    paths.spots[:, 4:] = 5000.0                       # crash between bar 3 and 4
    ladder = np.arange(5100.0, 6900.0 + 2.5, 5.0)
    short_i = int(np.searchsorted(ladder, 5900.0))    # 5900 short
    long_i = int(np.searchsorted(ladder, 5850.0))     # 5850 long (50-wide)
    entry = _entry_state(paths, short_i, long_i, fill=0.30)
    res = run_exits(model, cfg, strat, paths, ladder, entry, sl_multiplier=6.0)
    assert all(r.exit_reason == "stop" for r in res)
    assert all(r.exit_minute == 4 for r in res)       # first bar past the trigger
    assert abs(res[0].exit_debit - (0.30 * 6.0 + 0.10)) < 1e-9   # trigger + 0.10 exactly
    assert abs(res[0].pnl - (0.30 - 1.90) * 1 * 100) < 1e-9


def test_expiry_settles_intrinsic():
    cfg = SimRunConfig(strategy_name="T", bar_size="5m")
    strat, model = _strategy(), _model()
    paths = _paths(n=2)
    paths.spots[:, :] = 6000.0                        # never approaches the strikes
    ladder = np.arange(5100.0, 6900.0 + 2.5, 5.0)
    entry = _entry_state(paths, int(np.searchsorted(ladder, 5900.0)),
                         int(np.searchsorted(ladder, 5850.0)), fill=0.30)
    res = run_exits(model, cfg, strat, paths, ladder, entry, sl_multiplier=float("inf"))
    assert all(r.exit_reason == "expired" for r in res)
    assert all(r.exit_minute == 77 for r in res)
    assert res[0].exit_debit == 0.0                   # OTM at settle
    assert abs(res[0].pnl - 30.0) < 1e-9              # keep the full credit


def test_take_profit_fills_at_limit():
    strat = _strategy()
    strat.exit_rules.take_profit = __import__("strategy_models").TakeProfit(mode="pct_credit", value=0.5)
    cfg = SimRunConfig(strategy_name="T", bar_size="5m")
    model = _model()
    paths = _paths(n=2)
    paths.spots[:, :4] = 6000.0
    paths.spots[:, 4:] = 6080.0                       # spread mark decays toward 0
    ladder = np.arange(5100.0, 6900.0 + 2.5, 5.0)
    entry = _entry_state(paths, int(np.searchsorted(ladder, 5900.0)),
                         int(np.searchsorted(ladder, 5850.0)), fill=0.30)
    res = run_exits(model, cfg, strat, paths, ladder, entry, sl_multiplier=float("inf"))
    assert res[0].exit_reason == "take_profit"
    assert abs(res[0].exit_debit - 0.15) < 1e-9       # 50% of credit
    assert (np.isnan(res[0].mtm[res[0].exit_minute + 1:])).all()   # inactive after exit


def test_never_entered_paths_report_never():
    cfg = SimRunConfig(strategy_name="T", bar_size="5m")
    strat, model = _strategy(), _model()
    ladder = np.arange(5100.0, 6900.0 + 2.5, 5.0)
    n = 4
    entry = EntryState(entered=np.zeros(n, dtype=bool),
                       entry_minute=np.full(n, -1, dtype=np.int32),
                       short_idx=np.full(n, -1, dtype=np.int32),
                       long_idx=np.full(n, -1, dtype=np.int32),
                       width=np.zeros(n), qty=np.zeros(n, dtype=np.int32),
                       fill_credit=np.zeros(n), theo_credit=np.zeros(n))
    res = run_exits(model, cfg, strat, _paths(n=n), ladder, entry, sl_multiplier=6.0)
    assert all(r.exit_reason == "never" and r.pnl == 0.0 and not r.entered for r in res)


def test_run_cell_end_to_end_small():
    cfg = SimRunConfig(strategy_name="T", bar_size="5m")
    strat, model = _strategy(), _model()
    paths = _paths(n=8)
    ladder = np.arange(5100.0, 6900.0 + 2.5, 5.0)
    res = run_cell(model, cfg, strat, paths, ladder, sl_multiplier=6.0)
    assert len(res) == 8
    reasons = {r.exit_reason for r in res}
    assert reasons <= {"never", "expired", "stop", "take_profit"}
    for r in res:
        if r.entered:
            assert r.mtm is not None and np.isnan(r.mtm[: r.entry_minute]).all()


def test_explicit_dyn_matches_lazy_build():
    from sim_calibrate import build_dynamics
    from sim_data import parse_csv
    import os
    cfg = SimRunConfig(strategy_name="T", source="csv",
                       csv_path=os.path.join("tests", "fixtures", "sim_bars_5m.csv"),
                       bar_size="5m")
    bars = parse_csv(cfg.csv_path, 300)
    model = calibrate(bars, cfg)
    es_lazy = run_entry(model, cfg, _strategy(), _paths(), _ladder())
    es_explicit = run_entry(model, cfg, _strategy(), _paths(), _ladder(),
                            dyn=build_dynamics(model, cfg))
    assert np.array_equal(es_lazy.entered, es_explicit.entered)
    assert np.array_equal(es_lazy.fill_credit, es_explicit.fill_credit)


def test_skew_tilt_moves_put_credits_with_sigma_state():
    """Gate A behavioral signature (spec 2026-09-05 §8 Gate A(3), corrected).

    tilt = -skew_beta * clamp(sigma_t/sigma0 - 1, -1, +3) * m. A bull-put spread
    SELLS the near-ATM put (m ~ 0, where the tilt is ~0) and BUYS the deeper-OTM
    wing put (m < 0). On a vol SPIKE (ratio > 0) the tilt richens the put wing, so
    the leg we BUY appreciates more than the leg we SELL and the net fill credit
    FALLS monotonically; on a vol COLLAPSE (ratio < 0) the wing cheapens and the
    credit RISES monotonically; at sigma_t == sigma0 the tilt is identically zero
    and the fills are bit-identical. Same down-drift paths, only skew_beta toggles.
    """
    from sim_calibrate import DEFAULT_SMILE, CalibratedModel, GarchParams
    model = CalibratedModel(
        garch=GarchParams(omega=2e-10, alpha=0.05, gamma=0.10, beta=0.85, nu=6.0,
                          converged=True),
        ushape=np.ones(78), sigma0=0.0005, smile=DEFAULT_SMILE, vix0=15.0, source="test")
    cfg0 = SimRunConfig(strategy_name="T", bar_size="5m")
    cfg1 = SimRunConfig(strategy_name="T", bar_size="5m", skew_beta=1.0)
    strat = _strategy()

    def fills(sigma_mult):
        paths = _paths()
        paths.spots = paths.spots * np.linspace(1.0, 0.97, paths.spots.shape[1])[None, :]
        paths.sigmas = np.full_like(paths.sigmas, 0.0005 * sigma_mult)
        return (run_entry(model, cfg0, strat, paths, _ladder()).fill_credit,
                run_entry(model, cfg1, strat, paths, _ladder()).fill_credit)

    f0, f1 = fills(1.8)                       # vol spike: put wing richens -> credit falls
    assert (f1 <= f0 + 1e-12).all()
    assert f1.sum() < f0.sum()
    f0, f1 = fills(0.5)                       # vol collapse: put wing cheapens -> credit rises
    assert (f1 >= f0 - 1e-12).all()
    assert f1.sum() > f0.sum()
    f0, f1 = fills(1.0)                       # sigma_t == sigma0: tilt identically zero
    assert np.array_equal(f1, f0)
