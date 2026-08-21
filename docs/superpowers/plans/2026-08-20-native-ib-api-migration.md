# Native IB (ibapi) Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace every `ib_insync` usage in the SPX 0DTE GEX Dashboard with the native `ibapi` client, delivering event-driven order flow (place→ack→fill), first-tick snapshot completion, and socket-thread decoupling — then hard-cut `ib_insync`.

**Architecture:** A single `ib_client.py` subclasses `EWrapper` + `EClient`, runs the socket in one background thread, and bridges callbacks to asyncio via three patterns (one-shot requests → `asyncio.Future`; continuous streams → mutable `TickStream`; discrete events → `asyncio.Event`). Each existing module is reworked to consume that surface. `MockIB` in tests becomes `MockIBClient` implementing the same surface.

**Tech Stack:** Python 3.10+, native TWS API (`ibapi` from `C:\TWS API\source\pythonclient`), asyncio, FastAPI, pytest.

**Spec:** `docs/superpowers/specs/2026-08-20-native-ib-api-migration-design.md`

## Global Constraints

- Python 3.10+; Windows (asyncio `WindowsSelectorEventLoopPolicy` is set in `server.py` before any loop is created).
- `ibapi` is installed from local source (`pip install -e "C:\TWS API\source\pythonclient"`); it is **not** on PyPI under a stable name.
- **Every IB callback runs on the single socket thread; never block it** (no file I/O, no `await`, no locking beyond field writes).
- **Never poll IB for status/data.** Event-driven only: callbacks resolve asyncio `Future`s/`Event`s via `loop.call_soon_threadsafe`.
- Preserve module boundaries and the WebSocket message protocol. Business logic (GEX math, chain building, payloads) is unchanged.
- Respect the 100 concurrent market-data-line pacing: **snapshot batches capped at 50 contracts** (`min(BATCH_SIZE, 50)`).
- IB "unavailable" sentinels (`-1.0`, `1.7976931348623157e+308`) normalize to `None` via `_finite_or_none`.
- Every task ends with its tests green and a commit.

---

### Task 1: Spike — native latency probe vs ib_insync (paper)

**Files:**
- Create: `tests/spikes/spike_native_latency.py`

**Interfaces:**
- Produces: a printed latency report (connect→`nextValidId`, `reqContractDetails`, `placeOrder`→`orderStatus`, first tick) for native vs ib_insync. Throwaway code.

**Purpose:** Validate the premise before the full build. Runs in paper (TWS port 7497) and reports; nothing from it is kept.

- [ ] **Step 1: Write the native probe script**

```python
"""Throwaway latency probe: native ibapi vs ib_insync (paper, TWS 7497)."""
import sys, time, threading

sys.path.insert(0, r"C:\TWS API\source\pythonclient")
from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract
from ibapi.order import Order
from ibapi.order_cancel import OrderCancel


class Probe(EWrapper, EClient):
    def __init__(self):
        EClient.__init__(self, self)
        self.t0 = time.perf_counter()
        self.nvi_ms = None
        self.details_ms = None
        self.order_ms = None
        self.order_t0 = None
        self.order_id = 0
        self.qual_con_id = None

    def nextValidId(self, orderId):
        self.nvi_ms = (time.perf_counter() - self.t0) * 1000
        self.order_id = orderId
        print(f"[native] connect->nextValidId: {self.nvi_ms:.1f} ms")
        # qualify a far-OTM SPXW contract (100 strike, never traded)
        c = Contract(); c.symbol = "SPX"; c.secType = "OPT"; c.exchange = "SMART"
        c.currency = "USD"; c.lastTradeDateOrContractMonth = "20260918"
        c.strike = 100.0; c.right = "C"; c.multiplier = "100"; c.tradingClass = "SPXW"
        EClient.reqContractDetails(self, 1, c)

    def contractDetails(self, reqId, cd):
        self.details_ms = (time.perf_counter() - self.t0) * 1000
        self.qual_con_id = cd.contract.conId
        print(f"[native] reqContractDetails: {self.details_ms:.1f} ms (conId={self.qual_con_id})")

    def contractDetailsEnd(self, reqId):
        # place a GTC buy-limit far OTM (never fills): measure orderStatus latency, then cancel
        c = Contract(); c.conId = self.qual_con_id
        c.exchange = "SMART"; c.currency = "USD"   # conId suffices; no symbol needed
        o = Order(); o.action = "BUY"; o.totalQuantity = 1
        o.orderType = "LMT"; o.lmtPrice = 1.0; o.tif = "GTC"; o.transmit = True
        self.order_t0 = time.perf_counter()
        EClient.placeOrder(self, self.order_id, c, o)

    def orderStatus(self, orderId, status, filled, remaining, avgFillPrice, permId,
                    parentId, lastFillPrice, clientId, whyHeld, mktCapPrice):
        if status in ("Submitted", "PreSubmitted"):
            self.order_ms = (time.perf_counter() - self.order_t0) * 1000
            print(f"[native] placeOrder->orderStatus({status}): {self.order_ms:.1f} ms")
            EClient.cancelOrder(self, orderId, OrderCancel())


def main_native():
    app = Probe()
    app.connect("127.0.0.1", 7497, clientId=99)
    threading.Thread(target=app.run, daemon=True).start()
    time.sleep(8)
    app.disconnect()


if __name__ == "__main__":
    main_native()
```

- [ ] **Step 2: Add an ib_insync comparator to the same script**

Add a `main_insync()` that performs the identical steps with `ib_insync`: `IB().connectAsync("127.0.0.1", 7497, clientId=98)`, time until connected, `reqContractDetailsAsync`, and time from `placeOrder` (far-OTM GTC limit, then cancel) until `trade.orderStatus.status` leaves `PendingSubmit`. Print the same three numbers.

- [ ] **Step 3: Run it with TWS/paper open**

Run: `python tests/spikes/spike_native_latency.py`
Expected: three latency numbers print for native; then switch to `main_insync()` and print the same three for ib_insync.

- [ ] **Step 4: Record findings and stop**

Append the measured numbers to `docs/progress.md` under a "Native API spike" heading. This is a spike — do not keep the probe wired into anything.

- [ ] **Step 5: Commit**

```bash
git add tests/spikes/spike_native_latency.py docs/progress.md
git commit -m "spike: native ibapi latency probe vs ib_insync (paper)"
```

---

### Task 2: `ib_client.py` — connection lifecycle

**Files:**
- Create: `ib_client.py`
- Test: `tests/test_ib_client.py`

**Interfaces:**
- Produces: `class IBClient(EWrapper, EClient)` with:
  - `async connect(host, port, client_id, timeout=15.0) -> None`
  - `def disconnect() -> None`
  - attr `connected: bool`, `_loop`, `_next_order_id`, `_account_code`
  - EWrapper callbacks implemented: `nextValidId(orderId)`, `managedAccounts(accountsList)`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ib_client.py
import asyncio
import pytest
from unittest import mock
from ibapi.client import EClient

from ib_client import IBClient


@pytest.mark.asyncio
async def test_connect_async_waits_for_next_valid_id(monkeypatch):
    client = IBClient()
    monkeypatch.setattr(EClient, "connect", lambda self, *a, **k: True)
    monkeypatch.setattr(EClient, "run", lambda self: None)
    task = asyncio.create_task(client.connect("127.0.0.1", 7497, 1, timeout=1))
    await asyncio.sleep(0.01)
    client.nextValidId(42)            # simulate the socket-thread callback
    await asyncio.wait_for(task, timeout=2)
    assert client._next_order_id == 42
    assert client.connected


@pytest.mark.asyncio
async def test_connect_async_times_out():
    client = IBClient()
    with pytest.raises(ConnectionError):
        with mock.patch.object(EClient, "connect", return_value=True), \
             mock.patch.object(EClient, "run", lambda self: None):
            await client.connect("127.0.0.1", 7497, 1, timeout=0.05)


def test_disconnect_sets_connected_false():
    client = IBClient()
    client._connected_flag = True
    client.disconnect()
    assert not client.connected
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_ib_client.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ib_client'`

- [ ] **Step 3: Write minimal implementation**

```python
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
from typing import Callable, Dict, List, Optional

from ibapi.client import EClient
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


class IBClient(EWrapper, EClient):
    def __init__(self):
        EClient.__init__(self, self)
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_ib_client.py -v`
Expected: PASS (3 tests). Note: `disconnect()` calls `EClient.disconnect` which no-ops safely when never connected.

- [ ] **Step 5: Commit**

```bash
git add ib_client.py tests/test_ib_client.py
git commit -m "feat: IBClient bridge — connection lifecycle + nextValidId gating"
```

---

### Task 3: `ib_client.py` — one-shot requests (contract details, sec-def-opt-params)

