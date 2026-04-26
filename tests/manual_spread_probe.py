"""Manual SPXW spread probe against a live IB paper session.

This is not part of the automated pytest suite.
It connects to IB, drives the production BAG placement path, records IB events,
and can optionally reprice accepted combo orders until they fill or time out.
"""

import argparse
import asyncio
import json
import logging
import math
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ib_insync import ComboLeg, Contract, IB, Index, Option, Order, TagValue

from app_state import create_app_state
from chain_fetcher import get_chain_params
from config import IB_CLIENT_ID, IB_HOST, IB_PORT, RTH_OPEN, round_abs_to_tick, round_signed_to_tick, spx_tick_for_price
from market_hours import ET, find_next_expiration, market_status, now_et
from order_manager import handle_cancel_order, handle_place_order

logger = logging.getLogger("manual_spread_probe")

PENDING_STATUSES = {"", "PendingSubmit", "ApiPending"}
TERMINAL_STATUSES = {"Filled", "Cancelled", "ApiCancelled", "Inactive"}
ACCEPTED_SERVER_STATUSES = {"Submitted", "Filled"}


@dataclass
class LegQuote:
    bid: Optional[float]
    ask: Optional[float]
    last: Optional[float]
    reference: float


@dataclass
class SpreadScenario:
    name: str
    combo_action: str
    lower_strike: float
    upper_strike: float
    right: str


class EventRecorder:
    def __init__(self) -> None:
        self._started = time.time()
        self.events: List[Dict[str, Any]] = []

    def clear(self) -> None:
        self._started = time.time()
        self.events.clear()

    def _elapsed(self) -> float:
        return round(time.time() - self._started, 3)

    def _record(self, event_type: str, payload: Dict[str, Any]) -> None:
        self.events.append({
            "t": self._elapsed(),
            "event": event_type,
            **payload,
        })

    def on_error(self, req_id, error_code, error_string, contract=None, *args) -> None:
        self._record("errorEvent", {
            "reqId": req_id,
            "errorCode": error_code,
            "errorString": error_string,
            "contract": serialize_contract(contract),
        })

    def on_open_order(self, trade, *args) -> None:
        self._record("openOrderEvent", serialize_trade(trade))

    def on_order_status(self, trade, *args) -> None:
        self._record("orderStatusEvent", serialize_trade(trade))

    def on_exec_details(self, trade, fill, *args) -> None:
        payload = serialize_trade(trade)
        payload["fill"] = {
            "execution": {
                "execId": getattr(getattr(fill, "execution", None), "execId", None),
                "side": getattr(getattr(fill, "execution", None), "side", None),
                "shares": getattr(getattr(fill, "execution", None), "shares", None),
                "price": getattr(getattr(fill, "execution", None), "price", None),
            },
        }
        self._record("execDetailsEvent", payload)

    def on_new_order(self, trade, *args) -> None:
        self._record("newOrderEvent", serialize_trade(trade))


def _float_or_none(value: Any) -> Optional[float]:
    try:
        fval = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(fval):
        return None
    return fval


def _positive(value: Any) -> Optional[float]:
    fval = _float_or_none(value)
    if fval is None or fval <= 0:
        return None
    return fval


def serialize_contract(contract) -> Optional[Dict[str, Any]]:
    if contract is None:
        return None
    combo_legs = []
    for combo_leg in getattr(contract, "comboLegs", []) or []:
        combo_legs.append({
            "conId": getattr(combo_leg, "conId", None),
            "ratio": getattr(combo_leg, "ratio", None),
            "action": getattr(combo_leg, "action", None),
            "exchange": getattr(combo_leg, "exchange", None),
        })
    return {
        "symbol": getattr(contract, "symbol", None),
        "secType": getattr(contract, "secType", None),
        "exchange": getattr(contract, "exchange", None),
        "currency": getattr(contract, "currency", None),
        "lastTradeDateOrContractMonth": getattr(contract, "lastTradeDateOrContractMonth", None),
        "strike": getattr(contract, "strike", None),
        "right": getattr(contract, "right", None),
        "localSymbol": getattr(contract, "localSymbol", None),
        "tradingClass": getattr(contract, "tradingClass", None),
        "conId": getattr(contract, "conId", None),
        "comboLegs": combo_legs,
    }


