"""Credit-spread strategy engine: candidate generation, condition evaluation,
and the evaluation/take-profit loops. Works off live AppState; no extra IO."""
import asyncio
import logging
import time
from dataclasses import dataclass, field, asdict
from typing import List, Optional

from condition_helpers import (
    spread_width, spread_margin, combo_credit, nearest_row,
    wilder_rsi, percent_change, atm_iv,
)
from strategy_models import Strategy, Condition

logger = logging.getLogger(__name__)


@dataclass
class Candidate:
    direction: str
    short_strike: float
    long_strike: float
    width_points: float
    margin: float
    credit_bid: float
    credit_ask: float
    credit_mid: float
    short_delta: float
    long_delta: float
    atm_iv: Optional[float]

    def to_dict(self) -> dict:
        return asdict(self)


def chain_rows(state) -> list:
    """Rows from the live chain quotes cache."""
    cache = getattr(state, "chain_quotes_cache", None) or {}
    return cache.get("strikes", [])


def _side_field(row: dict, right: str, field: str):
    prefix = "call" if right == "C" else "put"
    return row.get(f"{prefix}_{field}")


def generate_candidates(strategy: Strategy, state, max_n: int = 20) -> List[Candidate]:
    rows = chain_rows(state)
    spot = float(getattr(state, "spx_price", 0) or 0.0)
    cond = {c.kind: c for c in strategy.conditions if c.enabled}
    delta_c = cond.get("short_delta")
    width_c = cond.get("spread_width")
    credit_c = cond.get("credit")

    dmin, dmax = (delta_c.params.get("min", 0.05), delta_c.params.get("max", 0.35)) if delta_c else (0.05, 0.35)
    wmin, wmax = (width_c.params.get("min", 5), width_c.params.get("max", 50)) if width_c else (5, 50)
    cmin, cmax = (credit_c.params.get("min", 0.0), credit_c.params.get("max", 1e6)) if credit_c else (0.0, 1e6)

    short_right = "P" if strategy.direction == "bull_put" else "C"
    long_right = short_right

    cands: List[Candidate] = []
    # Long leg sits on the opposite side of the short: for bull_put short_strike >
    # long_strike (long_sign -1); for bear_call short_strike < long_strike (+1).
    long_sign = -1 if strategy.direction == "bull_put" else 1
    for short_row in rows:
        s_strike = short_row.get("strike")
        if not s_strike:
            continue
        sd = _side_field(short_row, short_right, "delta")
        if sd is None or not (dmin <= abs(sd) <= dmax):
            continue
        for long_row in rows:
            l_strike = long_row.get("strike")
            if not l_strike:
                continue
            width = spread_width(strategy.direction, s_strike, l_strike)
            if not (wmin <= width <= wmax):
                continue
            if long_sign > 0 and l_strike <= s_strike:
                continue
            if long_sign < 0 and l_strike >= s_strike:
                continue
            cr = combo_credit(short_row, long_row, short_right)
            if cr["mid"] is None or not (cmin <= cr["mid"] <= cmax):
                continue
            ld = _side_field(long_row, short_right, "delta")
            cands.append(Candidate(
                direction=strategy.direction,
                short_strike=s_strike,
                long_strike=l_strike,
                width_points=width,
                margin=spread_margin(width),
                credit_bid=cr["bid"], credit_ask=cr["ask"], credit_mid=cr["mid"],
                short_delta=sd, long_delta=ld if ld is not None else 0.0,
                atm_iv=atm_iv(rows, spot) if spot else None,
            ))
    cands.sort(key=lambda c: c.credit_mid, reverse=True)
    return cands[:max_n]


def _find_row(rows: list, strike: float) -> Optional[dict]:
    for r in rows:
        if abs(r["strike"] - strike) < 0.01:
            return r
    return None


@dataclass
class StrategyEval:
    status: str                       # "ready" | "blocked"
    blocker: Optional[str] = None
    candidates: List[Candidate] = field(default_factory=list)
    values: dict = field(default_factory=dict)


def _passes_bucket(value: Optional[float], op: str, lo: float, hi: float) -> bool:
    if value is None:
        return False
    if op == "above":
        return value > hi
    if op == "below":
        return value < lo
    if op == "range":
        return lo <= value <= hi
    return False


