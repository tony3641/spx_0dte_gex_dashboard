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
from strategy_models import Strategy, Condition, TriggerSpec, RuntimeState, ExitRules, StopLoss
from market_hours import (
    now_et, is_short_trading_day, is_nfp_day, is_fomc_day, is_pm_settle,
    resolve_trading_expiration,
)

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


def _num(params: dict, key: str) -> Optional[float]:
    """Float value of a param, or None when absent/"" (i.e. the bound is 'n/a')."""
    v = params.get(key)
    if v is None or v == "":
        return None
    return float(v)


def _lo(params: dict, key: str) -> float:
    """Lower bound. A present-but-unset bound means unbounded (-inf)."""
    v = _num(params, key)
    return float("-inf") if v is None else v


def _hi(params: dict, key: str) -> float:
    """Upper bound. A present-but-unset bound means unbounded (+inf)."""
    v = _num(params, key)
    return float("inf") if v is None else v


def generate_candidates(strategy: Strategy, state, max_n: int = 20) -> List[Candidate]:
    rows = chain_rows(state)
    spot = float(getattr(state, "spx_price", 0) or 0.0)
    cond = {c.kind: c for c in strategy.conditions if c.enabled}
    delta_c = cond.get("short_delta")
    width_c = cond.get("spread_width")
    credit_c = cond.get("credit")

    dmin, dmax = (_lo(delta_c.params, "min"), _hi(delta_c.params, "max")) if delta_c else (0.05, 0.35)
    wmin, wmax = (_lo(width_c.params, "min"), _hi(width_c.params, "max")) if width_c else (5, 50)
    cmin, cmax = (_lo(credit_c.params, "min"), _hi(credit_c.params, "max")) if credit_c else (0.0, 1e6)

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


def _passes_bucket(value: Optional[float], op: str, lo: Optional[float], hi: Optional[float]) -> bool:
    if value is None:
        return False
    if op == "above":
        return hi is None or value > hi
    if op == "below":
        return lo is None or value < lo
    if op == "range":
        return (lo is None or value >= lo) and (hi is None or value <= hi)
    return False


def _bucket_params(p: dict, base: str) -> tuple:
    op = p.get(f"{base}_op", "range")
    lo = _num(p, f"{base}_low")
    if lo is None:
        lo = _num(p, f"{base}_value")
    hi = _num(p, f"{base}_high")
    if hi is None and _num(p, f"{base}_value") is not None:
        hi = _num(p, f"{base}_value")
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
        lo = _num(p, "low")
        if lo is None:
            lo = _num(p, "value")
        hi = _num(p, "high")
        if hi is None:
            hi = _num(p, "value") if _num(p, "value") is not None else lo
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
            if not (_lo(p, "min") <= abs(c.short_delta) <= _hi(p, "max")):
                ok = False
        if "spread_width" in per:
            p = per["spread_width"].params
            if not (_lo(p, "min") <= c.width_points <= _hi(p, "max")):
                ok = False
        if "credit" in per:
            p = per["credit"].params
            if not (_lo(p, "min") <= c.credit_mid <= _hi(p, "max")):
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


def get_runtime(state, name: str) -> RuntimeState:
    rt = getattr(state, "runtime", {}).get(name)
    if rt is None:
        rt = RuntimeState()
        state.runtime[name] = rt
    return rt


def reset_strategy_runtime(state, name: str) -> None:
    """Start a fresh arming cycle for a strategy and re-prime its children."""
    rt = get_runtime(state, name)
    rt.cycle += 1
    rt.entered = False
    rt.done = False
    rt.trade = None
    rt.time_met = False
    rt.parent_cycle = 0
    state.strategy_open_positions.pop(name, None)   # stale map entry (non-TP close) must not re-block
    parent = state.strategies.get(name)
    if parent is not None:
        for child in state.strategies.values():
            if child.parent_name == name:
                crt = get_runtime(state, child.name)
                crt.time_met = False
                crt.parent_cycle = 0


