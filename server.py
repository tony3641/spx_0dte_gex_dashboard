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
import math
import signal
import sys
from pathlib import Path
from typing import Optional

# Must be BEFORE any event-loop creation
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import nest_asyncio
import uvicorn
import numpy as np
from contextlib import asynccontextmanager
from fastapi import Body, FastAPI, WebSocket, HTTPException, Request
from pydantic import BaseModel
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from ib_client import IBClient

import config
from app_state import AppState
from ib_connection import (
    connect_ib, setup_spx_subscription, setup_chain_info,
    setup_es_subscription, fetch_es_baseline,
    setup_monthly_chain_info,
)
from account_manager import (
    refresh_account_state, build_account_payload,
    setup_account_subscription, account_push_loop,
)
from price_bars import fetch_historical_bars, price_push_loop
from chain_manager import chain_fetch_loop, chain_stream_loop
from ws_handler import (
    broadcast, make_broadcast_fn, make_ib_error_handler, status_push_loop,
    websocket_endpoint as ws_endpoint,
)
from market_hours import is_within_rth, market_status, get_expiration_display
from risk_free import get_risk_free_rate
from strategy_store import load_strategies
from strategy_engine import strategy_evaluation_loop, take_profit_loop
from ib_connection import setup_vix_subscription
from log_buffer import LogStoreHandler, log_push_loop
from discord_settings import (
    DiscordSettings, DiscordSettingsManager, load_initial_settings,
)
from env_store import update_env
import sim_jobs

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
ib: Optional[IBClient] = None
state = AppState()
broadcast_fn = None  # set in lifespan
discord_manager: Optional[DiscordSettingsManager] = None


# ---------------------------------------------------------------------------
# FastAPI lifespan
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(_app):
    """Startup and shutdown logic."""
    global ib, broadcast_fn
    logger.info("Starting SPX 0DTE GEX Dashboard...")

    # Capture every framework log record into the Log console's ring buffer.
    logging.getLogger().addHandler(LogStoreHandler(state))

    loop = asyncio.get_event_loop()
    nest_asyncio.apply(loop)
    ib = IBClient()
    broadcast_fn = make_broadcast_fn(state)

    # Discord bot (in-process, on the same loop). Owned by the manager so
    # IB reconnects (which cancel state.background_tasks) never kill it.
    # Constructed OUTSIDE the IB setup try: Discord startup/config must not
    # depend on IB health, and the settings endpoints need the manager to
    # exist even when IB boot fails.
    global discord_manager
    discord_manager = DiscordSettingsManager(ib, state)
    try:
        # persist=False: config-derived boot never writes back to .env — only
        # UI-authored applies persist (otherwise empty DISCORD_* keys would
        # shadow params.yaml on later boots).
        result = await discord_manager.apply(load_initial_settings(),
                                             persist=False)
        if result.get("ok"):
            logger.info("Discord bot started" if result["running"]
                        else "Discord disabled (no token)")
        else:
            logger.warning(f"Discord not started: {result.get('error')}")
    except Exception as e:
        logger.error(f"Failed to start Discord bot: {e}", exc_info=True)

    try:
        await connect_ib(ib, state)
        ib.error_handler = make_ib_error_handler(state, broadcast_fn)
        await setup_spx_subscription(ib, state)
        await setup_chain_info(ib, state)
        await setup_monthly_chain_info(ib, state)

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

        # Account subscription
        await setup_account_subscription(ib, state)

        # Strategy engine: load persisted strategies and subscribe VIX for the
        # volatility condition
        state.strategies = load_strategies()
        await setup_vix_subscription(ib, state)

        # Start background loops
        state.background_tasks.append(asyncio.create_task(price_push_loop(ib, state, broadcast_fn)))
        state.background_tasks.append(asyncio.create_task(status_push_loop(state, broadcast_fn)))
        state.background_tasks.append(asyncio.create_task(account_push_loop(ib, state, broadcast_fn)))
        state.background_tasks.append(asyncio.create_task(log_push_loop(state, broadcast_fn)))
        state.background_tasks.append(asyncio.create_task(strategy_evaluation_loop(ib, state, broadcast_fn)))
        state.background_tasks.append(asyncio.create_task(take_profit_loop(ib, state, broadcast_fn)))

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
    if discord_manager is not None:
        try:
            await discord_manager.stop()
        except Exception as e:
            logger.warning(f"Error stopping Discord bot: {e}")
    if getattr(ib, "connected", False):
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
        "gex_mode": state.gex_mode,
        "monthly_gex": state.monthly_latest_gex,
        "monthly_expiration": get_expiration_display(state.monthly_expiration) if state.monthly_expiration else "N/A",
    }