def serialize_trade(trade) -> Dict[str, Any]:
    if trade is None:
        return {}
    order = getattr(trade, "order", None)
    order_status = getattr(trade, "orderStatus", None)
    return {
        "orderId": getattr(order, "orderId", None),
        "permId": getattr(order, "permId", None),
        "clientId": getattr(order, "clientId", None),
        "action": getattr(order, "action", None),
        "orderType": getattr(order, "orderType", None),
        "totalQuantity": getattr(order, "totalQuantity", None),
        "lmtPrice": getattr(order, "lmtPrice", None),
        "auxPrice": getattr(order, "auxPrice", None),
        "tif": getattr(order, "tif", None),
        "outsideRth": getattr(order, "outsideRth", None),
        "transmit": getattr(order, "transmit", None),
        "parentId": getattr(order, "parentId", None),
        "status": getattr(order_status, "status", None),
        "filled": getattr(order_status, "filled", None),
        "remaining": getattr(order_status, "remaining", None),
        "avgFillPrice": getattr(order_status, "avgFillPrice", None),
        "contract": serialize_contract(getattr(trade, "contract", None)),
    }


def emit(label: str, payload: Dict[str, Any]) -> None:
    print(f"\n=== {label} ===")
    print(json.dumps(payload, indent=2, default=str))


async def wait_for_quote(ib: IB, contract, timeout: float = 4.0) -> LegQuote:
    ticker = ib.reqMktData(contract, genericTickList="", snapshot=False)
    bid = ask = last = None
    try:
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            bid = _positive(getattr(ticker, "bid", None))
            ask = _positive(getattr(ticker, "ask", None))
            last = _positive(getattr(ticker, "last", None))
            if bid and ask:
                reference = round_abs_to_tick((bid + ask) / 2.0, spx_tick_for_price((bid + ask) / 2.0))
                return LegQuote(bid=bid, ask=ask, last=last, reference=reference)
            if last:
                reference = round_abs_to_tick(last, spx_tick_for_price(last))
                return LegQuote(bid=bid, ask=ask, last=last, reference=reference)
            await asyncio.sleep(0.1)
    finally:
        try:
            ib.cancelMktData(contract)
        except Exception:
            pass
    raise RuntimeError(f"No quote available for {getattr(contract, 'localSymbol', getattr(contract, 'symbol', 'contract'))}")


async def get_underlying(ib: IB):
    qualified = await ib.qualifyContractsAsync(Index("SPX", "CBOE", "USD"))
    if not qualified:
        raise RuntimeError("Failed to qualify SPX index contract")
    return qualified[0]


async def get_spot_price(ib: IB, underlying) -> float:
    quote = await wait_for_quote(ib, underlying)
    return quote.reference


def pick_vertical_strikes(strikes: List[float], spot: float, width: float) -> Tuple[float, float]:
    candidates = sorted({float(s) for s in strikes if float(s) % 5 == 0})
    if len(candidates) < 2:
        raise RuntimeError("Not enough strikes returned to build a vertical spread")

    lower_candidates = [strike for strike in candidates if strike <= spot]
    lower_strike = lower_candidates[-1] if lower_candidates else min(candidates, key=lambda strike: abs(strike - spot))
    upper_strike = next((strike for strike in candidates if strike >= lower_strike + width), None)
    if upper_strike is None:
        lower_index = candidates.index(lower_strike)
        if lower_index + 1 >= len(candidates):
            raise RuntimeError("Unable to find an upper strike for the vertical spread")
        upper_strike = candidates[lower_index + 1]

    if upper_strike <= lower_strike:
        raise RuntimeError("Computed invalid vertical strike pair")

    return lower_strike, upper_strike


