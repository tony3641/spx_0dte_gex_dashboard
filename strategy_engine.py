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
