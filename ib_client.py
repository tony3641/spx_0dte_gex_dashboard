"""
Native IB (TWS API) client bridge.

Subclasses EWrapper + EClient, runs the socket in a background thread, and
bridges callbacks to asyncio. Three bridge patterns:
  - one-shot requests  -> asyncio.Future per reqId (_Request)
  - continuous streams -> TickStream per reqId (mutable, read by asyncio)
  - discrete events    -> asyncio.Event (order ack/fill, account dirty)
"""

import asyncio
import itertools
import logging
import threading
from collections import namedtuple
from datetime import datetime
from typing import Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

from ibapi.client import EClient
from ibapi.common import BarData  # noqa: F401  (used in Task 4)
from ibapi.wrapper import EWrapper

logger = logging.getLogger(__name__)

# Historical bar timestamps arrive as epoch seconds (format_date=2) in Eastern
# Time. DST-aware zone matching market_hours.ET (canonical project timezone).
ET = ZoneInfo("US/Eastern")

# TickType values (ibapi.ticktype.TickTypeEnum)
BID, ASK, LAST = 1, 2, 4
HIGH, LOW, CLOSE = 6, 7, 9
IMPLIED_VOL = 24
BID_SIZE, ASK_SIZE, LAST_SIZE = 0, 3, 5
VOLUME = 8
OPEN_INTEREST = 22
CALL_OPEN_INTEREST, PUT_OPEN_INTEREST = 27, 28
BID_OPT, ASK_OPT, LAST_OPT, MODEL_OPT = 10, 11, 12, 13

_UNSET = 1.7976931348623157e+308


def _finite_or_none(val):
    """Return None for -1.0 / UNSET (IB 'unavailable' sentinels)."""
    if val is None:
        return None
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    if f == -1.0 or f == _UNSET:
        return None
    return f


def _coerce_bar(bar):
    """format_date=2 → bar.date is epoch seconds; return a tz-aware datetime."""
    try:
        dt = datetime.fromtimestamp(float(bar.date), tz=ET)
    except (TypeError, ValueError, OSError):
        dt = bar.date
    bar.date = dt
    return bar


class _Request:
    """One-shot request accumulator resolved when its ...End callback fires."""

    def __init__(self, loop):
        self.future = loop.create_future()
        self.items: list = []


SecDefOptParams = namedtuple(
    "SecDefOptParams", "exchange tradingClass multiplier expirations strikes")


class Greeks:
    __slots__ = ("implied_vol", "delta", "gamma", "vega", "theta", "opt_price", "und_price")

    def __init__(self):
        self.implied_vol = self.delta = self.gamma = None
        self.vega = self.theta = self.opt_price = self.und_price = None

    def update(self, implied_vol, delta, gamma, vega, theta, opt_price, und_price):
        self.implied_vol = _finite_or_none(implied_vol)
        self.delta = _finite_or_none(delta)
        self.gamma = _finite_or_none(gamma)
        self.vega = _finite_or_none(vega)
        self.theta = _finite_or_none(theta)
        self.opt_price = _finite_or_none(opt_price)
        self.und_price = _finite_or_none(und_price)


class TickStream:
    """Per-subscription market-data state, written on the socket thread."""

    def __init__(self, req_id, contract):
        self.req_id = req_id
        self.contract = contract
        self.bid = self.ask = self.last = None
        self.high = self.low = self.close = None
        self.bid_size = self.ask_size = self.last_size = 0
        self.volume = 0
        self.call_oi = self.put_oi = self.open_interest = 0
        self.implied_volatility = None
        self.bid_greeks = Greeks()
        self.ask_greeks = Greeks()
        self.last_greeks = Greeks()
        self.model_greeks = Greeks()
        self._first_tick = False
        self._has_quote = False

    def received_any_tick(self):
        return self._first_tick

    def has_quote(self):
        return self._has_quote

    def _mark(self, has_quote):
        self._first_tick = True
        if has_quote:
            self._has_quote = True