def build_scenarios(lower_strike: float, upper_strike: float, right: str) -> List[SpreadScenario]:
    return [
        SpreadScenario(
            name="debit_vertical",
            combo_action="BUY",
            lower_strike=lower_strike,
            upper_strike=upper_strike,
            right=right,
        ),
        SpreadScenario(
            name="credit_vertical",
            combo_action="SELL",
            lower_strike=lower_strike,
            upper_strike=upper_strike,
            right=right,
        ),
    ]


def scenario_filter(scenarios: List[SpreadScenario], requested: str) -> List[SpreadScenario]:
    if requested == "both":
        return scenarios
    return [scenario for scenario in scenarios if scenario.name.startswith(requested)]


def next_rth_open(ref: Optional[datetime] = None) -> datetime:
    current = ref.astimezone(ET) if ref is not None else now_et()
    candidate_date = current.date()
    same_day_open = datetime.combine(candidate_date, RTH_OPEN, tzinfo=ET)

    if current.weekday() < 5 and current < same_day_open:
        return same_day_open

    candidate_date += timedelta(days=1)
    while candidate_date.weekday() >= 5:
        candidate_date += timedelta(days=1)
    return datetime.combine(candidate_date, RTH_OPEN, tzinfo=ET)


async def maybe_wait_for_rth(args) -> None:
    if not args.wait_for_rth:
        return
    if market_status() == "RTH":
        return

    target = next_rth_open()
    now = now_et()
    wait_seconds = max(0.0, (target - now).total_seconds())
    if args.max_wait_seconds is not None and wait_seconds > args.max_wait_seconds:
        raise RuntimeError(
            f"Next RTH open is {wait_seconds:.0f}s away at {target.isoformat()}, which exceeds --max-wait-seconds={args.max_wait_seconds}."
        )

    emit("waiting_for_rth", {
        "currentSession": market_status(now),
        "nowEt": now.isoformat(),
        "targetRthOpenEt": target.isoformat(),
        "waitSeconds": round(wait_seconds, 2),
    })

    while True:
        now = now_et()
        if now >= target and market_status(now) == "RTH":
            return
        remaining = max(0.0, (target - now).total_seconds())
        await asyncio.sleep(min(30.0, max(0.5, remaining)))


def quote_price_for_action(action: str, quote: LegQuote, price_basis: str) -> float:
    if price_basis == "mid":
        return quote.reference

    if action == "BUY":
        return quote.ask or quote.reference
    return quote.bid or quote.reference


def build_payload(
    scenario: SpreadScenario,
    expiry: str,
    combo_quantity: int,
    lower_quote: LegQuote,
    upper_quote: LegQuote,
    requested_outside_rth: bool,
    price_basis: str,
) -> Dict[str, Any]:
    if scenario.combo_action == "BUY":
        lower_action = "BUY"
        upper_action = "SELL"
    else:
        lower_action = "SELL"
        upper_action = "BUY"

    lower_price = quote_price_for_action(lower_action, lower_quote, price_basis)
    upper_price = quote_price_for_action(upper_action, upper_quote, price_basis)
    combo_price = lower_price - upper_price if scenario.combo_action == "BUY" else upper_price - lower_price
    combo_price = round_signed_to_tick(combo_price, spx_tick_for_price(combo_price))
    if price_basis == "aggressive":
        combo_price = next_more_aggressive_price(combo_price, scenario.combo_action)

    return {
        "legs": [
            {
                "symbol": "SPX",
                "expiry": expiry,
                "strike": scenario.lower_strike,
                "right": scenario.right,
                "action": lower_action,
                "qty": 1,
                "lmtPrice": lower_price,
            },
            {
                "symbol": "SPX",
                "expiry": expiry,
                "strike": scenario.upper_strike,
                "right": scenario.right,
                "action": upper_action,
                "qty": 1,
                "lmtPrice": upper_price,
            },
        ],
        "orderType": "LMT",
        "tif": "DAY",
        "outsideRth": requested_outside_rth,
        "comboAction": scenario.combo_action,
        "comboQuantity": combo_quantity,
        "comboLmtPrice": combo_price,
    }


