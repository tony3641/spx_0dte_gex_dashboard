"""
Light sanity tests for the MockIBClient rework (Task 9).

These verify the mock mirrors the real native IBClient surface: event-driven
OrderHandle placement, connect/isConnected toggling, bracket lifecycle, and
the market-data / one-shot request fakes. The full module behaviour is
exercised by the ported tests in Tasks 10-17.
"""

import pytest

from ib_client import OrderHandle, TickStream
from tests.conftest import MockContract, MockOrder, MockIBClient


def _contract():
    return MockContract(symbol="SPX", secType="OPT", strike=5200.0, right="C",
                        lastTradeDateOrContractMonth="20260410",
                        tradingClass="SPXW")


def _order(action="BUY", qty=1, lmt=3.50, parent_id=0, transmit=True):
    return MockOrder(action=action, totalQuantity=qty, orderType="LMT",
                     lmtPrice=lmt, parentId=parent_id, transmit=transmit)


# ---------------------------------------------------------------------------
# Order placement — event-driven OrderHandle
# ---------------------------------------------------------------------------

def test_place_order_fills_immediately_returns_order_handle():
    mock = MockIBClient(connected=True, fill_immediately=True)
    contract = _contract()
    order = _order()

    handle = mock.place_order(contract, order)

    assert isinstance(handle, OrderHandle)
    assert handle.order_id == 100
    assert order.orderId == 100
    assert handle.status == "Filled"
    assert handle.filled == 1
    assert handle.remaining == 0
    assert handle.avg_fill_price == 3.50
    # Filled orders resolve ack/fill/terminal immediately.
    assert handle.ack_event.is_set()
    assert handle.fill_event.is_set()
    assert handle.terminal_event.is_set()
    # Tracked in the order registries + call_log.
    assert mock.orders[100] is handle
    assert mock._orders[100] is handle
    assert mock.get_placed_orders() == [handle]
    assert mock.get_last_trade() is handle
    place = mock.call_log[-1]
    assert place["method"] == "place_order"
    assert place["status"] == "Filled"
    assert place["orderId"] == 100


@pytest.mark.asyncio
async def test_place_order_events_are_awaitable():
    mock = MockIBClient(connected=True, fill_immediately=True)
    handle = mock.place_order(_contract(), _order())

    # Waiting on an already-filled handle resolves immediately (no polling).
    await handle.ack(timeout=1)
    await handle.wait_fill(timeout=1)
    await handle.wait_terminal(timeout=1)
    assert handle.is_terminal()


def test_place_order_pending_mode_stays_submitted():
    mock = MockIBClient(connected=True, fill_immediately=False)
    handle = mock.place_order(_contract(), _order())

    assert handle.status == "Submitted"
    assert handle.filled == 0
    assert handle.remaining == 1
    # Submitted leaves PendingSubmit → ack fires (faithful to orderStatus).
    assert handle.ack_event.is_set()
    assert not handle.terminal_event.is_set()


def test_place_order_reject_mode_cancels():
    mock = MockIBClient(connected=True, reject=True)
    handle = mock.place_order(_contract(), _order())

    assert handle.status == "Cancelled"
    assert handle.filled == 0
    assert handle.remaining == 1
    # Cancelled is terminal → ack + terminal fire (faithful to orderStatus).
    assert handle.ack_event.is_set()
    assert handle.terminal_event.is_set()


def test_place_order_with_explicit_order_id_modifies_existing():
    """A place_order with an explicit order_id reuses the existing handle (a true
    modify) — no new order id is allocated and no new handle is appended."""
    mock = MockIBClient(connected=True, fill_immediately=False)
    contract = _contract()
    order = _order(lmt=3.50)
    handle = mock.place_order(contract, order)
    assert handle.order_id == 100

    order.lmtPrice = 3.60
    handle2 = mock.place_order(contract, order, order_id=handle.order_id)

    assert handle2 is handle
    assert handle.order_id == 100
    assert order.orderId == 100
    assert handle.order.lmtPrice == 3.60
    assert handle.contract is contract
    assert len(mock.get_placed_orders()) == 1
    assert mock._next_order_id == 101  # modify does not allocate a fresh id


# ---------------------------------------------------------------------------
# Connection toggling
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_connect_isConnected_toggle():
    mock = MockIBClient(connected=False)
    assert not mock.isConnected()

    await mock.connect("127.0.0.1", 7497, client_id=1)

    assert mock.isConnected()
    assert mock.connected is True
    assert mock.call_log[-1]["method"] == "connect"

    mock.disconnect()

    assert not mock.isConnected()
    assert mock.call_log[-1]["method"] == "disconnect"


