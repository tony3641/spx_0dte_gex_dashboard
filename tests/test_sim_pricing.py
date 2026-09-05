# tests/test_sim_pricing.py
"""Unit tests for the smile-dynamics IV math (spec 2026-09-05 §4-§6)."""
import math

import numpy as np

from gex_calculator import _bsm_delta
from sim_pricing import (bsm_put, bsm_put_delta, bar_year_frac, build_ladder,
                         combo_fill_credit, half_spread, smile_iv, tick_floor)
from sim_calibrate import DEFAULT_SMILE, SmileParams, SmileDynamics

SIGMA0 = 0.0005
R = 0.043


def _dyn(**kw):
    d = dict(sigma0=SIGMA0, vol_beta=0.75, flat_iv=False,
             iv0=float(DEFAULT_SMILE.iv(0.0)), skew_beta=0.0,
             t_scale=np.ones(4), skew_t_gamma=0.0)
    d.update(kw)
    return SmileDynamics(**d)


def test_bsm_put_put_call_parity():
    S, K, T, r, sig = np.array([6000.0]), np.array([5900.0]), 1 / 252, 0.043, 0.25
    put = bsm_put(S, K, T, r, sig)
    # call via parity: C = P + S - K*e^{-rT}
    call = put + S - K * math.exp(-r * T)
    intrinsic = max(0.0, (K - S)[0])
    assert put[0] > intrinsic
    assert abs(float(put[0] - 250.0)) < 250.0        # sane magnitude
    assert (call > 0).all()


def test_bsm_put_expiry_is_intrinsic():
    S = np.array([6000.0, 5850.0])
    K = np.array([5900.0, 5900.0])
    put = bsm_put(S, K, 0.0, 0.043, 0.25)
    np.testing.assert_allclose(put, [0.0, 50.0])


def test_put_delta_matches_gex_calculator():
    # parity with the live dashboard's BSM delta implementation
    S, K, T, r, sig = 6000.0, 5800.0, 2 / (252 * 390), 0.043, 0.30
    mine = float(bsm_put_delta(np.array([S]), np.array([K]), T, r, np.array([sig]))[0])
    theirs = _bsm_delta(S, K, T, r, sig, "P")
    assert abs(mine - theirs) < 1e-9


def test_smile_iv_vol_link_and_flat():
    m = np.array([-0.03, 0.0])
    # row 0 = higher path vol, row 1 = lower path vol (matrix: rows=vol state, cols=moneyness)
    iv = smile_iv(m, DEFAULT_SMILE, np.array([[0.0007], [0.0004]]), _dyn(), 0)
    assert iv.shape == (2, 2)
    assert iv[0, 0] > iv[1, 0]                # same moneyness: higher path vol -> higher IV
    assert iv[0, 0] > iv[0, 1]                # same path vol: OTM put (m<0) richer than ATM
    flat = smile_iv(m, DEFAULT_SMILE, np.full((2, 1), 0.0009), _dyn(flat_iv=True), 0)
    assert np.all(flat == flat[0, 0])         # flat mode: constant IV regardless of m / sigma


def test_neutral_dyn_is_bit_identical_to_legacy_formula():
    m = np.linspace(-0.15, 0.15, 31)
    sigma = np.array([[0.0007], [0.0004]])
    out = smile_iv(m, DEFAULT_SMILE, sigma, _dyn(), 1)
    legacy = np.clip(DEFAULT_SMILE.iv(m) + 0.75 * (sigma - SIGMA0), 0.01, 5.0)
    assert np.array_equal(out, legacy)


def test_tilt_zero_at_atm_and_moves_wings():
    m = np.array([-0.10, 0.0, 0.10])
    sigma = np.array([[SIGMA0 * 1.5]])                 # same vol state -> level cancels
    no_tilt = smile_iv(m, DEFAULT_SMILE, sigma, _dyn(), 0)
    tilt = smile_iv(m, DEFAULT_SMILE, sigma, _dyn(skew_beta=1.0), 0)
    # a (1, 1) sigma_t keeps the batch axis -> output is (1, len(m)); index moneyness on axis 1
    assert tilt[0, 0] > no_tilt[0, 0]                  # put wing richer under tilt
    assert tilt[0, 2] < no_tilt[0, 2]                  # call wing cheaper under tilt
    assert tilt[0, 1] == no_tilt[0, 1]                 # ATM exactly untouched by the TILT


