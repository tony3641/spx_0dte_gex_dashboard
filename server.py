"""
SPX 0DTE Dashboard Server — Thin Entrypoint

All functional logic lives in dedicated modules:
  config, app_state, ib_connection, account_manager, order_manager,
  price_bars, chain_manager, ws_handler

This file wires them together via FastAPI lifespan, HTTP routes,
and the WebSocket endpoint.
"""

import asyncio
import logging
import signal
import sys
from pathlib import Path
from typing import Optional

# Must be BEFORE any event-loop creation
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import nest_asyncio
import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from ib_insync import IB

import config
from app_state import AppState
from ib_connection import (
    connect_ib, setup_spx_subscription, setup_chain_info,
    make_pending_tickers_handler, setup_es_subscription, fetch_es_baseline,
)
from account_manager import (
    refresh_account_state, build_account_payload,
    setup_account_subscription, account_push_loop,
)
from price_bars import fetch_historical_bars, price_push_loop
from chain_manager import chain_fetch_loop, chain_stream_loop
from ws_handler import (
    broadcast, make_broadcast_fn, status_push_loop,
    ib_keepalive_loop, websocket_endpoint as ws_endpoint,
)
from market_hours import is_within_rth, market_status, get_expiration_display
from risk_free import get_risk_free_rate

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("server")

# ---------------------------------------------------------------------------
# Module-level wiring (created in lifespan, used by routes)
# ---------------------------------------------------------------------------
ib: Optional[IB] = None
state = AppState()
broadcast_fn = None  # set in lifespan


# ---------------------------------------------------------------------------
# FastAPI lifespan
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(_app):
    """Startup and shutdown logic."""
    global ib, broadcast_fn
    logger.info("Starting SPX 0DTE GEX Dashboard...")

    loop = asyncio.get_event_loop()
    nest_asyncio.apply(loop)
    ib = IB()
    broadcast_fn = make_broadcast_fn(state)

    try:
        await connect_ib(ib, state)
        await setup_spx_subscription(ib, state)
        await setup_chain_info(ib, state)

        # ES futures for off-hours derived price
        await setup_es_subscription(ib, state)

        # Use the current 7-day yield from SGOV as the risk-free rate
        state.risk_free_rate = get_risk_free_rate()
        logger.info(f"Risk-free rate set from SGOV 7 Day Yield: {state.risk_free_rate:.4%}")

        # Seed chart with current/last session's intraday bars
        await fetch_historical_bars(ib, state)

        # ES baseline only needed outside RTH
        if not is_within_rth():
            await fetch_es_baseline(ib, state)
            logger.info(
                f"Historical mode: showing {state.historical_date}, "
                f"ref price={state.spx_price:.2f}, "
                f"ES baseline={state.es_at_spx_close:.2f}"
            )

        # Register ticker update callback
        ib.pendingTickersEvent += make_pending_tickers_handler(state)

        # Account subscription
        await setup_account_subscription(ib, state)

        # Start background loops
        state.background_tasks.append(asyncio.create_task(ib_keepalive_loop(ib)))
        state.background_tasks.append(asyncio.create_task(price_push_loop(ib, state, broadcast_fn)))
        state.background_tasks.append(asyncio.create_task(status_push_loop(state, broadcast_fn)))
        state.background_tasks.append(asyncio.create_task(account_push_loop(ib, state, broadcast_fn)))

        # Chain snapshot loop — force immediate first snapshot
        if state.force_chain_fetch_event is None:
            state.force_chain_fetch_event = asyncio.Event()
        state.manual_refresh_requested = True
        state.force_chain_fetch_event.set()
        state.background_tasks.append(asyncio.create_task(chain_fetch_loop(ib, state, broadcast_fn)))

        # Wait for initial snapshot
        snapshot_ready = False
        for _ in range(120):
            if state.latest_gex is not None and len(state.chain_data) > 0:
                snapshot_ready = True
                break
            await asyncio.sleep(0.5)
        if snapshot_ready:
            logger.info("Initial dashboard snapshot ready; starting chain stream")
        else:
            logger.warning("Initial snapshot timeout; starting chain stream anyway")

        state.background_tasks.append(asyncio.create_task(chain_stream_loop(ib, state, broadcast_fn)))
        logger.info("All background tasks started")

    except Exception as e:
        logger.error(f"Startup failed: {e}", exc_info=True)

    yield  # App is running

    # Shutdown
    logger.info("Shutting down...")
    for task in state.background_tasks:
        task.cancel()
    if ib.isConnected():
        ib.disconnect()
        logger.info("Disconnected from IB")


