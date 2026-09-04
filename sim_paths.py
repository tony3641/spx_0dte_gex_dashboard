"""Chunked intraday path generation: GJR-GARCH recursion + standardized-t + U-shape."""
from dataclasses import dataclass

import numpy as np

from sim_calibrate import CalibratedModel
from sim_config import SimRunConfig


@dataclass
class SimPaths:
    spots: np.ndarray    # (n, steps): close of bar t
    sigmas: np.ndarray   # (n, steps): per-bar conditional vol used for that bar


def simulate_chunk(model: CalibratedModel, cfg: SimRunConfig, s0: float,
                   n_paths: int, seed_seq: np.random.SeedSequence) -> SimPaths:
    steps = cfg.steps_per_day()
    p = model.garch
    nu = float(cfg.nu_override) if cfg.nu_override else p.nu
    gamma = p.gamma * cfg.gamma_mult
    rng = np.random.default_rng(seed_seq)
    z = rng.standard_t(nu, size=(n_paths, steps))
    z /= np.sqrt(nu / (nu - 2.0))                     # standardized: E[z^2] = 1
    ushape = model.ushape

    # ATM-IV anchor: a near-integrated GJR-GARCH fit (alpha+gamma/2+beta ~ 0.99) lets the
    # volatility ratchet a small subset of paths up to 5-10x the calibrated sigma0, which
    # blows out the terminal fan's tails (1-day p0 of -25%+). Capping each per-bar sigma
    # at vol_cap_mult * (IV-implied 1-day vol) / sqrt(steps) keeps the fan consistent with
    # the market's own implied distribution. None -> historical GARCH level, uncapped.
    cap = float("inf")
    if cfg.atm_iv is not None:
        target_daily_vol = cfg.atm_iv / float(np.sqrt(252.0))
        cap = cfg.vol_cap_mult * target_daily_vol / float(np.sqrt(steps))

    spots = np.empty((n_paths, steps))
    sig_out = np.empty((n_paths, steps))
    s = np.full(n_paths, float(s0))
    sigma2 = np.full(n_paths, max(p.omega / max(1e-12, (1 - p.alpha - gamma / 2 - p.beta)), 1e-16))
    for t in range(steps):
        sig_t = np.minimum(np.sqrt(sigma2) * ushape[t], cap)
        eps = sig_t * z[:, t]
        s = s * np.exp(eps)
        spots[:, t] = s
        sig_out[:, t] = sig_t
        neg = eps < 0
        sigma2 = (p.omega + p.alpha * eps ** 2 + gamma * eps ** 2 * neg + p.beta * sigma2)
        sigma2 = np.maximum(sigma2, 1e-16)            # variance floor (spec §9)
    spots = np.clip(spots, s0 * 0.5, s0 * 1.5)        # spot guard band (spec §9)
    return SimPaths(spots=spots, sigmas=sig_out)
