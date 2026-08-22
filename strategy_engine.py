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


def signature_for_candidate(candidate: Candidate, state) -> set:
    """Contract signature (set of (strike, right)) to match a strategy to positions."""
    return {(candidate.short_strike, short_right_for(candidate.direction)),
            (candidate.long_strike, short_right_for(candidate.direction))}


def short_right_for(direction: str) -> str:
    return "P" if direction == "bull_put" else "C"


def _has_open_position(state, sig) -> bool:
    """True if any open position matches the signature set of (strike, right)."""
    for pos in getattr(state, "positions", []) or []:
        c = pos.get("contract") or {}
        if (float(c.get("strike", 0) or 0), c.get("right", "")) in sig:
            return True
    return False


def _build_entry_payload(strategy: Strategy, candidate: Candidate, state) -> dict:
    from config import spx_tick_for_price, round_signed_to_tick
    rows = chain_rows(state)
    short_row = _find_row(rows, candidate.short_strike)
    long_row = _find_row(rows, candidate.long_strike)
    right = short_right_for(candidate.direction)
    tick = spx_tick_for_price(abs(candidate.credit_mid))
    # Per-leg limit: SELL leg uses bid (receive), BUY leg uses ask (pay) — mirror order-entry.js.
    short_lmt = _side_field(short_row, right, "bid") if short_row else candidate.credit_mid
    long_lmt = _side_field(long_row, right, "ask") if long_row else candidate.credit_mid
    legs = [
        {"symbol": "SPX", "expiry": getattr(state, "expiration", ""), "strike": candidate.short_strike,
         "right": right, "action": "SELL", "qty": 1, "lmtPrice": float(short_lmt or 0.01), "secType": "OPT"},
        {"symbol": "SPX", "expiry": getattr(state, "expiration", ""), "strike": candidate.long_strike,
         "right": right, "action": "BUY", "qty": 1, "lmtPrice": float(long_lmt or 0.01), "secType": "OPT"},
    ]
    # Stop-loss bracket: the trigger/limit price = |credit| * multiplier
    # (signed negative, e.g. credit -0.30 with 5x -> -1.50). order_manager
    # abs()s the stop/limit, so the sign is for clarity.
    sl = strategy.exit_rules.stop_loss
    payload_stop_loss = None
    if sl is not None:
        stop_mag = abs(candidate.credit_mid) * sl.multiplier
        stop_tick = spx_tick_for_price(stop_mag)
        payload_stop_loss = {
            "stopPrice": round_signed_to_tick(-stop_mag, stop_tick),
            "limitPrice": round_signed_to_tick(-stop_mag, stop_tick),
        }

    return {
        "legs": legs,
        "orderType": "LMT", "tif": "DAY",
        "comboAction": "BUY",
        "comboLmtPrice": round_signed_to_tick(-abs(candidate.credit_mid), tick),
        "comboQuantity": 1,
        "outsideRth": False,
        "stopLoss": payload_stop_loss,
    }


async def place_strategy_entry(ib, state, strategy, candidate) -> dict:
    from order_manager import handle_place_order
    from account_manager import refresh_account_state
    payload = _build_entry_payload(strategy, candidate, state)
    resp = await handle_place_order(ib, state, payload, ws=None, refresh_fn=refresh_account_state)
    state.strategy_log.append({
        "ts": time.monotonic(), "slug": "entry",
        "strategy": strategy.name, "candidate": candidate.to_dict(), "resp": resp,
        "status": getattr(resp, "status", None),
    })
    return resp


async def strategy_evaluation_loop(ib, state, broadcast_fn):
    """On a cadence, evaluate armed strategies and act (scan or auto)."""
    from market_hours import now_et
    interval = 3.0
    while True:
        try:
            await asyncio.sleep(interval)
            if not getattr(state, "connected", False):
                continue
            for name, strat in list(state.strategies.items()):
                if not strat.armed:
                    continue
                ev = evaluate_conditions(strat, state, now=now_et())
                state.strategy_candidates[name] = [c.to_dict() for c in ev.candidates]
                if ev.status != "ready":
                    continue
                if getattr(state, "auto_trade_kill_switch", False):
                    continue
                if strat.auto_execute and ev.candidates:
                    best = ev.candidates[0]
                    if _has_open_position(state, signature_for_candidate(best, state)):
                        continue   # spec §11: one concurrent auto position per strategy
                    try:
                        await place_strategy_entry(ib, state, strat, best)
                        await broadcast_fn({"type": "strategy_exit", "data": {"name": name, "event": "auto_entry"}})
                    except Exception as e:
                        logger.error(f"Auto entry failed for {name}: {e}")
                elif not strat.auto_execute:
                    await broadcast_fn({"type": "strategy_candidate", "data": {"name": name, "candidates": [c.to_dict() for c in ev.candidates]}})
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"strategy_evaluation_loop error: {e}")
            await asyncio.sleep(3)