async def observe_trade(trade, timeout: float) -> List[Dict[str, Any]]:
    observations: List[Dict[str, Any]] = []
    if trade is None:
        return observations

    deadline = asyncio.get_event_loop().time() + timeout
    last_status = None
    while asyncio.get_event_loop().time() < deadline:
        snapshot = serialize_trade(trade)
        status = snapshot.get("status")
        if status != last_status:
            observations.append(snapshot)
            last_status = status
        if status in TERMINAL_STATUSES:
            break
        await asyncio.sleep(0.5)

    final_snapshot = serialize_trade(trade)
    if not observations or observations[-1] != final_snapshot:
        observations.append(final_snapshot)
    return observations


def accepted_by_ib(snapshot: Dict[str, Any]) -> bool:
    status = snapshot.get("status") or ""
    return status in ACCEPTED_SERVER_STATUSES


def accepted_status_from_result(result: Dict[str, Any], observations: List[Dict[str, Any]], snapshot: Dict[str, Any]) -> Optional[str]:
    statuses = [
        result.get("data", {}).get("status"),
        *[obs.get("status") for obs in observations],
        snapshot.get("status"),
    ]
    for status in statuses:
        if status in ACCEPTED_SERVER_STATUSES:
            return status
    return None


def next_more_aggressive_price(current_price: float, action: str) -> float:
    tick = spx_tick_for_price(current_price)
    if action == "BUY":
        return round_signed_to_tick(current_price + tick, spx_tick_for_price(current_price + tick))
    if current_price < 0:
        return round_signed_to_tick(current_price + tick, spx_tick_for_price(current_price + tick))
    return round_signed_to_tick(current_price - tick, spx_tick_for_price(current_price - tick))


async def run_acceptance_probe(
    ib: IB,
    state,
    scenario: SpreadScenario,
    submit_result: Dict[str, Any],
    trade,
    recorder: EventRecorder,
    ack_timeout: float,
) -> Dict[str, Any]:
    result = submit_result
    order_id = result.get("data", {}).get("orderId")
    observations = await observe_trade(trade, ack_timeout)
    snapshot_before_cancel = serialize_trade(trade)
    accepted_status = accepted_status_from_result(result, observations, snapshot_before_cancel)
    cancel_result = None
    if order_id and snapshot_before_cancel.get("status") not in TERMINAL_STATUSES:
        cancel_result = await handle_cancel_order(ib, state, order_id)
        await asyncio.sleep(1.0)
    final_snapshot = serialize_trade(trade)
    summary = {
        "scenario": scenario.name,
        "stage": "acceptance",
        "result": result,
        "accepted": accepted_status is not None,
        "acceptedStatus": accepted_status,
        "snapshotBeforeCancel": snapshot_before_cancel,
        "snapshotAfterCancel": final_snapshot,
        "observations": observations,
        "cancelResult": cancel_result,
        "events": list(recorder.events),
    }
    emit(f"{scenario.name} acceptance", summary)
    return summary