# ---------------------------------------------------------------------------
# Bracket lifecycle — parent fill activates children
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bracket_child_activates_on_parent_fill():
    mock = MockIBClient(connected=True, fill_immediately=True, bracket_mode=True)
    parent = mock.place_order(_contract(), _order(qty=2))
    stop = mock.place_order(_contract(), _order(action="SELL", parent_id=parent.order_id))

    # Parent fills immediately; bracket child waits PreSubmitted.
    assert parent.status == "Filled"
    assert stop.status == "PreSubmitted"
    assert stop.filled == 0
    assert stop in mock._bracket_children[parent.order_id]
    # PreSubmitted is not a PendingSubmit status → ack already fired
    # (faithful to IBClient.orderStatus; Task 10's bracket handling accounts
    # for it by waiting for the child to leave PreSubmitted).
    assert stop.ack_event.is_set()

    # Activate the child (as IB does when the parent fills).
    mock.simulate_parent_fill(parent.order_id)

    assert parent.status == "Filled"
    assert stop.status == "Submitted"
    assert stop.ack_event.is_set()  # watch_and_push_status's ack resolves
    assert mock.call_log[-1]["method"] == "simulate_parent_fill"


def test_bracket_child_activated_event_fires_on_activation():
    """activated_event fires exactly when a bracket child leaves PreSubmitted
    (e.g. PreSubmitted -> Submitted on parent fill)."""
    mock = MockIBClient(connected=True, fill_immediately=True, bracket_mode=True)
    parent = mock.place_order(_contract(), _order(qty=1))
    stop = mock.place_order(_contract(), _order(action="SELL", parent_id=parent.order_id))

    # Still PreSubmitted → activated_event not yet fired (status_event has).
    assert stop.status == "PreSubmitted"
    assert stop.status_event.is_set()
    assert not stop.activated_event.is_set()

    mock.simulate_parent_fill(parent.order_id)

    assert stop.status == "Submitted"
    assert stop.activated_event.is_set()


@pytest.mark.asyncio
async def test_cancel_order_cascades_to_bracket_children():
    mock = MockIBClient(connected=True, fill_immediately=True, bracket_mode=True)
    parent = mock.place_order(_contract(), _order())
    stop = mock.place_order(_contract(), _order(action="SELL", parent_id=parent.order_id))

    mock.cancel_order(parent.order_id)

    assert parent.status == "Cancelled"
    assert stop.status == "Cancelled"
    assert parent.order_id in mock._cancelled_orders
    assert stop.order_id in mock._cancelled_orders
    assert stop.terminal_event.is_set()
    assert mock.call_log[-1]["method"] == "cancel_order"


def test_set_fill_status_transitions_handle_and_fires_events():
    mock = MockIBClient(connected=True, fill_immediately=False)
    handle = mock.place_order(_contract(), _order(qty=4))

    mock.set_fill_status(handle.order_id, status="Filled", filled=4, avg_price=2.25)

    assert handle.status == "Filled"
    assert handle.filled == 4
    assert handle.remaining == 0
    assert handle.avg_fill_price == 2.25
    assert handle.fill_event.is_set()
    assert handle.terminal_event.is_set()


# ---------------------------------------------------------------------------
# Market data + one-shot request fakes
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_req_contract_details_assigns_fresh_con_id_and_min_tick():
    mock = MockIBClient()
    contract = _contract()

    details = await mock.req_contract_details(contract)

    assert len(details) == 1
    cd = details[0]
    assert cd.minTick == 0.05
    assert cd.contract.conId > 0
    assert cd.contract.symbol == "SPX"
    assert cd.contract.secType == "OPT"
    assert cd.contract.strike == 5200.0
    assert mock.call_log[-1]["method"] == "req_contract_details"
    assert mock.call_log[-1]["conId"] == cd.contract.conId


@pytest.mark.asyncio
async def test_subscribe_tick_and_fetch_snapshot_use_real_tickstream():
    mock = MockIBClient()
    contract = _contract()

    stream = mock.subscribe_tick(contract, generic="101")
    assert isinstance(stream, TickStream)
    assert stream.req_id == 1
    assert stream.contract is contract
    assert not stream.received_any_tick()
    assert mock._streams[1] is stream

    # fetch_snapshot subscribes a batch, then cancels — returns immediately.
    streams = await mock.fetch_snapshot([_contract(), _contract()], generic="101", timeout=1)
    assert len(streams) == 2
    assert all(isinstance(s, TickStream) for s in streams)
    assert not any(s.received_any_tick() for s in streams)
    # The batch was cancelled; only the manual subscription (req_id 1) remains.
    assert list(mock._streams) == [1]


@pytest.mark.asyncio
async def test_one_shot_and_account_fakes_record_in_call_log():
    mock = MockIBClient()
    contract = _contract()

    chains = await mock.req_sec_def_opt_params(contract.symbol, "", "OPT", 12345)
    assert chains and chains[0].tradingClass == "SPXW"
    assert chains[0].exchange == "SMART"
    assert mock.call_log[-1]["method"] == "req_sec_def_opt_params"

    bars = await mock.req_historical_bars(contract, duration="1 D", bar_size="1 min")
    assert bars == []
    assert mock.call_log[-1]["method"] == "req_historical_bars"

    mock.req_open_orders()
    mock.req_account_updates(True, "")
    mock.req_executions()

    methods = [c["method"] for c in mock.call_log]
    assert "req_open_orders" in methods
    assert "req_account_updates" in methods
    assert "req_executions" in methods

    # Default account state shape (seeded by tests in later tasks).
    assert mock.account_values == []
    assert mock.portfolio == []
    assert mock.executions == []
    assert mock.account_dirty is False
    assert mock.on_account_dirty is None
