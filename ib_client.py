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
from typing import Callable, Dict, List, Optional

from ibapi.client import EClient
from ibapi.common import BarData  # noqa: F401  (used in Task 4)
from ibapi.wrapper import EWrapper

logger = logging.getLogger(__name__)

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


class _Request:
    """One-shot request accumulator resolved when its ...End callback fires."""

    def __init__(self, loop):
        self.future = loop.create_future()
        self.items: list = []


SecDefOptParams = namedtuple(
    "SecDefOptParams", "exchange tradingClass multiplier expirations strikes")


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
            req.future.set_result(list(req.items))

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
