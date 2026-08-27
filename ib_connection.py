"""
IB connection management and spot-price streaming.

Functions accept `ib` (IB client) and `state` (AppState) explicitly
so they can be tested without globals.

Consumes the native bridge surface from ib_client.py (IBClient, TickStream).
Native only — no legacy broker wrapper.
"""

import logging
from datetime import datetime

from ibapi.contract import Contract

from config import IB_HOST, IB_PORT, IB_CLIENT_ID
from market_hours import (
    now_et,
    is_within_rth,
    last_trading_date,
    find_next_expiration,
    get_expiration_display,
)
from chain_fetcher import (
    get_chain_params,
    get_monthly_chain_params,
    find_monthly_expiration,
)

logger = logging.getLogger(__name__)


def _index_contract(symbol, exchange, currency):
    c = Contract()
    c.symbol = symbol
    c.secType = "IND"
    c.exchange = exchange
    c.currency = currency
    return c


async def connect_ib(ib, state, host: str = None, port: int = None,
                     client_id: int = None):
    """Connect to IB TWS/Gateway."""
    h = host or IB_HOST
    p = port or IB_PORT
    cid = client_id or IB_CLIENT_ID
    try:
        await ib.connect(h, p, cid, timeout=15)
        state.connected = True
        state.ib_port = p
        logger.info(f"Connected to IB at {h}:{p}")
    except Exception as e:
        logger.error(f"Failed to connect to IB: {e}")
        state.connected = False
        raise


async def setup_spx_subscription(ib, state):
    """Build a native SPX IND contract, qualify it (get conId), and subscribe."""
    spx = _index_contract("SPX", "CBOE", "USD")
    # Qualify SPX so downstream requests (sec-def-opt-params, historical bars)
    # carry a real conId — IB rejects reqSecDefOptParams with conId=0 (code 321).
    # Mirrors the pre-migration ib.qualifyContracts(spx) call.
    try:
        details = await ib.req_contract_details(spx)
        if details:
            spx.conId = details[0].contract.conId
            logger.info(f"SPX qualified: conId={spx.conId}")
        else:
            logger.warning("No SPX contract details returned — conId stays unqualified")
    except Exception as e:
        logger.warning(f"SPX qualification failed: {e}")
    state.spx_contract = spx
    stream = ib.subscribe_tick(spx, "233")
    state.spx_stream = stream
    logger.info(f"Subscribed to live SPX quotes (reqId={stream.req_id})")


async def setup_chain_info(ib, state):
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


async def setup_monthly_chain_info(ib, state):
    """Fetch SPX monthly chain parameters and determine target monthly expiration."""
    if state.spx_contract is None:
        return
    exps, strikes = await get_monthly_chain_params(ib, state.spx_contract)
    state.monthly_expirations = exps
    state.monthly_strikes = strikes
    exp = find_monthly_expiration(exps)
    if exp:
        state.monthly_expiration = exp
        logger.info(f"Monthly expiration: {get_expiration_display(exp)}")
    else:
        logger.warning("No valid SPX monthly expiration found")


async def update_spx_es_prices(state):
    """Read SPX/ES TickStreams and update state (called at loop cadence)."""
    spx = getattr(state, "spx_stream", None)
    if spx is not None:
        price = spx.bid if spx.bid and spx.bid > 0 else \
            spx.ask if spx.ask and spx.ask > 0 else spx.last
        if price is None or price <= 0:
            # Index feeds (esp. paper) can stream close-only with no BBO — fall
            # back to the last close tick (mirrors the legacy Ticker.marketPrice
            # behaviour).
            price = spx.close
        if price is not None and price > 0:
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

    es = getattr(state, "es_stream", None)
    if es is not None and es.last and es.last > 0:
        state.es_price = es.last
        if state.es_at_spx_close == 0:
            state.es_at_spx_close = es.last
            logger.info(f"ES baseline bootstrapped from first tick: {es.last:.2f}")
        if (state.data_mode != "live"
                and state.es_at_spx_close > 0
                and state.spx_last_close > 0):
            pct = (es.last - state.es_at_spx_close) / state.es_at_spx_close
            state.spx_price = round(state.spx_last_close * (1.0 + pct), 2)
            state.live_price = state.spx_price
            state.es_derived = True


async def setup_vix_subscription(ib, state):
    """Subscribe to spot VIX (CBOE index) for the strategy's volatility condition."""
    try:
        vix = _index_contract("VIX", "CBOE", "USD")
        details = await ib.req_contract_details(vix)
        if details:
            vix.conId = details[0].contract.conId
        state.vix_stream = ib.subscribe_tick(vix, "")
        logger.info(f"Subscribed to VIX (reqId={state.vix_stream.req_id})")
    except Exception as e:
        logger.warning(f"VIX subscription failed: {e}")


def update_vix(state):
    """Read the VIX TickStream into state.vix."""
    stream = getattr(state, "vix_stream", None)
    if stream is None:
        return
    price = stream.last if stream.last and stream.last > 0 else stream.bid
    if price and price > 0:
        state.vix = float(price)


async def setup_es_subscription(ib, state):
    """Find front-month ES futures and subscribe for off-hours SPX derivation."""
    try:
        es_generic = Contract()
        es_generic.symbol = "ES"
        es_generic.secType = "FUT"
        es_generic.exchange = "CME"
        es_generic.currency = "USD"
        details = await ib.req_contract_details(es_generic)
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
        stream = ib.subscribe_tick(state.es_contract, "")
        state.es_stream = stream
        logger.info(f"Subscribed to ES futures: {state.es_contract.localSymbol} "
                    f"(expiry {state.es_contract.lastTradeDateOrContractMonth})")
    except Exception as e:
        logger.warning(f"ES subscription failed: {e}")


async def fetch_es_baseline(ib, state):
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
            bars = await ib.req_historical_bars(
                contract=state.es_contract,
                end_date_time=end_dt,
                duration="600 S",
                bar_size="1 min",
                what_to_show=what_to_show,
                use_rth=False,
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
