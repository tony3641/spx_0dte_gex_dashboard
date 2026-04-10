"""
Order placement, cancellation, and status tracking.

All functions accept `ib` and `state` explicitly for testability.
The `refresh_fn` callback is used to trigger account state refresh after
order actions (injected by the caller, typically account_manager.refresh_account_state).
"""

import asyncio
import json
import logging
from typing import Optional

from ib_insync import Option, Stock, Order, Contract, ComboLeg, TagValue

from config import spx_tick_for_price, round_abs_to_tick, round_signed_to_tick

logger = logging.getLogger(__name__)

_PENDING_STATUSES = {'', 'PendingSubmit', 'ApiPending'}


async def await_order_status(trade, timeout: float = 5.0) -> str:
    """Poll trade.orderStatus until it leaves PendingSubmit/ApiPending, or timeout."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        st = trade.orderStatus.status or ''
        if st not in _PENDING_STATUSES:
            return st
        await asyncio.sleep(0.1)
    return trade.orderStatus.status or 'PendingSubmit'


async def watch_and_push_status(ws, trade, timeout: float = 30.0) -> None:
    """Background task: push an order_status WS message once the order settles."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        st = trade.orderStatus.status or ''
        if st not in _PENDING_STATUSES:
            break
        await asyncio.sleep(0.5)
    try:
        st = trade.orderStatus.status or 'Unknown'
        filled = trade.orderStatus.filled
        avg_fill = trade.orderStatus.avgFillPrice
        msg = f"Order {st}"
        if filled:
            msg += f" — filled {filled} @ {avg_fill:.2f}"
        await ws.send_text(json.dumps({
            "type": "order_status",
            "data": {
                "status": st,
                "orderId": trade.order.orderId,
                "message": msg,
                "filled": filled,
                "avgFillPrice": avg_fill,
            },
        }))
    except Exception:
        pass  # WS already closed


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
        contract = Option(
            symbol=leg.get("symbol", "SPX"),
            lastTradeDateOrContractMonth=leg["expiry"],
            strike=strike_val,
            right=leg["right"],
            exchange="SMART",
            multiplier="100",
            currency="USD",
            tradingClass="SPXW",
        )
        log_contract_desc = f"{leg['symbol']} {leg['expiry']} {leg['strike']}{leg['right']}"
        user_contract_desc = f"{leg['symbol']} {leg['strike']}{leg['right']}"
    elif sec_type == "STK":
        if not leg.get("symbol"):
            return {"type": "order_status", "data": {
                "status": "Error", "message": "Missing symbol for stock leg"
            }}
        contract = Stock(
            symbol=leg["symbol"],
            exchange="SMART",
            currency="USD",
        )
        log_contract_desc = f"{leg['symbol']} STK"
        user_contract_desc = f"{leg['symbol']}"
    else:
        return {"type": "order_status", "data": {
            "status": "Error",
            "message": f"Unsupported secType for liquidate/order path: {sec_type}"
        }}

    qualified = await ib.qualifyContractsAsync(contract)
    if not qualified or not qualified[0].conId:
        return {"type": "order_status", "data": {"status": "Error", "message": "Failed to qualify contract"}}
    contract = qualified[0]

    is_spx_opt = (sec_type == "OPT" and leg.get("symbol", "").upper() == "SPX")
    tick_size = 0.05 if is_spx_opt else 0.01
    try:
        cdetails = await ib.reqContractDetailsAsync(contract)
        if cdetails and getattr(cdetails[0], "minTick", 0):
            tick_size = max(0.0001, float(cdetails[0].minTick))
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
        ticker = ib.reqMktData(contract, genericTickList="", snapshot=False)
        mid = None
        try:
            for _ in range(8):
                await asyncio.sleep(0.1)
                bid = ticker.bid
                ask = ticker.ask
                if _valid_quote(bid) and _valid_quote(ask):
                    mid = (float(bid) + float(ask)) / 2.0
                    break
            if mid is None:
                last = ticker.last
                if _valid_quote(last):
                    mid = float(last)
        finally:
            try:
                ib.cancelMktData(contract)
            except Exception:
                pass
        return _round_to_tick(mid) if mid is not None else None

    order = Order(
        orderType=order_type,
        action=leg["action"],
        totalQuantity=int(leg["qty"]),
        tif=tif,
        outsideRth=outside_rth,
        transmit=(stop_loss_price is None),
    )
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

    trade = ib.placeOrder(contract, order)
    stop_trade = None

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
            stop_order = Order(
                orderType="STP LMT",
                action=stop_action,
                totalQuantity=int(leg["qty"]),
                auxPrice=stop_trigger,
                lmtPrice=stop_lmt,
                parentId=order.orderId,
                tif=tif,
                outsideRth=outside_rth,
                transmit=True,
            )
            stop_trade = ib.placeOrder(contract, stop_order)
            await asyncio.sleep(0.05)
            state.active_trades[stop_order.orderId] = stop_trade
            logger.info(
                f"Stop-limit attached: {stop_action} {leg['qty']} "
                f"{log_contract_desc} STP LMT @ stop={stop_trigger} lmt={stop_lmt} — "
                f"parentId={order.orderId} stopOrderId={stop_order.orderId}"
            )

    # Wait for IB acknowledgment
    if dynamic_fill and order_type == "LMT":
        final_status = await await_order_status(trade, timeout=3.0)
    else:
        final_status = await await_order_status(trade, timeout=10.0)
        if final_status in _PENDING_STATUSES:
            try:
                ib.reqOpenOrders()
            except Exception:
                pass
            await asyncio.sleep(0.5)
            final_status = trade.orderStatus.status or "PendingSubmit"

    # Dynamic fill reprice loop
    if dynamic_fill and order_type == "LMT":
        direction = 1.0 if leg["action"] == "BUY" else -1.0
        reprice_deadline = asyncio.get_event_loop().time() + 300.0
        max_reprice_iterations = 10
        reprice_iteration = 0
        while asyncio.get_event_loop().time() < reprice_deadline:
            reprice_iteration += 1
            status = trade.orderStatus.status or ""
            remaining = trade.orderStatus.remaining
            if status == "Filled" or (remaining is not None and remaining <= 0):
                break
            if status in {"Cancelled", "ApiCancelled", "Inactive"}:
                return {"type": "order_status", "data": {
                    "status": "Error",
                    "orderId": order.orderId,
                    "message": f"Order became {status} before fill"
                }}

            await asyncio.sleep(max(0.05, reprice_interval))

            status = trade.orderStatus.status or ""
            remaining = trade.orderStatus.remaining
            if status == "Filled" or (remaining is not None and remaining <= 0):
                break
            if status in {"Cancelled", "ApiCancelled", "Inactive"}:
                return {"type": "order_status", "data": {
                    "status": "Error",
                    "orderId": order.orderId,
                    "message": f"Order became {status} before fill"
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
            try:
                trade = ib.placeOrder(contract, order)
            except AssertionError as exc:
                logger.info(
                    "Dynamic fill modify aborted because order is already complete: %s",
                    exc,
                )
                await await_order_status(trade, timeout=1.0)
                break
            await asyncio.sleep(0.05)
        final_status = trade.orderStatus.status or "Unknown"

    state.active_trades[order.orderId] = trade
    refresh_fn()

    if ws:
        asyncio.create_task(watch_and_push_status(ws, trade))

    logger.info(
        f"Order placed: {leg['action']} {leg['qty']} "
        f"{log_contract_desc} "
        f"@ {order.lmtPrice if order_type=='LMT' else 'MKT'} — "
        f"outsideRth={outside_rth} "
        f"orderId={order.orderId} status={final_status}"
    )

    stop_msg = ""
    if stop_trade:
        if isinstance(stop_loss_price, dict):
            stop_msg = (
                f" | STP LMT stop={abs(float(stop_loss_price['stopPrice'])):.2f}"
                f" lmt={abs(float(stop_loss_price['limitPrice'])):.2f}"
                f" orderId={stop_trade.order.orderId}"
            )
        else:
            stop_msg = f" | STP LMT stop={stop_loss_price} orderId={stop_trade.order.orderId}"
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
    # Qualify each leg contract
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
        c = Option(
            symbol=leg.get("symbol", "SPX"),
            lastTradeDateOrContractMonth=leg["expiry"],
            strike=strike_val,
            right=leg["right"],
            exchange="SMART",
            multiplier="100",
            currency="USD",
            tradingClass="SPXW",
        )
        individual_contracts.append(c)

    qualified = await ib.qualifyContractsAsync(*individual_contracts)
    if len(qualified) != len(legs):
        return {"type": "order_status", "data": {
            "status": "Error",
            "message": f"Qualified {len(qualified)}/{len(legs)} legs"
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

    bag_symbol = (legs[0].get("symbol", "SPX") or "SPX").upper()
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
        cl.exchange = "SMART"
        combo_legs.append(cl)

    bag = Contract()
    bag.symbol = legs[0].get("symbol", "SPX")
    bag.secType = "BAG"
    bag.currency = "USD"
    bag.exchange = "SMART"
    bag.comboLegs = combo_legs

    order = Order(
        orderType=order_type,
        action=bag_action,
        totalQuantity=combo_quantity,
        tif=tif,
        outsideRth=outside_rth,
        transmit=(stop_loss_price is None),
    )
    order.smartComboRoutingParams = [TagValue("NonGuaranteed", "1")]
    if order_type == "LMT":
        order.lmtPrice = bag_lmt

    trade = ib.placeOrder(bag, order)

    # Attach stop-limit bracket for BAG
    bag_stop_trade = None
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
                cl.exchange = "SMART"
                close_combo_legs.append(cl)
            close_bag = Contract()
            close_bag.symbol = legs[0].get("symbol", "SPX")
            close_bag.secType = "BAG"
            close_bag.currency = "USD"
            close_bag.exchange = "SMART"
            close_bag.comboLegs = close_combo_legs
            stop_action = "SELL" if bag_action == "BUY" else "BUY"
            bag_stop_order = Order(
                orderType="STP LMT",
                action=stop_action,
                totalQuantity=combo_quantity,
                auxPrice=bag_stop_trigger,
                lmtPrice=bag_stop_lmt,
                parentId=order.orderId,
                tif=tif,
                outsideRth=outside_rth,
                transmit=True,
            )
            bag_stop_order.smartComboRoutingParams = [TagValue("NonGuaranteed", "1")]
            bag_stop_trade = ib.placeOrder(close_bag, bag_stop_order)
            await asyncio.sleep(0.05)
            state.active_trades[bag_stop_order.orderId] = bag_stop_trade
            logger.info(
                f"BAG stop-limit attached: {stop_action} combo STP LMT @ "
                f"stop={bag_stop_trigger} lmt={bag_stop_lmt} — "
                f"parentId={order.orderId} stopOrderId={bag_stop_order.orderId}"
            )

    # Wait for initial IB ack
    bag_status = await await_order_status(trade, timeout=5.0)
    if bag_status in _PENDING_STATUSES:
        try:
            ib.reqOpenOrders()
        except Exception:
            pass
        await asyncio.sleep(0.5)
        bag_status = trade.orderStatus.status or "PendingSubmit"

    state.active_trades[order.orderId] = trade
    refresh_fn()

    if ws:
        asyncio.create_task(watch_and_push_status(ws, trade))

    leg_desc = ", ".join(
        f"{l['action']} {l['qty']} {l['strike']}{l['right']}"
        for l in legs
    )
    stop_combo_msg = ""
    if bag_stop_trade:
        if isinstance(stop_loss_price, dict):
            stop_combo_msg = (
                f" | STP LMT stop={abs(float(stop_loss_price['stopPrice'])):.2f}"
                f" lmt={abs(float(stop_loss_price['limitPrice'])):.2f}"
                f" orderId={bag_stop_trade.order.orderId}"
            )
        else:
            stop_combo_msg = f" | STP LMT stop={stop_loss_price} orderId={bag_stop_trade.order.orderId}"
    logger.info(
        f"BAG order {bag_status}: {bag_action} combo @ {bag_lmt} — "
        f"legs=[{leg_desc}] orderId={order.orderId}"
    )

    return {"type": "order_status", "data": {
        "status": bag_status,
        "orderId": order.orderId,
        "message": f"Combo order {bag_status}: {leg_desc}{stop_combo_msg}",
    }}


async def handle_cancel_order(ib, state, order_id: int,
                              refresh_fn=None) -> dict:
    """Cancel an open order by orderId."""
    if not ib or not ib.isConnected():
        return {"type": "order_status", "data": {"status": "Error", "message": "Not connected to IB"}}

    try:
        trade = state.active_trades.get(order_id)
        if trade is None:
            for t in ib.openTrades():
                if t.order.orderId == order_id:
                    trade = t
                    break

        if trade is None:
            return {"type": "order_status", "data": {
                "status": "Error",
                "message": f"Order {order_id} not found in open trades"
            }}

        ib.cancelOrder(trade.order)
        await asyncio.sleep(0.1)
        if refresh_fn:
            refresh_fn(ib, state)

        logger.info(f"Order {order_id} cancellation requested")
        return {"type": "order_status", "data": {
            "status": "Cancelled",
            "orderId": order_id,
            "message": f"Cancel request sent for order {order_id}",
        }}
    except Exception as e:
        logger.error(f"handle_cancel_order exception: {e}", exc_info=True)
        return {"type": "order_status", "data": {"status": "Error", "message": str(e)}}
