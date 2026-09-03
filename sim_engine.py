"""Strategy simulation over synthetic paths: entry scan, exit scans, family re-entry.

Pricing is computed per minute inside the loops (no path x step x strike tensors).
Candidate masking/ranking mirrors strategy_engine.generate_candidates: conditions on
credit MID, best-first by mid; the FILL is the spec's combo rule (never better than
the natural, tick-floored). Entry window defaults and unset-bound semantics match
strategy_engine._lo/_hi.
"""
from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np

from sim_calibrate import CalibratedModel
from sim_config import SimRunConfig
from sim_pricing import (RISK_FREE_RATE, bar_year_frac, bsm_put, bsm_put_delta,
                         build_ladder, combo_fill_credit, half_spread, smile_iv, tick_floor)
from strategy_models import Condition, Strategy

RTH_START_MIN = 570          # 09:30
DEFAULT_WINDOW = (0, 360)    # 09:30 -> 15:30 bar-index window (live engine default)


@dataclass
class EntryState:
    entered: np.ndarray
    entry_minute: np.ndarray
    short_idx: np.ndarray
    long_idx: np.ndarray
    width: np.ndarray
    qty: np.ndarray
    fill_credit: np.ndarray
    theo_credit: np.ndarray


def _num(params: dict, key: str) -> Optional[float]:
    v = params.get(key)
    if v is None or v == "":
        return None
    return float(v)


def extract_conditions(strategy: Strategy) -> dict:
    """Same unset-bound semantics as strategy_engine._lo/_hi."""
    cond = {c.kind: c for c in strategy.conditions if c.enabled}
    out = dict(dmin=0.05, dmax=0.35, wmin=5.0, wmax=50.0, cmin=0.0, cmax=float("inf"),
               vix=None, trend=None)
    if "short_delta" in cond:
        p = cond["short_delta"].params
        out["dmin"] = _num(p, "min") if _num(p, "min") is not None else 0.05
        out["dmax"] = _num(p, "max") if _num(p, "max") is not None else 0.35
    if "spread_width" in cond:
        p = cond["spread_width"].params
        out["wmin"] = _num(p, "min") if _num(p, "min") is not None else 5.0
        out["wmax"] = _num(p, "max") if _num(p, "max") is not None else 50.0
    if "credit" in cond:
        p = cond["credit"].params
        out["cmin"] = _num(p, "min") if _num(p, "min") is not None else 0.0
        out["cmax"] = _num(p, "max") if _num(p, "max") is not None else float("inf")
    if "volatility" in cond and cond["volatility"].params.get("vix_enabled"):
        out["vix"] = cond["volatility"].params
    out["trend"] = cond.get("trend")
    return out


