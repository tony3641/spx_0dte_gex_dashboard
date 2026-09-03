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