def find_strategy_positions(candidate, state) -> list:
    """Positions matching the candidate's leg signature."""
    sig = signature_for_candidate(candidate, state)
    out = []
    for pos in getattr(state, "positions", []) or []:
        c = pos.get("contract") or {}
        key = (float(c.get("strike", 0) or 0), c.get("right", ""))
        if key in sig:
            out.append(pos)
    return out


def _tp_target(tp, max_credit: float) -> float:
    if tp is None:
        return 0.0
    if tp.mode == "pct_credit":
        return tp.value * max_credit
    if tp.mode == "dollar":
        return tp.value
    # credit_price mode: treat value as absolute & ignored here; handled by caller
    return tp.value


async def maybe_flatten_at_take_profit(ib, state, candidate, positions, tp) -> bool:
    """Close the spread when net PnL across its legs reaches the TP target.

    Returns True only when a close order was actually accepted (not on Error),
    so the caller can broadcast an exit once and not re-attempt every cadence.
    """
    net_pnl = sum(float(p.get("unrealizedPNL", 0) or 0) for p in positions)
    max_credit = float(candidate.credit_mid or 0)
    target = _tp_target(tp, max_credit)
    if net_pnl < target:
        return False
    # Marketable close: BUY back the short at ask, SELL the long at bid. The
    # framework needs a real per-leg limit (LMT validation in handle_place_order);
    # _place_multi_leg recomputes the signed BAG limit from per-leg prices.
    from order_manager import handle_place_order
    from account_manager import refresh_account_state
    from config import spx_tick_for_price, round_signed_to_tick
    right = short_right_for(candidate.direction)
    rows = chain_rows(state)
    short_row = _find_row(rows, candidate.short_strike)
    long_row = _find_row(rows, candidate.long_strike)
    short_ask = _side_field(short_row, right, "ask") if short_row else None
    long_bid = _side_field(long_row, right, "bid") if long_row else None
    if short_ask is None or long_bid is None:
        return False   # no quotes — cannot price a marketable LMT close
    net = round(float(short_ask) - float(long_bid), 2)
    tick = spx_tick_for_price(abs(net))
    legs = [
        {"symbol": "SPX", "expiry": getattr(state, "expiration", ""), "strike": candidate.short_strike,
         "right": right, "action": "BUY", "qty": 1, "lmtPrice": round(float(short_ask), 2), "secType": "OPT"},
        {"symbol": "SPX", "expiry": getattr(state, "expiration", ""), "strike": candidate.long_strike,
         "right": right, "action": "SELL", "qty": 1, "lmtPrice": round(float(long_bid), 2), "secType": "OPT"},
    ]
    payload = {"legs": legs, "orderType": "LMT", "tif": "DAY", "comboAction": "BUY",
               "comboLmtPrice": round_signed_to_tick(net, tick), "comboQuantity": 1, "outsideRth": False}
    resp = await handle_place_order(ib, state, payload, ws=None, refresh_fn=refresh_account_state)
    status = (resp.get("data") or {}).get("status", "")
    return status not in ("Error",)


async def take_profit_loop(ib, state, broadcast_fn):
    """Watch strategy-tagged positions and flatten at take-profit target."""
    from market_hours import now_et
    while True:
        try:
            await asyncio.sleep(3.0)
            if not getattr(state, "connected", False):
                continue
            for name, strat in list(state.strategies.items()):
                tp = strat.exit_rules.take_profit
                if tp is None:
                    continue
                cands = [Candidate(**c) for c in state.strategy_candidates.get(name, [])]
                for cand in cands:
                    positions = find_strategy_positions(cand, state)
                    if len(positions) < 2:
                        continue
                    closed = await maybe_flatten_at_take_profit(ib, state, cand, positions, tp)
                    if closed:
                        await broadcast_fn({"type": "strategy_exit", "data": {"name": name, "event": "take_profit"}})
                        break
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"take_profit_loop error: {e}")
            await asyncio.sleep(3)
