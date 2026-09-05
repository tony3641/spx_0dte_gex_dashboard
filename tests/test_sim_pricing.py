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
