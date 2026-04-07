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

from ib_insync import IB, Index, Contract, Future, Option, util

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
CHAIN_REFRESH_SECONDS = int(os.getenv("CHAIN_REFRESH_SECONDS", "10"))
DASHBOARD_CHAIN_REFRESH_SECONDS = int(os.getenv("DASHBOARD_CHAIN_REFRESH_SECONDS", "300"))
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
    # Option chain data for Tab 2
    chain_data: List[OptionData] = []           # raw option data from last fetch
    chain_quotes_cache: Optional[dict] = None   # serialized chain_quotes payload
    chain_fetch_active: Optional[asyncio.Event] = None  # set when NOT fetching
    chain_stream_tickers: dict = {}             # {(strike,right): Ticker}
    chain_stream_contracts: dict = {}           # {(strike,right): Contract}
    force_chain_fetch_event: Optional[asyncio.Event] = None
    active_tab: str = "dashboard"              # "dashboard" | "chain"
    manual_refresh_requested: bool = False

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

    # During RTH, pass endDateTime="" so IB returns bars right up to the current
    # minute (a future endDateTime causes IB to lag 30-60 min behind live).
    # Outside RTH, anchor to 16:30 to ensure the full completed session is returned.
    if is_within_rth():
        end_dt = ""
    else:
        end_dt = datetime(
            session_date.year, session_date.month, session_date.day,
            16, 30, 0,
        ).strftime("%Y%m%d-%H:%M:%S")

    logger.info(f"Fetching 1-min historical bars for {session_date.isoformat()} "
                f"(endDateTime={'now' if end_dt == '' else end_dt})...")

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
    # Only set historical mode if we're not already live
    if state.data_mode != "live":
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

            # When user is on option-chain tab, pause dashboard bar generation.
            if state.active_tab == "chain":
                current_bar = None
                current_minute = None
                continue

            # Only push live ticks when the market is open and streaming
            if state.live_price <= 0:
                continue

            now = now_et()
            minute_key = (now.hour, now.minute)
            price = round(state.live_price, 2)

            if minute_key != current_minute:
                # Close the previous bar (if any) and ship it
                if current_bar is not None:
                    # Avoid duplicating a bar already loaded from historical fetch
                    if not state.price_history or state.price_history[-1]["time"] != current_bar["time"]:
                        state.price_history.append(current_bar)
                    await broadcast({"type": "bar", "data": current_bar})

                # Start a new bar — resume from last historical bar if present
                bar_time = now.replace(second=0, microsecond=0)
                bar_time_iso = bar_time.isoformat()
                # If the current minute already exists at the tail of history
                # (e.g. an in-progress bar from historical fetch), inherit its OHLC
                if state.price_history and state.price_history[-1]["time"] == bar_time_iso:
                    existing = state.price_history[-1]
                    current_bar = {
                        "time": bar_time_iso,
                        "time_short": bar_time.strftime("%H:%M"),
                        "open": existing["open"],
                        "high": max(existing["high"], price),
                        "low": min(existing["low"], price),
                        "close": price,
                    }
                else:
                    current_bar = {
                        "time": bar_time_iso,
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

    if state.force_chain_fetch_event is None:
        state.force_chain_fetch_event = asyncio.Event()

    while True:
        try:
            force_manual_refresh = state.manual_refresh_requested
            state.manual_refresh_requested = False

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

            # Pause live chain streaming to free market data lines for batch fetch
            if state.chain_fetch_active is not None:
                state.chain_fetch_active.clear()
                # Cancel all live stream subscriptions
                for key, contract in list(state.chain_stream_contracts.items()):
                    try:
                        ib.cancelMktData(contract)
                    except Exception:
                        pass
                state.chain_stream_tickers.clear()
                state.chain_stream_contracts.clear()

            mode_label = "LIVE" if state.data_mode == "live" else "HIST"
            logger.info(
                f"[{mode_label}] Starting chain fetch: exp={state.expiration}, "
                f"spot={state.spx_price:.2f}, "
                f"{len(state.strikes)} total strikes available"
            )

            # Broadcast chain fetch start (pct=0) only when dashboard is active
            if state.active_tab != "chain":
                await broadcast({"type": "chain_progress", "data": {
                    "phase": "starting", "batch": 0, "total_batches": 1, "pct": 0
                }})

            # Progress callback — called from fetch_option_chain after each batch
            async def _on_progress(phase, batch, total_batches, pct):
                if state.active_tab != "chain":
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
                force_requalify=force_manual_refresh,
                allow_unknown_retry=force_manual_refresh,
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
                if state.active_tab != "chain":
                    await broadcast({
                        "type": "gex",
                        "data": gex_payload,
                    })

                # Store raw chain data and broadcast chain_quotes for Tab 2
                state.chain_data = options
                state.chain_quotes_cache = build_chain_quotes(
                    options, state.spx_price, gex_result, state.annual_vol)
                await broadcast({
                    "type": "chain_quotes",
                    "data": state.chain_quotes_cache,
                })
            else:
                logger.warning("No option data returned from chain fetch")

            # Signal chain fetch complete
            if state.active_tab != "chain":
                await broadcast({"type": "chain_progress", "data": {
                    "phase": "done", "batch": 1, "total_batches": 1, "pct": 100
                }})
            state.chain_fetching = False

            # Resume live chain streaming
            if state.chain_fetch_active is not None:
                state.chain_fetch_active.set()

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Chain fetch error: {e}", exc_info=True)
            state.chain_fetching = False
            if state.chain_fetch_active is not None:
                state.chain_fetch_active.set()

        # Refresh cadence depends on the active tab:
        # - dashboard tab: low-frequency (OI changes slowly)
        # - option-chain tab: faster updates
        refresh_timeout = CHAIN_REFRESH_SECONDS if state.active_tab == "chain" else DASHBOARD_CHAIN_REFRESH_SECONDS

        # Refresh every refresh_timeout seconds, or immediately when manual refresh is requested.
        try:
            await asyncio.wait_for(state.force_chain_fetch_event.wait(), timeout=refresh_timeout)
            state.force_chain_fetch_event.clear()
            logger.info("Manual chain refresh triggered")
        except asyncio.TimeoutError:
            pass


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


def build_chain_quotes(options: List[OptionData], spot_price: float,
                       gex_result: Optional[GEXResult] = None,
                       annual_vol: float = 0.20) -> dict:
    """Serialize a list of OptionData into the chain_quotes payload.

    Returns a dict shaped for the 'chain_quotes' WebSocket message, with one
    row per strike (calls on left, puts on right).
    """
    calls = {}
    puts = {}
    for o in options:
        if o.right == 'C':
            calls[o.strike] = o
        else:
            puts[o.strike] = o

    all_strikes = sorted(set(list(calls.keys()) + list(puts.keys())))

    rows = []
    for s in all_strikes:
        row = {"strike": s}
        c = calls.get(s)
        p = puts.get(s)
        if c:
            row.update({
                "call_bid": c.bid, "call_ask": c.ask,
                "call_bid_size": c.bid_size, "call_ask_size": c.ask_size,
                "call_last": c.last,
                "call_delta": round(c.delta, 4) if c.delta is not None else None,
                "call_gamma": round(c.gamma, 6) if c.gamma is not None else None,
                "call_oi": c.open_interest, "call_volume": c.volume,
                "call_iv": round(c.implied_vol * 100, 2) if c.implied_vol else None,
            })
        if p:
            row.update({
                "put_bid": p.bid, "put_ask": p.ask,
                "put_bid_size": p.bid_size, "put_ask_size": p.ask_size,
                "put_last": p.last,
                "put_delta": round(p.delta, 4) if p.delta is not None else None,
                "put_gamma": round(p.gamma, 6) if p.gamma is not None else None,
                "put_oi": p.open_interest, "put_volume": p.volume,
                "put_iv": round(p.implied_vol * 100, 2) if p.implied_vol else None,
            })
        rows.append(row)

    call_wall = gex_result.call_wall if gex_result else None
    put_wall = gex_result.put_wall if gex_result else None
    gamma_flip = gex_result.gamma_flip if gex_result else None

    return {
        "strikes": rows,
        "spot_price": round(spot_price, 2),
        "annual_vol": annual_vol,
        "call_wall": call_wall,
        "put_wall": put_wall,
        "gamma_flip": gamma_flip,
        "timestamp": now_et().strftime("%H:%M:%S"),
        "timestamp_iso": now_et().isoformat(),
    }


# Number of strikes above/below ATM to stream live quotes for
CHAIN_STREAM_HALF_WIDTH = 15


async def chain_stream_loop():
    """Maintain persistent market data subscriptions for ATM ±N strikes.

    Broadcasts 'chain_tick' messages every ~1 second with updated bid/ask/
    size/volume for all actively streamed options.  Pauses subscriptions while
    the periodic GEX chain fetch is running (to stay within IB market data
    line limits).
    """
    # Initialise the event (set = safe to stream)
    state.chain_fetch_active = asyncio.Event()
    state.chain_fetch_active.set()

    await asyncio.sleep(15)  # let first chain fetch populate data

    while True:
        try:
            # Wait until chain fetch is not running
            await state.chain_fetch_active.wait()

            if not state.connected or not state.expiration or state.spx_price <= 0:
                await asyncio.sleep(5)
                continue

            if not is_cboe_options_open():
                await asyncio.sleep(30)
                continue

            # Determine the ATM strike and desired window
            spot = state.spx_price
            avail = [s for s in state.strikes if s % 5 == 0]
            if not avail:
                await asyncio.sleep(10)
                continue

            atm = min(avail, key=lambda s: abs(s - spot))
            desired = set(s for s in avail
                         if atm - CHAIN_STREAM_HALF_WIDTH * 5 <= s <= atm + CHAIN_STREAM_HALF_WIDTH * 5)

            # Keys currently subscribed
            current_keys = set(state.chain_stream_tickers.keys())
            desired_keys = set()
            for s in desired:
                desired_keys.add((s, 'C'))
                desired_keys.add((s, 'P'))

            # Cancel stale subs
            for key in current_keys - desired_keys:
                try:
                    contract = state.chain_stream_contracts.pop(key, None)
                    if contract:
                        ib.cancelMktData(contract)
                except Exception:
                    pass
                state.chain_stream_tickers.pop(key, None)

            # Subscribe new strikes
            new_keys = desired_keys - current_keys
            if new_keys:
                new_contracts = []
                for (strike, right) in new_keys:
                    c = Option(
                        symbol='SPX',
                        lastTradeDateOrContractMonth=state.expiration,
                        strike=strike, right=right,
                        exchange='SMART', multiplier='100',
                        currency='USD', tradingClass='SPXW',
                    )
                    new_contracts.append(((strike, right), c))

                # Qualify in one batch
                try:
                    raw = [c for _, c in new_contracts]
                    ib.qualifyContracts(*raw)
                    for (key, c) in new_contracts:
                        if c.conId > 0:
                            ticker = ib.reqMktData(c, genericTickList='101', snapshot=False)
                            state.chain_stream_tickers[key] = ticker
                            state.chain_stream_contracts[key] = c
                except Exception as e:
                    logger.warning(f"Chain stream subscribe error: {e}")

            # Wait for data to arrive, then broadcast a tick update
            await asyncio.sleep(1)

            ticks = []
            for (strike, right), ticker in state.chain_stream_tickers.items():
                bid = ticker.bid if ticker.bid not in (None, -1) else None
                ask = ticker.ask if ticker.ask not in (None, -1) else None
                last_val = ticker.last if ticker.last not in (None, -1) else None
                bid_sz = int(ticker.bidSize) if ticker.bidSize not in (None, -1) and not (isinstance(ticker.bidSize, float) and math.isnan(ticker.bidSize)) else 0
                ask_sz = int(ticker.askSize) if ticker.askSize not in (None, -1) and not (isinstance(ticker.askSize, float) and math.isnan(ticker.askSize)) else 0
                vol = int(ticker.volume) if ticker.volume not in (None, -1) and not (isinstance(ticker.volume, float) and math.isnan(ticker.volume)) else 0

                greeks = ticker.modelGreeks or ticker.lastGreeks
                delta = None
                gamma = None
                iv = None
                if greeks:
                    if greeks.delta is not None and not math.isnan(greeks.delta):
                        delta = round(greeks.delta, 4)
                    if greeks.gamma is not None and not math.isnan(greeks.gamma):
                        gamma = round(greeks.gamma, 6)
                    if greeks.impliedVol is not None and not math.isnan(greeks.impliedVol):
                        iv = round(greeks.impliedVol * 100, 2)

                ticks.append({
                    "strike": strike, "right": right,
                    "bid": round(bid, 2) if bid is not None else None,
                    "ask": round(ask, 2) if ask is not None else None,
                    "bid_size": bid_sz, "ask_size": ask_sz,
                    "last": round(last_val, 2) if last_val is not None else None,
                    "volume": vol,
                    "delta": delta, "gamma": gamma, "iv": iv,
                })

            if ticks:
                await broadcast({
                    "type": "chain_tick",
                    "data": {
                        "ticks": ticks,
                        "timestamp_iso": now_et().isoformat(),
                    }
                })

        except asyncio.CancelledError:
            # Clean up all streaming subs
            for key, contract in state.chain_stream_contracts.items():
                try:
                    ib.cancelMktData(contract)
                except Exception:
                    pass
            state.chain_stream_tickers.clear()
            state.chain_stream_contracts.clear()
            break
        except Exception as e:
            logger.error(f"Chain stream error: {e}")
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

        # Always seed the chart with the current/last session's intraday bars.
        # During RTH this pre-fills today's session from 9:30 AM up to now;
        # outside RTH this loads the previous session's bars as before.
        await fetch_historical_bars()

        # Only needed outside RTH (ES-derived off-hours spot price)
        if not is_within_rth():
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
        state.background_tasks.append(asyncio.create_task(chain_stream_loop()))

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
                "chain_quotes": state.chain_quotes_cache,
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
                    state.manual_refresh_requested = True
                    if state.force_chain_fetch_event is not None:
                        state.force_chain_fetch_event.set()
                elif msg == "set_tab:chain":
                    state.active_tab = "chain"
                    logger.info("Client active tab: chain")
                elif msg == "set_tab:dashboard":
                    state.active_tab = "dashboard"
                    logger.info("Client active tab: dashboard")
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
