"""
SPX 0DTE Dashboard Server

FastAPI + WebSocket application that:
  1. Connects to IB TWS and streams live SPX quotes
  2. Periodically fetches the full SPXW 0DTE option chain (batched)
  3. Computes GEX, Put/Call Wall, Gamma Flip, Max Pain
  4. Pushes all data to the browser via WebSocket
"""

import asyncio
import json
import logging
import math
import os
import sys
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional, Set

# Must be BEFORE any event-loop creation
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import nest_asyncio

import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from ib_insync import IB, Index, Contract, Future, util

from market_hours import (
    now_et, is_within_rth, market_status,
    find_next_expiration, get_expiration_display, ET,
    is_cboe_options_open, last_trading_date,
)
from chain_fetcher import fetch_option_chain, get_chain_params
from gex_calculator import compute_gex, gex_result_to_dict, GEXResult, OptionData

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
IB_HOST = os.getenv("IB_HOST", "127.0.0.1")
IB_PORT = int(os.getenv("IB_PORT", "7497"))
IB_CLIENT_ID = int(os.getenv("IB_CLIENT_ID", "1"))
CHAIN_REFRESH_SECONDS = int(os.getenv("CHAIN_REFRESH_SECONDS", "60"))
PRICE_PUSH_INTERVAL = float(os.getenv("PRICE_PUSH_INTERVAL", "1.0"))  # seconds
SERVER_HOST = os.getenv("SERVER_HOST", "0.0.0.0")  # 0.0.0.0 = all interfaces
SERVER_PORT = int(os.getenv("SERVER_PORT", "8000"))

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
# IB connection (created lazily in lifespan to use uvicorn's event loop)
# ---------------------------------------------------------------------------
ib: Optional[IB] = None

# Shared state
class AppState:
    spx_contract: Optional[Contract] = None
    spx_price: float = 0.0          # latest known SPX price (live or historical)
    live_price: float = 0.0          # latest live streaming price (0 when not streaming)
    price_history: deque = deque(maxlen=28800)  # OHLC bars (1-min)
    latest_gex: Optional[dict] = None
    gex_result: Optional[GEXResult] = None
    expiration: str = ""
    expirations: List[str] = []
    strikes: List[float] = []
    connected: bool = False
    chain_fetching: bool = False
    last_chain_update: str = ""
    ws_clients: Set[WebSocket] = set()
    background_tasks: List[asyncio.Task] = []
    # Mode tracking
    data_mode: str = "initializing"   # "live" | "historical" | "initializing"
    historical_date: str = ""         # date string of the historical session shown
    # Volatility (computed at runtime from IB daily bars)
    annual_vol: float = 0.20          # fallback; updated by compute_annual_vol()
    # ES futures (used for off-hours derived SPX price)
    es_contract: Optional[Contract] = None
    es_price: float = 0.0            # latest ES streaming price
    es_at_spx_close: float = 0.0     # ES price at last SPX RTH close (baseline)
    spx_last_close: float = 0.0      # SPX price at last RTH close (fixed reference)
    es_derived: bool = False          # True when spx_price is computed from ES

state = AppState()


# ---------------------------------------------------------------------------
# WebSocket broadcast
# ---------------------------------------------------------------------------
async def broadcast(message: dict):
    """Send a message to all connected WebSocket clients."""
    if not state.ws_clients:
        return
    data = json.dumps(message)
    dead: List[WebSocket] = []
    for ws in state.ws_clients:
        try:
            await ws.send_text(data)
        except Exception:
            dead.append(ws)
    for ws in dead:
        state.ws_clients.discard(ws)


# ---------------------------------------------------------------------------
# IB connection & data handling
# ---------------------------------------------------------------------------
async def connect_ib():
    """Connect to IB TWS/Gateway."""
    try:
        await ib.connectAsync(IB_HOST, IB_PORT, clientId=IB_CLIENT_ID, timeout=15)
        state.connected = True
        logger.info(f"Connected to IB at {IB_HOST}:{IB_PORT}")
    except Exception as e:
        logger.error(f"Failed to connect to IB: {e}")
        state.connected = False
        raise


async def setup_spx_subscription():
    """Qualify SPX contract and subscribe to live quotes."""
    spx = Index('SPX', 'CBOE', 'USD')
    qualified = ib.qualifyContracts(spx)
    if not qualified:
        logger.error("Failed to qualify SPX contract")
        return
    state.spx_contract = spx
    logger.info(f"SPX contract: {spx}")

    # Request streaming market data (not snapshot)
    # genericTickList='233' → real-time volume (rtVolume)
    ib.reqMktData(spx, genericTickList='233', snapshot=False)
    logger.info("Subscribed to live SPX quotes")


