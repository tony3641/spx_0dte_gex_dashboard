# tests/test_sim_pricing.py
import math

import numpy as np

from gex_calculator import _bsm_delta
from sim_pricing import (bsm_put, bsm_put_delta, bar_year_frac, build_ladder,
                         combo_fill_credit, half_spread, smile_iv, tick_floor)
from sim_calibrate import SmileParams


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
    smile = SmileParams(a=0.04, b=1.8, rho=-0.75, m0=0.03, sigma=0.06, half_spread_atm=0.05)
    m = np.array([-0.03, 0.0])
    # row 0 = higher path vol, row 1 = lower path vol (matrix: rows=vol state, cols=moneyness)
    iv = smile_iv(m, smile, sigma_col=np.array([[0.0007], [0.0004]]), sigma0=0.0005,
                  vol_beta=1.0, flat_iv=False, iv_atm=0.2)
    assert iv.shape == (2, 2)
    assert iv[0, 0] > iv[1, 0]                # same moneyness: higher path vol -> higher IV
    assert iv[0, 0] > iv[0, 1]                # same path vol: OTM put (m<0) richer than ATM
    flat = smile_iv(m, smile, np.full((2, 1), 0.0009), 0.0005, 1.0, flat_iv=True, iv_atm=0.2)
    assert np.allclose(flat, 0.2)


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
