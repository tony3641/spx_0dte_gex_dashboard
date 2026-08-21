"""
Order placement, cancellation, and status tracking.

All functions accept `ib` and `state` explicitly for testability.
The `refresh_fn` callback is used to trigger account state refresh after
order actions (injected by the caller, typically account_manager.refresh_account_state).

The module is event-driven over the native ``IBClient`` surface: ``ib.place_order``
returns an ``OrderHandle`` whose ack/fill/terminal events are awaited rather than
polling ``orderStatus``. Contracts/orders are native ibapi objects.
"""

import asyncio
import json
import logging
from typing import Optional

from ibapi.contract import Contract, ComboLeg
from ibapi.order import Order
from ibapi.tag_value import TagValue

from config import spx_tick_for_price, round_abs_to_tick, round_signed_to_tick
from ib_client import _PENDING_STATUSES, _TERMINAL_STATUSES

logger = logging.getLogger(__name__)


def _option_contract(symbol, expiry, strike, right, exchange,
                     trading_class="SPXW"):
    """Build a native ibapi OPT Contract (ibapi.contract.Contract)."""
    c = Contract()
    c.symbol = symbol
    c.secType = "OPT"
    c.exchange = exchange
    c.currency = "USD"
    c.lastTradeDateOrContractMonth = expiry
    c.strike = float(strike)
    c.right = right
    c.multiplier = "100"
    c.tradingClass = trading_class
    return c


def _stock_contract(symbol):
    """Build a native ibapi STK Contract."""
    c = Contract()
    c.symbol = symbol
    c.secType = "STK"
    c.exchange = "SMART"
    c.currency = "USD"
    return c