def window_minutes(strategy: Strategy, steps: int, bar_seconds: int) -> Tuple[int, int]:
    """Entry window as bar-index range [w0, w1] inclusive."""
    bar_min = bar_seconds // 60
    cond = {c.kind: c for c in strategy.conditions if c.enabled}
    if "entry_window" not in cond:
        return DEFAULT_WINDOW
    p = cond["entry_window"].params

    def to_idx(hm: str) -> int:
        h, m = hm.split(":")
        minutes = int(h) * 60 + int(m)
        return int((minutes - RTH_START_MIN) // bar_min) - 1   # bar that CLOSES at/after hm

    w0 = max(to_idx(p.get("start", "09:30")), 0)
    w1 = min(to_idx(p.get("end", "15:30")), steps - 1)
    return w0, max(w0, w1)


def vix_map(model: CalibratedModel, sigma_col: np.ndarray) -> np.ndarray:
    """Spec §5.4: VIX_t = clip(vix0 * sigma/sigma0, 5, 100)."""
    return np.clip(model.vix0 * sigma_col / model.sigma0, 5.0, 100.0)


def _qty_for(budget, margin: np.ndarray) -> np.ndarray:
    if budget is None:
        return np.ones(len(margin), dtype=np.int32)
    return np.maximum(np.floor(float(budget) / np.maximum(margin, 1.0)), 0).astype(np.int32)


def run_entry(model: CalibratedModel, cfg: SimRunConfig, strategy: Strategy,
              paths, ladder: np.ndarray, per_path_start=None, k: Optional[float] = None) -> EntryState:
    n, steps = paths.spots.shape
    bar_secs = BAR_SECONDS_GET(cfg)
    w0, w1 = window_minutes(strategy, steps, bar_secs)
    starts = np.broadcast_to(
        np.asarray(per_path_start if per_path_start is not None else 0, dtype=int), (n,)).copy()
    cond = extract_conditions(strategy)
    T_left = np.arange(steps - 1, -1, -1) * bar_year_frac(bar_secs)   # years to expiry after bar t
    step = float(ladder[1] - ladder[0]) if len(ladder) > 1 else 5.0

    entered = np.zeros(n, dtype=bool)
    entry_minute = np.full(n, -1, dtype=np.int32)
    short_idx = np.full(n, -1, dtype=np.int32)
    long_idx = np.full(n, -1, dtype=np.int32)
    width = np.zeros(n)
    qty = np.zeros(n, dtype=np.int32)
    fill = np.zeros(n)
    theo = np.zeros(n)

    if k is not None:
        # ---- dynamic_k experiment: window-only gate, first window minute ----
        t = int(max(w0, int(starts.min())))
        if t >= steps:
            return EntryState(entered, entry_minute, short_idx, long_idx, width, qty, fill, theo)
        active = ~entered & (starts <= t)
        if not active.any():
            return EntryState(entered, entry_minute, short_idx, long_idx, width, qty, fill, theo)
        sigma_ann = model.sigma_annual(cfg)
        s_entry = paths.spots[:, t]
        target = s_entry * (1.0 - k * sigma_ann)
        s_idx = np.clip(((target - ladder[0]) / step).astype(int), 0, len(ladder) - 1)
        # ensure we never round UP past the target (short below target)
        s_idx = np.where(ladder[s_idx] > target, np.maximum(s_idx - 1, 0), s_idx)
        w_pts = float(cfg.width_points)
        l_idx = np.clip(s_idx - int(round(w_pts / step)), 0, len(ladder) - 1)
        ok = active & (s_idx > l_idx)
        iv_t = smile_iv((ladder - s_entry[:, None]) / s_entry[:, None], model.smile,
                        paths.sigmas[:, t:t + 1], model.sigma0, cfg.vol_beta, cfg.flat_iv,
                        float(model.smile.iv(0.0)))
        put = bsm_put(paths.spots[:, t:t + 1], ladder[None, :], T_left[t], RISK_FREE_RATE, iv_t)
        hs = half_spread((ladder - s_entry[:, None]) / s_entry[:, None], model.smile.half_spread_atm)
        rows = np.arange(n)
        cm = put[rows, s_idx] - put[rows, l_idx]              # mid
        cc = (put[rows, s_idx] - hs[rows, s_idx]) - (put[rows, l_idx] + hs[rows, l_idx])
        margin = (s_idx - l_idx) * step * 100.0
        q = _qty_for(strategy.budget, np.where(ok, margin, 1.0))
        ok &= q > 0
        entered, entry_minute = ok, np.where(ok, t, -1).astype(np.int32)
        short_idx, long_idx = np.where(ok, s_idx, -1).astype(np.int32), np.where(ok, l_idx, -1).astype(np.int32)
        width, qty = np.where(ok, (s_idx - l_idx) * step, 0.0), np.where(ok, q, 0).astype(np.int32)
        theo = np.where(ok, cm, 0.0)
        fill = np.where(ok, [combo_fill_credit(a, b, cfg.tick_size) for a, b in zip(cm, cc)], 0.0)
        return EntryState(entered, entry_minute, short_idx, long_idx, width, qty, fill, theo)

    # ---- engine mode: per-minute scan over the window, same semantics as generate_candidates ----
    widths_all = np.arange(int(np.ceil(cond["wmin"] / step)) * step, cond["wmax"] + step / 2, step)
    trend = cond["trend"]
    if trend is not None:
        # inline trend-series computation (binding note): pmove + Wilder-seeded RSI per window minute
        closes = paths.spots                                  # (n, steps)
        pm_n = int(trend.params.get("minutes", 5))
        pmove = np.full((n, steps), np.nan)
        if steps > pm_n:
            pmove[:, pm_n:] = (closes[:, pm_n:] / closes[:, :-pm_n] - 1.0) * 100.0
        rsi = np.full((n, steps), np.nan)
        period = int(trend.params.get("period", 14))
        for t2 in range(w0, w1 + 1):
            if t2 < period:
                continue
            win = closes[:, t2 - period:t2 + 1]               # (n, period+1)
            diffs = np.diff(win, axis=1)
            gains = np.where(diffs > 0, diffs, 0.0)
            losses = np.where(diffs < 0, -diffs, 0.0)
            ag, al = gains.mean(axis=1), losses.mean(axis=1)  # Wilder seeding is the
            rsi[:, t2] = np.where(al == 0, 100.0, 100.0 - 100.0 / (1.0 + ag / np.maximum(al, 1e-12)))
    best_cm = np.full(n, -np.inf)
    best_cc = np.zeros(n)
    best_s = np.full(n, -1, dtype=np.int32)
    best_w = np.zeros(n)
    for t in range(int(max(w0, int(starts.min()))), w1 + 1):
        todo = (~entered) & (starts <= t)
        if not todo.any():
            continue
        m = (ladder - paths.spots[:, t:t + 1]) / paths.spots[:, t:t + 1]   # (n, M) log-moneyness
        iv_t = smile_iv(m, model.smile, paths.sigmas[:, t:t + 1], model.sigma0,
                        cfg.vol_beta, cfg.flat_iv, float(model.smile.iv(0.0)))
        put = bsm_put(paths.spots[:, t:t + 1], ladder[None, :], T_left[t], RISK_FREE_RATE, iv_t)
        hs = half_spread(m, model.smile.half_spread_atm)
        bid, ask = put - hs, put + hs
        delta = bsm_put_delta(paths.spots[:, t:t + 1], ladder[None, :], T_left[t], RISK_FREE_RATE, iv_t)
        adelta = np.abs(delta)
        short_ok = (adelta >= cond["dmin"]) & (adelta <= cond["dmax"])
        if cond["vix"] is not None:
            v = vix_map(model, paths.sigmas[:, t])
            from strategy_engine import _passes_bucket, _bucket_params   # same bucket semantics
            op, lo, hi = _bucket_params(cond["vix"], "vix")
            vok = _passes_bucket(v, op, lo, hi)
        else:
            vok = np.ones(n, dtype=bool)
        if trend is not None:
            p = trend.params
            from strategy_engine import _passes_bucket, _bucket_params
            if p.get("pmove_enabled"):
                op, lo, hi = _bucket_params(p, "pmove")
                vok &= _passes_bucket(pmove[:, t], op, lo, hi)
            if p.get("rsi_enabled"):
                op, lo, hi = _bucket_params(p, "rsi")
                vok &= _passes_bucket(rsi[:, t], op, lo, hi)
        cand = todo & vok
        if not cand.any():
            continue
        for w in widths_all:
            off = int(round(w / step))
            if off <= 0 or off >= len(ladder):
                continue
            # bull put: SHORT at the higher strike (i+off), LONG at the lower (i)
            cm = put[:, off:] - put[:, :-off]                  # credit mid = put[short]-put[long] >= 0
            cc = bid[:, off:] - ask[:, :-off]                  # nat: sell short at bid, buy long at ask
            ok = (cand[:, None] & short_ok[:, off:]
                  & (cm >= cond["cmin"]) & (cm <= cond["cmax"]))
            if not ok.any():
                continue
            rows, cols = np.nonzero(ok)
            vals = cm[rows, cols]
            order = np.argsort(vals)                           # ascending; later wins ties? use >:
            for r, c, v in zip(rows[order], cols[order], vals[order]):
                if v > best_cm[r]:
                    best_cm[r] = v
                    best_cc[r] = cc[r, c]
                    best_s[r] = c                     # best_s = LONG index (low strike)
                    best_w[r] = w
        # commit the per-minute winners (best_s is the LONG index):
        newly = todo & np.isfinite(best_cm) & (best_cm > -np.inf)
        newly &= ~entered
        if newly.any():
            r = np.nonzero(newly)[0]
            q_r = _qty_for(strategy.budget, best_w[r] * 100.0)
            r_ok = r[q_r > 0]                                   # only spreads the path can afford
            if r_ok.size:
                entered[r_ok] = True
                entry_minute[r_ok] = t
                offs = np.round(best_w[r_ok] / step).astype(np.int32)   # width in ladder indices
                short_idx[r_ok] = best_s[r_ok] + offs                   # SHORT at high strike
                long_idx[r_ok] = best_s[r_ok]                           # LONG at low strike (best_s)
                width[r_ok] = best_w[r_ok]
                qty[r_ok] = q_r[q_r > 0]
                for j in r_ok:
                    fill[j] = combo_fill_credit(float(best_cm[j]), float(best_cc[j]), cfg.tick_size)
                theo[r_ok] = best_cm[r_ok]
            best_cm[r] = -np.inf   # clear per-minute best (entered + unaffordable alike)
        # (best_* state is reused next minute only for still-unentered paths)
    return EntryState(entered, entry_minute, short_idx, long_idx, width, qty, fill, theo)


def BAR_SECONDS_GET(cfg: SimRunConfig) -> int:
    from sim_config import BAR_SECONDS
    return BAR_SECONDS[cfg.bar_size]


@dataclass
class TrialResult:
    entered: bool
    entry_minute: int
    exit_minute: int
    exit_reason: str
    short_strike: float
    long_strike: float
    width: float
    qty: int
    fill_credit: float
    exit_debit: float
    pnl: float
    mtm: Optional[np.ndarray] = None


def _spread_rows(model, cfg, paths, ladder, t):
    """Put mids + half-spreads for every path at bar t -> (mid, bid, ask) each (n, M)."""
    m = (ladder - paths.spots[:, t:t + 1]) / paths.spots[:, t:t + 1]
    iv_t = smile_iv(m, model.smile, paths.sigmas[:, t:t + 1], model.sigma0,
                    cfg.vol_beta, cfg.flat_iv, float(model.smile.iv(0.0)))
    put = bsm_put(paths.spots[:, t:t + 1], ladder[None, :], t, RISK_FREE_RATE, iv_t)
    hs = half_spread(m, model.smile.half_spread_atm)
    return put, put - hs, put + hs


def run_exits(model: CalibratedModel, cfg: SimRunConfig, strategy: Strategy,
              paths, ladder: np.ndarray, entry: EntryState,
              sl_multiplier: Optional[float] = None) -> List[TrialResult]:
    n, steps = paths.spots.shape
    bar_secs = BAR_SECONDS_GET(cfg)
    if sl_multiplier is None:
        sl = strategy.exit_rules.stop_loss
        mult = float(sl.multiplier) if sl is not None else float("inf")
    else:
        mult = float(sl_multiplier)
    tp = strategy.exit_rules.take_profit
    tp_pct = float(tp.value) if (tp is not None and tp.mode == "pct_credit") else None

    out: List[TrialResult] = []
    rows = np.arange(n)
    for p in range(n):
        if not entry.entered[p]:
            out.append(TrialResult(entered=False, entry_minute=-1, exit_minute=-1,
                                   exit_reason="never", short_strike=0.0, long_strike=0.0,
                                   width=0.0, qty=0, fill_credit=0.0, exit_debit=0.0, pnl=0.0))
            continue
        si, li = int(entry.short_idx[p]), int(entry.long_idx[p])
        fc = float(entry.fill_credit[p])
        qty = int(entry.qty[p])
        stop_level = fc * mult
        tp_price = fc * tp_pct if tp_pct is not None else None
        t0 = int(entry.entry_minute[p])
        mtm = np.full(steps, np.nan)
        exit_t, exit_reason, exit_debit = -1, "expired", 0.0
        active = True
        for t in range(t0, steps):
            T = (steps - 1 - t) * bar_year_frac(bar_secs)
            m = (ladder - paths.spots[p, t]) / paths.spots[p, t]
            iv_t = smile_iv(m, model.smile, np.array([[paths.sigmas[p, t]]]), model.sigma0,
                            cfg.vol_beta, cfg.flat_iv, float(model.smile.iv(0.0)))[0]
            put = bsm_put(paths.spots[p, t], ladder, T, RISK_FREE_RATE, iv_t)
            hs = half_spread(m, model.smile.half_spread_atm)
            mark = float(put[si] - put[li])                    # mid mark of the spread
            mtm[t] = (fc - mark) * qty * 100.0
            if t == t0:
                continue                                       # no exit on the entry bar
            if stop_level < float("inf") and mark >= stop_level:
                exit_t, exit_reason = t, "stop"
                exit_debit = stop_level + cfg.stop_extra       # trigger + 0.10, exactly
                break
            if tp_price is not None and mark <= tp_price:
                exit_t, exit_reason = t, "take_profit"
                exit_debit = tp_price
                break
            if t == steps - 1:
                Ks, Kl = float(ladder[si]), float(ladder[li])
                exit_debit = max(Ks - paths.spots[p, t], 0.0) - max(Kl - paths.spots[p, t], 0.0)
                exit_t = t
                break
        pnl = (fc - exit_debit) * qty * 100.0
        if exit_reason == "expired" and exit_t == -1:
            exit_t, exit_debit = steps - 1, 0.0                # single-bar edge: expiry, OTM
        out.append(TrialResult(entered=True, entry_minute=t0, exit_minute=exit_t,
                               exit_reason=exit_reason, short_strike=float(ladder[si]),
                               long_strike=float(ladder[li]), width=float(entry.width[p]),
                               qty=qty, fill_credit=fc, exit_debit=exit_debit, pnl=pnl, mtm=mtm))
    return out


def run_cell(model: CalibratedModel, cfg: SimRunConfig, strategy: Strategy,
             paths, ladder: np.ndarray, sl_multiplier: Optional[float] = None,
             k: Optional[float] = None) -> List[TrialResult]:
    entry = run_entry(model, cfg, strategy, paths, ladder, k=k)
    return run_exits(model, cfg, strategy, paths, ladder, entry, sl_multiplier=sl_multiplier)