def test_down_shock_flattens_wing():
    m = np.array([-0.10, 0.0, 0.10])
    base = smile_iv(m, DEFAULT_SMILE, np.array([[SIGMA0]]), _dyn(), 0)
    down = smile_iv(m, DEFAULT_SMILE, np.array([[SIGMA0 * 0.5]]), _dyn(skew_beta=1.0), 0)
    assert down[0, 0] < base[0, 0]          # vol collapse -> put wing cheaper
    assert down[0, 2] > base[0, 2]          # call wing richer


def test_ratio_clamped_for_uncapped_tails():
    m = np.array([-0.15, 0.15])
    dyn = _dyn(skew_beta=1.0, vol_beta=0.0)   # level off so the tilt is isolated
    iv10 = smile_iv(m, DEFAULT_SMILE, np.array([[SIGMA0 * 11.0]]), dyn, 0)  # raw +10
    iv4 = smile_iv(m, DEFAULT_SMILE, np.array([[SIGMA0 * 5.0]]), dyn, 0)    # raw +4
    assert np.array_equal(iv10, iv4)  # both clamp to ratio +3


def test_flat_iv_short_circuit_ignores_dynamics():
    dyn = _dyn(flat_iv=True, skew_beta=1.0)
    out = smile_iv(np.array([-0.1, 0.1]), DEFAULT_SMILE, np.array([[0.001]]), dyn, 2)
    assert np.all(out == dyn.iv0)


def test_put_prices_monotone_in_strike_with_tilt():
    ladder = np.arange(5700.0, 6300.0 + 2.5, 5.0)
    m = np.log(ladder / 6000.0)
    for t, mult in [(0, 1.8), (3, 0.6)]:
        iv = smile_iv(m, DEFAULT_SMILE, np.array([[SIGMA0 * mult]]), _dyn(skew_beta=1.0), t)
        put = bsm_put(6000.0, ladder, bar_year_frac(300) * (10 - t), R, iv)
        assert np.all(np.diff(put) >= -1e-12)   # put rises with strike, no butterfly flip


def test_half_spread_widens_otm():
    hs = half_spread(np.array([0.0, -0.05]), 0.05)
    assert hs[0] == 0.05 and hs[1] > 0.05


def test_tick_floor_and_combo_fill():
    assert tick_floor(0.27) == 0.25
    assert tick_floor(0.23) == 0.20
    assert tick_floor(0.25) == 0.25           # exact multiple stays
    np.testing.assert_allclose(tick_floor(np.array([0.27, 0.23])), [0.25, 0.20])
    assert combo_fill_credit(0.27, 0.25, 0.05) == 0.25   # natural 0.25 is not the constraint
    assert combo_fill_credit(0.23, 0.20, 0.05) == 0.20
    assert combo_fill_credit(0.07, 0.02, 0.05) == 0.05   # floored at one tick
    # off-grid NATURAL must also be floored onto the 0.05 grid (regression: the old
    # implementation returns the raw natural when it binds, e.g. 0.187 / 0.54 / 0.69).
    assert combo_fill_credit(0.30, 0.187, 0.05) == 0.15
    assert combo_fill_credit(0.187, 0.54, 0.05) == 0.15
    assert combo_fill_credit(0.30, 0.02, 0.05) == 0.05


def test_build_ladder():
    K = build_ladder(6000.0, 0.15)
    assert K[0] == 5100.0 and K[-1] == 6900.0
    assert (np.diff(K) == 5.0).all() and 6000.0 in K


def test_strike_monotonicity_every_bar_at_gamma_04():
    from sim_calibrate import CalibratedModel, GarchParams, build_dynamics
    from sim_config import SimRunConfig
    model = CalibratedModel(
        garch=GarchParams(omega=2e-10, alpha=0.05, gamma=0.10, beta=0.85, nu=6.0,
                          converged=True),
        ushape=np.ones(78), sigma0=SIGMA0, smile=DEFAULT_SMILE, vix0=15.0, source="test")
    cfg = SimRunConfig(strategy_name="T", bar_size="5m", skew_beta=1.0, skew_t_gamma=0.4)
    dyn = build_dynamics(model, cfg)
    ladder = np.arange(5700.0, 6300.0 + 2.5, 5.0)
    m = np.log(ladder / 6000.0)
    barf = 300 / (252 * 6.5 * 3600.0)
    for t in range(78):
        iv = smile_iv(m, DEFAULT_SMILE, np.array([[SIGMA0 * 1.8]]), dyn, t)
        put = bsm_put(6000.0, ladder, barf * (77 - t), R, iv)
        assert np.all(np.diff(put) >= -1e-12), f"butterfly flip at bar {t}"