async def run_fill_probe(
    ib: IB,
    state,
    scenario: SpreadScenario,
    submit_result: Dict[str, Any],
    trade,
    recorder: EventRecorder,
    fill_timeout: float,
    reprice_interval: float,
) -> Dict[str, Any]:
    result = submit_result
    price_path: List[float] = []
    modified_order_ids: List[int] = []
    cancel_result = None
    observations: List[Dict[str, Any]] = []

    if trade is not None and getattr(trade.order, "lmtPrice", None) is not None:
        price_path.append(float(trade.order.lmtPrice))

    if trade is not None:
        observations = await observe_trade(trade, min(fill_timeout, 10.0))
        current_snapshot = serialize_trade(trade)
        accepted_status = accepted_status_from_result(result, observations, current_snapshot)
        if current_snapshot.get("status") != "Filled" and accepted_status is None:
            cancel_result = await handle_cancel_order(ib, state, trade.order.orderId)
            await asyncio.sleep(1.0)
            final_snapshot = serialize_trade(trade)
            summary = {
                "scenario": scenario.name,
                "stage": "fill",
                "result": result,
                "filled": False,
                "acceptedStatus": None,
                "finalSnapshot": final_snapshot,
                "observations": observations,
                "pricePath": price_path,
                "modifiedOrderIds": modified_order_ids,
                "cancelResult": cancel_result,
                "events": list(recorder.events),
            }
            emit(f"{scenario.name} fill", summary)
            return summary

    deadline = asyncio.get_event_loop().time() + fill_timeout
    while trade is not None and asyncio.get_event_loop().time() < deadline:
        status = trade.orderStatus.status or ""
        if status == "Filled":
            break
        if status in {"Cancelled", "ApiCancelled", "Inactive"}:
            break

        next_price = next_more_aggressive_price(float(trade.order.lmtPrice), trade.order.action)
        if next_price == float(trade.order.lmtPrice):
            break

        trade.order.lmtPrice = next_price
        trade = ib.placeOrder(trade.contract, trade.order)
        state.active_trades[trade.order.orderId] = trade
        modified_order_ids.append(trade.order.orderId)
        price_path.append(float(trade.order.lmtPrice))
        await asyncio.sleep(reprice_interval)

    final_snapshot = serialize_trade(trade)
    if trade is not None and final_snapshot.get("status") != "Filled":
        cancel_result = await handle_cancel_order(ib, state, trade.order.orderId)
        await asyncio.sleep(1.0)
        final_snapshot = serialize_trade(trade)

    summary = {
        "scenario": scenario.name,
        "stage": "fill",
        "result": result,
        "filled": final_snapshot.get("status") == "Filled",
        "acceptedStatus": accepted_status_from_result(result, observations, final_snapshot),
        "finalSnapshot": final_snapshot,
        "observations": observations,
        "pricePath": price_path,
        "modifiedOrderIds": modified_order_ids,
        "cancelResult": cancel_result,
        "events": list(recorder.events),
    }
    emit(f"{scenario.name} fill", summary)
    return summary


async def resolve_probe_inputs(args, ib: IB) -> Tuple[str, float, float, List[Any], LegQuote, LegQuote, float]:
    underlying = await get_underlying(ib)
    spot = await get_spot_price(ib, underlying)
    expirations, strikes = await get_chain_params(ib, underlying)
    expiry = args.expiry or find_next_expiration(expirations)
    if not expiry:
        raise RuntimeError("Unable to determine an expiration to probe")

    if args.lower_strike is not None and args.upper_strike is not None:
        lower_strike = float(args.lower_strike)
        upper_strike = float(args.upper_strike)
    else:
        lower_strike, upper_strike = pick_vertical_strikes(strikes, spot, float(args.width))

    lower_contract = Option(
        symbol="SPX",
        lastTradeDateOrContractMonth=expiry,
        strike=lower_strike,
        right=args.right,
        exchange=args.leg_exchange or "SMART",
        multiplier="100",
        currency="USD",
        tradingClass="SPXW",
    )
    upper_contract = Option(
        symbol="SPX",
        lastTradeDateOrContractMonth=expiry,
        strike=upper_strike,
        right=args.right,
        exchange=args.leg_exchange or "SMART",
        multiplier="100",
        currency="USD",
        tradingClass="SPXW",
    )

    qualified_legs = await ib.qualifyContractsAsync(lower_contract, upper_contract)
    if len(qualified_legs) != 2:
        raise RuntimeError("Failed to qualify both spread legs")

    lower_quote = await wait_for_quote(ib, qualified_legs[0])
    upper_quote = await wait_for_quote(ib, qualified_legs[1])
    return expiry, lower_strike, upper_strike, list(qualified_legs), lower_quote, upper_quote, spot


