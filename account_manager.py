"""
Account data management: subscriptions, serialization, refresh, and push loop.

All functions accept `ib` and/or `state` explicitly for testability.

The native bridge (``IBClient``) exposes account/portfolio/order/execution
state as plain lists/dicts (``account_values``, ``portfolio``, ``orders``,
``executions``) and a single ``on_account_dirty`` callback; this module
serializes that state for the frontend and runs the push loop.
"""

import asyncio
import logging
import re
import time
from datetime import datetime, timedelta
from typing import List, Optional

from config import FORCE_REFRESH_INTERVAL
from market_hours import now_et, ET

logger = logging.getLogger(__name__)

# IB's "not set" double sentinel for prices (matches ib_client._UNSET).
_UNSET_DOUBLE = 1.7976931348623157e+308


def _unset_or_none(val):
    """Return None for None / IB's UNSET double sentinel (prices that were never set)."""
    return val if val not in (None, _UNSET_DOUBLE) else None


# ---------------------------------------------------------------------------
# Serialisation helpers (pure functions — easy to unit test)
# ---------------------------------------------------------------------------

def serialize_account_values(av_list) -> dict:
    """Convert ib.account_values list into a flat dict of key metrics."""
    keys_wanted = {
        "NetLiquidation", "ExcessLiquidity", "FullAvailableFunds",
        "BuyingPower", "MaintMarginReq", "GrossPositionValue",
        "UnrealizedPnL", "RealizedPnL", "DayTradesRemaining",
        "TotalCashValue", "InitMarginReq", "EquityWithLoanValue",
    }
    result = {}
    for av in av_list:
        if av.tag in keys_wanted and av.currency == "USD":
            try:
                result[av.tag] = float(av.value)
            except (ValueError, TypeError):
                pass
    return result


def serialize_portfolio_item(item) -> dict:
    """Serialize a PortfolioItem (from ib.portfolio) to a dict."""
    c = item.contract
    contract_desc = {
        "conId": c.conId,
        "symbol": c.symbol,
        "secType": c.secType,
        "expiry": getattr(c, "lastTradeDateOrContractMonth", ""),
        "strike": float(c.strike) if getattr(c, "strike", None) and c.strike else None,
        "right": getattr(c, "right", ""),
        "multiplier": getattr(c, "multiplier", ""),
        "currency": c.currency,
        "exchange": c.exchange,
        "localSymbol": getattr(c, "localSymbol", ""),
        "tradingClass": getattr(c, "tradingClass", ""),
    }
    return {
        "contract": contract_desc,
        "position": item.position,
        "marketPrice": item.marketPrice,
        "marketValue": item.marketValue,
        "averageCost": item.averageCost,
        "unrealizedPNL": item.unrealizedPNL,
        "realizedPNL": item.realizedPNL,
        "account": item.account,
    }


def serialize_order_handle(handle) -> dict:
    """Serialize a native OrderHandle to a dict for the frontend."""
    o = handle.order
    c = handle.contract
    contract_desc = {
        "conId": c.conId,
        "symbol": c.symbol,
        "secType": c.secType,
        "expiry": getattr(c, "lastTradeDateOrContractMonth", ""),
        "strike": float(c.strike) if getattr(c, "strike", None) else None,
        "right": getattr(c, "right", ""),
        "multiplier": getattr(c, "multiplier", ""),
        "currency": c.currency,
        "localSymbol": getattr(c, "localSymbol", ""),
    }
    return {
        "orderId": o.orderId,
        "permId": handle.perm_id or 0,
        "clientId": o.clientId if hasattr(o, "clientId") else 0,
        "action": o.action,
        "totalQty": float(o.totalQuantity),
        "orderType": o.orderType,
        "lmtPrice": _unset_or_none(o.lmtPrice),
        "auxPrice": _unset_or_none(o.auxPrice),
        "tif": o.tif,
        "status": handle.status,
        "filled": handle.filled,
        "remaining": handle.remaining,
        "avgFillPrice": handle.avg_fill_price or None,
        "contract": contract_desc,
        "lastLogMsg": "",
    }