async def setup_chain_info():
    """Fetch SPXW chain parameters and determine target expiration."""
    if state.spx_contract is None:
        return

    exps, strikes = await get_chain_params(ib, state.spx_contract)
    state.expirations = exps
    state.strikes = strikes

    exp = find_next_expiration(exps)
    if exp:
        state.expiration = exp
        logger.info(f"Target expiration: {get_expiration_display(exp)}")
    else:
        logger.warning("No valid SPXW expiration found")


def on_pending_tickers(tickers):
    """Callback for streaming ticker updates from IB."""
    for ticker in tickers:
        contract = ticker.contract
        # Match by conId for reliability
        spx_id = state.spx_contract.conId if state.spx_contract else None
        es_id  = state.es_contract.conId  if state.es_contract  else None

        if spx_id and getattr(contract, 'conId', None) == spx_id:
            price = ticker.marketPrice()
            if price is not None and not math.isnan(price) and price > 0:
                state.spx_price = price
                state.live_price = price
                state.es_derived = False
                if state.data_mode != "live":
                    state.data_mode = "live"
                    logger.info("Switched to LIVE data mode")

        elif es_id and getattr(contract, 'conId', None) == es_id:
            price = ticker.marketPrice()
            if price is not None and not math.isnan(price) and price > 0:
                state.es_price = price
                # Bootstrap baseline from first tick if historical fetch failed
                if state.es_at_spx_close == 0:
                    state.es_at_spx_close = price
                    logger.info(
                        f"ES baseline bootstrapped from first tick: {price:.2f} "
                        f"(delta will accumulate from this point)"
                    )
                # Only apply ES-derived spot when SPX is not live
                if (state.data_mode != "live"
                        and state.es_at_spx_close > 0
                        and state.spx_last_close > 0):
                    pct = (price - state.es_at_spx_close) / state.es_at_spx_close
                    state.spx_price = round(state.spx_last_close * (1.0 + pct), 2)
                    state.es_derived = True


async def setup_es_subscription():
    """
    Find the front-month ES (E-mini S&P 500) futures contract via contract details
    and subscribe to live streaming for off-hours SPX price derivation.
    """
    try:
        es_generic = Future('ES', exchange='CME', currency='USD')
        details = await ib.reqContractDetailsAsync(es_generic)
        if not details:
            logger.warning("No ES contract details returned — off-hours derived price unavailable")
            return
        # Sort by expiry ascending, pick the nearest front-month that hasn't expired yet
        today_str = now_et().strftime("%Y%m%d")
        upcoming = [
            d for d in details
            if d.contract.lastTradeDateOrContractMonth >= today_str
        ]
        if not upcoming:
            logger.warning("No unexpired ES contracts found")
            return
        upcoming.sort(key=lambda d: d.contract.lastTradeDateOrContractMonth)
        # The contract from reqContractDetails is already fully specified — use it directly
        state.es_contract = upcoming[0].contract
        ib.reqMktData(state.es_contract, genericTickList='', snapshot=False)
        logger.info(f"Subscribed to ES futures: {state.es_contract.localSymbol} "
                    f"(expiry {state.es_contract.lastTradeDateOrContractMonth})")
    except Exception as e:
        logger.warning(f"ES subscription failed: {e}")


async def fetch_es_baseline():
    """
    Fetch the ES futures price at the time of the last SPX RTH close (~4:15 PM ET).
    Tries TRADES first, then MIDPOINT as fallback.  Stores result in
    state.es_at_spx_close.  If no historical bar is available, the baseline
    will be bootstrapped from the first live ES tick (delta = 0 initially).
    """
    if state.es_contract is None:
        return

    session_date = last_trading_date()
    # Window ending just after the 4:15 PM SPX options close
    end_dt = datetime(
        session_date.year, session_date.month, session_date.day,
        16, 20, 0,
    ).strftime("%Y%m%d-%H:%M:%S")

    for what_to_show in ('TRADES', 'MIDPOINT'):
        try:
            bars = await ib.reqHistoricalDataAsync(
                contract=state.es_contract,
                endDateTime=end_dt,
                durationStr='600 S',
                barSizeSetting='1 min',
                whatToShow=what_to_show,
                useRTH=False,
                formatDate=1,
            )
            if bars:
                state.es_at_spx_close = bars[-1].close
                logger.info(
                    f"ES baseline at SPX close ({what_to_show}): "
                    f"{state.es_at_spx_close:.2f} (bar {bars[-1].date})"
                )
                return
        except Exception as e:
            logger.warning(f"ES baseline fetch ({what_to_show}) failed: {e}")

    logger.warning(
        "ES baseline unavailable — will bootstrap from first live ES tick "
        "(off-hours derived delta will be ~0 until ES moves)"
    )