# ---------------------------------------------------------------------------
# Create app & HTTP routes
# ---------------------------------------------------------------------------
app = FastAPI(title="SPX 0DTE Option Dashboard", lifespan=lifespan)

STATIC_DIR = Path(__file__).parent / "static"


@app.get("/")
async def serve_index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/state")
async def get_state():
    """Return current full state (for initial page load / reconnection)."""
    return {
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
    }


@app.post("/api/reconnect_ib")
async def reconnect_ib(payload: dict):
    """Reconnect to the IB API using a specified port number."""
    if payload is None:
        raise HTTPException(status_code=400, detail="Request JSON body required")
    port = payload.get("port")
    if port is None:
        raise HTTPException(status_code=400, detail="Port number is required")
    try:
        port = int(port)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Port must be an integer")
    if port <= 0 or port > 65535:
        raise HTTPException(status_code=400, detail="Port must be between 1 and 65535")

    logger.info(f"Reconnecting to IB on port {port}")
    state.connected = False
    if state.chain_fetch_active is not None:
        state.chain_fetch_active.clear()

    for key, contract in list(state.chain_stream_contracts.items()):
        try:
            ib.cancelMktData(contract)
        except Exception:
            pass
    state.chain_stream_tickers.clear()
    state.chain_stream_contracts.clear()
    state.chain_stream_unknown_keys.clear()

    try:
        if ib.isConnected():
            ib.disconnect()
            logger.info("Disconnected IB before reconnecting")
    except Exception as e:
        logger.warning(f"Error disconnecting IB before reconnect: {e}")

    try:
        await connect_ib(ib, state, port=port)
        await setup_spx_subscription(ib, state)
        await setup_chain_info(ib, state)
        state.manual_refresh_requested = True
        if state.force_chain_fetch_event is not None:
            state.force_chain_fetch_event.set()
        await broadcast(state, {"type": "status", "data": {
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
        }})
    except Exception as e:
        logger.error(f"Reconnect IB failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    return {"status": "ok", "port": port}


# ---------------------------------------------------------------------------
# WebSocket endpoint (delegates to ws_handler)
# ---------------------------------------------------------------------------
@app.websocket("/ws")
async def websocket_route(ws: WebSocket):
    await ws_endpoint(ws, ib, state, broadcast_fn)


# ---------------------------------------------------------------------------
# Static files (must be after routes)
# ---------------------------------------------------------------------------
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    nest_asyncio.apply(loop)

    uvi_config = uvicorn.Config(
        app,
        host=config.SERVER_HOST,
        port=config.SERVER_PORT,
        log_level="info",
        loop="none",
    )
    server = uvicorn.Server(uvi_config)

    shutdown_requested = {"value": False}

    def _handle_shutdown_signal(signum, _frame):
        try:
            signame = signal.Signals(signum).name
        except Exception:
            signame = str(signum)

        if not shutdown_requested["value"]:
            shutdown_requested["value"] = True
            logger.info(f"Shutdown signal received ({signame}); stopping server gracefully...")
            server.should_exit = True
        else:
            logger.warning(f"Second shutdown signal received ({signame}); forcing exit...")
            server.force_exit = True

    for sig_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, sig_name, None)
        if sig is not None:
            try:
                signal.signal(sig, _handle_shutdown_signal)
            except Exception as e:
                logger.debug(f"Unable to register handler for {sig_name}: {e}")

    logger.info(f"Server starting on {config.SERVER_HOST}:{config.SERVER_PORT}")
    if config.SERVER_HOST == "0.0.0.0":
        logger.info(f"  Local access    : http://localhost:{config.SERVER_PORT}")
        logger.info(f"  Network access  : http://<your-local-ip>:{config.SERVER_PORT}")
    else:
        logger.info(f"  Access at       : http://{config.SERVER_HOST}:{config.SERVER_PORT}")

    try:
        loop.run_until_complete(server.serve())
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt received; shutting down...")
        server.should_exit = True
    finally:
        pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()
        logger.info("Server shutdown complete")