def _trigger_aggregate(child: Strategy, state, now=None):
    """Aggregate a child's enabled triggers against the parent's current trade."""
    now = now or now_et()   # spec §5: T1 time-of-day binds to the ET clock
    prt = get_runtime(state, child.parent_name)
    crt = get_runtime(state, child.name)
    ptrade = prt.trade
    enabled = [t for t in child.subsequent_triggers if t.enabled]
    if not enabled:
        return False

    def one(t):
        if t.kind == "time_of_day":
            if crt.time_met and crt.parent_cycle == prt.cycle:
                return True
            hm = now.strftime("%H:%M")
            start, end = t.params.get("start", "09:30"), t.params.get("end", "15:30")
            return start <= hm <= end
        if ptrade is None:
            return False
        if t.kind == "parent_exit_reason":
            return ptrade.get("close_reason") == t.params.get("reason")
        if t.kind == "parent_unrealized_pnl":
            credit = float(ptrade.get("credit") or 0.0)
            if credit <= 0:
                return False
            gain = t.params.get("gain_multiple")
            loss = t.params.get("loss_multiple")
            if gain is None and loss is None:
                return False
            hi = float(ptrade.get("high_water_mult") or 0.0)
            lo = float(ptrade.get("low_water_mult") or 0.0)
            if gain is not None and hi >= float(gain):
                return True
            if loss is not None and lo <= -float(loss):
                return True
            return False
        return False

    results = [one(t) for t in enabled]
    return all(results) if child.trigger_logic == "all" else any(results)


def _child_is_eligible(child: Strategy, state, now=None):
    """A child may be evaluated/entered only if its trigger fired AND its
    parent has closed its trade this cycle (child waits for parent flat)."""
    parent = state.strategies.get(child.parent_name)
    if parent is None:
        return False
    prt = get_runtime(state, parent.name)
    crt = get_runtime(state, child.name)
    if crt.entered or crt.done:
        return False
    if not prt.entered or not prt.done:
        return False   # parent must have traded AND closed
    # prt.done flips the moment a TP close order is accepted, possibly before
    # the close legs are confirmed dropped — the child must wait for the parent
    # to be truly flat (spec §3), or we'd briefly hold two positions in a branch.
    parent_trade = prt.trade
    if parent_trade is None:
        return False
    parent_cand = Candidate(**parent_trade["candidate"])
    if len(find_strategy_positions(parent_cand, state)) >= 2:
        return False   # parent not truly flat yet
    return _trigger_aggregate(child, state, now)


def _has_open_position(state, sig) -> bool:
    """True if any open position matches the signature set of (strike, right)."""
    for pos in getattr(state, "positions", []) or []:
        c = pos.get("contract") or {}
        if (float(c.get("strike", 0) or 0), c.get("right", "")) in sig:
            return True
    return False