**Files:**
- Modify: `ib_client.py`
- Test: `tests/test_ib_client.py`

**Interfaces:**
- Consumes: `_Request` from Task 2; `self._req_id`.
- Produces:
  - `async req_contract_details(contract) -> List[ContractDetails]`
  - `async req_sec_def_opt_params(symbol, fut_fop_exchange, sec_type, con_id) -> List[SecDefOptParams]`
  - `SecDefOptParams = namedtuple("SecDefOptParams", "exchange tradingClass multiplier expirations strikes")`
  - EWrapper: `contractDetails`, `contractDetailsEnd`, `securityDefinitionOptionParameter`, `securityDefinitionOptionParameterEnd`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ib_client.py  (append)
from ibapi.contract import Contract, ContractDetails


@pytest.mark.asyncio
async def test_req_contract_details_aggregates_until_end():
    client = IBClient()
    client._loop = asyncio.get_running_loop()
    c = Contract(); c.symbol = "SPX"; c.secType = "IND"
    fut = asyncio.create_task(client.req_contract_details(c))
    await asyncio.sleep(0.01)
    assert len(client._requests) == 1
    req_id = next(iter(client._requests))
    cd = ContractDetails(); cd.contract = c
    client.contractDetails(req_id, cd)      # simulate socket thread
    client.contractDetailsEnd(req_id)
    result = await asyncio.wait_for(fut, timeout=1)
    assert len(result) == 1 and result[0].contract.symbol == "SPX"