async def compute_annual_vol(lookback_days: int = 30) -> float:
    """
    Compute annualised realised volatility from IB daily close bars.
    Uses `lookback_days` calendar days of daily data and returns σ_annual.
    Falls back to the existing state.annual_vol on error.
    """
    if state.spx_contract is None:
        return state.annual_vol

    try:
        bars = await ib.reqHistoricalDataAsync(
            contract=state.spx_contract,
            endDateTime='',                  # now
            durationStr=f'{lookback_days} D',
            barSizeSetting='1 day',
            whatToShow='TRADES',
            useRTH=True,
            formatDate=1,
        )
    except Exception as e:
        logger.warning(f"Vol fetch failed, keeping {state.annual_vol:.1%}: {e}")
        return state.annual_vol

    if not bars or len(bars) < 5:
        logger.warning(f"Only {len(bars) if bars else 0} daily bars — not enough for vol calc")
        return state.annual_vol

    # Daily log-returns → annualise
    closes = [b.close for b in bars]
    log_returns = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    daily_std = (sum(r ** 2 for r in log_returns) / len(log_returns)) ** 0.5  # RMS (mean≈0)
    annual = daily_std * math.sqrt(252)
    state.annual_vol = annual
    logger.info(
        f"Realised vol computed from {len(log_returns)} daily bars: "
        f"daily σ={daily_std:.4f}, annual σ={annual:.2%}"
    )
    return annual


async def fetch_historical_bars():
    """
    Fetch 1-min intraday bars for the last RTH session.
    Populates price_history and sets spx_price to the last close.
    """
    if state.spx_contract is None:
        return

    session_date = last_trading_date()
    end_dt = datetime(
        session_date.year, session_date.month, session_date.day,
        16, 30, 0,  # well after RTH close
    ).strftime("%Y%m%d-%H:%M:%S")

    logger.info(f"Fetching 1-min historical bars for {session_date.isoformat()}...")

    try:
        bars = await ib.reqHistoricalDataAsync(
            contract=state.spx_contract,
            endDateTime=end_dt,
            durationStr='1 D',
            barSizeSetting='1 min',
            whatToShow='TRADES',
            useRTH=True,
            formatDate=1,
        )
    except Exception as e:
        logger.error(f"Historical bar fetch failed: {e}")
        return

    if not bars:
        logger.warning("No historical bars returned")
        return

    # Convert bars to OHLC price_history points
    state.price_history.clear()
    for bar in bars:
        # bar.date is a datetime object for intraday bars
        bar_dt = bar.date.astimezone(ET) if bar.date.tzinfo else bar.date.replace(tzinfo=ET)
        state.price_history.append({
            "time": bar_dt.isoformat(),
            "time_short": bar_dt.strftime("%H:%M"),
            "open": round(bar.open, 2),
            "high": round(bar.high, 2),
            "low": round(bar.low, 2),
            "close": round(bar.close, 2),
        })

    # Set the reference price from the last bar's close
    last_close = bars[-1].close
    state.spx_price = last_close
    state.spx_last_close = last_close   # fixed reference for ES-derived calc
    state.historical_date = session_date.isoformat()
    state.data_mode = "historical"

    logger.info(
        f"Loaded {len(bars)} historical bars for {session_date.isoformat()}, "
        f"last close={last_close:.2f}"
    )


# ---------------------------------------------------------------------------
# Background tasks
# ---------------------------------------------------------------------------
async def price_push_loop():
    """Aggregate live ticks into 1-minute OHLC bars and push to clients."""
    current_bar = None       # {time, time_short, open, high, low, close}
    current_minute = None    # (hour, minute) of the bar being built

    while True:
        try:
            await asyncio.sleep(PRICE_PUSH_INTERVAL)

            # Only push live ticks when the market is open and streaming
            if state.live_price <= 0:
                continue

            now = now_et()
            minute_key = (now.hour, now.minute)
            price = round(state.live_price, 2)

            if minute_key != current_minute:
                # Close the previous bar (if any) and ship it
                if current_bar is not None:
                    state.price_history.append(current_bar)
                    await broadcast({"type": "bar", "data": current_bar})

                # Start a new bar
                bar_time = now.replace(second=0, microsecond=0)
                current_bar = {
                    "time": bar_time.isoformat(),
                    "time_short": bar_time.strftime("%H:%M"),
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price,
                }
                current_minute = minute_key
            else:
                # Update the current bar
                current_bar["high"] = max(current_bar["high"], price)
                current_bar["low"] = min(current_bar["low"], price)
                current_bar["close"] = price

                # Broadcast in-progress bar update (type "bar_update")
                await broadcast({"type": "bar_update", "data": current_bar})

        except asyncio.CancelledError:
            # Flush the last bar
            if current_bar is not None:
                state.price_history.append(current_bar)
            break
        except Exception as e:
            logger.error(f"Price push error: {e}")
            await asyncio.sleep(1)