async def submit_probe_order(
    ib: IB,
    state,
    payload: Dict[str, Any],
    submit_style: str,
    qualified_legs: List[Any],
    requested_outside_rth: bool,
    bag_exchange: Optional[str],
    leg_exchange: Optional[str],
) -> Tuple[Dict[str, Any], Any]:
    if submit_style == "production":
        result = await handle_place_order(ib, state, payload)
        order_id = result.get("data", {}).get("orderId")
        return result, state.active_trades.get(order_id) if order_id else None

    symbol = (payload["legs"][0].get("symbol") or "SPX").upper()
    use_direct_cboe_combo = symbol == "SPX"
    resolved_leg_exchange = leg_exchange or ("CBOE" if use_direct_cboe_combo else None)
    resolved_bag_exchange = bag_exchange or ("CBOE" if use_direct_cboe_combo else None)

    combo_legs = []
    for qc, leg in zip(qualified_legs, payload["legs"]):
        combo_leg = ComboLeg()
        combo_leg.conId = qc.conId
        combo_leg.ratio = int(leg["qty"])
        combo_leg.action = leg["action"]
        combo_leg.exchange = resolved_leg_exchange or getattr(qc, "exchange", "SMART") or "SMART"
        combo_legs.append(combo_leg)

    bag = Contract()
    bag.symbol = payload["legs"][0].get("symbol", "SPX")
    bag.secType = "BAG"
    bag.currency = "USD"
    bag.exchange = resolved_bag_exchange or (combo_legs[0].exchange if combo_legs else "SMART")
    bag.comboLegs = combo_legs

    order = Order(
        orderType=payload.get("orderType", "LMT"),
        action=payload["comboAction"],
        totalQuantity=int(payload.get("comboQuantity", 1) or 1),
        tif=payload.get("tif", "DAY"),
        outsideRth=bool(requested_outside_rth),
        transmit=True,
        lmtPrice=float(payload["comboLmtPrice"]),
    )
    if bag.exchange == "SMART":
        order.smartComboRoutingParams = [TagValue("NonGuaranteed", "1")]
    order.eTradeOnly = False
    order.firmQuoteOnly = False

    trade = ib.placeOrder(bag, order)
    state.active_trades[order.orderId] = trade
    await asyncio.sleep(0.2)
    status = trade.orderStatus.status or "PendingSubmit"
    result = {
        "type": "order_status",
        "data": {
            "status": status,
            "orderId": order.orderId,
            "message": (
                f"Raw combo order {status}: {payload['comboAction']} "
                f"{payload['legs'][0]['strike']}{payload['legs'][0]['right']}/"
                f"{payload['legs'][1]['strike']}{payload['legs'][1]['right']}"
            ),
        },
    }
    return result, trade


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manual IB paper spread probe")
    parser.add_argument("--host", default=IB_HOST)
    parser.add_argument("--port", type=int, default=IB_PORT)
    parser.add_argument("--client-id", type=int, default=max(IB_CLIENT_ID + 40, 41))
    parser.add_argument("--expiry", help="Explicit expiration in YYYYMMDD format")
    parser.add_argument("--lower-strike", type=float)
    parser.add_argument("--upper-strike", type=float)
    parser.add_argument("--width", type=float, default=10.0)
    parser.add_argument("--right", choices=["C", "P"], default="C")
    parser.add_argument("--scenario", choices=["both", "debit", "credit"], default="both")
    parser.add_argument("--mode", choices=["ack", "fill", "both"], default="both")
    parser.add_argument("--submit-style", choices=["production", "raw"], default="production")
    parser.add_argument("--bag-exchange", help="Optional raw BAG exchange override, for example SMART or CBOE")
    parser.add_argument("--leg-exchange", help="Optional option leg exchange override, for example SMART or CBOE")
    parser.add_argument("--quantity", type=int, default=1)
    parser.add_argument("--price-basis", choices=["mid", "natural", "aggressive"], default="natural")
    parser.add_argument("--ack-timeout", type=float, default=8.0)
    parser.add_argument("--fill-timeout", type=float, default=30.0)
    parser.add_argument("--reprice-interval", type=float, default=2.0)
    parser.add_argument("--wait-for-rth", action="store_true")
    parser.add_argument("--max-wait-seconds", type=float)
    parser.add_argument("--json-out", help="Optional path to write the full probe summary as JSON")
    session_group = parser.add_mutually_exclusive_group()
    session_group.add_argument("--outside-rth", action="store_true", dest="outside_rth")
    session_group.add_argument("--inside-rth", action="store_false", dest="outside_rth")
    parser.set_defaults(outside_rth=None)
    return parser