def parse_execution_time(raw_time):
    """Parse various IB execution time formats to a timezone-aware datetime."""
    if raw_time is None:
        return None

    if isinstance(raw_time, datetime):
        dt = raw_time
    else:
        text = str(raw_time).strip()
        if not text:
            return None

        today_et = now_et().date()
        used_today = False

        if re.match(r'^\d{8}\s\d{2}(?:[:]\d{2})?(?:[:]\d{2})?(?:[+-]\d{2}:\d{2})?$', text):
            date_part = text[:8]
            time_part = text[9:]
            if re.match(r'^\d{2}$', time_part):
                time_part += ':00:00'
            elif re.match(r'^\d{2}:\d{2}$', time_part):
                time_part += ':00'
            text = f"{date_part[:4]}-{date_part[4:6]}-{date_part[6:8]}T{time_part}"
        elif re.match(r'^\d{4}-\d{2}-\d{2}\s', text):
            text = text.replace(' ', 'T', 1)
        elif re.match(r'^\d{2}(?::\d{2})?(?::\d{2})?(?:[+-]\d{2}:\d{2})?$', text):
            if re.match(r'^\d{2}$', text):
                text += ':00:00'
            elif re.match(r'^\d{2}:\d{2}$', text):
                text += ':00'
            if re.match(r'^\d{2}[+-]\d{2}:\d{2}$', text):
                text = f"{today_et.isoformat()}T{text[:2]}:00:00{text[2:]}"
            else:
                text = f"{today_et.isoformat()}T{text}"
                used_today = True

        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None

        if used_today:
            dt = dt.replace(tzinfo=ET)
            now_dt = now_et()
            if dt > now_dt:
                dt -= timedelta(days=1)

    if dt.tzinfo is None:
        # IB can return naive datetimes in TWS/session timezone; treat as ET.
        return dt.replace(tzinfo=ET)
    return dt.astimezone(ET)


def format_execution_time_et(dt: Optional[datetime]) -> str:
    """Format a datetime in Eastern Time for display."""
    if dt is None:
        return ''
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ET)
    else:
        dt = dt.astimezone(ET)
    return dt.strftime('%H:%M:%S %Z')


def serialize_execution(ib, exec_filter=None) -> List[dict]:
    """Serialize today's executions from ib.executions (native records)."""
    today_et = now_et().date()
    result = []
    for rec in ib.executions:
        ex = rec.execution
        exec_dt = parse_execution_time(ex.time)
        if exec_dt is None:
            continue
        if exec_dt.astimezone(ET).date() != today_et:
            continue
        c = rec.contract
        commission_val = rec.commission.commissionAndFees if rec.commission else None
        result.append({
            "execId": ex.execId,
            "time": format_execution_time_et(exec_dt),
            "symbol": c.symbol,
            "secType": c.secType,
            "expiry": getattr(c, "lastTradeDateOrContractMonth", ""),
            "strike": float(c.strike) if getattr(c, "strike", None) and c.strike else None,
            "right": getattr(c, "right", ""),
            "localSymbol": getattr(c, "localSymbol", ""),
            "side": ex.side,
            "shares": ex.shares,
            "price": ex.price,
            "orderId": ex.orderId,
            "commission": commission_val if commission_val and commission_val < 1e8 else None,
        })
    result.sort(key=lambda x: x["time"], reverse=True)
    return result


# ---------------------------------------------------------------------------
# State refresh & payload building
# ---------------------------------------------------------------------------

def refresh_account_state(ib, state):
    """Pull current account / portfolio / order state from the native bridge."""
    try:
        if ib.account_values:
            state.account_summary = serialize_account_values(ib.account_values)
    except Exception as e:
        logger.debug(f"account_values error: {e}")

    try:
        state.positions = [serialize_portfolio_item(p) for p in ib.portfolio]
    except Exception as e:
        logger.debug(f"portfolio error: {e}")

    try:
        trades = [h for h in ib.orders.values() if h.status not in
                  {"Filled", "Cancelled", "ApiCancelled", "Inactive"}]
        state.open_orders = [serialize_order_handle(h) for h in trades]
        state.active_trades = dict(ib.orders)
    except Exception as e:
        logger.debug(f"open_orders error: {e}")

    try:
        state.executions = serialize_execution(ib)
    except Exception as e:
        logger.debug(f"executions error: {e}")

    state.account_dirty = True


def build_account_payload(state) -> dict:
    """Build the account_update WebSocket payload from current state."""
    return {
        "summary": state.account_summary,
        "positions": state.positions,
        "orders": state.open_orders,
        "executions": state.executions,
    }


# ---------------------------------------------------------------------------
# Async loops
# ---------------------------------------------------------------------------

async def setup_account_subscription(ib, state):
    """Subscribe to IB account updates and wire up event callbacks."""
    ib.on_account_dirty = lambda: setattr(state, "account_dirty", True)
    ib.req_account_updates(True, "")
    ib.req_open_orders()
    ib.req_executions()
    refresh_account_state(ib, state)
    logger.info("IB account subscription started")


async def account_push_loop(ib, state, broadcast_fn):
    """Broadcast account updates whenever IB fires a relevant event."""
    last_force_refresh = 0.0

    while True:
        try:
            await asyncio.sleep(1.0)
            now_mono = time.monotonic()

            if now_mono - last_force_refresh >= FORCE_REFRESH_INTERVAL:
                refresh_account_state(ib, state)
                last_force_refresh = now_mono

            if state.account_dirty and state.ws_clients:
                state.account_dirty = False
                await broadcast_fn({
                    "type": "account_update",
                    "data": build_account_payload(state),
                })
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Account push error: {e}")
            await asyncio.sleep(2)