async def chain_fetch_loop():
    """Periodically fetch the full option chain and compute GEX."""
    # Wait a bit for initial connection and price to settle
    await asyncio.sleep(5)

    while True:
        try:
            if not state.connected or not state.expiration:
                logger.info("Waiting for connection/expiration...")
                await asyncio.sleep(10)
                continue

            if state.spx_price <= 0:
                logger.info("No reference price yet, fetching historical bars...")
                await fetch_historical_bars()
                if state.spx_price <= 0:
                    await asyncio.sleep(30)
                    continue

            # Check if expiration needs updating (e.g. crossed 4 PM boundary)
            new_exp = find_next_expiration(state.expirations)
            if new_exp and new_exp != state.expiration:
                state.expiration = new_exp
                logger.info(f"Expiration updated to: {get_expiration_display(new_exp)}")

            # Skip chain fetch only during the daily maintenance gap (5:00–8:15 PM ET)
            if not is_cboe_options_open():
                logger.info("SPX options in daily gap (5:00–8:15 PM ET) — skipping chain fetch")
                await asyncio.sleep(CHAIN_REFRESH_SECONDS)
                continue

            state.chain_fetching = True
            mode_label = "LIVE" if state.data_mode == "live" else "HIST"
            logger.info(
                f"[{mode_label}] Starting chain fetch: exp={state.expiration}, "
                f"spot={state.spx_price:.2f}, "
                f"{len(state.strikes)} total strikes available"
            )

            # Broadcast chain fetch start (pct=0)
            await broadcast({"type": "chain_progress", "data": {
                "phase": "starting", "batch": 0, "total_batches": 1, "pct": 0
            }})

            # Progress callback — called from fetch_option_chain after each batch
            async def _on_progress(phase, batch, total_batches, pct):
                await broadcast({"type": "chain_progress", "data": {
                    "phase": phase,
                    "batch": batch,
                    "total_batches": total_batches,
                    "pct": pct,
                }})

            # Refresh annualised vol from recent daily bars
            await compute_annual_vol(lookback_days=30)

            # Fetch chain within ±8 daily std-dev of spot
            options = await fetch_option_chain(
                ib=ib,
                underlying=state.spx_contract,
                expiration=state.expiration,
                strikes=state.strikes,
                spot_price=state.spx_price,
                std_dev_range=8.0,
                annual_vol=state.annual_vol,
                progress_callback=_on_progress,
            )

            if options:
                # If OI is all zero (common for snapshot data), use volume as a proxy
                total_oi = sum(o.open_interest for o in options)
                if total_oi == 0:
                    logger.info("OI is all zeros, using volume as proxy for GEX weight")
                    for o in options:
                        o.open_interest = o.volume

                # Compute time to expiry in trading-year fractions for charm
                tte_years = 0.0
                if state.expiration:
                    try:
                        from datetime import datetime as _dt
                        exp_date = _dt.strptime(state.expiration, "%Y%m%d").date()
                        now = now_et()
                        if exp_date == now.date():
                            # 0DTE: minutes until 16:00 ET close
                            close_dt = now.replace(hour=16, minute=0, second=0, microsecond=0)
                            mins_left = max((close_dt - now).total_seconds() / 60.0, 1.0)
                            tte_years = mins_left / (390.0 * 252.0)
                        else:
                            # Multi-day: trading days × 390 min
                            days_left = (exp_date - now.date()).days
                            tte_years = max(days_left, 1) * 390.0 / (390.0 * 252.0)
                    except Exception:
                        tte_years = 0.0

                # Compute GEX
                gex_result = compute_gex(options, state.spx_price,
                                         time_to_expiry_years=tte_years)
                gex_result.expiration = state.expiration
                gex_result.timestamp = now_et().isoformat()

                state.gex_result = gex_result
                state.latest_gex = gex_result_to_dict(gex_result)
                state.last_chain_update = now_et().strftime("%H:%M:%S")

                logger.info(
                    f"GEX computed: Call Wall={gex_result.call_wall}, "
                    f"Put Wall={gex_result.put_wall}, "
                    f"Gamma Flip={gex_result.gamma_flip}, "
                    f"Max Pain={gex_result.max_pain}"
                )

                # Broadcast GEX update (inject es_derived flag)
                gex_payload = dict(state.latest_gex)
                gex_payload["es_derived"] = state.es_derived
                await broadcast({
                    "type": "gex",
                    "data": gex_payload,
                })
            else:
                logger.warning("No option data returned from chain fetch")

            # Signal chain fetch complete
            await broadcast({"type": "chain_progress", "data": {
                "phase": "done", "batch": 1, "total_batches": 1, "pct": 100
            }})
            state.chain_fetching = False

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Chain fetch error: {e}", exc_info=True)
            state.chain_fetching = False

        await asyncio.sleep(CHAIN_REFRESH_SECONDS)


