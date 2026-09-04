"""Risk analytics for simulation results: distribution stats, DD, ruin probability."""
from typing import List

import numpy as np

from sim_config import SimRunConfig
from sim_engine import TrialResult


def summarize(pnls: np.ndarray) -> dict:
    p = np.asarray(pnls, dtype=float)
    p = p[np.isfinite(p)]
    if len(p) == 0:
        return dict(mean=0.0, median=0.0, std=0.0, win_rate=0.0, cvar5=0.0, cvar1=0.0,
                    worst_day=0.0, n=0, entered=0)
    tail5 = p[p <= np.quantile(p, 0.05)]
    tail1 = p[p <= np.quantile(p, 0.01)]
    return dict(
        mean=float(p.mean()), median=float(np.median(p)), std=float(p.std()),
        win_rate=float((p > 0).mean()), cvar5=float(tail5.mean()) if len(tail5) else float(p.min()),
        cvar1=float(tail1.mean()) if len(tail1) else float(p.min()),
        worst_day=float(p.min()), n=int(len(p)), entered=int(len(p)))


def breakdown(results: List[TrialResult]) -> dict:
    counts = {"expired": 0, "stop": 0, "take_profit": 0, "never": 0}
    for r in results:
        counts[r.exit_reason] = counts.get(r.exit_reason, 0) + 1
    return counts


def intraday_max_dd(mtm: np.ndarray) -> float:
    x = np.asarray(mtm, dtype=float)
    x = x[~np.isnan(x)]
    if len(x) == 0:
        return 0.0
    peak = np.maximum.accumulate(x)
    return float(np.max(peak - x))


def fan_quantiles(mtms: List[np.ndarray]) -> dict:
    if not mtms:
        return dict(minutes=[], q05=[], q25=[], q50=[], q75=[], q95=[])
    mat = np.vstack(mtms)

    def _quantile(q) -> list:
        # A column can be entirely NaN (minutes before an entered path's entry minute /
        # after the latest exit — e.g. an entry window starting after bar 0). Emit None
        # for such columns: np.nan would raise ValueError in the API layer's JSON
        # serialization (allow_nan=False), while JSON null is a Plotly line gap.
        out = []
        for c in range(mat.shape[1]):
            col = mat[:, c]
            out.append(None if np.all(np.isnan(col)) else float(np.nanquantile(col, q)))
        return out

    return dict(minutes=list(range(mat.shape[1])), q05=_quantile(0.05), q25=_quantile(0.25),
                q50=_quantile(0.50), q75=_quantile(0.75), q95=_quantile(0.95))


def histogram(pnls: np.ndarray, bins: int = 40) -> dict:
    """Binned PnL distribution.

    Note: the empty/near-empty-array result (edges/counts for a zero-length input) is
    numpy-version dependent; callers with no entered paths guard on ``stats.entered == 0``
    (the sim-tab UI skips the hist chart in that case) rather than parsing the edges.
    """
    p = np.asarray(pnls, dtype=float)
    p = p[np.isfinite(p)]
    counts, edges = np.histogram(p, bins=bins)
    return dict(edges=edges.tolist(), counts=counts.tolist())


def bootstrap_ruin(pnls: np.ndarray, equity: float, threshold_pct: float,
                   n_seqs: int, seq_len: int, seed: int) -> dict:
    p = np.asarray(pnls, dtype=float)
    p = p[np.isfinite(p)]
    if len(p) == 0:
        return dict(max_dd=np.zeros(0).tolist(), mean_max_dd=0.0, p95_max_dd=0.0, ruin_prob=0.0)
    rng = np.random.default_rng(seed)
    sample = p[rng.integers(0, len(p), size=(n_seqs, seq_len))]
    curves = np.concatenate([np.zeros((n_seqs, 1)), np.cumsum(sample, axis=1)], axis=1)
    peaks = np.maximum.accumulate(curves, axis=1)
    max_dd = (peaks - curves).max(axis=1)
    threshold = equity * threshold_pct
    return dict(max_dd=max_dd.tolist(), mean_max_dd=float(max_dd.mean()),
                p95_max_dd=float(np.quantile(max_dd, 0.95)),
                ruin_prob=float((max_dd >= threshold).mean()))


def build_cell_payload(results: List[TrialResult], cfg: SimRunConfig,
                       sl_multiplier=None, k=None) -> dict:
    entered = [r for r in results if r.entered]
    pnls = np.array([r.pnl for r in results if r.entered]) if entered else np.zeros(0)
    stats = summarize(pnls)
    stats["n"] = len(results)
    stats["entered"] = len(entered)
    stats["never_entered_pct"] = (len(results) - len(entered)) / max(len(results), 1)
    dds = [intraday_max_dd(r.mtm) for r in entered if r.mtm is not None]
    mtms = [r.mtm for r in entered if r.mtm is not None]
    ruin = bootstrap_ruin(pnls, cfg.equity, cfg.ruin_threshold_pct,
                          cfg.bootstrap_seqs, cfg.bootstrap_len, cfg.seed)
    dd_stats = dict(mean=float(np.mean(dds)) if dds else 0.0,
                    p95=float(np.quantile(dds, 0.95)) if dds else 0.0,
                    worst=float(np.max(dds)) if dds else 0.0)
    return dict(sl_multiplier=("inf" if sl_multiplier == float("inf") else sl_multiplier),
                k=k, stats=stats, breakdown=breakdown(results), hist=histogram(pnls),
                dd=dd_stats, ruin_prob=ruin["ruin_prob"], dd_hist=ruin,
                fan=fan_quantiles(mtms))