def _bucket_params(p: dict, base: str) -> tuple:
    op = p.get(f"{base}_op", "range")
    lo = float(p.get(f"{base}_low", p.get(f"{base}_value", 0)))
    hi = float(p.get(f"{base}_high", p.get(f"{base}_value", 0)))
    return op, lo, hi


def _eval_condition(cond: Condition, state, now) -> tuple:
    """Return (passed, value) for a single condition; value is None on fail."""
    p = cond.params
    if cond.kind == "entry_window":
        hm = now.strftime("%H:%M")
        start, end = p.get("start", "09:30"), p.get("end", "15:30")
        return start <= hm <= end, hm
    if cond.kind == "volatility":
        vix = float(getattr(state, "vix", 0) or 0.0)
        vix_ok = True
        vix_val = None
        if p.get("vix_enabled", False):
            op, lo, hi = _bucket_params(p, "vix")
            vix_ok = _passes_bucket(vix, op, lo, hi)
            vix_val = vix
        atm_ok = True
        atm_val = None
        if p.get("atm_iv_enabled", False):
            rows = chain_rows(state)
            spot = float(getattr(state, "spx_price", 0) or 0.0)
            iv = atm_iv(rows, spot) if spot else None
            op, lo, hi = _bucket_params(p, "atm_iv")
            atm_ok = _passes_bucket(iv, op, lo, hi)
            atm_val = iv
        if not (p.get("vix_enabled", False) or p.get("atm_iv_enabled", False)):
            return True, None
        return (vix_ok and atm_ok), {"vix": vix_val, "atm_iv": atm_val}
    if cond.kind == "trend":
        closes = [b["close"] for b in getattr(state, "price_history", [])]
        indicator = p.get("indicator", "rsi")
        if indicator == "rsi":
            val = wilder_rsi(closes, int(p.get("period", 14)))
        else:
            val = percent_change(closes, int(p.get("minutes", 5)))
        op = p.get("op", "range")
        lo = float(p.get("low", p.get("value", 0)))
        hi = float(p.get("high", p.get("value", lo)))
        return _passes_bucket(val, op, lo, hi), val
    # per-candidate conditions short-circuit in evaluate_conditions
    return True, None


def evaluate_conditions(strategy: Strategy, state, now=None, candidates=None) -> StrategyEval:
    """Evaluate global conditions in priority order, then per-candidate ones.

    Global conditions (entry_window, trend, volatility) are checked first, in
    user order; if any fails, return blocked without building candidates.
    Then build candidates and drop any failing per-candidate conditions
    (short_delta, spread_width, credit). If none remain -> blocked.
    """
    import datetime as dt
    now = now or dt.datetime.now()
    values = {}
    enabled = [c for c in strategy.conditions if c.enabled]

    # 1) Global conditions, priority order, short-circuit.
    for cond in enabled:
        if cond.kind in ("short_delta", "spread_width", "credit"):
            continue
        passed, value = _eval_condition(cond, state, now)
        values[cond.kind] = value
        if not passed:
            return StrategyEval(status="blocked", blocker=cond.kind, values=values)

    # 2) Build candidates, then drop those failing per-candidate conditions.
    cands = candidates if candidates is not None else generate_candidates(strategy, state)
    per = {c.kind: c for c in enabled if c.kind in ("short_delta", "spread_width", "credit")}
    if not per:
        cands = cands or generate_candidates(strategy, state)
        if not cands:
            return StrategyEval(status="blocked", blocker="no_candidate", values=values)
        return StrategyEval(status="ready", candidates=cands, values=values)

    passing = []
    for c in cands:
        ok = True
        if "short_delta" in per:
            p = per["short_delta"].params
            if not (float(p.get("min", 0.05)) <= abs(c.short_delta) <= float(p.get("max", 0.35))):
                ok = False
        if "spread_width" in per:
            p = per["spread_width"].params
            if not (float(p.get("min", 5)) <= c.width_points <= float(p.get("max", 50))):
                ok = False
        if "credit" in per:
            p = per["credit"].params
            if not (float(p.get("min", 0.0)) <= c.credit_mid <= float(p.get("max", 1e6))):
                ok = False
        if ok:
            passing.append(c)
    if not passing:
        return StrategyEval(status="blocked", blocker="no_candidate_pass", values=values)
    return StrategyEval(status="ready", candidates=passing, values=values)
