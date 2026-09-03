"""GJR-GARCH(1,1) with Student-t innovations, fitted by MLE (scipy).

Preset fallback on non-convergence keeps runs alive; warnings surface in the UI
calibration panel. All functions are pure — no IO.
"""
import math
from dataclasses import dataclass
from typing import List, Tuple

import numpy as np
from scipy.optimize import minimize
from scipy.special import gammaln, ndtr

PRESET = dict(alpha=0.04, gamma=0.10, beta=0.85, nu=6.0)


@dataclass
class GarchParams:
    omega: float
    alpha: float
    gamma: float        # GJR leverage term (asymmetric response to negative shocks)
    beta: float
    nu: float           # Student-t dof
    converged: bool = True


def gjr_variance_path(p: GarchParams, eps: np.ndarray) -> np.ndarray:
    """sigma^2_t for t = 0..n-1; warm-up variance backcast as EWMA of the first 50 |eps|."""
    n = len(eps)
    back = float(np.mean(eps[: min(50, n)] ** 2)) if n else 1e-12
    s2 = np.empty(n)
    prev = max(back, 1e-12)
    prev_e2 = back
    for t in range(n):
        shock_neg = 1.0 if (t > 0 and eps[t - 1] < 0) else 0.0
        s2[t] = p.omega + p.alpha * prev_e2 + p.gamma * prev_e2 * shock_neg + p.beta * prev
        prev = max(s2[t], 1e-16)
        prev_e2 = eps[t] ** 2
    return s2


def _neg_loglik(theta: np.ndarray, eps: np.ndarray) -> float:
    omega, alpha, gamma, beta, nu = theta
    if omega <= 0 or alpha < 0 or gamma < 0 or beta < 0 or nu <= 2.05:
        return 1e12
    if alpha + gamma / 2 + beta >= 0.999:
        return 1e12
    p = GarchParams(omega=omega, alpha=alpha, gamma=gamma, beta=beta, nu=nu)
    s2 = gjr_variance_path(p, eps)
    if not np.isfinite(s2).all() or (s2 <= 0).any():
        return 1e12
    c = math.lgamma((nu + 1) / 2) - math.lgamma(nu / 2) - 0.5 * math.log(math.pi * (nu - 2))
    ll = c - 0.5 * np.log(s2) - ((nu + 1) / 2) * np.log1p(eps ** 2 / (s2 * (nu - 2)))
    val = -float(np.sum(ll))
    return val if math.isfinite(val) else 1e12


def fit_gjr_t(returns: np.ndarray, nu_init: float = 6.0) -> Tuple[GarchParams, List[str]]:
    """Fit GJR-GARCH(1,1)-t; falls back to a variance-targeted preset when MLE fails."""
    warnings: List[str] = []
    eps = np.asarray(returns, dtype=float)
    eps = eps[np.isfinite(eps)]
    var0 = float(np.var(eps)) if len(eps) > 10 else 1e-10
    var0 = max(var0, 1e-16)
    # Zero-variance input (flat/empty returns): the Student-t likelihood is
    # degenerate and unbounded below, so Nelder-Mead can "converge" to a
    # nonsense interior point (negll ~ -1e3) instead of failing, and the
    # best_val >= 1e11 fallback below never fires. Short-circuit straight to
    # the variance-targeted preset so flat series get a flagged, sane fit.
    if len(eps) == 0 or float(np.var(eps)) < 1e-16:
        omega = var0 * (1 - PRESET["alpha"] - PRESET["gamma"] / 2 - PRESET["beta"])
        warnings.append("GARCH MLE did not converge — preset parameters in use (flagged in UI)")
        return GarchParams(omega=omega, alpha=PRESET["alpha"], gamma=PRESET["gamma"],
                           beta=PRESET["beta"], nu=PRESET["nu"], converged=False), warnings
    # variance targeting: omega = var * (1 - alpha - gamma/2 - beta)
    x0 = np.array([var0 * (1 - PRESET["alpha"] - PRESET["gamma"] / 2 - PRESET["beta"]),
                   PRESET["alpha"], PRESET["gamma"], PRESET["beta"], nu_init])
    best, best_val = None, np.inf
    for nu_start in (nu_init, 4.0, 10.0):
        x = x0.copy()
        x[4] = nu_start
        try:
            res = minimize(_neg_loglik, x, args=(eps,), method="Nelder-Mead",
                           options=dict(maxiter=2000, xatol=1e-6, fatol=1e-6))
            res2 = minimize(_neg_loglik, res.x, args=(eps,), method="Nelder-Mead",
                            options=dict(maxiter=1000, xatol=1e-7, fatol=1e-7))
            cand = res2 if res2.fun < res.fun else res
        except Exception:
            continue
        if cand.fun < best_val and np.isfinite(cand.fun):
            best, best_val = cand.x, float(cand.fun)
    if best is None or best_val >= 1e11:
        omega = var0 * (1 - PRESET["alpha"] - PRESET["gamma"] / 2 - PRESET["beta"])
        warnings.append("GARCH MLE did not converge — preset parameters in use (flagged in UI)")
        return GarchParams(omega=omega, alpha=PRESET["alpha"], gamma=PRESET["gamma"],
                           beta=PRESET["beta"], nu=PRESET["nu"], converged=False), warnings
    omega, alpha, gamma, beta, nu = (float(v) for v in best)
    return GarchParams(omega=max(omega, 1e-16), alpha=alpha, gamma=gamma, beta=beta,
                       nu=max(nu, 2.2), converged=True), warnings