async def reconnect_ib_on(port: int) -> dict:
    """Reconnect IB on `port` (validated by callers). Same teardown/reconnect/
    restart behavior the /api/reconnect_ib endpoint had."""
    global ib
    logger.info(f"Reconnecting to IB on port {port}")
    state.connected = False
    if state.chain_fetch_active is not None:
        state.chain_fetch_active.clear()

    # Stop the old background loops up front. If the reconnect fails below, we
    # must not leave them running against the (soon to be disconnected) old
    # client — cancel and clear them now so a failure is atomic: no loops, but
    # a consistent fresh client, and a retry recovers cleanly.
    for task in state.background_tasks:
        task.cancel()
    await asyncio.gather(*state.background_tasks, return_exceptions=True)
    state.background_tasks.clear()

    # Tear down every active market-data stream on the old client (the native
    # bridge tracks subscriptions by reqId, not per-contract).
    try:
        ib.unsubscribe_all()
    except Exception:
        pass
    state.chain_stream_tickers.clear()
    state.chain_stream_contracts.clear()
    state.chain_stream_unknown_keys.clear()

    # Disconnect the old client, then swap in a fresh IBClient — the native
    # bridge does not support re-connecting a disconnected instance.
    try:
        ib.disconnect()
        logger.info("Disconnected IB before reconnecting")
    except Exception as e:
        logger.warning(f"Error disconnecting IB before reconnect: {e}")
    ib = IBClient()
    if discord_manager is not None:
        discord_manager.ib = ib   # keep the manager on the live client

    try:
        await connect_ib(ib, state, port=port)
        ib.error_handler = make_ib_error_handler(state, broadcast_fn)
        await setup_spx_subscription(ib, state)
        await setup_chain_info(ib, state)
        await setup_monthly_chain_info(ib, state)
        # The fresh IBClient has no account/ES subscriptions (unlike the
        # pre-migration in-place reconnect), so re-subscribe them here.
        await setup_account_subscription(ib, state)
        await setup_es_subscription(ib, state)
        # Restart the background loops against the new client (the originals
        # were cancelled up front so a failed reconnect leaves no loops running).
        state.background_tasks.append(asyncio.create_task(price_push_loop(ib, state, broadcast_fn)))
        state.background_tasks.append(asyncio.create_task(status_push_loop(state, broadcast_fn)))
        state.background_tasks.append(asyncio.create_task(account_push_loop(ib, state, broadcast_fn)))
        state.background_tasks.append(asyncio.create_task(log_push_loop(state, broadcast_fn)))
        state.background_tasks.append(asyncio.create_task(chain_fetch_loop(ib, state, broadcast_fn)))
        state.background_tasks.append(asyncio.create_task(chain_stream_loop(ib, state, broadcast_fn)))
        # Strategy engine: re-subscribe VIX, reload strategies, and restart the
        # auto-entry / take-profit loops so a manual reconnect does NOT silently
        # stop auto-trading or position management.
        await setup_vix_subscription(ib, state)
        state.strategies = load_strategies()
        state.background_tasks.append(asyncio.create_task(strategy_evaluation_loop(ib, state, broadcast_fn)))
        state.background_tasks.append(asyncio.create_task(take_profit_loop(ib, state, broadcast_fn)))
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
# Settings endpoints (localhost-only)
# ---------------------------------------------------------------------------
def _is_localhost(request: Request) -> bool:
    client = request.client
    return client is not None and client.host in ("127.0.0.1", "::1")


class DiscordSettingsIn(BaseModel):
    token: Optional[str] = None   # None = keep existing, "" = clear, str = set
    guild_id: str = ""
    channel_id: str = ""
    allowed_user_ids: str = ""
    allowed_role: str = ""


class IbSettingsIn(BaseModel):
    port: int


@app.get("/api/settings/discord")
async def get_discord_settings(request: Request):
    if not _is_localhost(request):
        raise HTTPException(status_code=403, detail="Localhost only")
    if discord_manager is None:
        raise HTTPException(status_code=503,
                            detail="Discord settings unavailable (server startup incomplete)")
    s = discord_manager.settings
    return {
        "token_set": bool(s.token),
        "token_hint": ("…" + s.token[-4:]) if s.token else None,
        "guild_id": s.guild_id,
        "channel_id": s.channel_id,
        "allowed_user_ids": list(s.allowed_user_ids),
        "allowed_role": s.allowed_role,
        "running": discord_manager.running,
    }