def _has_margin(state, candidate, budget=None) -> bool:
    """Conservative buying-power check against account ExcessLiquidity and the
    strategy's per-trade budget cap.

    Allows placement when account data is unavailable (excess unknown) or when
    the candidate's margin requirement is within excess liquidity. When a
    per-strategy budget is set, the candidate's margin must also fit within it
    (a malformed budget refuses to auto-trade rather than slipping through).
    """
    summary = getattr(state, "account_summary", {}) or {}
    excess = summary.get("ExcessLiquidity")

    # Per-strategy budget cap on a single entry's margin requirement.
    if budget is not None:
        try:
            if candidate.margin > float(budget):
                return False
        except (TypeError, ValueError):
            return False   # malformed budget -> refuse to auto-trade

    # Account ExcessLiquidity (conservatively allow when unknown).
    if excess is not None:
        try:
            return candidate.margin <= float(excess)
        except (TypeError, ValueError):
            return True
    return True


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
    trading_class = getattr(state, "trading_class", "SPXW")
    legs = [
        {"symbol": "SPX", "expiry": getattr(state, "expiration", ""), "strike": candidate.short_strike,
         "right": right, "action": "SELL", "qty": 1, "lmtPrice": float(short_lmt or 0.01),
         "secType": "OPT", "trading_class": trading_class},
        {"symbol": "SPX", "expiry": getattr(state, "expiration", ""), "strike": candidate.long_strike,
         "right": right, "action": "BUY", "qty": 1, "lmtPrice": float(long_lmt or 0.01),
         "secType": "OPT", "trading_class": trading_class},
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
    # PM-settle guard: never place a trade on an AM-settled (open) contract.
    trading_class = getattr(state, "trading_class", "SPXW")
    if not is_pm_settle(trading_class):
        logger.error(f"{strategy.name}: refusing to trade AM-settled class '{trading_class}'")
        resp = {"type": "order_status", "data": {"status": "Error", "message": "AM-settled option refused"}}
        state.strategy_log.append({
            "ts": time.monotonic(), "slug": "entry",
            "strategy": strategy.name, "candidate": candidate.to_dict(), "resp": resp,
            "status": "Error",
        })
        return resp
    payload = _build_entry_payload(strategy, candidate, state)
    resp = await handle_place_order(ib, state, payload, ws=None, refresh_fn=refresh_account_state)
    status = (resp.get("data") or {}).get("status", "")
    if status not in ("Error", ""):
        state.strategy_open_positions[strategy.name] = candidate.to_dict()
    state.strategy_log.append({
        "ts": time.monotonic(), "slug": "entry",
        "strategy": strategy.name, "candidate": candidate.to_dict(), "resp": resp,
        "status": status,
    })
    return resp


def _strategy_should_run_today(strat: Strategy, state, today=None) -> bool:
    """Schedule gate: is this strategy allowed to run on ``today``?

    Enforced in order: day-of-week → holiday/no-0DTE → early-close half-day →
    FOMC → NFP. The holiday check only applies once expiration data is loaded,
    so an unpopulated test state isn't mistaken for a holiday.
    """
    today = today or now_et().date()
    if today.weekday() not in strat.run_days:
        return False
    if (getattr(state, "expirations", None) or getattr(state, "monthly_expirations", None)):
        if resolve_trading_expiration(state, ref=today) is None:
            return False   # holiday / no 0DTE today
    if is_short_trading_day(ref=today) and not strat.short_day_enabled:
        return False
    if is_fomc_day(ref=today) and not strat.run_on_fomc:
        return False
    if is_nfp_day(ref=today) and not strat.run_on_nfp:
        return False
    return True


async def strategy_evaluation_loop(ib, state, broadcast_fn):
    """On a cadence, evaluate armed strategies and act (scan or auto).

    One-shot per strategy per day/cycle: a strategy is skipped once its cycle's
    trade is used (entered/done) or a position is awaiting close. Children are
    gated on the parent's close triggers via _child_is_eligible; a positioned
    strategy's parent role is kept live via _update_parent_role.
    """
    interval = 3.0
    while True:
        try:
            await asyncio.sleep(interval)
            if not getattr(state, "connected", False):
                continue
            today = now_et().date()
            day_key = today.isoformat()
            if getattr(state, "day_key", None) != day_key:
                _daily_reset(state)
                state.day_key = day_key
            for name, strat in list(state.strategies.items()):
                if not strat.armed:
                    continue
                if not _strategy_should_run_today(strat, state):
                    state.strategy_candidates[name] = []
                    continue
                rt = get_runtime(state, name)
                if rt.entered or rt.done or name in state.strategy_open_positions:
                    # this cycle's one trade is used (or a position is awaiting
                    # close); keep the parent role live and do not re-enter
                    await _update_parent_role(name, state, broadcast_fn)
                    state.strategy_candidates[name] = []
                    continue
                if strat.parent_name:
                    if not _child_is_eligible(strat, state):
                        state.strategy_candidates[name] = []
                        continue
                ev = evaluate_conditions(strat, state, now=now_et())
                state.strategy_candidates[name] = [c.to_dict() for c in ev.candidates]
                if ev.status != "ready":
                    continue
                if getattr(state, "auto_trade_kill_switch", False):
                    continue
                if strat.auto_execute and ev.candidates:
                    best = ev.candidates[0]
                    if not _has_margin(state, best, budget=strat.budget):
                        logger.info(f"{strat.name}: insufficient margin/budget, skipping auto entry")
                        continue
                    try:
                        resp = await place_strategy_entry(ib, state, strat, best)
                        if (resp.get("data") or {}).get("status", "") not in ("Error", ""):
                            rt.entered = True
                            rt.trade = {
                                "candidate": best.to_dict(),
                                "credit": float(best.credit_mid),
                                "open_ts": time.monotonic(),
                                "close_ts": None,
                                "close_reason": None,
                                "high_water_mult": 0.0,
                                "low_water_mult": 0.0,
                            }
                            await broadcast_fn({"type": "strategy_exit", "data": {"name": name, "event": "auto_entry"}})
                    except Exception as e:
                        logger.error(f"Auto entry failed for {name}: {e}")
                elif not strat.auto_execute:
                    await broadcast_fn({"type": "strategy_candidate",
                                        "data": {"name": name, "candidates": [c.to_dict() for c in ev.candidates]}})
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


def classify_parent_close(strat: Strategy, cand: Candidate, state) -> str:
    """Why did the parent's trade close? TP is set by take_profit_loop; here we
    infer the fallback: the IB stop bracket if configured, else expire/manual."""
    if strat.exit_rules.stop_loss is not None:
        return "stop_loss"
    exp = getattr(state, "expiration", "")
    if exp and exp <= now_et().strftime("%Y%m%d"):
        return "expire"
    return "manual"


def _refresh_trade_credit(state, trade: dict) -> None:
    """Best-effort actual-credit update from fills (side SLD = short leg)."""
    cand = trade.get("candidate") or {}
    if not cand:
        return
    right = short_right_for(cand.get("direction", "bull_put"))
    short_strike = cand.get("short_strike")
    long_strike = cand.get("long_strike")
    short_fill = long_fill = None
    for ex in getattr(state, "executions", []) or []:
        if ex.get("strike") == short_strike and ex.get("right") == right and ex.get("side") == "SLD":
            short_fill = ex.get("price")
        if ex.get("strike") == long_strike and ex.get("right") == right and ex.get("side") == "BOT":
            long_fill = ex.get("price")
    if short_fill is not None and long_fill is not None:
        trade["credit"] = round(float(short_fill) - float(long_fill), 4)


def _latch_time_triggers(strat: Strategy, state) -> None:
    """While the parent is open, latch each child's time-of-day window."""
    prt = get_runtime(state, strat.name)
    hm = now_et().strftime("%H:%M")
    for child in state.strategies.values():
        if child.parent_name != strat.name:
            continue
        crt = get_runtime(state, child.name)
        if crt.parent_cycle != prt.cycle:
            crt.time_met = False
            crt.parent_cycle = prt.cycle
        for t in child.subsequent_triggers:
            if t.kind == "time_of_day" and t.enabled:
                start, end = t.params.get("start", "09:30"), t.params.get("end", "15:30")
                if start <= hm <= end:
                    crt.time_met = True


async def fire_children(parent: Strategy, state, broadcast_fn=None) -> None:
    """Evaluate children's triggers at the parent's close and announce eligible ones."""
    for child in state.strategies.values():
        if child.parent_name != parent.name:
            continue
        if _trigger_aggregate(child, state):
            if broadcast_fn is not None:
                await broadcast_fn({"type": "strategy_trigger",
                                    "data": {"name": child.name, "event": "eligible"}})


async def _update_parent_role(name: str, state, broadcast_fn=None) -> None:
    """Maintain a strategy's parent role while positioned; on close, classify the
    reason and fire children. Idempotent via done."""
    strat = state.strategies.get(name)
    rt = get_runtime(state, name)
    if strat is None or rt.done or rt.trade is None:
        return
    trade = rt.trade
    cand = Candidate(**trade["candidate"])
    positions = find_strategy_positions(cand, state)
    if len(positions) >= 2:
        # still open: refresh actual credit + water marks + time latches
        _refresh_trade_credit(state, trade)
        net_pnl = sum(float(p.get("unrealizedPNL", 0) or 0) for p in positions)
        credit = float(trade.get("credit") or 0.0)
        if credit > 0:
            mult = net_pnl / credit
            trade["high_water_mult"] = max(float(trade.get("high_water_mult", 0.0)), mult)
            trade["low_water_mult"] = min(float(trade.get("low_water_mult", 0.0)), mult)
        _latch_time_triggers(strat, state)
        return
    # closed
    trade["close_ts"] = time.monotonic()
    trade["close_reason"] = classify_parent_close(strat, cand, state)
    rt.done = True
    await fire_children(strat, state, broadcast_fn)


def _daily_reset(state) -> None:
    """Start a fresh cycle each day, preserving any still-open position."""
    for name, strat in state.strategies.items():
        rt = get_runtime(state, name)
        if rt.trade is None:
            continue
        cand = Candidate(**rt.trade["candidate"])
        if len(find_strategy_positions(cand, state)) >= 2:
            continue   # open -> preserve
        rt.cycle += 1
        rt.entered = False
        rt.done = False
        rt.trade = None
        rt.time_met = False
        rt.parent_cycle = 0
        state.strategy_open_positions.pop(name, None)   # stale map entry (non-TP close) must not re-block


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
    trading_class = getattr(state, "trading_class", "SPXW")
    legs = [
        {"symbol": "SPX", "expiry": getattr(state, "expiration", ""), "strike": candidate.short_strike,
         "right": right, "action": "BUY", "qty": 1, "lmtPrice": round(float(short_ask), 2),
         "secType": "OPT", "trading_class": trading_class},
        {"symbol": "SPX", "expiry": getattr(state, "expiration", ""), "strike": candidate.long_strike,
         "right": right, "action": "SELL", "qty": 1, "lmtPrice": round(float(long_bid), 2),
         "secType": "OPT", "trading_class": trading_class},
    ]
    payload = {"legs": legs, "orderType": "LMT", "tif": "DAY", "comboAction": "BUY",
               "comboLmtPrice": round_signed_to_tick(net, tick), "comboQuantity": 1, "outsideRth": False}
    resp = await handle_place_order(ib, state, payload, ws=None, refresh_fn=refresh_account_state)
    status = (resp.get("data") or {}).get("status", "")
    return status not in ("Error",)


async def take_profit_loop(ib, state, broadcast_fn):
    """Watch strategy-tagged positions and flatten at take-profit target."""
    while True:
        try:
            await asyncio.sleep(3.0)
            if not getattr(state, "connected", False):
                continue
            for name, cand_dict in list(state.strategy_open_positions.items()):
                strat = state.strategies.get(name)
                if strat is None:
                    continue
                tp = strat.exit_rules.take_profit
                if tp is None:
                    continue
                cand = Candidate(**cand_dict)
                positions = find_strategy_positions(cand, state)
                if len(positions) < 2:
                    continue
                closed = await maybe_flatten_at_take_profit(ib, state, cand, positions, tp)
                if closed:
                    rt = get_runtime(state, name)
                    rt.done = True
                    if rt.trade is not None:
                        rt.trade["close_ts"] = time.monotonic()
                        rt.trade["close_reason"] = "take_profit"
                    state.strategy_open_positions.pop(name, None)
                    await broadcast_fn({"type": "strategy_exit", "data": {"name": name, "event": "take_profit"}})
                    await fire_children(strat, state, broadcast_fn)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"take_profit_loop error: {e}")
            await asyncio.sleep(3)
