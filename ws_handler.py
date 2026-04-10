"""
WebSocket endpoint, message routing, and broadcast utility.

The WebSocket handler dispatches incoming messages to the appropriate
module-level handlers (order_manager, account_manager, etc.).
"""

import asyncio
import json
import logging
import math
import time

from fastapi import WebSocket, WebSocketDisconnect

from config import VIEWPORT_CENTER_MIN_INTERVAL
from market_hours import now_et, market_status, get_expiration_display, is_within_rth
from chain_fetcher import clear_qualification_cache
from account_manager import refresh_account_state, build_account_payload
from order_manager import handle_place_order, handle_cancel_order

logger = logging.getLogger(__name__)


async def broadcast(state, message: dict):
    """Send a message to all connected WebSocket clients."""
    if not state.ws_clients:
        return
    try:
        data = json.dumps(message, allow_nan=False)
    except ValueError as e:
        logger.error(f"Dropping non-JSON-serializable payload type={message.get('type')}: {e}")
        return
    dead = []
    for ws in state.ws_clients:
        try:
            await ws.send_text(data)
        except Exception:
            dead.append(ws)
    for ws in dead:
        state.ws_clients.discard(ws)


def make_broadcast_fn(state):
    """Return a broadcast(message) coroutine bound to the given state."""
    async def _broadcast(message: dict):
        await broadcast(state, message)
    return _broadcast


async def status_push_loop(state, broadcast_fn):
    """Push status updates every few seconds."""
    while True:
        try:
            await asyncio.sleep(5)

            if not is_within_rth() and state.data_mode == "live":
                state.data_mode = "historical"

            status = {
                "type": "status",
                "data": {
                    "connected": state.connected,
                    "market_status": market_status(),
                    "expiration": get_expiration_display(state.expiration) if state.expiration else "N/A",
                    "chain_fetching": state.chain_fetching,
                    "last_chain_update": state.last_chain_update or "Never",
                    "spot_price": round(state.spx_price, 2),
                    "price_history_len": len(state.price_history),
                    "data_mode": state.data_mode,
                    "historical_date": state.historical_date,
                    "es_derived": state.es_derived,
                    "es_price": round(state.es_price, 2) if state.es_price > 0 else None,
                },
            }
            await broadcast_fn(status)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Status push error: {e}")
            await asyncio.sleep(5)


async def ib_keepalive_loop(ib):
    """Keep ib_insync event loop processing IB messages."""
    while True:
        try:
            ib.sleep(0.1)
            await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            break
        except Exception:
            await asyncio.sleep(0.5)


async def websocket_endpoint(ws: WebSocket, ib, state, broadcast_fn):
    """Handle a single WebSocket client connection.

    Called from the FastAPI route; ib, state, and broadcast_fn are injected
    by the server module.
    """
    await ws.accept()
    state.ws_clients.add(ws)
    logger.info(f"WebSocket client connected (total: {len(state.ws_clients)})")

    try:
        # Send initial state
        init_msg = {
            "type": "init",
            "data": {
                "connected": state.connected,
                "market_status": market_status(),
                "expiration": get_expiration_display(state.expiration) if state.expiration else "N/A",
                "spot_price": round(state.spx_price, 2),
                "price_history": list(state.price_history),
                "gex": state.latest_gex,
                "last_chain_update": state.last_chain_update or "Never",
                "data_mode": state.data_mode,
                "historical_date": state.historical_date,
                "es_derived": state.es_derived,
                "es_price": round(state.es_price, 2) if state.es_price > 0 else None,
                "chain_quotes": state.chain_quotes_cache,
                "account": build_account_payload(state),
            }
        }
        await ws.send_text(json.dumps(init_msg))

        while True:
            try:
                msg = await asyncio.wait_for(ws.receive_text(), timeout=30)

                if msg == "refresh_chain":
                    logger.info("Client requested chain refresh")
                    clear_qualification_cache("option-tab manual refresh")
                    state.manual_refresh_requested = True
                    if state.force_chain_fetch_event is not None:
                        state.force_chain_fetch_event.set()

                elif msg == "set_tab:chain":
                    state.active_tab = "chain"
                    state.viewport_center_strike = 0.0
                    logger.info("Client active tab: chain")

                elif msg == "set_tab:dashboard":
                    state.active_tab = "dashboard"
                    state.viewport_center_strike = 0.0
                    logger.info("Client active tab: dashboard")

                elif msg == "set_tab:account":
                    state.active_tab = "account"
                    logger.info("Client active tab: account")
                    refresh_account_state(ib, state)
                    await ws.send_text(json.dumps({
                        "type": "account_update",
                        "data": build_account_payload(state),
                    }))

                elif msg.startswith("place_order:"):
                    try:
                        payload = json.loads(msg.split(":", 1)[1])
                        resp = await handle_place_order(
                            ib, state, payload, ws=ws,
                            refresh_fn=refresh_account_state,
                        )
                        await ws.send_text(json.dumps(resp))
                    except Exception as e:
                        logger.error(f"place_order error: {e}", exc_info=True)
                        await ws.send_text(json.dumps({
                            "type": "order_status",
                            "data": {"status": "Error", "message": str(e)},
                        }))

                elif msg.startswith("cancel_order:"):
                    try:
                        order_id = int(msg.split(":", 1)[1])
                        resp = await handle_cancel_order(
                            ib, state, order_id,
                            refresh_fn=refresh_account_state,
                        )
                        await ws.send_text(json.dumps(resp))
                    except Exception as e:
                        logger.error(f"cancel_order error: {e}", exc_info=True)
                        await ws.send_text(json.dumps({
                            "type": "order_status",
                            "data": {"status": "Error", "message": str(e)},
                        }))

                elif msg.startswith("viewport_center:"):
                    try:
                        strike = float(msg.split(":", 1)[1])
                    except (TypeError, ValueError):
                        continue
                    if not math.isfinite(strike) or strike <= 0:
                        continue
                    now_mono = time.monotonic()
                    if (now_mono - state.viewport_center_last_ts) < VIEWPORT_CENTER_MIN_INTERVAL:
                        continue
                    state.viewport_center_last_ts = now_mono
                    state.viewport_center_strike = round(strike, 1)

            except asyncio.TimeoutError:
                try:
                    await ws.send_text(json.dumps({"type": "ping"}))
                except Exception:
                    break

    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.debug(f"WebSocket error: {e}")
    finally:
        state.ws_clients.discard(ws)
        logger.info(f"WebSocket client disconnected (total: {len(state.ws_clients)})")
