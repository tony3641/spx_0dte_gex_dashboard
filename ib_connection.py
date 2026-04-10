"""
IB connection management and spot-price streaming.

Functions accept `ib` (IB client) and `state` (AppState) explicitly
so they can be tested without globals.
"""

import math
import logging
from datetime import datetime

from ib_insync import IB, Index, Future

from config import IB_HOST, IB_PORT, IB_CLIENT_ID
from market_hours import now_et, is_within_rth, last_trading_date
from chain_fetcher import get_chain_params
from market_hours import find_next_expiration, get_expiration_display

logger = logging.getLogger(__name__)


async def connect_ib(ib: IB, state, host: str = None, port: int = None,
                     client_id: int = None):
    """Connect to IB TWS/Gateway."""
    h = host or IB_HOST
    p = port or IB_PORT
    cid = client_id or IB_CLIENT_ID
    try:
        await ib.connectAsync(h, p, clientId=cid, timeout=15)
        state.connected = True
        logger.info(f"Connected to IB at {h}:{p}")
    except Exception as e:
        logger.error(f"Failed to connect to IB: {e}")
        state.connected = False
        raise


async def setup_spx_subscription(ib: IB, state):
    """Qualify SPX contract and subscribe to live quotes."""
    spx = Index('SPX', 'CBOE', 'USD')
    qualified = ib.qualifyContracts(spx)
    if not qualified:
        logger.error("Failed to qualify SPX contract")
        return
    state.spx_contract = spx
    logger.info(f"SPX contract: {spx}")
    ib.reqMktData(spx, genericTickList='233', snapshot=False)
    logger.info("Subscribed to live SPX quotes")


async def setup_chain_info(ib: IB, state):
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


def make_pending_tickers_handler(state):
    """Return an on_pending_tickers callback bound to the given state."""

    def on_pending_tickers(tickers):
        for ticker in tickers:
            contract = ticker.contract
            spx_id = state.spx_contract.conId if state.spx_contract else None
            es_id = state.es_contract.conId if state.es_contract else None

            if spx_id and getattr(contract, 'conId', None) == spx_id:
                price = ticker.marketPrice()
                if price is not None and not math.isnan(price) and price > 0:
                    if is_within_rth():
                        state.spx_price = price
                        state.live_price = price
                        state.es_derived = False
                        if state.data_mode != "live":
                            state.data_mode = "live"
                            logger.info("Switched to LIVE data mode")
                    else:
                        if state.data_mode == "live":
                            state.data_mode = "historical"
                            logger.info("Exited RTH: switched to HISTORICAL mode")

            elif es_id and getattr(contract, 'conId', None) == es_id:
                price = ticker.marketPrice()
                if price is not None and not math.isnan(price) and price > 0:
                    state.es_price = price
                    if state.es_at_spx_close == 0:
                        state.es_at_spx_close = price
                        logger.info(
                            f"ES baseline bootstrapped from first tick: {price:.2f} "
                            f"(delta will accumulate from this point)"
                        )
                    if (state.data_mode != "live"
                            and state.es_at_spx_close > 0
                            and state.spx_last_close > 0):
                        pct = (price - state.es_at_spx_close) / state.es_at_spx_close
                        state.spx_price = round(state.spx_last_close * (1.0 + pct), 2)
                        state.live_price = state.spx_price
                        state.es_derived = True

    return on_pending_tickers


async def setup_es_subscription(ib: IB, state):
    """Find front-month ES futures and subscribe for off-hours SPX derivation."""
    try:
        es_generic = Future('ES', exchange='CME', currency='USD')
        details = await ib.reqContractDetailsAsync(es_generic)
        if not details:
            logger.warning("No ES contract details returned — off-hours derived price unavailable")
            return
        today_str = now_et().strftime("%Y%m%d")
        upcoming = [
            d for d in details
            if d.contract.lastTradeDateOrContractMonth >= today_str
        ]
        if not upcoming:
            logger.warning("No unexpired ES contracts found")
            return
        upcoming.sort(key=lambda d: d.contract.lastTradeDateOrContractMonth)
        state.es_contract = upcoming[0].contract
        ib.reqMktData(state.es_contract, genericTickList='', snapshot=False)
        logger.info(f"Subscribed to ES futures: {state.es_contract.localSymbol} "
                    f"(expiry {state.es_contract.lastTradeDateOrContractMonth})")
    except Exception as e:
        logger.warning(f"ES subscription failed: {e}")


async def fetch_es_baseline(ib: IB, state):
    """Fetch ES price at last SPX RTH close for off-hours delta calculation."""
    if state.es_contract is None:
        return
    session_date = last_trading_date()
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