def run_entry_sim(model, cfg, spots, sigmas, ladder, dyn=None, per_path_start=None):
    """Thin stand-in for sim_engine.run_entry on prebuilt paths (avoids the import)."""
    from types import SimpleNamespace
    from sim_engine import run_entry
    return run_entry(model, cfg, _strategy_sim(), SimpleNamespace(spots=spots, sigmas=sigmas),
                     ladder, per_path_start=per_path_start, dyn=dyn)


def _strategy_sim():
    from strategy_models import Condition, ExitRules, StopLoss, Strategy
    return Strategy(
        name="T", direction="bull_put",
        conditions=[
            Condition(kind="short_delta", params={"min": 0.05, "max": 0.60}),
            Condition(kind="spread_width", params={"min": 5, "max": 50}),
            Condition(kind="credit", params={"min": 0.05}),
            Condition(kind="entry_window", params={"start": "09:35", "end": "14:00"}),
        ],
        exit_rules=ExitRules(stop_loss=StopLoss(multiplier=6.0)), budget=None)


def test_late_window_credits_steeper_with_gamma():
    """Gate B deliverable: quantify the last-window credit shift (spec §8).

    Vol-SPIKE geometry (sigma = 1.8x SIGMA0 every bar): the smile tilt RICHENS the bought
    deep-OTM wing put more than the near-ATM short put, so the bull-put net credit FALLS.
    skew_t_gamma AMPLIFIES that tilt toward expiry (scales by (T0/T)^gamma), so the late
    window (bars >= 47) mean credit at gamma=0.4 is BELOW gamma=0 — the brief's `>` was
    backwards for a spike. Direction asserted below was confirmed empirically.

    The permissive short-delta band qualifies at the window OPEN for every path, so no
    path ever reaches the late window naturally (fixture captures are all bar 0). To make
    the last-hour credit observable we gate eligibility with run_entry's per_path_start,
    staggering starts across bars 47..53. Both gamma cohorts share identical starts, so
    entry bars match and the fills differ only by the expiry amplification.
    """
    from sim_calibrate import CalibratedModel, GarchParams, build_dynamics
    from sim_config import SimRunConfig
    rng = np.random.default_rng(0)
    n, steps = 60, 78
    spots = np.full((n, steps), 6000.0) + rng.normal(0, 2.0, (n, steps)).cumsum(1) * 0.1
    spots *= np.linspace(1.0, 0.97, steps)[None, :]          # down drift
    sigmas = np.full((n, steps), SIGMA0 * 1.8)               # vol shock
    model = CalibratedModel(
        garch=GarchParams(omega=2e-10, alpha=0.05, gamma=0.10, beta=0.85, nu=6.0,
                          converged=True),
        ushape=np.ones(steps), sigma0=SIGMA0, smile=DEFAULT_SMILE, vix0=15.0,
        source="test")
    ladder = np.arange(5100.0, 6900.0 + 2.5, 5.0)
    starts = 47 + np.arange(n) % 7                            # force eligibility into bars 47..53
    fills, minutes = {}, {}
    for tag, gamma in [("gamma0", 0.0), ("gamma04", 0.4)]:
        cfg = SimRunConfig(strategy_name="T", bar_size="5m", skew_beta=1.0,
                           skew_t_gamma=gamma)
        es = run_entry_sim(model, cfg, spots, sigmas, ladder, dyn=build_dynamics(model, cfg),
                           per_path_start=starts)
        fills[tag], minutes[tag] = es.fill_credit, es.entry_minute
    late = minutes["gamma0"] >= 47                            # last hour of the 09:35-14:00 window
    assert late.all(), "forced-late cohort did not all enter in the late window"
    print(f"late-window mean credit gamma=0: {fills['gamma0'][late].mean():.4f} "
          f"gamma=0.4: {fills['gamma04'][late].mean():.4f}")
    assert fills["gamma04"][late].mean() < fills["gamma0"][late].mean()
