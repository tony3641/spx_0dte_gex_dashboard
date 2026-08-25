"""
Discord bridge: command logic + event alerting.

Pure, engine-facing functions (the unit-test surface) are separated from the
discord.py glue so they can be tested without a live gateway.
"""
import logging
from typing import List, Optional, Set

import config
from account_manager import build_account_payload
from market_hours import market_status, get_expiration_display
from strategy_engine import (build_candidate_views, reset_strategy_runtime)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------
def is_authorized(user_id: int, allowed_user_ids: list, allowed_role: str,
                  role_names, role_ids) -> bool:
    """True when the user is in the ID allowlist or has a matching role."""
    if user_id in (allowed_user_ids or []):
        return True
    if allowed_role:
        role = str(allowed_role)
        names = {str(n).lower() for n in (role_names or [])}
        ids = {str(r) for r in (role_ids or [])}
        if role.lower() in names or role in ids:
            return True
    return False


# ---------------------------------------------------------------------------
# Query views (read current state; no order actions)
# ---------------------------------------------------------------------------
def account_view(state) -> dict:
    return build_account_payload(state)


def positions_view(state, filter=None) -> dict:
    pos = list(getattr(state, "positions", []) or [])
    if filter:
        f = str(filter).lower()
        pos = [p for p in pos if f in (p.get("contract") or {}).get("symbol", "").lower()
               or f in (p.get("contract") or {}).get("localSymbol", "").lower()]
    return {"count": len(pos), "positions": pos}


def orders_view(state) -> dict:
    orders = list(getattr(state, "open_orders", []) or [])
    return {"count": len(orders), "orders": orders}


def status_view(state) -> dict:
    return {
        "connected": getattr(state, "connected", False),
        "market_status": market_status(),
        "expiration": get_expiration_display(getattr(state, "expiration", "")) if getattr(state, "expiration", "") else "N/A",
        "data_mode": getattr(state, "data_mode", "initializing"),
        "gex_mode": getattr(state, "gex_mode", "0dte"),
        "spot": round(float(getattr(state, "spx_price", 0) or 0), 2),
        "vix": getattr(state, "vix", None),
    }


def strategies_view(state, name=None) -> dict:
    if name:
        s = getattr(state, "strategies", {}).get(name)
        if s is None:
            return {"ok": False, "error": f"Strategy '{name}' not found"}
        return {"ok": True, "name": name, "strategy": s.to_dict()}
    return {"ok": True,
            "strategies": [s.to_dict() for s in getattr(state, "strategies", {}).values()],
            "kill_switch": getattr(state, "auto_trade_kill_switch", False)}


def candidates_view(state, name) -> dict:
    strategies = getattr(state, "strategies", {})
    s = strategies.get(name)
    if s is None:
        return {"ok": False, "error": f"Strategy '{name}' not found"}
    cands = getattr(state, "strategy_candidates", {}).get(name) or build_candidate_views(s, state)
    if not cands:
        return {"ok": True, "name": name, "armed": s.armed, "auto_execute": s.auto_execute,
                "count": 0, "rows": [],
                "note": "No live candidates (strategy not armed, market closed, or no qualifying combo)."}
    right = "P" if s.direction == "bull_put" else "C"
    rows = [{
        "index": i,
        "direction": s.direction,
        "short_strike": c.get("short_strike"),
        "long_strike": c.get("long_strike"),
        "right": right,
        "credit_mid": c.get("credit_mid"),
        "short_delta": c.get("short_delta"),
        "width_points": c.get("width_points"),
        "size": c.get("size"),
        "total_credit": c.get("total_credit"),
        "total_margin": c.get("total_margin"),
        "auto_execute": s.auto_execute,
    } for i, c in enumerate(cands)]
    return {"ok": True, "name": name, "armed": s.armed, "auto_execute": s.auto_execute,
            "count": len(rows), "rows": rows}


# ---------------------------------------------------------------------------
# Actions (arm/disarm/kill-switch) — synchronous, safe, no order placement
# ---------------------------------------------------------------------------
def arm_strategy(state, name) -> dict:
    s = getattr(state, "strategies", {}).get(name)
    if s is None:
        return {"ok": False, "error": f"Strategy '{name}' not found"}
    s.armed = True
    reset_strategy_runtime(state, s.name)
    return {"ok": True, "name": s.name, "armed": True, "auto_execute": s.auto_execute,
            "direction": s.direction, "budget": s.budget,
            "kill_switch": getattr(state, "auto_trade_kill_switch", False)}


def disarm_strategy(state, name) -> dict:
    s = getattr(state, "strategies", {}).get(name)
    if s is None:
        return {"ok": False, "error": f"Strategy '{name}' not found"}
    s.armed = False
    return {"ok": True, "name": s.name, "armed": False}


def set_kill_switch(state, on: bool) -> bool:
    state.auto_trade_kill_switch = bool(on)
    return state.auto_trade_kill_switch


# ---------------------------------------------------------------------------
# Order refusal (Discord never places orders — confirm in web UI)
# ---------------------------------------------------------------------------
def place_refusal(state, name, index: int) -> dict:
    strategies = getattr(state, "strategies", {})
    s = strategies.get(name)
    if s is None:
        return {"ok": False, "message": f"Strategy '{name}' not found"}
    cands = getattr(state, "strategy_candidates", {}).get(name) or build_candidate_views(s, state)
    if not cands:
        return {"ok": False, "message": "No candidate to place — re-run /candidates."}
    c = cands[index] if 0 <= index < len(cands) else cands[0]
    right = "P" if s.direction == "bull_put" else "C"
    msg = (f"Order placement is not allowed from Discord. "
           f"Open the dashboard web UI to confirm this spread: "
           f"SELL {c.get('short_strike')}{right} / BUY {c.get('long_strike')}{right} "
           f"credit ~${c.get('credit_mid'):.2f} x{c.get('size', 1)}")
    return {"ok": False, "message": msg}