@pytest.mark.asyncio
async def test_req_sec_def_opt_params_aggregates_until_end():
    client = IBClient()
    client._loop = asyncio.get_running_loop()
    fut = asyncio.create_task(
        client.req_sec_def_opt_params("SPX", "", "IND", 123)
    )
    await asyncio.sleep(0.01)
    req_id = next(iter(client._requests))
    client.securityDefinitionOptionParameter(
        req_id, "SMART", 123, "SPXW", "100", ["20260821"], [5000.0])
    client.securityDefinitionOptionParameterEnd(req_id)
    result = await asyncio.wait_for(fut, timeout=1)
    assert result[0].tradingClass == "SPXW"
    assert result[0].expirations == ["20260821"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_ib_client.py -k "req_" -v`
Expected: FAIL — `AttributeError: 'IBClient' object has no attribute 'req_contract_details'`

- [ ] **Step 3: Write implementation**

```python
# ib_client.py — add imports and module-level record
from collections import namedtuple
from ibapi.common import BarData  # noqa: F401  (used in Task 4)

SecDefOptParams = namedtuple(
    "SecDefOptParams", "exchange tradingClass multiplier expirations strikes")


class IBClient(EWrapper, EClient):
    # ... existing __init__ ...

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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_ib_client.py -k "req_" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add ib_client.py tests/test_ib_client.py
git commit -m "feat: IBClient one-shot requests — contract details, sec-def-opt-params"
```

---

### Task 4: `ib_client.py` — historical bars

**Files:**
- Modify: `ib_client.py`
- Test: `tests/test_ib_client.py`

**Interfaces:**
- Consumes: `_Request`, `_finish_request`.
- Produces:
  - `async req_historical_bars(contract, end_date_time="", duration="1 D", bar_size="1 min", what_to_show="TRADES", use_rth=True, format_date=2) -> List[BarData]`
  - Bars are returned with `date` converted to a timezone-aware `datetime` (ET) — so callers see the same shape ib_insync produced.
  - EWrapper: `historicalData`, `historicalDataEnd`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ib_client.py  (append)
from datetime import datetime, timezone, timedelta
from ibapi.common import BarData

ET = timezone(timedelta(hours=-4))


@pytest.mark.asyncio
async def test_req_historical_bars_accumulates_and_converts_dates():
    client = IBClient()
    client._loop = asyncio.get_running_loop()
    c = Contract(); c.symbol = "SPX"; c.secType = "IND"
    fut = asyncio.create_task(client.req_historical_bars(c))
    await asyncio.sleep(0.01)
    req_id = next(iter(client._requests))
    b = BarData(); b.date = 1724176800.0; b.open = 1.0; b.high = 2.0; b.low = 0.5; b.close = 1.5; b.volume = 10
    client.historicalData(req_id, b)
    client.historicalDataEnd(req_id, "", "")
    bars = await asyncio.wait_for(fut, timeout=1)
    assert len(bars) == 1
    assert isinstance(bars[0].date, datetime)
    assert bars[0].close == 1.5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_ib_client.py -k "historical" -v`
Expected: FAIL — `'IBClient' object has no attribute 'req_historical_bars'`

- [ ] **Step 3: Write implementation**

```python
# ib_client.py — inside IBClient

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


def _coerce_bar(bar):
    """format_date=2 → bar.date is epoch seconds; return a tz-aware datetime."""
    try:
        dt = datetime.fromtimestamp(float(bar.date), tz=ET)
    except (TypeError, ValueError, OSError):
        dt = bar.date
    bar.date = dt
    return bar
```

Add `ET = timezone(timedelta(hours=-4))` at module top (imports: `from datetime import datetime, timedelta, timezone`).

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_ib_client.py -k "historical" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add ib_client.py tests/test_ib_client.py
git commit -m "feat: IBClient historical bars (accumulate until end, tz-aware dates)"
```

---

### Task 5: `ib_client.py` — `TickStream`, `Greeks`, market-data callbacks

**Files:**
- Modify: `ib_client.py`
- Test: `tests/test_ib_client.py`

**Interfaces:**
- Produces:
  - `class Greeks` — attrs `implied_vol, delta, gamma, vega, theta, opt_price, und_price`
  - `class TickStream` — attrs `req_id, contract, bid, ask, last, high, low, close, bid_size, ask_size, last_size, volume, call_oi, put_oi, open_interest, implied_volatility, bid_greeks, ask_greeks, last_greeks, model_greeks`; methods `received_any_tick()`, `has_quote()`
  - EWrapper: `tickPrice`, `tickSize`, `tickOptionComputation`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ib_client.py  (append)
from ib_client import BID, ASK, LAST, BID_SIZE, CALL_OPEN_INTEREST, TickStream


@pytest.mark.asyncio
async def test_tick_stream_maps_tick_price_and_size():
    client = IBClient()
    client._loop = asyncio.get_running_loop()
    c = Contract(); c.symbol = "SPX"; c.secType = "OPT"
    stream = client._streams[1] = TickStream(1, c)

    client.tickPrice(1, BID, 3.40, None)
    client.tickPrice(1, ASK, 3.60, None)
    client.tickPrice(1, LAST, 3.50, None)
    client.tickSize(1, BID_SIZE, 5)
    client.tickSize(1, CALL_OPEN_INTEREST, 120)

    assert stream.bid == 3.40 and stream.ask == 3.60 and stream.last == 3.50
    assert stream.bid_size == 5
    assert stream.call_oi == 120
    assert stream.has_quote() and stream.received_any_tick()


@pytest.mark.asyncio
async def test_tick_option_computation_populates_model_greeks():
    client = IBClient()
    client._loop = asyncio.get_running_loop()
    c = Contract(); c.symbol = "SPX"; c.secType = "OPT"
    stream = client._streams[2] = TickStream(2, c)

    client.tickOptionComputation(2, 13, 0, 0.18, 0.5, 3.5, 0.0, 0.003, 1.2, 0.05, 5200.0)
    client.tickOptionComputation(2, 13, 0, -1.0, -1.0, -1.0, -1.0, -1.0, -1.0, -1.0, -1.0)

    assert stream.model_greeks.implied_vol == 0.18
    assert stream.model_greeks.delta == 0.5
    assert stream.model_greeks.gamma == 0.003
    assert stream.model_greeks.vega == 1.2
    # -1.0 sentinel from IB → None
    assert stream.model_greeks.implied_vol != -1.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_ib_client.py -k "tick" -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ib_client.TickStream'` (or AttributeError)

- [ ] **Step 3: Write implementation**

```python
# ib_client.py — module-level classes (before IBClient)

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


# inside IBClient
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

    def _on_stream_tick(self, stream):
        """Hook for fetch_snapshot completion (implemented in Task 6)."""
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_ib_client.py -k "tick" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add ib_client.py tests/test_ib_client.py
git commit -m "feat: IBClient TickStream + tickPrice/tickSize/tickOptionComputation mapping"
```

---

### Task 6: `ib_client.py` — subscribe/unsubscribe + `fetch_snapshot` (first-tick completion)

**Files:**
- Modify: `ib_client.py`
- Test: `tests/test_ib_client.py`

**Interfaces:**
- Consumes: `TickStream`, `_on_stream_tick`, `_req_id`.
- Produces:
  - `def subscribe_tick(contract, generic="") -> TickStream` (assigns reqId, calls `reqMktData` with `snapshot=False`)
  - `def unsubscribe_tick(req_id) -> None` (calls `cancelMktData`, drops stream)
  - `async fetch_snapshot(contracts, generic="101", timeout=5.0) -> List[TickStream]` — subscribes a batch, completes event-driven when every stream has a first tick (or an errored contract is marked done), cancels all, returns streams.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ib_client.py  (append)
from unittest import mock
from ibapi.contract import Contract


@pytest.mark.asyncio
async def test_fetch_snapshot_returns_on_first_ticks(monkeypatch):
    client = IBClient()
    client._loop = asyncio.get_running_loop()
    monkeypatch.setattr(EClient, "reqMktData", lambda self, *a, **k: None)
    monkeypatch.setattr(EClient, "cancelMktData", lambda self, *a, **k: None)

    contracts = []
    for i in range(3):
        c = Contract(); c.symbol = "SPX"; c.secType = "OPT"; c.strike = float(i)
        contracts.append(c)

    task = asyncio.create_task(client.fetch_snapshot(contracts, timeout=5.0))
    await asyncio.sleep(0.01)
    # simulate socket thread delivering one tick per contract
    for req_id, stream in list(client._streams.items()):
        client.tickPrice(req_id, 1, 3.50, None)
    streams = await asyncio.wait_for(task, timeout=1)
    assert len(streams) == 3
    assert all(s.bid == 3.50 for s in streams)
    assert len(client._streams) == 0      # cancelled


@pytest.mark.asyncio
async def test_fetch_snapshot_falls_back_to_timeout(monkeypatch):
    client = IBClient()
    client._loop = asyncio.get_running_loop()
    monkeypatch.setattr(EClient, "reqMktData", lambda self, *a, **k: None)
    monkeypatch.setattr(EClient, "cancelMktData", lambda self, *a, **k: None)
    c = Contract(); c.symbol = "SPX"; c.secType = "OPT"
    streams = await client.fetch_snapshot([c], timeout=0.05)   # no ticks arrive
    assert len(streams) == 1
    assert not streams[0].received_any_tick()
    assert len(client._streams) == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_ib_client.py -k "snapshot" -v`
Expected: FAIL — `'IBClient' object has no attribute 'fetch_snapshot'`

- [ ] **Step 3: Write implementation**

```python
# ib_client.py — inside IBClient

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
```

Add to `IBClient.__init__`: `self._snapshot_pending: Optional[set] = None` and `self._snapshot_done: Optional[asyncio.Event] = None`.

Note: `error()` (Task 15 wires it) will also drop a reqId from `_snapshot_pending` so a dead contract never holds the batch.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_ib_client.py -k "snapshot" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add ib_client.py tests/test_ib_client.py
git commit -m "feat: IBClient subscribe/unsubscribe + first-tick snapshot completion"
```

---

### Task 7: `ib_client.py` — `OrderHandle` + place/cancel/open-orders

**Files:**
- Modify: `ib_client.py`
- Test: `tests/test_ib_client.py`

**Interfaces:**
- Consumes: `_next_order_id`, `_orders`.
- Produces:
  - `class OrderHandle` — attrs `order_id, contract, order, status, filled, remaining, avg_fill_price, last_fill_price, perm_id, parent_id`; events `ack_event, fill_event, terminal_event`; methods `async ack(timeout=5.0)`, `async wait_fill(timeout=30.0)`, `async wait_terminal(timeout=30.0)`, `is_terminal()`
  - `def place_order(contract, order) -> OrderHandle`
  - `def cancel_order(order_id) -> None`
  - `def req_open_orders() -> None`
  - EWrapper: `orderStatus`, `openOrder`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ib_client.py  (append)
from ibapi.order import Order
from ibapi.order_state import OrderState


@pytest.mark.asyncio
async def test_place_order_ack_and_fill_events(monkeypatch):
    client = IBClient()
    client._loop = asyncio.get_running_loop()
    client._next_order_id = 100
    monkeypatch.setattr(EClient, "placeOrder", lambda self, *a, **k: None)

    c = Contract(); c.symbol = "SPX"; c.secType = "OPT"
    o = Order(); o.action = "BUY"; o.totalQuantity = 1; o.orderType = "LMT"; o.lmtPrice = 3.50
    handle = client.place_order(c, o)

    ack_task = asyncio.create_task(handle.ack(timeout=1))
    await asyncio.sleep(0.01)
    client.orderStatus(100, "PendingSubmit", 0, 1, 0.0, 1, 0, 0.0, 1, "", 0.0)
    client.orderStatus(100, "Submitted", 0, 1, 0.0, 1, 0, 0.0, 1, "", 0.0)
    await asyncio.wait_for(ack_task, timeout=1)
    assert handle.status == "Submitted"
    assert handle.ack_event.is_set()

    fill_task = asyncio.create_task(handle.wait_fill(timeout=1))
    client.orderStatus(100, "Filled", 1, 0, 3.50, 1, 0, 3.50, 1, "", 0.0)
    await asyncio.wait_for(fill_task, timeout=1)
    assert handle.filled == 1 and handle.avg_fill_price == 3.50


@pytest.mark.asyncio
async def test_cancel_order(monkeypatch):
    client = IBClient()
    client._loop = asyncio.get_running_loop()
    calls = []
    monkeypatch.setattr(EClient, "cancelOrder", lambda self, oid, oc: calls.append(oid))
    client.cancel_order(55)
    assert calls == [55]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_ib_client.py -k "order" -v`
Expected: FAIL — `'IBClient' object has no attribute 'place_order'`

- [ ] **Step 3: Write implementation**

```python
# ib_client.py — module-level class
_TERMINAL_STATUSES = {"Filled", "Cancelled", "ApiCancelled", "Inactive"}
_PENDING_STATUSES = {"", "PendingSubmit", "ApiPending"}


class OrderHandle:
    def __init__(self, order_id, contract, order):
        self.order_id = order_id
        self.contract = contract
        self.order = order
        self.status = ""
        self.filled = 0.0
        self.remaining = 0.0
        self.avg_fill_price = None
        self.last_fill_price = None
        self.perm_id = None
        self.parent_id = None
        self.ack_event = asyncio.Event()
        self.fill_event = asyncio.Event()
        self.terminal_event = asyncio.Event()

    async def ack(self, timeout=5.0):
        await asyncio.wait_for(self.ack_event.wait(), timeout)

    async def wait_fill(self, timeout=30.0):
        await asyncio.wait_for(self.fill_event.wait(), timeout)

    async def wait_terminal(self, timeout=30.0):
        await asyncio.wait_for(self.terminal_event.wait(), timeout)

    def is_terminal(self):
        return self.status in _TERMINAL_STATUSES


# inside IBClient
    def place_order(self, contract, order):
        if self._next_order_id is None:
            raise RuntimeError("Order ID unavailable (not connected / no nextValidId)")
        order_id = self._next_order_id
        self._next_order_id += 1
        order.orderId = order_id
        handle = OrderHandle(order_id, contract, order)
        self._orders[order_id] = handle
        EClient.placeOrder(self, order_id, contract, order)
        return handle

    def cancel_order(self, order_id):
        from ibapi.order_cancel import OrderCancel
        try:
            EClient.cancelOrder(self, order_id, OrderCancel())
        except Exception:
            pass

    def req_open_orders(self):
        EClient.reqOpenOrders(self)

    # -- EWrapper: order status ----------------------------------------------

    def orderStatus(self, orderId, status, filled, remaining, avgFillPrice, permId,
                    parentId, lastFillPrice, clientId, whyHeld, mktCapPrice):
        handle = self._orders.get(orderId)
        if handle is None:
            return
        handle.status = status
        handle.filled = float(filled)
        handle.remaining = float(remaining)
        handle.avg_fill_price = _finite_or_none(avgFillPrice)
        handle.last_fill_price = _finite_or_none(lastFillPrice)
        handle.perm_id = permId
        handle.parent_id = parentId
        if self._loop is not None:
            if status not in _PENDING_STATUSES:
                self._loop.call_soon_threadsafe(handle.ack_event.set)
            if status == "Filled" and float(remaining) <= 0:
                self._loop.call_soon_threadsafe(handle.fill_event.set)
            if status in _TERMINAL_STATUSES:
                self._loop.call_soon_threadsafe(handle.terminal_event.set)

    def openOrder(self, orderId, contract, order, orderState):
        handle = self._orders.get(orderId)
        if handle is None:
            handle = OrderHandle(orderId, contract, order)
            self._orders[orderId] = handle
        handle.contract = contract
        handle.order = order
        handle.status = orderState.status
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_ib_client.py -k "order" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add ib_client.py tests/test_ib_client.py
git commit -m "feat: IBClient OrderHandle — event-driven place/cancel/ack/fill"
```

---

### Task 8: `ib_client.py` — account callbacks + exec records

**Files:**
- Modify: `ib_client.py`
- Test: `tests/test_ib_client.py`

**Interfaces:**
- Consumes: `_loop`, `_orders`.
- Produces module-level records:
  - `AccountValue = namedtuple("AccountValue", "tag value currency")`
  - `PortfolioItem = namedtuple("PortfolioItem", "contract position marketPrice marketValue averageCost unrealizedPNL realizedPNL account")`
  - `ExecutionRecord = namedtuple("ExecutionRecord", "contract execution commission")`
- On `IBClient`:
  - attrs `account_values: List[AccountValue]`, `portfolio: List[PortfolioItem]`, `executions: List[ExecutionRecord]`, `on_account_dirty: Optional[Callable]`, `account_dirty: bool`
  - `def req_account_updates(subscribe, account="") -> None`
  - `def req_executions(exec_filter=None) -> None`
  - EWrapper: `updateAccountValue`, `updatePortfolio`, `accountDownloadEnd`, `execDetails`, `commissionReport`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ib_client.py  (append)
from decimal import Decimal


@pytest.mark.asyncio
async def test_account_callbacks_populate_state_and_mark_dirty(monkeypatch):
    client = IBClient()
    client._loop = asyncio.get_running_loop()
    monkeypatch.setattr(EClient, "reqAccountUpdates", lambda self, *a, **k: None)
    monkeypatch.setattr(EClient, "reqExecutions", lambda self, *a, **k: None)
    dirty = []
    client.on_account_dirty = lambda: dirty.append(True)

    client.req_account_updates(True)
    client.req_executions()
    client.updateAccountValue("NetLiquidation", "100000.0", "USD", "DU123")
    c = Contract(); c.symbol = "SPX"; c.secType = "OPT"
    client.updatePortfolio(c, Decimal(1), 5200.0, 5200.0, 5000.0, 200.0, 0.0, "DU123")
    client.accountDownloadEnd("DU123")

    assert client.account_values[0].tag == "NetLiquidation"
    assert client.portfolio[0].position == 1
    assert client.account_dirty
    assert dirty == [True]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_ib_client.py -k "account" -v`
Expected: FAIL — `'IBClient' object has no attribute 'req_account_updates'`

- [ ] **Step 3: Write implementation**

```python
# ib_client.py — module-level records
AccountValue = namedtuple("AccountValue", "tag value currency")
PortfolioItem = namedtuple("PortfolioItem", "contract position marketPrice marketValue "
                                            "averageCost unrealizedPNL realizedPNL account")
ExecutionRecord = namedtuple("ExecutionRecord", "contract execution commission")


class IBClient(EWrapper, EClient):
    def __init__(self):
        # ... existing ...
        self.account_values: List[AccountValue] = []
        self.portfolio: List[PortfolioItem] = []
        self.executions: List[ExecutionRecord] = []
        self.account_dirty = False
        self.on_account_dirty: Optional[Callable] = None
        self._exec_by_id: Dict[str, ExecutionRecord] = {}

    # -- account requests -----------------------------------------------------

    def req_account_updates(self, subscribe, account=""):
        EClient.reqAccountUpdates(self, subscribe, account)

    def req_executions(self, exec_filter=None):
        if exec_filter is None:
            from ibapi.execution import ExecutionFilter
            exec_filter = ExecutionFilter()
        EClient.reqExecutions(self, next(self._req_id), exec_filter)

    def _mark_dirty(self):
        self.account_dirty = True
        if self.on_account_dirty is not None:
            self.on_account_dirty()

    # -- EWrapper: account ----------------------------------------------------

    def updateAccountValue(self, key, val, currency, accountName):
        self.account_values.append(AccountValue(key, val, currency))
        self._mark_dirty()

    def updatePortfolio(self, contract, position, marketPrice, marketValue,
                        averageCost, unrealizedPNL, realizedPNL, accountName):
        self.portfolio.append(PortfolioItem(contract, float(position), marketPrice,
                                            marketValue, averageCost, unrealizedPNL,
                                            realizedPNL, accountName))
        self._mark_dirty()

    def accountDownloadEnd(self, accountName):
        self._mark_dirty()

    def execDetails(self, reqId, contract, execution):
        rec = ExecutionRecord(contract, execution, None)
        self._exec_by_id[execution.execId] = rec
        self.executions.append(rec)
        self._mark_dirty()

    def commissionReport(self, commissionReport):
        rec = self._exec_by_id.get(commissionReport.execId)
        if rec is not None:
            self.executions[self.executions.index(rec)] = \
                ExecutionRecord(rec.contract, rec.execution, commissionReport)
        self._mark_dirty()
```

Add to `IBClient.__init__` (Task 8 replaces the placeholder `self._exec_by_id` only; also add the new attrs from the interface list).

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_ib_client.py -k "account" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add ib_client.py tests/test_ib_client.py
git commit -m "feat: IBClient account callbacks — values/portfolio/executions + dirty flag"
```

---

### Task 9: `tests/conftest.py` — `MockIBClient`

**Files:**
- Modify: `tests/conftest.py`
- Test: `tests/test_mock_ib_client.py` (light sanity, may be folded into existing tests)

**Interfaces:**
- Produces: `class MockIBClient` implementing the full `IBClient` surface for module tests, plus fixtures `mock_ib`, `mock_ib_pending`, `mock_ib_reject`, `mock_ib_disconnected`, `mock_ib_bracket`, `app_state`, `sample_legs_single`, `sample_legs_combo`, `sample_stop_loss`, `mock_ws`, `log_capture`. Keeps `MockOrderStatus`, `MockOrder`, `MockLogEntry`, `MockContract`, `MockContractDetails` (used by ported tests), and the `call_log` / `get_placed_orders` / `get_call_log_json` / `simulate_parent_fill` / `set_fill_status` helpers.

- [ ] **Step 1: Rework conftest**

Replace the `MockIB` class body with `MockIBClient` (same constructor params: `connected, fill_immediately, reject, bracket_mode`). Reuse the real `OrderHandle`, `TickStream`, `Greeks` from `ib_client.py`. Place orders return real `OrderHandle`s seeded with a status per mode; `place_order` records into `call_log` and `_placed_orders`; `cancel_order` transitions handles to `Cancelled` and cascades to bracket children; `simulate_parent_fill` fills the parent and moves children to `Submitted`. Implement `req_contract_details` (assigns conIds via `MockContract`), `req_sec_def_opt_params`, `req_historical_bars`, `subscribe_tick`/`unsubscribe_tick`/`fetch_snapshot`, `req_open_orders`, `req_account_updates`, `req_executions`, `connect`/`disconnect`, and the `account_values`/`portfolio`/`orders`/`executions`/`on_account_dirty`/`account_dirty` attrs. `isConnected()` is kept as an alias for `connected` so existing guards work.

Example of the core ordering method:

```python
def place_order(self, contract, order):
    order_id = self._next_order_id
    self._next_order_id += 1
    order.orderId = order_id
    if self._reject:
        status = "Cancelled"; filled = 0; remaining = float(order.totalQuantity)
    elif self._bracket_mode and getattr(order, "parentId", 0) != 0:
        status = "PreSubmitted"; filled = 0; remaining = float(order.totalQuantity)
    elif self._fill_immediately:
        status = "Filled"; filled = float(order.totalQuantity); remaining = 0
    else:
        status = "Submitted"; filled = 0; remaining = float(order.totalQuantity)
    handle = OrderHandle(order_id, contract, order)
    handle.status = status
    handle.filled = filled
    handle.remaining = remaining
    handle.avg_fill_price = float(order.lmtPrice or 3.50)
    if status == "Filled":
        handle.ack_event.set(); handle.fill_event.set(); handle.terminal_event.set()
    self._orders[order_id] = handle
    self._placed_orders.append(handle)
    if getattr(order, "parentId", 0) != 0:
        self._bracket_children.setdefault(order.parentId, []).append(handle)
    self.call_log.append({"method": "place_order", "orderId": order_id,
                          "action": order.action, "totalQuantity": float(order.totalQuantity),
                          "orderType": order.orderType, "lmtPrice": getattr(order, "lmtPrice", None),
                          "auxPrice": getattr(order, "auxPrice", None),
                          "transmit": order.transmit, "parentId": getattr(order, "parentId", 0),
                          "status": status})
    return handle
```

- [ ] **Step 2: Verify the fixture suite imports**

Run: `python -m pytest tests/test_market_hours.py tests/test_config.py -v`
Expected: PASS — conftest loads without breaking the untouched tests.

- [ ] **Step 3: Commit**

```bash
git add tests/conftest.py tests/test_mock_ib_client.py
git commit -m "test: rework MockIB into MockIBClient over the new bridge surface"
```

---

### Task 10: `order_manager.py` — port to `OrderHandle`

**Files:**
- Modify: `order_manager.py` (contract construction, qualification, mid-price, place/cancel, watchers)
- Test: `tests/test_order_placement.py`

**Interfaces:**
- Consumes: `ib.place_order(contract, order) -> OrderHandle`, `ib.cancel_order(order_id)`, `ib.req_open_orders()`, `ib.req_contract_details(contract)`, `ib.subscribe_tick(contract, "")` + `TickStream.has_quote()`, `handle.ack(timeout)`, `handle.wait_fill(timeout)`, `handle.wait_terminal(timeout)`, `handle.is_terminal()`, `handle.contract`, `handle.order`, `handle.avg_fill_price`, `handle.filled`, `handle.status`, `OrderHandle._TERMINAL_STATUSES` via `handle.is_terminal()`.
- Produces: `handle_place_order`, `handle_cancel_order` (same signatures as today).

- [ ] **Step 1: Port contract construction and qualification**

Replace `from ib_insync import Option, Stock, Order, Contract, ComboLeg, TagValue` with native imports and a builder:

```python
from ibapi.contract import Contract, ComboLeg
from ibapi.order import Order
from ibapi.tag_value import TagValue


def _option_contract(symbol, expiry, strike, right, exchange, trading_class="SPXW"):
    c = Contract()
    c.symbol = symbol
    c.secType = "OPT"
    c.exchange = exchange
    c.currency = "USD"
    c.lastTradeDateOrContractMonth = expiry
    c.strike = float(strike)
    c.right = right
    c.multiplier = "100"
    c.tradingClass = trading_class
    return c
```

Replace `Stock(...)` with `Contract()` + `secType = "STK"` + `exchange = "SMART"` + `currency = "USD"`.

- [ ] **Step 2: Port qualification + minTick**

Replace `await ib.qualifyContractsAsync(contract)` with:

```python
details = await ib.req_contract_details(contract)
if not details or not details[0].contract.conId:
    return {"type": "order_status", "data": {"status": "Error", "message": "Failed to qualify contract"}}
contract = details[0].contract
```

Replace `await ib.reqContractDetailsAsync(contract)` + `cdetails[0].minTick` with the same `details[0].minTick`.

- [ ] **Step 3: Port mid-price fetch (event-driven)**

Replace `_get_mid_price`'s `reqMktData` + 8×100 ms poll with a subscribe-and-await-first-quote:

```python
    async def _get_mid_price() -> Optional[float]:
        stream = ib.subscribe_tick(contract, "")
        mid = None
        try:
            for _ in range(8):
                if stream.has_quote():
                    if _valid_quote(stream.bid) and _valid_quote(stream.ask):
                        mid = (float(stream.bid) + float(stream.ask)) / 2.0
                        break
                await asyncio.sleep(0.05)
            if mid is None and _valid_quote(stream.last):
                mid = float(stream.last)
        finally:
            ib.unsubscribe_tick(stream.req_id)
        return _round_to_tick(mid) if mid is not None else None
```

- [ ] **Step 4: Port placement, ack, stop-bracket, and watchers**

- `trade = ib.placeOrder(contract, order)` → `handle = ib.place_order(contract, order)`; the `Order` object is built exactly as today (native `Order` has `orderId, action, totalQuantity (Decimal), orderType, lmtPrice, auxPrice, tif, outsideRth, transmit, parentId`).
- `state.active_trades[order.orderId] = trade` → `state.active_trades[order.orderId] = handle`.
- Replace `await_order_status(trade, ...)` + the `reqOpenOrders()` re-poll block with `await handle.ack(timeout=...)` in a `try/except asyncio.TimeoutError`, then read `handle.status` (fallback `"PendingSubmit"`).
- Dynamic-fill loop: replace `status = trade.orderStatus.status` / `remaining = trade.orderStatus.remaining` reads with `handle.status` / `handle.remaining`, and the inner wait with:

```python
try:
    await asyncio.wait_for(handle.wait_fill(timeout=reprice_interval), timeout=reprice_interval)
except asyncio.TimeoutError:
    pass
if handle.status == "Filled" or handle.remaining <= 0:
    break
```

  The `order.lmtPrice` reprice and re-`place_order` stay identical (native re-submits the modified `Order`).
- Stop-bracket: `stop_trade = ib.place_order(contract, stop_order)`; `state.active_trades[stop_order.orderId] = stop_trade`.
- `watch_and_push_status(ws, handle)` → a task: `await handle.ack(timeout)` then push the `order_status` WS message (reading `handle.status`, `handle.filled`, `handle.avg_fill_price`, `handle.order.orderId`).
- `watch_parent_and_cancel_child(ib, ws, parent, child)` → a task that `await`s `parent.wait_terminal()` then, if `parent.status != "Filled"`, calls `ib.cancel_order(child.order_id)` and pushes the child cancellation message.
- `handle_cancel_order`: lookup `state.active_trades.get(order_id)`; if absent, iterate `ib.orders.values()` for the id; `ib.cancel_order(order_id)`; `await asyncio.sleep(0.1)`; `refresh_fn(ib, state)`.

- [ ] **Step 5: Port `test_order_placement.py`**

Update the assertions that referenced ib_insync names: `ib.placeOrder` → `ib.place_order` (and the `call_log` `"method": "place_order"` key), `trade.orderStatus.status` → `handle.status`, `get_placed_orders()` returns `OrderHandle`s (`.order.orderId`, `.avg_fill_price`). The bracket/dynamic-fill/reject scenarios' expectations stay identical. Key assertions to preserve:

```python
def test_single_leg_buy_limit_places_order(mock_ib, app_state, sample_legs_single, mock_ws):
    resp = await handle_place_order(mock_ib, app_state, sample_legs_single, ws=mock_ws)
    orders = mock_ib.get_placed_orders()
    assert len(orders) == 1
    assert orders[0].order.action == "BUY"
    assert orders[0].order.lmtPrice == 3.50
    assert resp["data"]["status"] in ("Filled", "Submitted")
```

- [ ] **Step 6: Run ported tests**

Run: `python -m pytest tests/test_order_placement.py -v`
Expected: PASS (all order placement scenarios against MockIBClient)

- [ ] **Step 7: Commit**

```bash
git add order_manager.py tests/test_order_placement.py
git commit -m "refactor: order_manager on OrderHandle — event-driven place/ack/cancel/bracket"
```

---

### Task 11: `ib_connection.py` — port connection, SPX/ES subs, chain info

**Files:**
- Modify: `ib_connection.py`
- Test: `tests/test_ws_handler.py` (indirect), manual paper smoke

**Interfaces:**
- Consumes: `ib.connect(host, port, client_id, timeout)`, `ib.subscribe_tick(contract, generic)`, `ib.req_contract_details(contract)`.
- Produces: `connect_ib`, `setup_spx_subscription`, `setup_es_subscription`, `setup_chain_info`, `setup_monthly_chain_info`, `make_pending_tickers_handler` (same names as today).

- [ ] **Step 1: Replace imports and contract building**

Remove `from ib_insync import IB, Index, Future`. `connect_ib` body becomes:

```python
async def connect_ib(ib, state, host=None, port=None, client_id=None):
    h = host or IB_HOST
    p = port or IB_PORT
    cid = client_id or IB_CLIENT_ID
    try:
        await ib.connect(h, p, clientId=cid, timeout=15)
        state.connected = True
        logger.info(f"Connected to IB at {h}:{p}")
    except Exception as e:
        logger.error(f"Failed to connect to IB: {e}")
        state.connected = False
        raise
```

`setup_spx_subscription`:

```python
def _index_contract(symbol, exchange, currency):
    c = Contract(); c.symbol = symbol; c.secType = "IND"; c.exchange = exchange; c.currency = currency
    return c

async def setup_spx_subscription(ib, state):
    spx = _index_contract("SPX", "CBOE", "USD")
    state.spx_contract = spx
    stream = ib.subscribe_tick(spx, "233")
    state.spx_stream = stream
    logger.info(f"Subscribed to live SPX quotes (reqId={stream.req_id})")
```

- [ ] **Step 2: Port `make_pending_tickers_handler` to a stream reader**

Return an async polling function instead of an event callback:

```python
async def update_spx_es_prices(state):
    """Read SPX/ES TickStreams and update state (called at loop cadence)."""
    spx = getattr(state, "spx_stream", None)
    if spx is not None:
        price = spx.bid if spx.bid and spx.bid > 0 else spx.ask if spx.ask and spx.ask > 0 else spx.last
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
    es = getattr(state, "es_stream", None)
    if es is not None and es.last and es.last > 0:
        state.es_price = es.last
        if state.es_at_spx_close == 0:
            state.es_at_spx_close = es.last
            logger.info(f"ES baseline bootstrapped from first tick: {es.last:.2f}")
        if state.data_mode != "live" and state.es_at_spx_close > 0 and state.spx_last_close > 0:
            pct = (es.last - state.es_at_spx_close) / state.es_at_spx_close
            state.spx_price = round(state.spx_last_close * (1.0 + pct), 2)
            state.live_price = state.spx_price
            state.es_derived = True
```

Delete `make_pending_tickers_handler` (the old event callback). `server.py` calls `update_spx_es_prices` from `price_push_loop` (Task 17 wiring).

- [ ] **Step 3: Port ES subscription**

```python
async def setup_es_subscription(ib, state):
    try:
        es_generic = Contract(); es_generic.symbol = "ES"; es_generic.secType = "FUT"
        es_generic.exchange = "CME"; es_generic.currency = "USD"
        details = await ib.req_contract_details(es_generic)
        if not details:
            logger.warning("No ES contract details returned")
            return
        today_str = now_et().strftime("%Y%m%d")
        upcoming = [d for d in details if d.contract.lastTradeDateOrContractMonth >= today_str]
        if not upcoming:
            logger.warning("No unexpired ES contracts found")
            return
        upcoming.sort(key=lambda d: d.contract.lastTradeDateOrContractMonth)
        state.es_contract = upcoming[0].contract
        stream = ib.subscribe_tick(state.es_contract, "")
        state.es_stream = stream
        logger.info(f"Subscribed to ES futures: {state.es_contract.localSymbol}")
    except Exception as e:
        logger.warning(f"ES subscription failed: {e}")
```

`fetch_es_baseline` uses `ib.req_historical_bars(...)` with the same params (`duration="600 S"`, `bar_size="1 min"`, `what_to_show=what_to_show`, `use_rth=False`).

- [ ] **Step 4: Port chain-info setup**

`setup_chain_info` / `setup_monthly_chain_info` are unchanged except `get_chain_params`/`get_monthly_chain_params` (ported in Task 12). No code change needed here beyond what Step 1 did.

- [ ] **Step 5: Standalone bridge smoke in paper**

`server.py` is still on ib_insync until Task 17, so smoke the bridge directly. Run with TWS/paper open:

```python
# tests/spikes/smoke_bridge.py  (throwaway)
import asyncio, sys
sys.path.insert(0, r"C:\TWS API\source\pythonclient")
from ib_client import IBClient
from ib_connection import connect_ib, setup_spx_subscription
from app_state import AppState


async def main():
    ib = IBClient()
    state = AppState()
    await connect_ib(ib, state)
    await setup_spx_subscription(ib, state)
    for _ in range(20):
        s = getattr(state, "spx_stream", None)
        if s is not None and (s.bid or s.ask or s.last):
            print(f"[smoke] SPX quote bid={s.bid} ask={s.ask} last={s.last}")
            break
        await asyncio.sleep(0.5)
    ib.disconnect()


asyncio.run(main())
```

Expected: connects, `SPX quote` prints within ~10 s. (Full end-to-end is finalized in Task 17.)

- [ ] **Step 6: Commit**

```bash
git add ib_connection.py
git commit -m "refactor: ib_connection on native streams — SPX/ES subscriptions + connect"
```

---

### Task 12: `chain_fetcher.py` — port snapshot fetch + qualification + chain params

**Files:**
- Modify: `chain_fetcher.py`
- Test: `tests/test_chain_fetcher.py`

**Interfaces:**
- Consumes: `ib.req_sec_def_opt_params`, `ib.req_contract_details`, `ib.fetch_snapshot(contracts, "101", timeout)`, `TickStream`.
- Produces: `fetch_option_chain`, `get_chain_params`, `get_monthly_chain_params` (same signatures), plus `_stream_to_option_data(stream) -> Optional[OptionData]`.

- [ ] **Step 1: Port `get_chain_params`**

```python
async def get_chain_params(ib, underlying):
    chains = await ib.req_sec_def_opt_params(
        underlying.symbol, "", underlying.secType, underlying.conId)
    spxw_chain = next((ch for ch in chains
                       if ch.tradingClass == "SPXW" and ch.exchange == "SMART"), None)
    if spxw_chain is None:
        spxw_chain = next((ch for ch in chains if ch.tradingClass == "SPXW"), None)
    if spxw_chain is None:
        logger.error("No SPXW chain found!")
        return [], []
    expirations = sorted(spxw_chain.expirations)
    strikes = sorted(spxw_chain.strikes)
    return expirations, strikes
```

`get_monthly_chain_params` is identical with `tradingClass == "SPX"`.

- [ ] **Step 2: Port contract building**

Replace `Option(...)` construction with `_option_contract(symbol="SPX", expiration, strike, right, exchange="SMART", trading_class=trading_class)` (from Task 10, moved here or imported). `_contract_key` and the strike-filter/qualification-cache logic stay **unchanged**.

- [ ] **Step 3: Port qualification phase**

Replace `ib.qualifyContracts(*batch)` with:

```python
results = await asyncio.gather(*(ib.req_contract_details(c) for c in batch))
result_ok = []
for c, res in zip(batch, results):
    if res and res[0].contract.conId > 0:
        qualified_contract = res[0].contract
        result_ok.append(qualified_contract)
```

Keep the unknown-keys bookkeeping and `await asyncio.sleep(0.1)` pacing exactly as today. Keep `QUALIFY_BATCH_SIZE` batching.

- [ ] **Step 4: Port snapshot phase**

Replace `_snapshot_batch` + `_ticker_to_option_data` with:

```python
async def _snapshot_batch(ib, contracts, timeout=6.0):
    batch_cap = min(len(contracts), 50)     # 100-line pacing guard
    streams = await ib.fetch_snapshot(contracts[:batch_cap], generic="101", timeout=timeout)
    return [s for s in streams]
```

For each stream, convert with `_stream_to_option_data` (reads `bid/ask/last/bid_size/ask_size/volume/call_oi/put_oi`, and greeks from `model_greeks`→`last_greeks`→`bid_greeks`→`ask_greeks` in that priority, using the existing `_safe_float`/`_safe_int`/`_normalize_iv` helpers). The OptionData mapping is identical to `_ticker_to_option_data`; only the source object changes.

Because batches are now capped at 50, wrap the batch loop over `range(0, len(qualified), min(BATCH_SIZE, 50))`.

- [ ] **Step 5: Port `test_chain_fetcher.py`**

Update MockIB method names (`req_sec_def_opt_params`, `fetch_snapshot`, `req_contract_details`). Assertions for strike filtering, ±8σ range, cache-hit vs re-qualify, and the unknown-blacklist retry logic are preserved unchanged.

- [ ] **Step 6: Run ported tests**

Run: `python -m pytest tests/test_chain_fetcher.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add chain_fetcher.py tests/test_chain_fetcher.py
git commit -m "refactor: chain_fetcher on native — first-tick snapshots, native qualification"
```

---

### Task 13: `price_bars.py` — port historical bars + volatility

**Files:**
- Modify: `price_bars.py`
- Test: `tests/test_ws_handler.py` (indirect), manual smoke

**Interfaces:**
- Consumes: `ib.req_historical_bars(contract, end_date_time, duration, bar_size, what_to_show, use_rth)`.
- Produces: `compute_annual_vol`, `fetch_historical_bars` (same signatures).

- [ ] **Step 1: Swap the two call sites**

- `compute_annual_vol`: `ib.req_historical_bars(contract=state.spx_contract, end_date_time="", duration=f"{lookback_days} D", bar_size="1 day", what_to_show="TRADES", use_rth=True)`.
- `fetch_historical_bars`: `ib.req_historical_bars(contract=state.spx_contract, end_date_time=end_dt, duration="1 D", bar_size="1 min", what_to_show="TRADES", use_rth=True)`.

Because the bridge already converts `bar.date` to a tz-aware `datetime`, the existing `bar.date.astimezone(ET)` logic and all bar→state code are **unchanged**.

- [ ] **Step 2: Wire the SPX/ES stream reader into the price loop**

In `price_push_loop`, before reading `state.live_price`, call:

```python
from ib_connection import update_spx_es_prices
await update_spx_es_prices(state)
```

This replaces the old `pendingTickersEvent` callback as the live-price source.

- [ ] **Step 3: Standalone smoke probe**

Extend `tests/spikes/smoke_bridge.py` from Task 11 to also call `fetch_historical_bars(ib, state)` and `compute_annual_vol(ib, state)`, then assert `state.spx_price > 0` and `state.annual_vol > 0`. Expected: chart bars seed and vol computes without error. (Full-app smoke is Task 17.)

- [ ] **Step 4: Commit**

```bash
git add price_bars.py
git commit -m "refactor: price_bars on native req_historical_bars + stream-driven SPX price"
```

---

### Task 14: `chain_manager.py` — port chain stream loop

**Files:**
- Modify: `chain_manager.py`
- Test: `tests/test_ws_handler.py` (indirect), existing pure-function tests (`build_chain_quotes` unchanged)

**Interfaces:**
- Consumes: `ib.subscribe_tick`, `ib.unsubscribe_tick`, `ib.req_contract_details`, `TickStream` (`.bid/.ask/.last/.bid_size/.ask_size/.volume/.call_oi/.put_oi/.model_greeks` etc.), `chain_fetcher.fetch_option_chain`.
- Produces: `chain_stream_loop`, `monthly_gex_fetch` (same signatures).

- [ ] **Step 1: Port subscription management in `chain_stream_loop`**

- Build contracts with `_option_contract(...)` (exchange `"SMART"`, trading_class `"SPXW"`).
- Replace `result = ib.qualifyContracts(*raw)` with:

```python
results = await asyncio.gather(*(ib.req_contract_details(c) for c in raw))
for (key, _), c, res in zip(batch, raw, results):
    qc = res[0].contract if res and res[0].contract.conId > 0 else None
    if qc is None:
        state.chain_stream_unknown_keys.add(key)
        continue
    stream = ib.subscribe_tick(qc, "101")
    state.chain_stream_tickers[key] = stream
    state.chain_stream_contracts[key] = qc
```

- Replace `ib.cancelMktData(contract)` with `ib.unsubscribe_tick(stream.req_id)` for removed/expiration-changed subs, and clear the dicts.

- [ ] **Step 2: Port tick reads**

Replace `ticker.bid`/`ticker.ask`/`ticker.last`/`ticker.bidSize`/`ticker.askSize`/`ticker.volume`/`ticker.callOpenInterest`/`ticker.putOpenInterest` with `stream.bid`/`stream.ask`/`stream.last`/`stream.bid_size`/`stream.ask_size`/`stream.volume`/`stream.call_oi`/`stream.put_oi`. Replace `_extract_stream_greeks(ticker)` with a version reading `stream.model_greeks` → `stream.last_greeks` → `stream.bid_greeks` → `stream.ask_greeks` (using the same `_safe_stream_float`/`_normalize_stream_iv` helpers). `build_chain_quotes` and the WS payloads are **unchanged**.

- [ ] **Step 3: Run tests**

Run: `python -m pytest tests/test_ws_handler.py -v` (existing ws tests pass with the conftest rework). Live chain-stream behavior is verified in the Task 17 full-app smoke; there is no standalone smoke here because `chain_stream_loop` needs the full server wiring.

- [ ] **Step 4: Commit**

```bash
git add chain_manager.py
git commit -m "refactor: chain_manager on native TickStreams — subscribe/cancel/read"
```

---

### Task 15: `account_manager.py` — port to native callbacks

**Files:**
- Modify: `account_manager.py`
- Test: `tests/test_account_manager.py`

**Interfaces:**
- Consumes: `ib.req_account_updates(subscribe, account)`, `ib.req_open_orders()`, `ib.req_executions()`, `ib.on_account_dirty`, `ib.account_values: List[AccountValue]`, `ib.portfolio: List[PortfolioItem]`, `ib.orders: Dict[int, OrderHandle]`, `ib.executions: List[ExecutionRecord]`.
- Produces: `serialize_account_values`, `serialize_portfolio_item`, `serialize_order_handle`, `serialize_execution`, `parse_execution_time`, `refresh_account_state`, `build_account_payload`, `setup_account_subscription`, `account_push_loop` (same names).

- [ ] **Step 1: Remove the ib_insync import and port `parse_execution_time`**

Remove `from ib_insync.util import parseIBDatetime`. In `parse_execution_time`, delete the `parseIBDatetime` branch (lines ~119–125) and keep the regex fallback. Native `execution.time` strings like `"20260820 14:30:00"` are already handled by the fallback.

- [ ] **Step 2: Port serialization**

- `serialize_account_values(av_list)` — unchanged (iterates `.tag/.value/.currency`; `AccountValue` namedtuples provide these).
- `serialize_portfolio_item(item)` — unchanged (`PortfolioItem` namedtuple provides `.contract/.position/.marketPrice/...`).
- `serialize_trade(trade)` → `serialize_order_handle(handle)`:

```python
def serialize_order_handle(handle) -> dict:
    o = handle.order
    c = handle.contract
    contract_desc = {
        "conId": c.conId,
        "symbol": c.symbol,
        "secType": c.secType,
        "expiry": getattr(c, "lastTradeDateOrContractMonth", ""),
        "strike": float(c.strike) if getattr(c, "strike", None) else None,
        "right": getattr(c, "right", ""),
        "multiplier": getattr(c, "multiplier", ""),
        "currency": c.currency,
        "localSymbol": getattr(c, "localSymbol", ""),
    }
    return {
        "orderId": o.orderId,
        "permId": handle.perm_id or 0,
        "clientId": o.clientId if hasattr(o, "clientId") else 0,
        "action": o.action,
        "totalQty": float(o.totalQuantity),
        "orderType": o.orderType,
        "lmtPrice": _unset_or_none(o.lmtPrice),
        "auxPrice": _unset_or_none(o.auxPrice),
        "tif": o.tif,
        "status": handle.status,
        "filled": handle.filled,
        "remaining": handle.remaining,
        "avgFillPrice": handle.avg_fill_price or None,
        "contract": contract_desc,
        "lastLogMsg": "",
    }
```

Add `_unset_or_none` (mirrors the old `o.lmtPrice not in (None, 1.797...e308)` check).

- `serialize_execution(ib, exec_filter=None)`: iterate `ib.executions` instead of `ib.fills()`; each record has `.contract`, `.execution`, `.commission`. Replace `fill.commissionReport.commission` with `rec.commission.commissionAndFees if rec.commission else None`. Everything else (`ex.execId`, `ex.time`, `ex.side`, `ex.shares`, `ex.price`, `ex.orderId`) unchanged.

- [ ] **Step 3: Port `refresh_account_state` and `setup_account_subscription`**

```python
def refresh_account_state(ib, state):
    try:
        if ib.account_values:
            state.account_summary = serialize_account_values(ib.account_values)
    except Exception as e:
        logger.debug(f"account_values error: {e}")
    try:
        state.positions = [serialize_portfolio_item(p) for p in ib.portfolio]
    except Exception as e:
        logger.debug(f"portfolio error: {e}")
    try:
        trades = [h for h in ib.orders.values() if h.status not in
                  {"Filled", "Cancelled", "ApiCancelled", "Inactive"}]
        state.open_orders = [serialize_order_handle(h) for h in trades]
        state.active_trades = dict(ib.orders)
    except Exception as e:
        logger.debug(f"open_orders error: {e}")
    try:
        state.executions = serialize_execution(ib)
    except Exception as e:
        logger.debug(f"executions error: {e}")
    state.account_dirty = True


async def setup_account_subscription(ib, state):
    ib.on_account_dirty = lambda: setattr(state, "account_dirty", True)
    ib.req_account_updates(True, "")
    ib.req_open_orders()
    ib.req_executions()
    refresh_account_state(ib, state)
    logger.info("IB account subscription started")
```

`account_push_loop` is unchanged. `build_account_payload` is unchanged.

- [ ] **Step 4: Port `test_account_manager.py`**

Update to `MockIBClient` state (`mock_ib.account_values`, `mock_ib.portfolio`, `mock_ib.executions` seeded as real `AccountValue`/`PortfolioItem`/`ExecutionRecord`), and call `serialize_order_handle` in the serialization tests. Assertion expectations (keys, values, USD filtering, today-only executions) unchanged.

- [ ] **Step 5: Run ported tests**

Run: `python -m pytest tests/test_account_manager.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add account_manager.py tests/test_account_manager.py
git commit -m "refactor: account_manager on native callbacks — values/portfolio/orders/executions"
```

---

### Task 16: `ws_handler.py` — error handler + remove keepalive

**Files:**
- Modify: `ws_handler.py`
- Test: `tests/test_ws_handler.py`

**Interfaces:**
- Consumes: `IBClient.error_handler` (set by server), `OrderHandle.contract`.
- Produces: `make_ib_error_handler(state, broadcast_fn)`, `websocket_endpoint`, `broadcast`, `make_broadcast_fn`, `status_push_loop`. `ib_keepalive_loop` is **deleted**.

- [ ] **Step 1: Port the error handler**

`make_ib_error_handler` keeps its signature `(state, broadcast_fn)` and its callback signature `(req_id, error_code, error_string, contract=None, *args)`. It is now dispatched from `IBClient.error` via `call_soon_threadsafe` (see Task 16 Step 2). Update the contract-resolution branch: `state.active_trades.get(req_id)` now holds `OrderHandle`s, and `getattr(trade, "contract", None)` still works because `OrderHandle.contract` exists. No other change.

- [ ] **Step 2: Add `error()` dispatch to `ib_client.py`**

Add the `error_handler` attribute to `IBClient.__init__`, and:

```python
    def error(self, reqId, errorCode, errorString, advancedOrderRejectJson=""):
        logger.warning("IB error reqId=%s code=%s: %s", reqId, errorCode, errorString)
        if self._snapshot_pending is not None:
            self._snapshot_pending.discard(reqId)   # don't let a dead stream hold a batch
        handler = self.error_handler
        if handler is not None and self._loop is not None:
            handle = self._orders.get(reqId)
            contract = handle.contract if handle is not None else None
            self._loop.call_soon_threadsafe(
                handler, reqId, errorCode, errorString, contract)
```

- [ ] **Step 3: Delete `ib_keepalive_loop`**

Remove the function and its `server.py` registration (Task 17). If a test references it, drop that test.

- [ ] **Step 4: Update `test_ws_handler.py`**

Assert `make_ib_error_handler(...)` still serializes `ib_error` messages from a `(req_id, code, msg, contract)` call, and that `OrderHandle`-backed `active_trades` resolves the contract. Remove the keepalive test if present.

- [ ] **Step 5: Run tests**

Run: `python -m pytest tests/test_ws_handler.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add ws_handler.py tests/test_ws_handler.py ib_client.py
git commit -m "refactor: error path via native error() dispatch; remove ib keepalive loop"
```

---

### Task 17: `server.py` — wiring, reconnect, shutdown

**Files:**
- Modify: `server.py`
- Test: manual paper smoke

**Interfaces:**
- Consumes: `IBClient`, `connect_ib`, `setup_spx_subscription`, `setup_chain_info`, `setup_monthly_chain_info`, `setup_es_subscription`, `fetch_es_baseline`, `update_spx_es_prices`, `fetch_historical_bars`, `price_push_loop`, `chain_fetch_loop`, `chain_stream_loop`, `setup_account_subscription`, `account_push_loop`, `make_ib_error_handler`, `status_push_loop`.
- Produces: the FastAPI app, `/api/reconnect_ib`, `/ws`, `/api/state` (unchanged routes).

- [ ] **Step 1: Replace the client + registration**

- `from ib_insync import IB` → `from ib_client import IBClient`.
- `ib = IB()` → `ib = IBClient()`.
- In `lifespan`, after `await connect_ib(...)`, register:

```python
        ib.error_handler = make_ib_error_handler(state, broadcast_fn)
```

- Remove `ib.pendingTickersEvent += make_pending_tickers_handler(state)` and `ib.errorEvent += make_ib_error_handler(...)` (replaced by the error_handler registration above and the stream reader in `price_push_loop`).
- Remove the `state.background_tasks.append(asyncio.create_task(ib_keepalive_loop(ib)))` line.
- Add `ib.on_account_dirty` wiring is inside `setup_account_subscription` (Task 15); no change here.

- [ ] **Step 2: Port reconnect**

In `/api/reconnect_ib`, replace the per-contract `ib.cancelMktData(contract)` cleanup with stream teardown:

```python
    for req_id in list(ib._streams.keys()):
        try:
            ib.unsubscribe_tick(req_id)
        except Exception:
            pass
```

Then `ib.disconnect()`, create a **fresh** `IBClient()` (`global ib; ib = IBClient()`), `await connect_ib(ib, state, port=port)`, re-run `setup_spx_subscription`/`setup_chain_info`/`setup_monthly_chain_info`, re-register `ib.error_handler`, set the force-refresh event, and broadcast the same status payload.

- [ ] **Step 3: Port shutdown**

Replace `if ib.isConnected(): ib.disconnect()` with:

```python
    if getattr(ib, "connected", False):
        ib.disconnect()
```

- [ ] **Step 4: Full paper smoke test**

Run: `python server.py` (paper, TWS 7497). Verify: connects; SPX live/historical price; GEX snapshot computes (watch chain_progress go from `qualifying` → `fetching` → `computing` and finish quickly); option-chain tab streams; place/cancel an order from the Account tab; reconnect via `/api/reconnect_ib`; Ctrl+C shuts down cleanly.

- [ ] **Step 5: Commit**

```bash
git add server.py
git commit -m "refactor: server on IBClient — wiring, reconnect, shutdown, no keepalive"
```

---

### Task 18: `app_state.py` — OrderHandle types (cosmetic)

**Files:**
- Modify: `app_state.py`

**Interfaces:**
- Produces: `AppState.active_trades: dict` documented as `{orderId: OrderHandle}`; `AppState.spx_stream`, `AppState.es_stream` attrs for the new stream reader.

- [ ] **Step 1: Update annotations and add stream attrs**

```python
        # SPX index
        self.spx_contract = None          # Optional[Contract]
        self.spx_stream = None            # Optional[TickStream]  (native)
        ...
        self.es_contract = None           # Optional[Contract]
        self.es_stream = None             # Optional[TickStream]  (native)
        ...
        self.active_trades: dict = {}     # {orderId: OrderHandle}
```

- [ ] **Step 2: Run the fast sanity tests**

Run: `python -m pytest tests/test_config.py tests/test_market_hours.py -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add app_state.py
git commit -m "refactor: app_state stream/trade annotations for native handles"
```

---

### Task 19: Full-suite parity + paper smoke

**Files:**
- None (verification only)

- [ ] **Step 1: Run the entire suite**

Run: `python -m pytest -v`
Expected: ALL PASS (test_account_manager, test_chain_fetcher, test_config, test_ib_client, test_market_hours, test_order_placement, test_risk_free, test_ws_handler, and any others).

- [ ] **Step 2: Grep for leftover ib_insync**

Run: `grep -rn "ib_insync" --include="*.py" .`
Expected: matches only in `docs/progress.md` and `docs/superpowers/specs/...` (history), none in source or tests.

- [ ] **Step 3: Paper smoke (extended)**

Run the dashboard for a full session: verify order place/cancel from the UI, bracket stop-loss attach, dynamic-fill liquidation, chain snapshot timing (record the new refresh time vs the old ~60 s in `docs/progress.md`), and reconnect.

- [ ] **Step 4: Commit**

```bash
git add docs/progress.md
git commit -m "test: full suite green on native bridge; record snapshot latency"
```

---

### Task 20: Hard cut — remove `ib_insync`

**Files:**
- Modify: `requirements.txt`, `README.md`

- [ ] **Step 1: Update requirements**

Remove the `ib_insync>=0.9.86` line. Add:

```
-e file:///C:/TWS%20API/source/pythonclient
```

(or, equivalently, document `pip install -e "C:\TWS API\source\pythonclient"` in a setup note) with a comment that `ibapi` must be installed from the local TWS API source.

- [ ] **Step 2: Update README**

In the stack table, change the Broker API row to `native ibapi → IB TWS/Gateway (port 7497)`. Add an install note for `ibapi`. Update the "Broker API" line in the Quick Start.

- [ ] **Step 3: Final verification**

Run: `python -m pytest -v` → PASS. Run `python server.py` → boots and connects.

- [ ] **Step 4: Commit**

```bash
git add requirements.txt README.md
git commit -m "chore: hard cut from ib_insync to native ibapi"
```

---

## Self-Review Notes

- **Spec coverage:** every spec section maps to a task — connection/lifecycle (2, 11, 17), order flow (7, 10), market data streams/snapshot (5, 6, 11, 12, 14), historical/vol (4, 13), account (8, 15), errors/keepalive (16), tests (9, 19), dependency/cut (20). The Milestone-1 spike is Task 1.
- **Type consistency:** the new surface (`req_contract_details`, `req_sec_def_opt_params`, `req_historical_bars`, `subscribe_tick`, `fetch_snapshot`, `place_order`, `cancel_order`, `req_open_orders`, `req_account_updates`, `req_executions`, `account_values`, `portfolio`, `orders`, `executions`, `on_account_dirty`, `error_handler`) is defined once in Tasks 2–8 and consumed consistently by Tasks 10–17. `OrderHandle.avg_fill_price`/`.filled`/`.status`/`.is_terminal()` naming is used uniformly.
- **Snapshots:** batch cap `min(BATCH_SIZE, 50)` appears in both Task 12 and the Global Constraints.
- **config.py `BATCH_SIZE=200`:** reconciled by the Task 12 cap — no config change needed.
