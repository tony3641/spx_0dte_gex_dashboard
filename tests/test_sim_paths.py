# tests/test_sim_paths.py
import numpy as np

from sim_calibrate import CalibratedModel, DEFAULT_SMILE, GarchParams
from sim_config import SimRunConfig
from sim_paths import simulate_chunk


def _model(nu=6.0, gamma=0.10):
    return CalibratedModel(
        garch=GarchParams(omega=1.25e-8, alpha=0.05, gamma=gamma, beta=0.85, nu=nu, converged=True),
        ushape=np.interp(np.arange(78), [0, 6, 39, 72, 77], [2.0, 0.7, 0.6, 1.6, 2.4]),
        sigma0=0.0005, smile=DEFAULT_SMILE, vix0=15.0, source="test")


def test_reproducible_and_shape():
    cfg = SimRunConfig(strategy_name="Main", bar_size="5m")
    m = _model()
    a = simulate_chunk(m, cfg, 6000.0, 50, np.random.SeedSequence([42, 0, 0]))
    b = simulate_chunk(m, cfg, 6000.0, 50, np.random.SeedSequence([42, 0, 0]))
    c = simulate_chunk(m, cfg, 6000.0, 50, np.random.SeedSequence([42, 0, 1]))
    assert a.spots.shape == (50, 78) and a.sigmas.shape == (50, 78)
    assert np.array_equal(a.spots, b.spots)      # same seed -> identical
    assert not np.array_equal(a.spots, c.spots)  # different chunk -> different


def test_vol_link_and_ushape():
    cfg = SimRunConfig(strategy_name="Main")
    m = _model()
    sp = simulate_chunk(m, cfg, 6000.0, 4000, np.random.SeedSequence([1, 0, 0]))
    rets = np.diff(np.log(sp.spots), axis=1)                     # (n, steps-1)
    realized_bar_vol = rets.std(axis=0)                          # per step
    assert realized_bar_vol[0] > realized_bar_vol[38]            # open busier than midday
    assert realized_bar_vol[-8:].mean() > realized_bar_vol[38]   # close busier than midday
    # sigmas recorded for the smile link track the U-shape
    assert abs(sp.sigmas.mean() / (0.0005 * m.ushape.mean()) - 1.0) < 0.5


def test_stress_nu_3_fatter_tails():
    cfg = SimRunConfig(strategy_name="Main", nu_override=3.0)
    sp = simulate_chunk(_model(), cfg, 6000.0, 8000, np.random.SeedSequence([5, 0, 0]))
    rets = np.diff(np.log(sp.spots), axis=1).ravel()
    kurt = float(((rets - rets.mean()) ** 4).mean() / rets.var() ** 2)
    cfg6 = SimRunConfig(strategy_name="Main")
    sp6 = simulate_chunk(_model(), cfg6, 6000.0, 8000, np.random.SeedSequence([5, 0, 0]))
    rets6 = np.diff(np.log(sp6.spots), axis=1).ravel()
    kurt6 = float(((rets6 - rets6.mean()) ** 4).mean() / rets6.var() ** 2)
    assert kurt > kurt6                   # nu=3 heavier tails than nu=6
    assert (np.abs(sp.spots / 6000.0 - 1) < 0.5).all()   # guard rail: within ±50%


def test_atm_iv_caps_conditional_sigma():
    # Near-integrated GJR-GARCH: beta=0.92 shrinks the stability margin to 0.985, so
    # the vol-ratchet can run a subset of paths up to 5-10x the calibrated sigma0. The
    # atm_iv anchor hard-caps each per-bar sigma at cap_mult * IV-implied per-bar vol.
    m = CalibratedModel(
        garch=GarchParams(omega=1.25e-8, alpha=0.06, gamma=0.01, beta=0.92, nu=7.0, converged=True),
        ushape=np.ones(78), sigma0=0.0005, smile=DEFAULT_SMILE, vix0=15.0, source="test")
    cfg = SimRunConfig(strategy_name="Main", bar_size="5m", atm_iv=0.167, vol_cap_mult=2.0)
    steps = cfg.steps_per_day()            # 78
    cap = 2.0 * (0.167 / np.sqrt(252.0)) / np.sqrt(steps)
    sp = simulate_chunk(m, cfg, 6000.0, 4000, np.random.SeedSequence([5, 0, 0]))
    assert sp.sigmas.max() <= cap * (1 + 1e-9)          # cap is enforced
    # the same seed without the anchor runs far wider (the cap is binding)
    sp_u = simulate_chunk(m, SimRunConfig(strategy_name="Main", bar_size="5m"),
                          6000.0, 4000, np.random.SeedSequence([5, 0, 0]))
    assert sp_u.sigmas.max() > cap


def test_atm_iv_anchors_terminal_breadth():
    # Anchored at 16.7% IV the terminal fan should be ~1.05%/day, NOT the fat left tail
    # the near-integrated GARCH produces on its own.
    m = CalibratedModel(
        garch=GarchParams(omega=1.25e-8, alpha=0.06, gamma=0.01, beta=0.92, nu=7.0, converged=True),
        ushape=np.ones(78), sigma0=0.0005, smile=DEFAULT_SMILE, vix0=15.0, source="test")
    img_cfg = SimRunConfig(strategy_name="Main", bar_size="5m", atm_iv=0.167)
    sp = simulate_chunk(m, img_cfg, 6000.0, 8000, np.random.SeedSequence([5, 0, 0]))
    lr = np.log(sp.spots[:, -1] / sp.spots[:, 0])
    target = 0.167 / np.sqrt(252.0)
    assert abs(lr.std() - target) / target < 0.45       # ~1.05% daily, not 3-6%
    sp_u = simulate_chunk(m, SimRunConfig(strategy_name="Main", bar_size="5m"),
                          6000.0, 8000, np.random.SeedSequence([5, 0, 0]))
    lr_u = np.log(sp_u.spots[:, -1] / sp_u.spots[:, 0])
    assert lr.min() > lr_u.min()          # worst-case path is (far less) extreme when capped
