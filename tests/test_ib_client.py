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


def test_disconnect_sets_connected_false(monkeypatch):
    client = IBClient()
    client.connected = True
    disconnect_called = False
    def fake_disconnect(self):
        nonlocal disconnect_called
        disconnect_called = True
    monkeypatch.setattr(EClient, "disconnect", fake_disconnect)
    client.disconnect()
    assert not client.connected
    assert disconnect_called


# Task 3: one-shot requests
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


# Task 4: historical bars
from datetime import datetime, timedelta
from ibapi.common import BarData


@pytest.mark.asyncio
async def test_req_historical_bars_accumulates_and_converts_dates():
    client = IBClient()
    client._loop = asyncio.get_running_loop()
    c = Contract(); c.symbol = "SPX"; c.secType = "IND"
    fut = asyncio.create_task(client.req_historical_bars(c))
    await asyncio.sleep(0.01)
    req_id = next(iter(client._requests))
    summer = BarData(); summer.date = 1724176800.0; summer.open = 1.0; summer.high = 2.0
    summer.low = 0.5; summer.close = 1.5; summer.volume = 10
    winter = BarData(); winter.date = 1734176800.0; winter.open = 2.0; winter.high = 3.0
    winter.low = 1.0; winter.close = 2.5; winter.volume = 20
    client.historicalData(req_id, summer)
    client.historicalData(req_id, winter)
    client.historicalDataEnd(req_id, "", "")
    bars = await asyncio.wait_for(fut, timeout=1)
    assert len(bars) == 2
    assert isinstance(bars[0].date, datetime)
    assert bars[0].date.utcoffset() == timedelta(hours=-4)   # 2024-08-20 is EDT
    assert bars[0].close == 1.5
    assert bars[1].date.utcoffset() == timedelta(hours=-5)   # 2024-12-14 is EST
    assert bars[1].close == 2.5


# Task 5: TickStream + Greeks + market-data callbacks
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

    assert stream.model_greeks.implied_vol == 0.18
    assert stream.model_greeks.delta == 0.5
    assert stream.model_greeks.gamma == 0.003
    assert stream.model_greeks.vega == 1.2

    client.tickOptionComputation(2, 13, 0, -1.0, -1.0, -1.0, -1.0, -1.0, -1.0, -1.0, -1.0)
    # -1.0 sentinel from IB → None (not the raw -1.0, not stale 0.18)
    assert stream.model_greeks.implied_vol is None
    assert stream.model_greeks.implied_vol != -1.0


# Task 6: subscribe/unsubscribe + fetch_snapshot (first-tick completion)
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
