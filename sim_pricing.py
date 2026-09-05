"""Vectorized option pricing for the simulated market.

BSM family (mirrors gex_calculator's math, vectorized via scipy.special.ndtr),
quadratic smile with a vol-level link, synthetic bid/ask, and the repo's fill
tick rules. Pure functions — no state.
"""
import numpy as np
from scipy.special import ndtr

RISK_FREE_RATE = 0.043   # mirrors config.DEFAULT_RISK_FREE_RATE fallback


def bar_year_frac(bar_seconds: int) -> float:
    """Year fraction of one bar: 252 RTH days x 6.5 h."""
    return bar_seconds / (252 * 6.5 * 3600.0)


def bsm_put(S, K, T, r, sigma):
    S = np.asarray(S, dtype=float)
    K = np.asarray(K, dtype=float)
    sigma = np.maximum(np.asarray(sigma, dtype=float), 1e-4)
    T = np.asarray(T, dtype=float)
    if T.ndim == 0 and float(T) <= 0.0:
        return np.maximum(K - S, 0.0)
    sqrtT = np.sqrt(T)
    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * sqrtT)
    d2 = d1 - sigma * sqrtT
    return K * np.exp(-r * T) * ndtr(-d2) - S * ndtr(-d1)


def bsm_put_delta(S, K, T, r, sigma):
    sigma = np.maximum(np.asarray(sigma, dtype=float), 1e-4)
    T = np.asarray(T, dtype=float)
    if T.ndim == 0 and float(T) <= 0.0:
        return np.where(K > S, -1.0, 0.0)
    sqrtT = np.sqrt(T)
    d1 = (np.log(np.asarray(S) / np.asarray(K)) + (r + 0.5 * sigma ** 2) * T) / (sigma * sqrtT)
    return ndtr(d1) - 1.0


_VOL_RATIO_MIN = -1.0
_VOL_RATIO_MAX = 3.0


def smile_iv(m, smile, sigma_t, dyn, t):
    """IV at bar t from log-moneyness m + per-path per-bar sigma_t (smile-dynamics spec §4).

    dyn is a sim_calibrate.SmileDynamics carrying the run's dials/tables; neutral dyn
    reproduces the legacy formula exactly:
        clip(smile.iv(m) + vol_beta*(sigma - sigma0), 0.01, 5.0)
    With atm_budget the level is the variance-budget anchor: annualized remaining
    expected variance conditioned on the path's sigma state, reattached to the
    snapshot's iv0 at t=0.
    """
    m = np.asarray(m, dtype=float)
    sigma = np.asarray(sigma_t, dtype=float)
    if dyn.flat_iv:
        return np.full(np.broadcast_shapes(m.shape, sigma.shape), dyn.iv0)
    base = smile.iv(m)
    if dyn.atm_budget:
        sig2t = dyn.v_bar + dyn.budget_beta * (sigma * sigma - dyn.v_bar)
        v = dyn.a_tab[t] + dyn.b_tab[t] * sig2t
        level = dyn.iv0 * (np.sqrt(v / dyn.v0 * float(dyn.t_scale[t])) - 1.0)
    else:
        level = dyn.vol_beta * (sigma - dyn.sigma0)
    ratio = np.clip(sigma / dyn.sigma0 - 1.0, _VOL_RATIO_MIN, _VOL_RATIO_MAX)
    tilt = -dyn.skew_beta * (float(dyn.t_scale[t]) ** dyn.skew_t_gamma) * ratio * m
    return np.clip(base + level + tilt, 0.01, 5.0)


def half_spread(m, hs_atm):
    return np.clip(hs_atm * (1.0 + 8.0 * np.abs(m)), 0.01, 2.0)


def build_ladder(s0: float, range_pct: float, step: float = 5.0) -> np.ndarray:
    lo = int(np.floor(s0 * (1 - range_pct) / step) * step)
    hi = int(np.ceil(s0 * (1 + range_pct) / step) * step)
    return np.arange(lo, hi + step / 2, step, dtype=float)


def tick_floor(x, tick: float = 0.05):
    a = np.asarray(x, dtype=float)
    out = np.floor((a + 1e-9) / tick) * tick
    # round off binary-FP drift (e.g. 3 * 0.05 -> 0.15000000000000002) so results sit
    # cleanly on the tick grid in reports.
    out = np.round(out, 6)
    return float(out) if out.ndim == 0 else out


def combo_fill_credit(mid_credit: float, cons_credit: float, tick: float) -> float:
    """Entry fill: never better than the natural, rounded down to the tick grid.

    Spec: fill = min(tick-floor(mid), conservative side), floored at one tick.
    The RESULT is itself on the tick grid too — the conservative side
    (bid_short - ask_long) is an arbitrary float, so it is floored as well.
    """
    raw = min(tick_floor(mid_credit, tick), cons_credit)
    return float(max(tick, tick_floor(raw, tick)))