class IBClient(EWrapper, EClient):
    def __init__(self):
        EClient.__init__(self, self)
        # ibapi 10.45 useProtoBuf() crashes on None serverVersion when unconnected
        # (`unifiedVersion <= None`). Default to 0 so one-shot request methods degrade
        # to the not-connected error path instead of raising; the connect handshake
        # overwrites this with the real negotiated version. (Task 3)
        self.serverVersion_ = 0
        self.connected = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._req_id = itertools.count(1)
        self._next_order_id: Optional[int] = None
        self._account_code: Optional[str] = None
        self._requests: Dict[int, _Request] = {}
        self._streams: Dict[int, "TickStream"] = {}
        self._snapshot_pending: Optional[set] = None
        self._snapshot_done: Optional[asyncio.Event] = None
        self._orders: Dict[int, "OrderHandle"] = {}
        self._thread: Optional[threading.Thread] = None
        self._connected_evt: Optional[asyncio.Event] = None

    # -- connection ---------------------------------------------------------

    async def connect(self, host, port, client_id, timeout=15.0):
        self._loop = asyncio.get_running_loop()
        self._connected_evt = asyncio.Event()
        EClient.connect(self, host, port, client_id)
        self._thread = threading.Thread(target=self.run, daemon=True)
        self._thread.start()
        try:
            await asyncio.wait_for(self._connected_evt.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            self.disconnect()
            raise ConnectionError(f"IB connect timed out after {timeout}s")
        self.connected = True

    def disconnect(self):
        self.connected = False
        try:
            EClient.disconnect(self)
        except Exception:
            pass

    # -- EWrapper: connection callbacks --------------------------------------

    def nextValidId(self, orderId):
        self._next_order_id = orderId
        if self._loop is not None and self._connected_evt is not None:
            self._loop.call_soon_threadsafe(self._connected_evt.set)

    def managedAccounts(self, accountsList):
        self._account_code = (accountsList or "").split(",")[0] or None

    # -- one-shot request plumbing ------------------------------------------

    def _start_request(self):
        req_id = next(self._req_id)
        req = _Request(self._loop)
        self._requests[req_id] = req
        return req_id, req

    def _finish_request(self, req_id):
        req = self._requests.pop(req_id, None)
        if req is not None and not req.future.done():
            self._loop.call_soon_threadsafe(req.future.set_result, list(req.items))

    async def req_contract_details(self, contract):
        req_id, req = self._start_request()
        EClient.reqContractDetails(self, req_id, contract)
        try:
            return await req.future
        finally:
            self._requests.pop(req_id, None)

    async def req_sec_def_opt_params(self, symbol, fut_fop_exchange, sec_type, con_id):
        req_id, req = self._start_request()
        EClient.reqSecDefOptParams(self, req_id, symbol, fut_fop_exchange, sec_type, con_id)
        try:
            return await req.future
        finally:
            self._requests.pop(req_id, None)

    async def req_historical_bars(self, contract, end_date_time="", duration="1 D",
                                  bar_size="1 min", what_to_show="TRADES",
                                  use_rth=True, format_date=2):
        req_id, req = self._start_request()
        EClient.reqHistoricalData(
            self, req_id, contract, end_date_time, duration, bar_size,
            what_to_show, use_rth, format_date, False, [])
        try:
            bars = await req.future
        finally:
            self._requests.pop(req_id, None)
        return [_coerce_bar(b) for b in bars]

    def historicalData(self, reqId, bar):
        req = self._requests.get(reqId)
        if req is not None:
            req.items.append(bar)

    def historicalDataEnd(self, reqId, start, end):
        self._finish_request(reqId)

    # -- EWrapper: contract details ------------------------------------------

    def contractDetails(self, reqId, contractDetails):
        req = self._requests.get(reqId)
        if req is not None:
            req.items.append(contractDetails)

    def contractDetailsEnd(self, reqId):
        self._finish_request(reqId)

    # -- EWrapper: sec-def-opt-params ----------------------------------------

    def securityDefinitionOptionParameter(self, reqId, exchange, underlyingConId,
                                          tradingClass, multiplier, expirations, strikes):
        req = self._requests.get(reqId)
        if req is not None:
            req.items.append(SecDefOptParams(exchange, tradingClass, multiplier,
                                             list(expirations), list(strikes)))

    def securityDefinitionOptionParameterEnd(self, reqId):
        self._finish_request(reqId)

    # -- market data callbacks (socket thread; never block) -------------------

    def _get_stream(self, req_id):
        stream = self._streams.get(req_id)
        if stream is None:
            stream = TickStream(req_id, None)
            self._streams[req_id] = stream
        return stream

    def tickPrice(self, reqId, tickType, price, attribs):
        stream = self._get_stream(reqId)
        p = _finite_or_none(price)
        if tickType == BID:
            stream.bid = p
        elif tickType == ASK:
            stream.ask = p
        elif tickType == LAST:
            stream.last = p
        elif tickType == HIGH:
            stream.high = p
        elif tickType == LOW:
            stream.low = p
        elif tickType == CLOSE:
            stream.close = p
        elif tickType == IMPLIED_VOL:
            stream.implied_volatility = p
        stream._mark(has_quote=tickType in (BID, ASK))
        self._on_stream_tick(stream)

    def tickSize(self, reqId, tickType, size):
        stream = self._get_stream(reqId)
        n = int(size) if size not in (None, -1) else 0
        if tickType == BID_SIZE:
            stream.bid_size = n
        elif tickType == ASK_SIZE:
            stream.ask_size = n
        elif tickType == LAST_SIZE:
            stream.last_size = n
        elif tickType == VOLUME:
            stream.volume = n
        elif tickType == CALL_OPEN_INTEREST:
            stream.call_oi = n
        elif tickType == PUT_OPEN_INTEREST:
            stream.put_oi = n
        elif tickType == OPEN_INTEREST:
            stream.open_interest = n
        stream._mark(has_quote=False)
        self._on_stream_tick(stream)

    def tickOptionComputation(self, reqId, tickType, tickAttrib, impliedVol, delta,
                              optPrice, pvDividend, gamma, vega, theta, undPrice):
        stream = self._get_stream(reqId)
        g = {BID_OPT: stream.bid_greeks, ASK_OPT: stream.ask_greeks,
             LAST_OPT: stream.last_greeks, MODEL_OPT: stream.model_greeks}.get(tickType)
        if g is not None:
            g.update(impliedVol, delta, gamma, vega, theta, optPrice, undPrice)
        stream._mark(has_quote=False)
        self._on_stream_tick(stream)

    # -- market data subscription -------------------------------------------

    def subscribe_tick(self, contract, generic=""):
        req_id = next(self._req_id)
        stream = TickStream(req_id, contract)
        self._streams[req_id] = stream
        EClient.reqMktData(self, req_id, contract, generic, False, False, [])
        return stream

    def unsubscribe_tick(self, req_id):
        self._streams.pop(req_id, None)
        try:
            EClient.cancelMktData(self, req_id)
        except Exception:
            pass

    # -- snapshot completion state ------------------------------------------

    def _on_stream_tick(self, stream):
        if self._snapshot_pending is not None:
            self._snapshot_pending.discard(stream.req_id)
            if not self._snapshot_pending and self._loop is not None and self._snapshot_done is not None:
                self._loop.call_soon_threadsafe(self._snapshot_done.set)

    async def fetch_snapshot(self, contracts, generic="101", timeout=5.0):
        streams = [self.subscribe_tick(c, generic) for c in contracts]
        self._snapshot_pending = {s.req_id for s in streams}
        self._snapshot_done = asyncio.Event()
        try:
            await asyncio.wait_for(self._snapshot_done.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        for s in streams:
            self.unsubscribe_tick(s.req_id)
        self._snapshot_pending = None
        self._snapshot_done = None
        return streams
