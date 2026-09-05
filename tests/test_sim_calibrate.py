# tests/test_sim_calibrate.py
import json

import numpy as np
import pytest

from sim_calibrate import fit_gjr_t, gjr_variance_path


def _simulate_gjr(n=8000, omega=2e-10, alpha=0.05, gamma=0.10, beta=0.85, nu=6.0, seed=3):
    rng = np.random.default_rng(seed)
    eps = np.empty(n)
    s2 = omega / (1 - alpha - gamma / 2 - beta)
    for t in range(n):
        z = rng.standard_t(nu) / np.sqrt(nu / (nu - 2))
        eps[t] = np.sqrt(s2) * z
        s2 = omega + alpha * eps[t] ** 2 + gamma * eps[t] ** 2 * (eps[t] < 0) + beta * s2
    return eps


def test_fit_recovers_parameters_loosely():
    eps = _simulate_gjr()
    params, warnings = fit_gjr_t(eps)
    assert params.converged
    assert not warnings
    assert 0 < params.alpha < 0.35 and 0 <= params.gamma < 0.5 and 0.3 < params.beta < 0.99
    assert params.alpha + params.beta + params.gamma / 2 < 1.0    # stationary
    assert 2.5 < params.nu < 30


def test_variance_path_matches_recursion():
    eps = _simulate_gjr(n=200)
    p, _ = fit_gjr_t(eps)
    v = gjr_variance_path(p, eps)
    assert v.shape == eps.shape
    assert (v > 0).all()


def test_fit_degenerate_returns_preset():
    eps = np.zeros(100)                    # zero variance -> optimizer cannot work
    p, warnings = fit_gjr_t(eps)
    assert not p.converged
    assert warnings and "preset" in warnings[0].lower()
    assert p.alpha > 0 and p.beta < 1.0    # preset values, variance-targeted omega


from sim_calibrate import (CalibratedModel, DEFAULT_SMILE, SmileParams,
                           calibrate, fit_smile, fit_ushape, load_smile_snapshot)


def _make_bars(n_days=6, bars=78, seed=11):
    from sim_data import BarSeries
    rng = np.random.default_rng(seed)
    # morning + afternoon active, midday quiet -> U-shape
    ushape = np.interp(np.arange(bars), [0, 6, 39, 72, 77], [2.0, 0.7, 0.6, 1.6, 2.4])
    ushape = ushape / ushape.mean()
    closes, mods = [], []
    s = 6000.0
    for d in range(n_days):
        for b in range(bars):
            eps = 0.0005 * ushape[b] * rng.standard_normal()
            s *= float(np.exp(eps))
            closes.append(s)
            mods.append(570 + 5 * (b + 1))
    return BarSeries(closes=np.array(closes), minute_of_day=np.array(mods),
                     bar_seconds=300, source="csv")


def test_fit_ushape_peaks_at_open_and_close():
    bars = _make_bars()
    rets = np.diff(np.log(bars.closes))
    u = fit_ushape(rets, bars.minute_of_day[1:], steps_per_day=78)
    assert u.shape == (78,)
    assert abs(u.mean() - 1.0) < 0.2
    assert u[:6].mean() > u[30:50].mean()      # open busier than midday
    assert u[-6:].mean() > u[30:50].mean()     # close busier than midday


def test_fit_smile_svi_recovers_skew():
    # exactly in-family SVI data: the fit should recover a sane, bounded smile.
    m = np.linspace(-0.06, 0.01, 12)
    iv_true = DEFAULT_SMILE.iv(m)
    smile, warnings = fit_smile(m, iv_true, DEFAULT_SMILE)
    assert not warnings
    assert 0.10 < smile.iv(0.0) < 0.30                 # ATM IV sane
    far = smile.iv(-0.15)
    assert np.isfinite(far) and 0.0 < far < 1.0        # far-OTM bounded (the core fix)
    assert far > smile.iv(0.0)                          # put skew present


def test_fit_smile_insufficient_points_falls_back():
    smile, warnings = fit_smile(np.array([-0.01]), np.array([0.22]), DEFAULT_SMILE)
    assert warnings and smile == DEFAULT_SMILE


def test_snapshot_round_trip(tmp_path, monkeypatch):
    import sim_calibrate as sc
    monkeypatch.setattr(sc, "SMILE_CAPTURE_PATH", str(tmp_path / "sim_smile.json"))
    monkeypatch.setattr(sc, "SMILE_DEFAULT_PATH", str(tmp_path / "sim_smile_default.json"))
    sc.save_smile_snapshot(SmileParams(a=0.05, b=2.0, rho=-0.60, m0=0.02, sigma=0.07,
                                       half_spread_atm=0.06))
    smile, src = sc.load_smile_snapshot()
    assert src == "captured"
    assert abs(smile.rho + 0.60) < 1e-9 and abs(smile.sigma - 0.07) < 1e-9
    assert smile.half_spread_atm == 0.06
    (tmp_path / "sim_smile.json").unlink()
    smile2, src2 = sc.load_smile_snapshot()
    assert src2 == "builtin" and smile2 == sc.DEFAULT_SMILE


def test_legacy_quadratic_snapshot_is_skipped(tmp_path, monkeypatch):
    import sim_calibrate as sc
    with pytest.raises(ValueError):
        sc.SmileParams.from_dict({"a": 0.2, "b": -0.35, "c": 1.2, "half_spread_atm": 0.05})
    monkeypatch.setattr(sc, "SMILE_CAPTURE_PATH", str(tmp_path / "sim_smile.json"))
    (tmp_path / "sim_smile.json").write_text(
        json.dumps({"a": 0.2, "b": -0.35, "c": 1.2, "half_spread_atm": 0.05}), encoding="utf-8")
    smile, src = sc.load_smile_snapshot()
    assert src != "captured" and smile == sc.DEFAULT_SMILE