async def watch_and_push_status(ws, handle, timeout: float = 30.0,
                                bracket_child: bool = False) -> None:
    """Background task: push an order_status WS message once the order settles.

    Non-bracket orders push once IB acknowledges the order — ``handle.ack()``
    resolves the moment status leaves the pending set (for combo parents this
    is 'PreSubmitted').

    Bracket children stage at 'PreSubmitted' — their ack already fired there
    (PreSubmitted is not a pending status), so with ``bracket_child=True`` we
    additionally wait through PreSubmitted before pushing.
    """
    try:
        await handle.ack(timeout=timeout)
    except asyncio.TimeoutError:
        pass
    if bracket_child and handle.status == "PreSubmitted":
        # Push when the child activates (PreSubmitted -> Submitted) rather than
        # deferring to fill/cancel or a 30s timeout. ``activated_event`` fires
        # exactly once when the status leaves PreSubmitted.
        try:
            await asyncio.wait_for(handle.activated_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
    try:
        st = handle.status or "Unknown"
        filled = handle.filled or 0
        avg_fill = handle.avg_fill_price
        msg = f"Order {st}"
        if filled:
            if avg_fill is not None:
                msg += f" — filled {filled} @ {avg_fill:.2f}"
            else:
                msg += f" — filled {filled}"
        await ws.send_text(json.dumps({
            "type": "order_status",
            "data": {
                "status": st,
                "orderId": handle.order.orderId,
                "message": msg,
                "filled": filled,
                "avgFillPrice": avg_fill,
            },
        }))
    except Exception:
        pass  # WS already closed


async def watch_parent_and_cancel_child(ib, ws, parent, child,
                                        timeout: float = 86400.0) -> None:
    """Background task: cancel a bracket child order if its parent is cancelled.

    Awaits the parent's terminal event.  If the parent reaches a terminal status
    other than 'Filled' (i.e. Cancelled/Inactive), explicitly cancel the child
    order and push a status update over WS.  Self-terminates once the parent is
    terminal.
    """
    try:
        await parent.wait_terminal(timeout=timeout)
    except asyncio.TimeoutError:
        return
    if parent.status != "Filled":
        if not child.is_terminal():
            try:
                ib.cancel_order(child.order_id)
            except Exception:
                pass
        if ws:
            child_st = child.status or "Cancelled"
            try:
                await ws.send_text(json.dumps({
                    "type": "order_status",
                    "data": {
                        "status": child_st,
                        "orderId": child.order_id,
                        "message": f"Stop order cancelled (parent {parent.status})",
                        "filled": 0,
                        "avgFillPrice": 0.0,
                    },
                }))
            except Exception:
                pass


async def handle_place_order(ib, state, payload: dict, ws=None,
                             refresh_fn=None) -> dict:
    """Handle a place_order WebSocket message.

    Parameters
    ----------
    ib : IB client (or mock)
    state : AppState
    payload : dict — order payload from frontend
    ws : WebSocket (optional) — for background status push
    refresh_fn : callable (optional) — e.g. account_manager.refresh_account_state

    Returns {"type": "order_status", "data": {...}}.
    """
    if not ib or not ib.isConnected():
        return {"type": "order_status", "data": {"status": "Error", "message": "Not connected to IB"}}

    legs = payload.get("legs", [])
    if not legs:
        return {"type": "order_status", "data": {"status": "Error", "message": "No legs provided"}}

    order_type = payload.get("orderType", "LMT")
    tif = payload.get("tif", "DAY")
    outside_rth: bool = bool(payload.get("outsideRth", False))
    stop_loss_price = payload.get("stopLoss")
    dynamic_fill = bool(payload.get("dynamicFill", False))
    reprice_interval = float(payload.get("repriceIntervalSec", 0.3) or 0.3)

    # Validate limit prices are present for all legs
    for leg in legs:
        if order_type == "LMT":
            if dynamic_fill and len(legs) == 1:
                continue
            if leg.get("lmtPrice") is None:
                leg_label = f"{leg.get('symbol', '')} {leg.get('strike', '')}{leg.get('right', '')}".strip()
                return {"type": "order_status", "data": {
                    "status": "Error",
                    "message": f"Missing lmtPrice for leg {leg_label or 'unknown'}"
                }}

    def _do_refresh():
        if refresh_fn:
            refresh_fn(ib, state)

    try:
        if len(legs) == 1:
            return await _place_single_leg(ib, state, payload, legs[0],
                                           order_type, tif, outside_rth,
                                           stop_loss_price, dynamic_fill,
                                           reprice_interval, ws, _do_refresh)
        else:
            return await _place_multi_leg(ib, state, payload, legs,
                                          order_type, tif, outside_rth,
                                          stop_loss_price, ws, _do_refresh)
    except Exception as e:
        logger.error(f"handle_place_order exception: {e}", exc_info=True)
        return {"type": "order_status", "data": {"status": "Error", "message": str(e) or "Internal error"}}


async def _place_single_leg(ib, state, payload, leg,
                            order_type, tif, outside_rth,
                            stop_loss_price, dynamic_fill,
                            reprice_interval, ws, refresh_fn):
    """Handle single-leg order placement."""
    sec_type = leg.get("secType", "OPT")

    if sec_type == "OPT":
        strike_raw = leg.get("strike")
        if strike_raw is None:
            return {"type": "order_status", "data": {
                "status": "Error", "message": "Missing strike for option leg"
            }}
        try:
            strike_val = float(strike_raw)
        except (TypeError, ValueError):
            return {"type": "order_status", "data": {
                "status": "Error", "message": f"Invalid strike value: {strike_raw}"
            }}
        contract = _option_contract(
            symbol=leg.get("symbol", "SPX"),
            expiry=leg["expiry"],
            strike=strike_val,
            right=leg["right"],
            exchange="SMART",
        )
        log_contract_desc = f"{leg['symbol']} {leg['expiry']} {leg['strike']}{leg['right']}"
        user_contract_desc = f"{leg['symbol']} {leg['strike']}{leg['right']}"
    elif sec_type == "STK":
        if not leg.get("symbol"):
            return {"type": "order_status", "data": {
                "status": "Error", "message": "Missing symbol for stock leg"
            }}
        contract = _stock_contract(leg["symbol"])
        log_contract_desc = f"{leg['symbol']} STK"
        user_contract_desc = f"{leg['symbol']}"
    else:
        return {"type": "order_status", "data": {
            "status": "Error",
            "message": f"Unsupported secType for liquidate/order path: {sec_type}"
        }}

    details = await ib.req_contract_details(contract)
    if not details or not details[0].contract.conId:
        return {"type": "order_status", "data": {"status": "Error", "message": "Failed to qualify contract"}}
    contract = details[0].contract

    is_spx_opt = (sec_type == "OPT" and leg.get("symbol", "").upper() == "SPX")
    tick_size = 0.05 if is_spx_opt else 0.01
    try:
        if details and getattr(details[0], "minTick", 0):
            tick_size = max(0.0001, float(details[0].minTick))
    except Exception:
        pass

    def _effective_tick_for_price(price: float) -> float:
        if is_spx_opt:
            return spx_tick_for_price(price)
        return tick_size

    def _round_to_tick(price: float) -> float:
        t = _effective_tick_for_price(price)
        ticks = round(float(price) / t)
        rounded = ticks * t
        return max(t, round(rounded, 2))

    def _valid_quote(v) -> bool:
        try:
            f = float(v)
            return f > 0
        except Exception:
            return False

    async def _get_mid_price() -> Optional[float]:
        stream = ib.subscribe_tick(contract, "")
        mid = None
        try:
            for _ in range(8):
                if stream.has_quote():
                    if _valid_quote(stream.bid) and _valid_quote(stream.ask):
                        mid = (float(stream.bid) + float(stream.ask)) / 2.0
                        break
                await asyncio.sleep(0.05)
            if mid is None and _valid_quote(stream.last):
                mid = float(stream.last)
        finally:
            ib.unsubscribe_tick(stream.req_id)
        return _round_to_tick(mid) if mid is not None else None

    order = Order()
    order.orderType = order_type
    order.action = leg["action"]
    order.totalQuantity = int(leg["qty"])
    order.tif = tif
    order.outsideRth = outside_rth
    order.transmit = (stop_loss_price is None)
    if order_type == "LMT":
        if dynamic_fill:
            mid = await _get_mid_price()
            if mid is None:
                return {"type": "order_status", "data": {
                    "status": "Error",
                    "message": "Unable to derive midpoint for dynamic liquidation"
                }}
            order.lmtPrice = mid
        else:
            try:
                raw_lmt = abs(float(leg["lmtPrice"]))
                if raw_lmt <= 0:
                    raise ValueError("Limit price must be > 0")
                if sec_type == "OPT" and leg.get("symbol", "").upper() == "SPX":
                    order.lmtPrice = round_abs_to_tick(raw_lmt, spx_tick_for_price(raw_lmt))
                else:
                    order.lmtPrice = _round_to_tick(raw_lmt)
            except (TypeError, ValueError):
                return {"type": "order_status", "data": {
                    "status": "Error",
                    "message": f"Invalid lmtPrice: {leg.get('lmtPrice')}"
                }}

    handle = ib.place_order(contract, order)
    stop_handle = None

    # Attach stop-limit child BEFORE waiting for acknowledgment
    if stop_loss_price is not None:
        try:
            if isinstance(stop_loss_price, dict):
                stop_trigger = round(abs(float(stop_loss_price["stopPrice"])), 2)
                stop_lmt = round(abs(float(stop_loss_price["limitPrice"])), 2)
            else:
                stop_trigger = round(abs(float(stop_loss_price)), 2)
                stop_lmt = stop_trigger
        except (TypeError, ValueError, KeyError):
            stop_trigger = None
            stop_lmt = None
        if stop_trigger and stop_trigger > 0 and stop_lmt and stop_lmt > 0:
            if sec_type == "OPT" and leg.get("symbol", "").upper() == "SPX":
                stop_trigger = round_abs_to_tick(stop_trigger, spx_tick_for_price(stop_trigger))
                stop_lmt = round_abs_to_tick(stop_lmt, spx_tick_for_price(stop_lmt))
            stop_action = "SELL" if leg["action"] == "BUY" else "BUY"
            stop_order = Order()
            stop_order.orderType = "STP LMT"
            stop_order.action = stop_action
            stop_order.totalQuantity = int(leg["qty"])
            stop_order.auxPrice = stop_trigger
            stop_order.lmtPrice = stop_lmt
            stop_order.parentId = order.orderId
            stop_order.tif = tif
            stop_order.outsideRth = outside_rth
            stop_order.transmit = True
            stop_handle = ib.place_order(contract, stop_order)
            await asyncio.sleep(0.05)
            state.active_trades[stop_order.orderId] = stop_handle
            logger.info(
                f"Stop-limit attached: {stop_action} {leg['qty']} "
                f"{log_contract_desc} STP LMT @ stop={stop_trigger} lmt={stop_lmt} — "
                f"parentId={order.orderId} stopOrderId={stop_order.orderId}"
            )

    # Safety: if stop was intended but not created (e.g. zero prices),
    # re-submit parent with transmit=True so it isn't stuck at IB.
    if stop_loss_price is not None and stop_handle is None and not order.transmit:
        order.transmit = True
        handle = ib.place_order(contract, order, order_id=handle.order_id)

    # Wait for IB acknowledgment (event-driven: ack resolves once status leaves
    # the pending set — no reqOpenOrders re-poll).
    try:
        await handle.ack(timeout=3.0 if (dynamic_fill and order_type == "LMT") else 10.0)
    except asyncio.TimeoutError:
        pass
    final_status = handle.status or "PendingSubmit"

    # Dynamic fill reprice loop
    if dynamic_fill and order_type == "LMT":
        original_handle = handle
        direction = 1.0 if leg["action"] == "BUY" else -1.0
        reprice_deadline = asyncio.get_event_loop().time() + 300.0
        max_reprice_iterations = 10
        reprice_iteration = 0
        while asyncio.get_event_loop().time() < reprice_deadline:
            reprice_iteration += 1
            if handle.status == "Filled" or (handle.remaining is not None and handle.remaining <= 0):
                break
            if handle.status in _TERMINAL_STATUSES:
                return {"type": "order_status", "data": {
                    "status": "Error",
                    "orderId": order.orderId,
                    "message": f"Order became {handle.status} before fill"
                }}

            try:
                await asyncio.wait_for(handle.wait_fill(timeout=reprice_interval),
                                       timeout=reprice_interval)
            except asyncio.TimeoutError:
                pass

            if handle.status == "Filled" or (handle.remaining is not None and handle.remaining <= 0):
                break
            if handle.status in _TERMINAL_STATUSES:
                return {"type": "order_status", "data": {
                    "status": "Error",
                    "orderId": order.orderId,
                    "message": f"Order became {handle.status} before fill"
                }}

            if reprice_iteration >= max_reprice_iterations:
                logger.warning(
                    "Dynamic fill reached max iterations (%d); stopping to avoid infinite loop",
                    max_reprice_iterations,
                )
                break

            current_price = float(order.lmtPrice)
            step_tick = _effective_tick_for_price(current_price)
            next_price = _round_to_tick(current_price + direction * step_tick)
            if next_price == current_price:
                next_price = round(current_price + direction * step_tick, 2)
            order.lmtPrice = max(step_tick, next_price)
            # Reprice must MODIFY the live order (same orderId), not submit a new
            # one — otherwise each iteration leaves the prior order live (N+1
            # orders, double exposure).
            handle = ib.place_order(contract, order, order_id=original_handle.order_id)
        final_status = handle.status or "Unknown"

    state.active_trades[order.orderId] = handle
    refresh_fn()

    if ws:
        asyncio.create_task(watch_and_push_status(ws, handle))
        if stop_handle:
            asyncio.create_task(
                watch_and_push_status(ws, stop_handle, bracket_child=True)
            )
            asyncio.create_task(
                watch_parent_and_cancel_child(ib, ws, handle, stop_handle)
            )

    logger.info(
        f"Order placed: {leg['action']} {leg['qty']} "
        f"{log_contract_desc} "
        f"@ {order.lmtPrice if order_type=='LMT' else 'MKT'} — "
        f"outsideRth={outside_rth} "
        f"orderId={order.orderId} status={final_status}"
    )

    stop_msg = ""
    if stop_handle:
        if isinstance(stop_loss_price, dict):
            stop_msg = (
                f" | STP LMT stop={abs(float(stop_loss_price['stopPrice'])):.2f}"
                f" lmt={abs(float(stop_loss_price['limitPrice'])):.2f}"
                f" orderId={stop_handle.order.orderId}"
            )
        else:
            stop_msg = f" | STP LMT stop={stop_loss_price} orderId={stop_handle.order.orderId}"
    return {"type": "order_status", "data": {
        "status": final_status,
        "orderId": order.orderId,
        "message": f"Order {final_status}: {leg['action']} {leg['qty']} "
                   f"{user_contract_desc}{stop_msg}",
    }}


async def _place_multi_leg(ib, state, payload, legs,
                           order_type, tif, outside_rth,
                           stop_loss_price, ws, refresh_fn):
    """Handle multi-leg BAG order placement."""
    bag_symbol = (legs[0].get("symbol", "SPX") or "SPX").upper()
    requested_outside_rth = bool(outside_rth)
    outside_rth = requested_outside_rth
    use_direct_cboe_combo = bag_symbol == "SPX"
    session_note = ""

    # Qualify each leg contract (native one-shot request per leg)
    individual_contracts = []
    for leg in legs:
        sec_type = leg.get("secType", "OPT")
        if sec_type != "OPT":
            return {"type": "order_status", "data": {
                "status": "Error",
                "message": f"Unsupported secType in combo leg: {sec_type}"
            }}
        strike_raw = leg.get("strike")
        if strike_raw is None:
            return {"type": "order_status", "data": {
                "status": "Error", "message": "Missing strike in combo leg"
            }}
        try:
            strike_val = float(strike_raw)
        except (TypeError, ValueError):
            return {"type": "order_status", "data": {
                "status": "Error",
                "message": f"Invalid strike in combo leg: {strike_raw}"
            }}
        c = _option_contract(
            symbol=leg.get("symbol", "SPX"),
            expiry=leg["expiry"],
            strike=strike_val,
            right=leg["right"],
            exchange="CBOE" if use_direct_cboe_combo else "SMART",
        )
        individual_contracts.append(c)

    qualified = []
    for c in individual_contracts:
        details = await ib.req_contract_details(c)
        if not details or not details[0].contract.conId:
            qualified = None
            break
        qualified.append(details[0].contract)

    if qualified is None or len(qualified) != len(legs):
        return {"type": "order_status", "data": {
            "status": "Error",
            "message": f"Qualified {len(qualified) if qualified is not None else 0}/{len(legs)} legs"
        }}

    # Compute net combo limit price
    combo_price = 0.0
    for leg in legs:
        sign = 1.0 if leg["action"] == "BUY" else -1.0
        try:
            leg_lmt = float(leg.get("lmtPrice", 0))
        except (TypeError, ValueError):
            return {"type": "order_status", "data": {
                "status": "Error",
                "message": f"Invalid lmtPrice in combo leg: {leg.get('lmtPrice')}"
            }}
        try:
            leg_ratio = int(leg.get("qty", 1) or 1)
        except (TypeError, ValueError):
            leg_ratio = 1
        combo_price += sign * leg_lmt * max(1, leg_ratio)

    try:
        combo_quantity = int(payload.get("comboQuantity", 1) or 1)
    except (TypeError, ValueError):
        combo_quantity = 1
    combo_quantity = max(1, combo_quantity)

    bag_action = payload.get("comboAction") or "BUY"
    try:
        if payload.get("comboLmtPrice") is not None:
            bag_lmt = float(payload.get("comboLmtPrice"))
        else:
            bag_lmt = float(combo_price)
    except (TypeError, ValueError):
        return {"type": "order_status", "data": {
            "status": "Error",
            "message": f"Invalid comboLmtPrice: {payload.get('comboLmtPrice')}"
        }}

    if bag_symbol == "SPX":
        bag_lmt = round_signed_to_tick(bag_lmt, spx_tick_for_price(bag_lmt))
    else:
        bag_lmt = round(bag_lmt, 2)

    # Build BAG contract
    combo_legs = []
    for qc, leg in zip(qualified, legs):
        cl = ComboLeg()
        cl.conId = qc.conId
        cl.ratio = int(leg["qty"])
        cl.action = leg["action"]
        cl.exchange = "CBOE" if use_direct_cboe_combo else (getattr(qc, "exchange", "SMART") or "SMART")
        combo_legs.append(cl)

    bag = Contract()
    bag.symbol = legs[0].get("symbol", "SPX")
    bag.secType = "BAG"
    bag.currency = "USD"
    bag.exchange = "CBOE" if use_direct_cboe_combo else (combo_legs[0].exchange if combo_legs else "SMART")
    bag.comboLegs = combo_legs

    logger.info(
        "Submitting BAG order: action=%s qty=%s type=%s tif=%s requestedOutsideRth=%s "
        "effectiveOutsideRth=%s bagExchange=%s",
        bag_action,
        combo_quantity,
        order_type,
        tif,
        requested_outside_rth,
        outside_rth,
        bag.exchange,
    )

    order = Order()
    order.orderType = order_type
    order.action = bag_action
    order.totalQuantity = combo_quantity
    order.tif = tif
    order.outsideRth = outside_rth
    order.transmit = (stop_loss_price is None)
    if not use_direct_cboe_combo:
        order.smartComboRoutingParams = [TagValue("NonGuaranteed", "1")]
    if order_type == "LMT":
        order.lmtPrice = bag_lmt

    handle = ib.place_order(bag, order)

    # Attach stop-limit bracket for BAG
    bag_stop_handle = None
    if stop_loss_price is not None:
        try:
            if isinstance(stop_loss_price, dict):
                bag_stop_trigger = round(abs(float(stop_loss_price["stopPrice"])), 2)
                bag_stop_lmt = round(abs(float(stop_loss_price["limitPrice"])), 2)
            else:
                bag_stop_trigger = round(abs(float(stop_loss_price)), 2)
                bag_stop_lmt = bag_stop_trigger
        except (TypeError, ValueError, KeyError):
            bag_stop_trigger = None
            bag_stop_lmt = None
        if bag_stop_trigger and bag_stop_trigger > 0 and bag_stop_lmt and bag_stop_lmt > 0:
            if bag.symbol.upper() == "SPX":
                bag_stop_trigger = round_signed_to_tick(bag_stop_trigger, spx_tick_for_price(bag_stop_trigger))
                bag_stop_lmt = round_signed_to_tick(bag_stop_lmt, spx_tick_for_price(bag_stop_lmt))
            close_combo_legs = []
            for qc, leg in zip(qualified, legs):
                cl = ComboLeg()
                cl.conId = qc.conId
                cl.ratio = int(leg["qty"])
                cl.action = "SELL" if leg["action"] == "BUY" else "BUY"
                cl.exchange = "CBOE" if use_direct_cboe_combo else (getattr(qc, "exchange", "SMART") or "SMART")
                close_combo_legs.append(cl)
            close_bag = Contract()
            close_bag.symbol = legs[0].get("symbol", "SPX")
            close_bag.secType = "BAG"
            close_bag.currency = "USD"
            close_bag.exchange = "CBOE" if use_direct_cboe_combo else (close_combo_legs[0].exchange if close_combo_legs else "SMART")
            close_bag.comboLegs = close_combo_legs
            stop_action = "SELL" if bag_action == "BUY" else "BUY"
            bag_stop_order = Order()
            bag_stop_order.orderType = "STP LMT"
            bag_stop_order.action = stop_action
            bag_stop_order.totalQuantity = combo_quantity
            bag_stop_order.auxPrice = bag_stop_trigger
            bag_stop_order.lmtPrice = bag_stop_lmt
            bag_stop_order.parentId = order.orderId
            bag_stop_order.tif = tif
            bag_stop_order.outsideRth = outside_rth
            bag_stop_order.transmit = True
            if not use_direct_cboe_combo:
                bag_stop_order.smartComboRoutingParams = [TagValue("NonGuaranteed", "1")]
            bag_stop_handle = ib.place_order(close_bag, bag_stop_order)
            await asyncio.sleep(0.05)
            state.active_trades[bag_stop_order.orderId] = bag_stop_handle
            logger.info(
                f"BAG stop-limit attached: {stop_action} combo STP LMT @ "
                f"stop={bag_stop_trigger} lmt={bag_stop_lmt} — "
                f"parentId={order.orderId} stopOrderId={bag_stop_order.orderId}"
            )

    # Safety: if stop was intended but not created, re-submit with transmit=True
    if stop_loss_price is not None and bag_stop_handle is None and not order.transmit:
        order.transmit = True
        handle = ib.place_order(bag, order, order_id=handle.order_id)

    # Wait for initial IB ack (event-driven; no reqOpenOrders re-poll)
    try:
        await handle.ack(timeout=5.0)
    except asyncio.TimeoutError:
        pass
    bag_status = handle.status or "PendingSubmit"

    state.active_trades[order.orderId] = handle
    refresh_fn()

    if ws:
        asyncio.create_task(
            watch_and_push_status(ws, handle)
        )
        if bag_stop_handle:
            asyncio.create_task(
                watch_and_push_status(ws, bag_stop_handle, bracket_child=True)
            )
            asyncio.create_task(
                watch_parent_and_cancel_child(ib, ws, handle, bag_stop_handle)
            )

    leg_desc = ", ".join(
        f"{l['action']} {l['qty']} {l['strike']}{l['right']}"
        for l in legs
    )
    stop_combo_msg = ""
    if bag_stop_handle:
        if isinstance(stop_loss_price, dict):
            stop_combo_msg = (
                f" | STP LMT stop={abs(float(stop_loss_price['stopPrice'])):.2f}"
                f" lmt={abs(float(stop_loss_price['limitPrice'])):.2f}"
                f" orderId={bag_stop_handle.order.orderId}"
            )
        else:
            stop_combo_msg = f" | STP LMT stop={stop_loss_price} orderId={bag_stop_handle.order.orderId}"
    logger.info(
        f"BAG order {bag_status}: {bag_action} combo @ {bag_lmt} — "
        f"legs=[{leg_desc}] orderId={order.orderId} outsideRth={outside_rth}"
    )

    return {"type": "order_status", "data": {
        "status": bag_status,
        "orderId": order.orderId,
        "message": f"Combo order {bag_status}: {leg_desc}{stop_combo_msg}{session_note}",
    }}


async def handle_cancel_order(ib, state, order_id: int,
                              refresh_fn=None) -> dict:
    """Cancel an open order by orderId."""
    if not ib or not ib.isConnected():
        return {"type": "order_status", "data": {"status": "Error", "message": "Not connected to IB"}}

    try:
        handle = state.active_trades.get(order_id)
        if handle is None:
            # Fall back to the bridge's order registry (public `orders` dict on
            # the mock; the real IBClient exposes it from Task 15).
            for h in getattr(ib, "orders", {}).values():
                if h.order_id == order_id:
                    handle = h
                    break

        if handle is None:
            logger.debug(f"Cancel order {order_id}: not found in open trades")
            return {"type": "order_status", "data": {
                "status": "Error",
                "message": f"Order {order_id} not found in open trades"
            }}

        ib.cancel_order(order_id)
        await asyncio.sleep(0.1)
        if refresh_fn:
            refresh_fn(ib, state)

        logger.debug(f"Order {order_id} cancellation requested")
        return {"type": "order_status", "data": {
            "status": "Cancelled",
            "orderId": order_id,
            "message": f"Cancel request sent for order {order_id}",
        }}
    except Exception as e:
        logger.debug(f"Cancel order {order_id} failed: {e}")
        logger.error(f"handle_cancel_order exception: {e}", exc_info=True)
        return {"type": "order_status", "data": {"status": "Error", "message": str(e)}}
