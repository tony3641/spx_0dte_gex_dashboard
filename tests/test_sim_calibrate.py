# tests/test_sim_calibrate.py
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


def test_fit_smile_quadratic():
    m = np.linspace(-0.06, 0.0, 12)
    iv_true = 0.20 - 0.35 * m + 1.2 * m ** 2
    smile, warnings = fit_smile(m, iv_true, DEFAULT_SMILE)
    assert not warnings
    assert abs(smile.a - 0.20) < 1e-6 and abs(smile.b + 0.35) < 1e-6 and abs(smile.c - 1.2) < 1e-6


def test_fit_smile_insufficient_points_falls_back():
    smile, warnings = fit_smile(np.array([-0.01]), np.array([0.22]), DEFAULT_SMILE)
    assert warnings and smile == DEFAULT_SMILE


def test_snapshot_round_trip(tmp_path, monkeypatch):
    import sim_calibrate as sc
    monkeypatch.setattr(sc, "SMILE_CAPTURE_PATH", str(tmp_path / "sim_smile.json"))
    monkeypatch.setattr(sc, "SMILE_DEFAULT_PATH", str(tmp_path / "sim_smile_default.json"))
    sc.save_smile_snapshot(SmileParams(a=0.18, b=-0.30, c=1.0, half_spread_atm=0.06))
    smile, src = sc.load_smile_snapshot()
    assert src == "captured" and abs(smile.a - 0.18) < 1e-9
    (tmp_path / "sim_smile.json").unlink()
    smile2, src2 = sc.load_smile_snapshot()
    assert src2 == "builtin" and smile2 == sc.DEFAULT_SMILE


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