async def status_push_loop():
    """Push status updates every few seconds."""
    while True:
        try:
            await asyncio.sleep(5)
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
            await broadcast(status)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Status push error: {e}")
            await asyncio.sleep(5)


async def ib_keepalive_loop():
    """Keep ib_insync event loop processing IB messages."""
    while True:
        try:
            ib.sleep(0.1)
            await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            break
        except Exception:
            await asyncio.sleep(0.5)


# ---------------------------------------------------------------------------
# FastAPI lifespan (replaces deprecated on_event)
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app):
    """Startup and shutdown logic."""
    global ib
    logger.info("Starting SPX 0DTE GEX Dashboard...")

    # Apply nest_asyncio on the CURRENT (uvicorn) loop and create IB here
    loop = asyncio.get_event_loop()
    nest_asyncio.apply(loop)
    ib = IB()

    try:
        await connect_ib()
        await setup_spx_subscription()
        await setup_chain_info()

        # Always subscribe to ES futures for off-hours derived price
        await setup_es_subscription()

        # If market is not open, seed the chart with last session's bars
        if not is_within_rth():
            await fetch_historical_bars()
            await fetch_es_baseline()
            logger.info(f"Historical mode: showing {state.historical_date}, "
                        f"ref price={state.spx_price:.2f}, "
                        f"ES baseline={state.es_at_spx_close:.2f}")

        # Register ticker update callback
        ib.pendingTickersEvent += on_pending_tickers

        # Start background loops
        state.background_tasks.append(asyncio.create_task(ib_keepalive_loop()))
        state.background_tasks.append(asyncio.create_task(price_push_loop()))
        state.background_tasks.append(asyncio.create_task(chain_fetch_loop()))
        state.background_tasks.append(asyncio.create_task(status_push_loop()))

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


# ---------------------------------------------------------------------------
# WebSocket endpoint
# ---------------------------------------------------------------------------
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
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
            }
        }
        await ws.send_text(json.dumps(init_msg))

        # Keep the connection alive, listen for client messages
        while True:
            try:
                msg = await asyncio.wait_for(ws.receive_text(), timeout=30)
                # Handle client messages (e.g., force refresh)
                if msg == "refresh_chain":
                    logger.info("Client requested chain refresh")
                    # Trigger an immediate chain fetch by just logging - the loop will handle it
            except asyncio.TimeoutError:
                # Send keepalive ping
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


# ---------------------------------------------------------------------------
# Static files (must be after routes)
# ---------------------------------------------------------------------------
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    # Create a single event loop, patch it, and run everything on it
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    nest_asyncio.apply(loop)

    config = uvicorn.Config(
        app,
        host=SERVER_HOST,
        port=SERVER_PORT,
        log_level="info",
        loop="none",  # Don't create a new loop; use ours
    )
    server = uvicorn.Server(config)
    
    # Log accessible URLs
    logger.info(f"Server starting on {SERVER_HOST}:{SERVER_PORT}")
    if SERVER_HOST == "0.0.0.0":
        logger.info(f"  Local access    : http://localhost:{SERVER_PORT}")
        logger.info(f"  Network access  : http://<your-local-ip>:{SERVER_PORT}  (e.g. http://192.168.1.100:{SERVER_PORT})")
    else:
        logger.info(f"  Access at       : http://{SERVER_HOST}:{SERVER_PORT}")
    
    loop.run_until_complete(server.serve())