async def async_main(args) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    ib = IB()
    state = create_app_state()
    recorder = EventRecorder()

    ib.errorEvent += recorder.on_error
    ib.openOrderEvent += recorder.on_open_order
    ib.orderStatusEvent += recorder.on_order_status
    ib.execDetailsEvent += recorder.on_exec_details
    ib.newOrderEvent += recorder.on_new_order

    await ib.connectAsync(args.host, args.port, clientId=args.client_id, timeout=15)
    state.connected = True

    try:
        await maybe_wait_for_rth(args)

        effective_outside_rth = args.outside_rth
        if effective_outside_rth is None:
            effective_outside_rth = market_status() != "RTH"

        expiry, lower_strike, upper_strike, qualified_legs, lower_quote, upper_quote, spot = await resolve_probe_inputs(args, ib)
        selected_scenarios = scenario_filter(
            build_scenarios(lower_strike, upper_strike, args.right),
            args.scenario,
        )

        run_summary: Dict[str, Any] = {
            "marketStatus": market_status(),
            "requestedOutsideRth": effective_outside_rth,
            "host": args.host,
            "port": args.port,
            "clientId": args.client_id,
            "submitStyle": args.submit_style,
            "expiry": expiry,
            "spot": spot,
            "lowerStrike": lower_strike,
            "upperStrike": upper_strike,
            "lowerQuote": lower_quote.__dict__,
            "upperQuote": upper_quote.__dict__,
            "results": [],
        }
        emit("probe setup", run_summary)

        for scenario in selected_scenarios:
            payload = build_payload(
                scenario,
                expiry,
                max(1, args.quantity),
                lower_quote,
                upper_quote,
                bool(effective_outside_rth),
                args.price_basis,
            )
            emit(f"{scenario.name} payload", payload)

            if args.mode in {"ack", "both"}:
                recorder.clear()
                submit_result, trade = await submit_probe_order(
                    ib,
                    state,
                    payload,
                    args.submit_style,
                    qualified_legs,
                    bool(effective_outside_rth),
                    args.bag_exchange,
                    args.leg_exchange,
                )
                run_summary["results"].append(
                    await run_acceptance_probe(
                        ib,
                        state,
                        scenario,
                        submit_result,
                        trade,
                        recorder,
                        args.ack_timeout,
                    )
                )

            if args.mode in {"fill", "both"}:
                recorder.clear()
                submit_result, trade = await submit_probe_order(
                    ib,
                    state,
                    payload,
                    args.submit_style,
                    qualified_legs,
                    bool(effective_outside_rth),
                    args.bag_exchange,
                    args.leg_exchange,
                )
                run_summary["results"].append(
                    await run_fill_probe(
                        ib,
                        state,
                        scenario,
                        submit_result,
                        trade,
                        recorder,
                        args.fill_timeout,
                        args.reprice_interval,
                    )
                )

        if args.json_out:
            output_path = Path(args.json_out)
            output_path.write_text(json.dumps(run_summary, indent=2, default=str), encoding="utf-8")
            logger.info("Wrote probe summary to %s", output_path)

        emit("probe summary", run_summary)

        all_ok = True
        for result in run_summary["results"]:
            if result["stage"] == "acceptance" and not result.get("accepted"):
                all_ok = False
            if result["stage"] == "fill" and not result.get("filled"):
                all_ok = False
        return 0 if all_ok else 1
    finally:
        if ib.isConnected():
            ib.disconnect()


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()
    return asyncio.run(async_main(args))


if __name__ == "__main__":
    raise SystemExit(main())