@app.post("/api/settings/discord")
async def post_discord_settings(body: DiscordSettingsIn, request: Request):
    if not _is_localhost(request):
        raise HTTPException(status_code=403, detail="Localhost only")
    if discord_manager is None:
        raise HTTPException(status_code=503,
                            detail="Discord settings unavailable (server startup incomplete)")
    cur = discord_manager.settings
    ids = config.parse_user_ids(body.allowed_user_ids)
    if body.allowed_user_ids.strip() and not ids:
        raise HTTPException(status_code=400,
                            detail="No valid user IDs (comma-separated digits)")
    if body.guild_id.strip() and not body.guild_id.strip().isdigit():
        raise HTTPException(status_code=400, detail="Guild ID must be numeric")
    if body.channel_id.strip() and not body.channel_id.strip().isdigit():
        raise HTTPException(status_code=400, detail="Channel ID must be numeric")
    new = DiscordSettings(
        token=body.token if body.token is not None else cur.token,
        guild_id=body.guild_id.strip(),
        channel_id=body.channel_id.strip(),
        allowed_user_ids=ids,
        allowed_role=body.allowed_role.strip(),
    )
    result = await discord_manager.apply(new)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "Apply failed"))
    return result


@app.get("/api/settings/ib")
async def get_ib_settings(request: Request):
    if not _is_localhost(request):
        raise HTTPException(status_code=403, detail="Localhost only")
    return {"port": state.ib_port, "connected": state.connected}


@app.post("/api/settings/ib")
async def post_ib_settings(body: IbSettingsIn, request: Request):
    if not _is_localhost(request):
        raise HTTPException(status_code=403, detail="Localhost only")
    if body.port <= 0 or body.port > 65535:
        raise HTTPException(status_code=400, detail="Port must be between 1 and 65535")
    try:
        await reconnect_ib_on(body.port)
    except Exception as e:
        logger.error(f"IB reconnect to port {body.port} failed: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    persisted = True
    try:
        update_env({"IB_PORT": str(body.port)})
    except Exception as e:
        logger.warning(f"Failed to persist IB_PORT to .env: {e}")
        persisted = False
    return {"ok": True, "port": body.port, "persisted": persisted}


# ---------------------------------------------------------------------------
# WebSocket endpoint (delegates to ws_handler)
# ---------------------------------------------------------------------------
@app.websocket("/ws")
async def websocket_route(ws: WebSocket):
    await ws_endpoint(ws, ib, state, broadcast_fn)


# ---------------------------------------------------------------------------
# Simulation (intraday MC stress test)
# ---------------------------------------------------------------------------

@app.post("/api/sim/run")
async def api_sim_run(body: dict = Body(...)):
    try:
        out = sim_jobs.start_run(body, state=state, ib=ib)
        return out
    except ValueError as e:
        return JSONResponse(status_code=400, content={"detail": str(e)})
    except RuntimeError as e:
        return JSONResponse(status_code=409, content={"detail": str(e)})


@app.get("/api/sim/status/{job_id}")
async def api_sim_status(job_id: str):
    try:
        return sim_jobs.get_status(job_id)
    except KeyError:
        return JSONResponse(status_code=404, content={"detail": "unknown job"})


@app.get("/api/sim/result/{job_id}")
async def api_sim_result(job_id: str):
    try:
        result = sim_jobs.get_result(job_id)
    except KeyError:
        return JSONResponse(status_code=404, content={"detail": "unknown job"})
    if result is None:
        return JSONResponse(status_code=409, content={"detail": "job not finished"})
    return result


@app.post("/api/sim/cancel/{job_id}")
async def api_sim_cancel(job_id: str):
    return {"cancelled": sim_jobs.cancel(job_id)}


@app.get("/api/sim/smile")
async def api_sim_smile():
    from sim_calibrate import load_smile_snapshot
    smile, src = load_smile_snapshot()
    return {"smile": smile.to_dict(), "source": src}


@app.post("/api/sim/smile/capture")
async def api_sim_smile_capture():
    rows = getattr(state, "chain_quotes_cache", {}) or {}
    strikes = rows.get("strikes") or []
    spot = float(getattr(state, "spx_price", 0) or 0)
    pts_m, pts_iv = [], []
    for row in strikes:
        iv = row.get("put_iv")
        if iv and spot and row.get("strike"):
            m = math.log(float(row["strike"]) / spot)
            if abs(m) < 0.15:
                pts_m.append(m)
                pts_iv.append(float(iv) / 100.0)
    if len(pts_m) < 5:
        return JSONResponse(status_code=409, content={"detail": "live chain not available"})
    from sim_calibrate import fit_smile, DEFAULT_SMILE, save_smile_snapshot
    smile, warnings = fit_smile(np.array(pts_m), np.array(pts_iv), DEFAULT_SMILE)
    if warnings:
        return JSONResponse(status_code=409, content={"detail": warnings[0]})
    save_smile_snapshot(smile)
    return {"smile": smile.to_dict(), "source": "captured", "points": len(pts_m)}


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
