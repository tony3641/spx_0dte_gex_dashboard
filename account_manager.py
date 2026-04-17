"""
Account data management: subscriptions, serialization, refresh, and push loop.

All functions accept `ib` and/or `state` explicitly for testability.
"""

import asyncio
import logging
import re
import time
from datetime import datetime, timedelta
from typing import List, Optional

from ib_insync.util import parseIBDatetime
from config import FORCE_REFRESH_INTERVAL
from market_hours import now_et, ET

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Serialisation helpers (pure functions — easy to unit test)
# ---------------------------------------------------------------------------

def serialize_account_values(av_list) -> dict:
    """Convert ib.accountValues() list into a flat dict of key metrics."""
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
    """Serialize a PortfolioItem (from ib.portfolio()) to a dict."""
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


def serialize_trade(trade) -> dict:
    """Serialize an ib_insync Trade to a dict for the frontend."""
    o = trade.order
    c = trade.contract
    contract_desc = {
        "conId": c.conId,
        "symbol": c.symbol,
        "secType": c.secType,
        "expiry": getattr(c, "lastTradeDateOrContractMonth", ""),
        "strike": float(c.strike) if getattr(c, "strike", None) and c.strike else None,
        "right": getattr(c, "right", ""),
        "multiplier": getattr(c, "multiplier", ""),
        "currency": c.currency,
        "localSymbol": getattr(c, "localSymbol", ""),
    }
    log = trade.log[-1] if trade.log else None
    return {
        "orderId": o.orderId,
        "permId": o.permId,
        "clientId": o.clientId,
        "action": o.action,
        "totalQty": o.totalQuantity,
        "orderType": o.orderType,
        "lmtPrice": o.lmtPrice if o.lmtPrice not in (None, 1.7976931348623157e+308) else None,
        "auxPrice": o.auxPrice if o.auxPrice not in (None, 1.7976931348623157e+308) else None,
        "tif": o.tif,
        "status": trade.orderStatus.status,
        "filled": trade.orderStatus.filled,
        "remaining": trade.orderStatus.remaining,
        "avgFillPrice": trade.orderStatus.avgFillPrice if trade.orderStatus.avgFillPrice else None,
        "contract": contract_desc,
        "lastLogMsg": log.message if log else "",
    }


def parse_execution_time(raw_time):
    """Parse various IB execution time formats to a timezone-aware datetime."""
    if raw_time is None:
        return None

    dt = None
    if isinstance(raw_time, datetime):
        dt = raw_time
    else:
        text = str(raw_time).strip()
        if not text:
            return None

        # Prefer ib_insync's own parser for IB-native date/time strings.
        try:
            parsed = parseIBDatetime(text)
            if isinstance(parsed, datetime):
                dt = parsed
        except Exception:
            dt = None

        if dt is None:
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
    """Serialize today's executions from ib.fills()."""
    today_et = now_et().date()
    result = []
    for fill in ib.fills():
        ex = fill.execution
        exec_dt = parse_execution_time(ex.time)
        if exec_dt is None:
            continue
        if exec_dt.astimezone(ET).date() != today_et:
            continue
        c = fill.contract
        commission_val = fill.commissionReport.commission if fill.commissionReport else None
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
    """Pull current account / portfolio / order state from ib_insync cache."""
    try:
        avs = ib.accountValues()
        if avs:
            state.account_summary = serialize_account_values(avs)
    except Exception as e:
        logger.debug(f"accountValues error: {e}")

    try:
        state.positions = [serialize_portfolio_item(p) for p in ib.portfolio()]
    except Exception as e:
        logger.debug(f"portfolio error: {e}")

    try:
        trades = ib.openTrades()
        state.open_orders = [serialize_trade(t) for t in trades]
        state.active_trades = {t.order.orderId: t for t in trades}
    except Exception as e:
        logger.debug(f"openTrades error: {e}")

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
    try:
        ib.reqAccountUpdates(subscribe=True, account="")
        logger.info("IB account subscription started")
    except Exception as e:
        logger.warning(f"reqAccountUpdates failed: {e}")

    def _mark_dirty(*_args):
        state.account_dirty = True

    ib.updatePortfolioEvent += _mark_dirty
    ib.accountValueEvent += _mark_dirty
    ib.orderStatusEvent += _mark_dirty
    ib.execDetailsEvent += _mark_dirty
    ib.commissionReportEvent += _mark_dirty
    ib.newOrderEvent += _mark_dirty

    refresh_account_state(ib, state)


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
