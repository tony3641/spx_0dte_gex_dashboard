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


# ---------- smile, U-shape, VIX mapping, full pipeline ----------
import json
import os
from dataclasses import dataclass, field

from sim_config import SimRunConfig
from sim_data import BarSeries

SMILE_CAPTURE_PATH = os.path.join("config", "sim_smile.json")
SMILE_DEFAULT_PATH = os.path.join("config", "sim_smile_default.json")
RTH_START_MIN = 570


@dataclass
class SmileParams:
    a: float
    b: float
    c: float
    half_spread_atm: float

    def iv(self, m):
        m = np.asarray(m, dtype=float)
        return self.a + self.b * m + self.c * m * m

    def to_dict(self) -> dict:
        return {"a": self.a, "b": self.b, "c": self.c, "half_spread_atm": self.half_spread_atm}

    @classmethod
    def from_dict(cls, d: dict) -> "SmileParams":
        return cls(a=float(d["a"]), b=float(d["b"]), c=float(d["c"]),
                   half_spread_atm=float(d.get("half_spread_atm", 0.05)))


DEFAULT_SMILE = SmileParams(a=0.20, b=-0.35, c=1.20, half_spread_atm=0.05)


@dataclass
class CalibratedModel:
    garch: GarchParams
    ushape: np.ndarray            # (steps_per_day,), mean ~ 1
    sigma0: float                 # mean per-bar conditional vol (decimal return units)
    smile: SmileParams
    vix0: float
    source: str
    warnings: List[str] = field(default_factory=list)

    def sigma_annual(self, cfg: SimRunConfig) -> float:
        return float(self.sigma0 * np.sqrt(cfg.steps_per_day() * 252))


def fit_ushape(returns: np.ndarray, minute_of_day: np.ndarray, steps_per_day: int) -> np.ndarray:
    """Multiplicative per-bar vol profile from mean |return| by minute-of-day, smoothed."""
    # RTH day is 390 minutes (09:30-16:00). Derive the per-bar width in whole
    # minutes from steps_per_day so minute-of-day aligns to bar buckets;
    # sub-minute bars collapse to minute resolution (minute_of_day loses seconds).
    bar_min = max(1, int(round(390 / steps_per_day)))
    idx = np.clip(((minute_of_day - RTH_START_MIN) // bar_min - 1).astype(int), 0, steps_per_day - 1)
    sums = np.zeros(steps_per_day)
    counts = np.zeros(steps_per_day)
    np.add.at(sums, idx, np.abs(returns))
    np.add.at(counts, idx, 1.0)
    mean_abs = np.where(counts > 0, sums / np.maximum(counts, 1), np.nan)
    # fill empty buckets from neighbours, then normalize and smooth (15-bar moving mean)
    n = len(mean_abs)
    fill = np.where(np.isfinite(mean_abs), mean_abs, np.nanmean(mean_abs))
    fill = np.where(np.isfinite(fill), fill, 1.0)
    kernel = np.ones(15) / 15.0
    smooth = np.convolve(fill, kernel, mode="same")
    out = smooth / max(float(np.mean(smooth)), 1e-12)
    return np.clip(out, 0.25, 4.0)


def fit_smile(m_points, iv_points, fallback: SmileParams) -> Tuple[SmileParams, List[str]]:
    """Least-squares quadratic IV(m); falls back when too few points or degenerate fit."""
    m = np.asarray(m_points, dtype=float)
    iv = np.asarray(iv_points, dtype=float)
    ok = np.isfinite(m) & np.isfinite(iv) & (iv > 0.005) & (iv < 5.0)
    m, iv = m[ok], iv[ok]
    if len(m) < 5:
        return fallback, ["smile: too few IV points — using fallback snapshot"]
    A = np.vstack([np.ones_like(m), m, m * m]).T
    coef, *_ = np.linalg.lstsq(A, iv, rcond=None)
    if not np.isfinite(coef).all() or abs(coef[0]) > 5:
        return fallback, ["smile: degenerate fit — using fallback snapshot"]
    return SmileParams(a=float(coef[0]), b=float(coef[1]), c=float(coef[2]),
                       half_spread_atm=fallback.half_spread_atm), []


def load_smile_snapshot() -> Tuple[SmileParams, str]:
    """captured (config/sim_smile.json) -> default file -> built-in constants."""
    for path, src in ((SMILE_CAPTURE_PATH, "captured"), (SMILE_DEFAULT_PATH, "default")):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return SmileParams.from_dict(json.load(f)), src
        except Exception:
            continue
    return DEFAULT_SMILE, "builtin"


def save_smile_snapshot(smile: SmileParams) -> None:
    os.makedirs(os.path.dirname(SMILE_CAPTURE_PATH), exist_ok=True)
    with open(SMILE_CAPTURE_PATH, "w", encoding="utf-8") as f:
        json.dump(smile.to_dict(), f, indent=2)


def calibrate(bars: BarSeries, cfg: SimRunConfig) -> CalibratedModel:
    """Full calibration: returns -> GJR-t MLE -> U-shape -> smile snapshot -> VIX mapping."""
    warnings: List[str] = list(bars.warnings)
    closes = bars.closes
    rets = np.diff(np.log(closes))
    mods = bars.minute_of_day[1:]
    garch, w = fit_gjr_t(rets)
    warnings += w
    ushape = fit_ushape(rets, mods, cfg.steps_per_day())
    sigma0 = float(np.mean(np.sqrt(gjr_variance_path(garch, rets))))
    smile, smile_src = load_smile_snapshot()
    if smile_src != "captured":
        warnings.append(f"smile: {smile_src} snapshot (capture a live chain for best results)")
    vix0 = 20.0
    if bars.vix_closes is not None and len(bars.vix_closes):
        vix0 = float(np.mean(bars.vix_closes[-20:]))
    else:
        warnings.append("VIX series unavailable — mapping anchored at VIX0=20")
    return CalibratedModel(garch=garch, ushape=ushape, sigma0=sigma0, smile=smile,
                           vix0=vix0, source=bars.source, warnings=warnings)