def test_skewed_chain_bounded_far_otm():
    # Observed put IVs reach only ~5-6% OTM (the live-chain limit); the fit must
    # extrapolate to the sim's +/-15% ladder without exploding or dipping.
    m = np.linspace(-0.06, 0.01, 12)
    iv = DEFAULT_SMILE.iv(m)                       # put-skewed chain (~35% at -6% OTM)
    smile, warnings = fit_smile(m, iv, DEFAULT_SMILE)
    assert not warnings
    far = smile.iv(-0.15)
    assert np.isfinite(far) and 0.0 < far < 1.0    # bounded at the ladder edge
    assert smile.iv(-0.15) >= smile.iv(-0.06)      # wing keeps rising, no dip


def test_degenerate_stray_outlier_falls_back():
    m = np.linspace(-0.06, 0.01, 12)
    iv = DEFAULT_SMILE.iv(m)
    iv[0] = 1.8                                    # wild far-OTM put IV outlier
    smile, warnings = fit_smile(m, iv, DEFAULT_SMILE)
    assert warnings and smile == DEFAULT_SMILE


def test_default_smile_bounded():
    iv = DEFAULT_SMILE.iv(np.array([-0.15, 0.0, 0.15]))
    assert np.isfinite(iv).all() and (iv > 0).all()
    assert iv[0] < 1.0
    assert abs(DEFAULT_SMILE.iv(0.0) - 0.20) < 0.02


def _two_day_series_with_overnight_gap():
    from sim_data import BarSeries
    rng = np.random.default_rng(21)
    bars = 78
    mods = 570 + 5 * (np.arange(bars) + 1)                # 575..960 = one RTH day at 5m
    day0 = 6000.0 * np.exp(0.0004 * np.cumsum(rng.standard_normal(bars)))
    # +50% overnight gap: day1's first close is 1.5x day0's last close. The cross-day
    # log-diff (~0.405) is a prior-16:00 -> next-09:3x move, NOT part of 0DTE intraday
    # dynamics. If it were folded in it would land in the opening U-shape bucket + inflate sigma0.
    day1 = (float(day0[-1]) * 1.5) * np.exp(0.0004 * np.cumsum(rng.standard_normal(bars)))
    return BarSeries(closes=np.concatenate([day0, day1]),
                     minute_of_day=np.concatenate([mods, mods]),
                     bar_seconds=300, source="csv")


def test_calibrate_excludes_overnight_cross_day_return():
    from sim_calibrate import calibrate
    from sim_config import SimRunConfig
    bars = _two_day_series_with_overnight_gap()
    model = calibrate(bars, SimRunConfig(strategy_name="Main"))
    # The overnight log-diff (~0.405) would land in the opening minute bucket and blow it out
    # (pre-fix ushape[:3] clips at the 4.0 cap). Excluding cross-day diffs keeps the opening
    # U-shape at the intraday scale (~1.0) instead of an inflated 4.0.
    assert model.ushape[:3].mean() < 3.0


def test_calibrate_end_to_end():
    from sim_config import SimRunConfig
    from sim_calibrate import calibrate
    bars = _make_bars(n_days=8)
    model = calibrate(bars, SimRunConfig(strategy_name="Main"))
    assert model.garch.converged or model.warnings
    assert model.ushape.shape == (78,)
    assert model.sigma0 > 0 and model.vix0 > 0
    assert model.source == "csv"
    ann = model.sigma_annual(SimRunConfig(strategy_name="Main"))
    assert 0.02 < ann < 5.0


def test_build_dynamics_neutral_fields():
    from sim_calibrate import build_dynamics
    from sim_config import SimRunConfig
    bars = _make_bars()
    cfg = SimRunConfig(strategy_name="T", source="csv", bar_size="5m")
    model = calibrate(bars, cfg)
    dyn = build_dynamics(model, cfg)
    assert dyn.sigma0 == model.sigma0
    assert dyn.vol_beta == cfg.vol_beta == 0.75
    assert dyn.flat_iv is False
    assert dyn.iv0 == float(model.smile.iv(0.0))
    assert dyn.skew_beta == 0.0
    assert dyn.skew_t_gamma == 0.0
    assert dyn.atm_budget is False
    assert dyn.a_tab is None and dyn.b_tab is None
    assert dyn.t_scale.shape == (cfg.steps_per_day(),)
    assert np.array_equal(dyn.t_scale, np.ones(cfg.steps_per_day()))


def test_build_dynamics_t_scale_table():
    from sim_calibrate import build_dynamics
    from sim_config import SimRunConfig
    model = calibrate(_make_bars(), SimRunConfig(strategy_name="T", source="csv",
                                                 bar_size="5m"))
    cfg0 = SimRunConfig(strategy_name="T", source="csv", bar_size="5m")
    assert np.array_equal(build_dynamics(model, cfg0).t_scale, np.ones(78))
    cfg4 = SimRunConfig(strategy_name="T", source="csv", bar_size="5m",
                        skew_t_gamma=0.4)
    ts = build_dynamics(model, cfg4).t_scale
    assert ts.shape == (78,)
    assert ts[0] == 1.0                                  # anchored at the first bar
    assert np.all(np.diff(ts) > 0.0)                     # grows monotonically to expiry
    assert ts[-1] == pytest.approx(77 / 0.5)    # T_floor = half a 5-min bar (raw; gamma applied at eval)
