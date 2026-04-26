"""
Order placement unit tests — highest priority test module.

Exercises single-leg, multi-leg BAG, stop-loss brackets, dynamic fill,
tick rounding, validation errors, and IB disconnected scenarios.

All tests use MockIB (from conftest) — no live IB connection required.
"""

import asyncio
import copy
import sys
import os
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from order_manager import (
    await_order_status, handle_place_order, handle_cancel_order, _PENDING_STATUSES,
    watch_and_push_status, watch_parent_and_cancel_child,
)
from config import spx_tick_for_price, round_abs_to_tick, round_signed_to_tick


# ---------------------------------------------------------------------------
# 1. Single-leg LMT order — happy path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_single_leg_lmt_order(mock_ib, app_state, sample_legs_single):
    """Verify contract qualification, tick rounding, and order params for a
    basic single-leg LMT order."""
    result = await handle_place_order(mock_ib, app_state, sample_legs_single)

    assert result["type"] == "order_status"
    data = result["data"]
    assert data["status"] == "Filled"
    assert "orderId" in data
    assert data["orderId"] > 0

    # Verify the order that was placed
    trade = mock_ib.get_last_trade()
    assert trade is not None
    assert trade.order.action == "BUY"
    assert trade.order.totalQuantity == 1
    assert trade.order.orderType == "LMT"
    # 3.50 rounded to SPX tick (0.10 since >$2) = 3.50
    assert trade.order.lmtPrice == 3.50
    assert trade.order.tif == "DAY"
    assert trade.order.transmit is True  # no stop-loss → transmit


# ---------------------------------------------------------------------------
# 2. Single-leg with stop-loss bracket
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_single_leg_with_stop_loss(mock_ib, app_state, sample_legs_single, sample_stop_loss):
    """Verify parent transmit=False, child transmit=True, parentId linkage."""
    sample_legs_single["stopLoss"] = sample_stop_loss

    result = await handle_place_order(mock_ib, app_state, sample_legs_single)

    data = result["data"]
    assert data["status"] == "Filled"

    orders = mock_ib.get_placed_orders()
    assert len(orders) >= 2, f"Expected >=2 orders (parent+stop), got {len(orders)}"

    parent = orders[0]
    stop = orders[1]

    # Parent should have transmit=False (held until child placed)
    assert parent.order.transmit is False
    assert parent.order.orderType == "LMT"
    assert parent.order.action == "BUY"

    # Stop child
    assert stop.order.orderType == "STP LMT"
    assert stop.order.transmit is True
    assert stop.order.parentId == parent.order.orderId
    assert stop.order.action == "SELL"  # opposite of parent BUY
    assert stop.order.auxPrice == 1.50  # stopPrice rounded to tick
    assert stop.order.lmtPrice == 1.40  # limitPrice rounded to tick


# ---------------------------------------------------------------------------
# 3. Multi-leg BAG combo order
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_combo_bag_order(mock_ib, app_state, sample_legs_combo):
    """Verify ComboLeg construction, combo price calculation, bag action/limit."""
    result = await handle_place_order(mock_ib, app_state, sample_legs_combo)

    data = result["data"]
    assert data["status"] == "Filled"
    assert "orderId" in data

    # The BAG order should be the last placed order
    trade = mock_ib.get_last_trade()
    assert trade is not None
    assert trade.contract.exchange == "CBOE"
    assert all(getattr(cl, "exchange", None) == "CBOE" for cl in trade.contract.comboLegs)
    assert trade.order.action == "BUY"
    assert trade.order.orderType == "LMT"
    assert getattr(trade.order, "smartComboRoutingParams", None) in (None, [])

    # Combo price: BUY 5.00 + SELL -3.00 = 2.00 net debit
    # Rounded to SPX tick: 2.00 → tick=0.05 since abs(2.00) <= 2.0
    # Actually abs(2.00) == 2.0, so tick = 0.05 (not > 2.0)
    expected_lmt = round_signed_to_tick(2.0, spx_tick_for_price(2.0))
    assert trade.order.lmtPrice == expected_lmt


# ---------------------------------------------------------------------------
# 4. BAG combo with stop-loss
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_combo_bag_with_stop_loss(mock_ib, app_state, sample_legs_combo, sample_stop_loss):
    """Verify reversed legs in close BAG and stop order params."""
    sample_legs_combo["stopLoss"] = sample_stop_loss

    result = await handle_place_order(mock_ib, app_state, sample_legs_combo)

    data = result["data"]
    assert data["status"] == "Filled"

    orders = mock_ib.get_placed_orders()
    assert len(orders) >= 2, f"Expected >=2 orders (parent BAG + stop BAG), got {len(orders)}"

    parent_bag = orders[0]
    stop_bag = orders[1]

    # Parent BAG should have transmit=False
    assert parent_bag.order.transmit is False
    assert parent_bag.order.action == "BUY"

    # Stop BAG child
    assert stop_bag.order.orderType == "STP LMT"
    assert stop_bag.order.transmit is True
    assert stop_bag.order.parentId == parent_bag.order.orderId
    assert stop_bag.order.action == "SELL"  # opposite of BUY parent


@pytest.mark.asyncio
async def test_combo_bag_preserves_outside_rth_for_spx(mock_ib, app_state, sample_legs_combo, sample_stop_loss):
    """SPX BAG orders should preserve outsideRth when routed directly to CBOE."""
    payload = copy.deepcopy(sample_legs_combo)
    payload["outsideRth"] = True
    payload["stopLoss"] = sample_stop_loss

    result = await handle_place_order(mock_ib, app_state, payload)

    data = result["data"]
    assert data["status"] == "Filled"

    orders = mock_ib.get_placed_orders()
    assert len(orders) >= 2, f"Expected >=2 orders (parent BAG + stop BAG), got {len(orders)}"

    parent_bag = orders[0]
    stop_bag = orders[1]
    assert parent_bag.contract.exchange == "CBOE"
    assert stop_bag.contract.exchange == "CBOE"
    assert parent_bag.order.outsideRth is True
    assert stop_bag.order.outsideRth is True


@pytest.mark.asyncio
async def test_combo_bag_uses_qualified_exchange_for_parent_and_stop(
    mock_ib, app_state, sample_legs_combo, sample_stop_loss
):
    """SPX BAG parent and stop should route directly to CBOE with CBOE legs."""
    payload = copy.deepcopy(sample_legs_combo)
    payload["outsideRth"] = True
    payload["stopLoss"] = sample_stop_loss

    result = await handle_place_order(mock_ib, app_state, payload)

    data = result["data"]
    assert data["status"] == "Filled"

    orders = mock_ib.get_placed_orders()
    assert len(orders) >= 2, f"Expected >=2 orders (parent BAG + stop BAG), got {len(orders)}"

    parent_bag = orders[0]
    stop_bag = orders[1]

    assert parent_bag.contract.exchange == "CBOE"
    assert stop_bag.contract.exchange == "CBOE"
    assert all(getattr(cl, "exchange", None) == "CBOE" for cl in parent_bag.contract.comboLegs)
    assert all(getattr(cl, "exchange", None) == "CBOE" for cl in stop_bag.contract.comboLegs)
    assert getattr(parent_bag.order, "smartComboRoutingParams", None) in (None, [])
    assert getattr(stop_bag.order, "smartComboRoutingParams", None) in (None, [])


# ---------------------------------------------------------------------------
# 5. Dynamic fill — reprice advances limit per interval
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dynamic_fill_reprice(mock_ib, app_state):
    """Verify reprice loop advances limit by tick_size each interval, then fills."""
    # Use mock that keeps orders pending so reprice loop runs
    mock_ib._fill_immediately = False

    payload = {
        "legs": [{
            "symbol": "SPX",
            "expiry": "20260410",
            "strike": 5200.0,
            "right": "C",
            "action": "BUY",
            "qty": 1,
        }],
        "orderType": "LMT",
        "tif": "DAY",
        "dynamicFill": True,
        "repriceIntervalSec": 0.01,  # very fast for testing
    }

    # Poll until orders exist, then wait for reprice loop to iterate, then fill
    async def fill_after_delay():
        # Wait for the first placeOrder to happen
        for _ in range(200):
            if mock_ib.get_placed_orders():
                break
            await asyncio.sleep(0.01)
        # Give reprice loop time for a few iterations
        await asyncio.sleep(0.3)
        orders = mock_ib.get_placed_orders()
        if orders:
            last = orders[-1]
            last.orderStatus.status = "Filled"
            last.orderStatus.filled = 1
            last.orderStatus.remaining = 0
            last.orderStatus.avgFillPrice = last.order.lmtPrice

    task = asyncio.create_task(fill_after_delay())
    result = await handle_place_order(mock_ib, app_state, payload)
    await task

    data = result["data"]
    # Should eventually get a status (either Filled or the last known)
    assert "orderId" in data

    # Verify repricing happened — multiple placeOrder calls
    place_calls = [c for c in mock_ib.call_log if c["method"] == "placeOrder"]
    assert len(place_calls) >= 2, (
        f"Expected multiple placeOrder calls from reprice loop, got {len(place_calls)}"
    )

    # Limit price should have increased (BUY direction = +1)
    first_lmt = place_calls[0]["lmtPrice"]
    last_lmt = place_calls[-1]["lmtPrice"]
    assert last_lmt >= first_lmt, (
        f"BUY reprice should increase: first={first_lmt}, last={last_lmt}"
    )


@pytest.mark.asyncio
async def test_dynamic_fill_handles_filled_order_before_modify(monkeypatch, mock_ib, app_state):
    """Verify dynamic fill does not crash when the order is filled before a modify."""
    mock_ib._fill_immediately = False

    payload = {
        "legs": [{
            "symbol": "SPX",
            "expiry": "20260410",
            "strike": 5200.0,
            "right": "C",
            "action": "BUY",
            "qty": 1,
        }],
        "orderType": "LMT",
        "tif": "DAY",
        "dynamicFill": True,
        "repriceIntervalSec": 0.01,
    }

    original_place = mock_ib.placeOrder
    call_count = {"count": 0}

    def place_order_wrapper(contract, order):
        call_count["count"] += 1
        if call_count["count"] == 1:
            return original_place(contract, order)
        raise AssertionError("Cannot modify a filled order.")

    monkeypatch.setattr(mock_ib, "placeOrder", place_order_wrapper)

    result = await handle_place_order(mock_ib, app_state, payload)

    assert result["type"] == "order_status"
    assert result["data"]["status"] != "Error"
    assert call_count["count"] == 2


@pytest.mark.asyncio
async def test_dynamic_fill_spx_above_two_uses_ten_cent_tick(mock_ib, app_state):
    """Dynamic fill on SPX should move in 0.10 ticks once abs(price) > 2."""
    mock_ib._fill_immediately = False

    payload = {
        "legs": [{
            "symbol": "SPX",
            "expiry": "20260410",
            "strike": 5200.0,
            "right": "C",
            "action": "BUY",
            "qty": 1,
        }],
        "orderType": "LMT",
        "tif": "DAY",
        "dynamicFill": True,
        "repriceIntervalSec": 0.01,
    }

    async def fill_after_delay():
        for _ in range(200):
            if mock_ib.get_placed_orders():
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.2)
        orders = mock_ib.get_placed_orders()
        if orders:
            last = orders[-1]
            last.orderStatus.status = "Filled"
            last.orderStatus.filled = 1
            last.orderStatus.remaining = 0
            last.orderStatus.avgFillPrice = last.order.lmtPrice

    task = asyncio.create_task(fill_after_delay())
    await handle_place_order(mock_ib, app_state, payload)
    await task

    place_calls = [c for c in mock_ib.call_log if c["method"] == "placeOrder"]
    lmts = [float(c["lmtPrice"]) for c in place_calls if c.get("lmtPrice") is not None]
    assert len(lmts) >= 2

    deltas = []
    for prev, curr in zip(lmts, lmts[1:]):
        if curr > prev:
            deltas.append(round(curr - prev, 2))
    assert deltas, "Expected at least one upward reprice increment"
    assert 0.1 in deltas, f"Expected a 0.10 increment when above $2.00, got {deltas}"


# ---------------------------------------------------------------------------
# 6. Dynamic fill cancellation — exits loop on Cancelled status
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dynamic_fill_cancellation(mock_ib, app_state):
    """Verify reprice loop exits on Cancelled status and returns error."""
    mock_ib._fill_immediately = False

    payload = {
        "legs": [{
            "symbol": "SPX",
            "expiry": "20260410",
            "strike": 5200.0,
            "right": "C",
            "action": "BUY",
            "qty": 1,
        }],
        "orderType": "LMT",
        "tif": "DAY",
        "dynamicFill": True,
        "repriceIntervalSec": 0.01,
    }

    # Poll until orders exist, then cancel
    async def cancel_after_delay():
        for _ in range(200):
            if mock_ib.get_placed_orders():
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.15)
        orders = mock_ib.get_placed_orders()
        if orders:
            last = orders[-1]
            last.orderStatus.status = "Cancelled"

    task = asyncio.create_task(cancel_after_delay())
    result = await handle_place_order(mock_ib, app_state, payload)
    await task

    data = result["data"]
    assert data["status"] == "Error"
    assert "Cancelled" in data["message"]


# ---------------------------------------------------------------------------
# 7. Missing lmtPrice → error response
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_missing_lmt_price(mock_ib, app_state):
    """Order with missing lmtPrice should return an error."""
    payload = {
        "legs": [{
            "symbol": "SPX",
            "expiry": "20260410",
            "strike": 5200.0,
            "right": "C",
            "action": "BUY",
            "qty": 1,
            # lmtPrice intentionally omitted
        }],
        "orderType": "LMT",
        "tif": "DAY",
    }

    result = await handle_place_order(mock_ib, app_state, payload)

    data = result["data"]
    assert data["status"] == "Error"
    assert "Missing lmtPrice" in data["message"]


# ---------------------------------------------------------------------------
# 8. Invalid strike → error response
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_invalid_strike(mock_ib, app_state):
    """Order with non-numeric strike should return an error."""
    payload = {
        "legs": [{
            "symbol": "SPX",
            "expiry": "20260410",
            "strike": "not_a_number",
            "right": "C",
            "action": "BUY",
            "qty": 1,
            "lmtPrice": 3.50,
        }],
        "orderType": "LMT",
        "tif": "DAY",
    }

    result = await handle_place_order(mock_ib, app_state, payload)

    data = result["data"]
    assert data["status"] == "Error"
    assert "Invalid strike" in data["message"]


# ---------------------------------------------------------------------------
# 9. IB disconnected → error response
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ib_disconnected(mock_ib_disconnected, app_state, sample_legs_single):
    """Order when IB is disconnected should return an error."""
    result = await handle_place_order(mock_ib_disconnected, app_state, sample_legs_single)

    data = result["data"]
    assert data["status"] == "Error"
    assert "Not connected" in data["message"]


# ---------------------------------------------------------------------------
# 10. No legs provided → error response
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_legs(mock_ib, app_state):
    """Order with empty legs should return error."""
    result = await handle_place_order(mock_ib, app_state, {"legs": []})

    data = result["data"]
    assert data["status"] == "Error"
    assert "No legs" in data["message"]


# ---------------------------------------------------------------------------
# 11. Tick rounding — SPX rules
# ---------------------------------------------------------------------------

def test_spx_tick_below_2():
    """Prices ≤ $2.00 use 0.05 tick."""
    assert spx_tick_for_price(1.50) == 0.05
    assert spx_tick_for_price(2.00) == 0.05
    assert spx_tick_for_price(0.50) == 0.05


def test_spx_tick_above_2():
    """Prices > $2.00 use 0.10 tick."""
    assert spx_tick_for_price(2.01) == 0.10
    assert spx_tick_for_price(5.00) == 0.10
    assert spx_tick_for_price(100.0) == 0.10


def test_round_abs_to_tick():
    """Absolute rounding to nearest tick."""
    assert round_abs_to_tick(3.53, 0.10) == 3.50
    assert round_abs_to_tick(3.56, 0.10) == 3.60
    assert round_abs_to_tick(1.93, 0.05) == 1.95
    assert round_abs_to_tick(0.03, 0.05) == 0.05  # never rounds to 0


def test_round_signed_to_tick():
    """Signed rounding preserves sign for credit/debit combos."""
    assert round_signed_to_tick(2.53, 0.10) == 2.50
    assert round_signed_to_tick(-2.53, 0.10) == -2.50
    assert round_signed_to_tick(-1.93, 0.05) == -1.95
    assert round_signed_to_tick(0.03, 0.05) == 0.05


# ---------------------------------------------------------------------------
# 12. Combo price sign logic
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_combo_credit_spread(mock_ib, app_state):
    """Credit spread: SELL higher-priced + BUY lower-priced → negative combo price.

    When comboAction='SELL' and comboLmtPrice is negative, the BAG limit
    should be negative (credit to seller).
    """
    payload = {
        "legs": [
            {
                "symbol": "SPX",
                "expiry": "20260410",
                "strike": 5200.0,
                "right": "P",
                "action": "SELL",
                "qty": 1,
                "lmtPrice": 5.00,
            },
            {
                "symbol": "SPX",
                "expiry": "20260410",
                "strike": 5190.0,
                "right": "P",
                "action": "BUY",
                "qty": 1,
                "lmtPrice": 3.00,
            },
        ],
        "orderType": "LMT",
        "tif": "DAY",
        "comboAction": "SELL",
        "comboLmtPrice": -2.00,
    }

    result = await handle_place_order(mock_ib, app_state, payload)

    data = result["data"]
    assert data["status"] == "Filled"

    trade = mock_ib.get_last_trade()
    assert trade.order.action == "SELL"
    # Negative comboLmtPrice → round_signed_to_tick preserves sign
    assert trade.order.lmtPrice == round_signed_to_tick(-2.0, spx_tick_for_price(-2.0))


# ---------------------------------------------------------------------------
# 13. Cancel order — happy path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cancel_order(mock_ib, app_state, sample_legs_single):
    """Place an order, then cancel it."""
    # Place first
    result = await handle_place_order(mock_ib, app_state, sample_legs_single)
    order_id = result["data"]["orderId"]

    # Cancel
    cancel_result = await handle_cancel_order(mock_ib, app_state, order_id)

    data = cancel_result["data"]
    assert data["status"] == "Cancelled"
    assert data["orderId"] == order_id
    assert order_id in mock_ib._cancelled_orders


# ---------------------------------------------------------------------------
# 14. Cancel non-existent order → error
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cancel_nonexistent_order(mock_ib, app_state):
    """Cancelling an order that doesn't exist should return error."""
    result = await handle_cancel_order(mock_ib, app_state, 99999)

    data = result["data"]
    assert data["status"] == "Error"
    assert "not found" in data["message"]


# ---------------------------------------------------------------------------
# 15. Unsupported secType → error
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unsupported_sec_type(mock_ib, app_state):
    """Order with unsupported secType should return error."""
    payload = {
        "legs": [{
            "symbol": "SPX",
            "expiry": "20260410",
            "strike": 5200.0,
            "right": "C",
            "action": "BUY",
            "qty": 1,
            "lmtPrice": 3.50,
            "secType": "FUT",
        }],
        "orderType": "LMT",
        "tif": "DAY",
    }

    result = await handle_place_order(mock_ib, app_state, payload)

    data = result["data"]
    assert data["status"] == "Error"
    assert "Unsupported secType" in data["message"]


# ---------------------------------------------------------------------------
# 16. Single-leg SELL order tick rounding
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sell_order_tick_rounding(mock_ib, app_state):
    """SELL order with price needing SPX tick rounding."""
    payload = {
        "legs": [{
            "symbol": "SPX",
            "expiry": "20260410",
            "strike": 5200.0,
            "right": "P",
            "action": "SELL",
            "qty": 2,
            "lmtPrice": 1.73,  # should round to 1.75 (0.05 tick, < $2)
        }],
        "orderType": "LMT",
        "tif": "DAY",
    }

    result = await handle_place_order(mock_ib, app_state, payload)

    data = result["data"]
    assert data["status"] == "Filled"

    trade = mock_ib.get_last_trade()
    assert trade.order.action == "SELL"
    assert trade.order.totalQuantity == 2
    # 1.73 → abs → round_abs_to_tick(1.73, 0.05) = 1.75
    assert trade.order.lmtPrice == 1.75


# ---------------------------------------------------------------------------
# 17. outsideRth flag propagation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_outside_rth_flag(mock_ib, app_state):
    """Verify outsideRth flag passes through to the order."""
    payload = {
        "legs": [{
            "symbol": "SPX",
            "expiry": "20260410",
            "strike": 5200.0,
            "right": "C",
            "action": "BUY",
            "qty": 1,
            "lmtPrice": 3.50,
        }],
        "orderType": "LMT",
        "tif": "DAY",
        "outsideRth": True,
    }

    result = await handle_place_order(mock_ib, app_state, payload)
    assert result["data"]["status"] == "Filled"

    trade = mock_ib.get_last_trade()
    assert trade.order.outsideRth is True


# ===========================================================================
# STOP LIMIT ORDER LIFECYCLE TESTS
# ===========================================================================

# ---------------------------------------------------------------------------
# 18. Bracket child starts as PreSubmitted, transitions on parent fill
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stop_order_stays_presubmitted_until_parent_fills(mock_ib_bracket, app_state, sample_stop_loss):
    """Stop order should start as PreSubmitted. After parent fills,
    stop should transition to Submitted."""
    payload = {
        "legs": [{
            "symbol": "SPX",
            "expiry": "20260410",
            "strike": 5200.0,
            "right": "C",
            "action": "BUY",
            "qty": 1,
            "lmtPrice": 3.50,
        }],
        "orderType": "LMT",
        "tif": "DAY",
        "stopLoss": sample_stop_loss,
    }

    result = await handle_place_order(mock_ib_bracket, app_state, payload)

    orders = mock_ib_bracket.get_placed_orders()
    assert len(orders) >= 2
    parent = orders[0]
    stop = orders[1]

    # Parent fills immediately in bracket_mode
    assert parent.orderStatus.status == "Filled"
    # Stop should be PreSubmitted (bracket child)
    assert stop.orderStatus.status == "PreSubmitted"
    assert stop.order.parentId == parent.order.orderId

    # Simulate IB fill propagation
    mock_ib_bracket.simulate_parent_fill(parent.order.orderId)
    assert stop.orderStatus.status == "Submitted"


# ---------------------------------------------------------------------------
# 19. Stop order cancelled when parent is cancelled
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stop_order_cancelled_when_parent_cancelled(mock_ib_bracket, app_state, sample_stop_loss):
    """When the parent order is cancelled, the stop child must also be cancelled
    (IB cascade behavior)."""
    payload = {
        "legs": [{
            "symbol": "SPX",
            "expiry": "20260410",
            "strike": 5200.0,
            "right": "C",
            "action": "BUY",
            "qty": 1,
            "lmtPrice": 3.50,
        }],
        "orderType": "LMT",
        "tif": "DAY",
        "stopLoss": sample_stop_loss,
    }

    result = await handle_place_order(mock_ib_bracket, app_state, payload)
    orders = mock_ib_bracket.get_placed_orders()
    parent = orders[0]
    stop = orders[1]

    assert stop.orderStatus.status == "PreSubmitted"

    # Cancel the parent — MockIB now cascades to children
    mock_ib_bracket.cancelOrder(parent.order)
    assert parent.orderStatus.status == "Cancelled"
    assert stop.orderStatus.status == "Cancelled"
    assert stop.order.orderId in mock_ib_bracket._cancelled_orders


# ---------------------------------------------------------------------------
# 20. Stop action reversal for SELL parent
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stop_order_action_reversal_sell_parent(mock_ib, app_state, sample_stop_loss):
    """SELL parent → stop should be BUY (opposite action)."""
    payload = {
        "legs": [{
            "symbol": "SPX",
            "expiry": "20260410",
            "strike": 5200.0,
            "right": "P",
            "action": "SELL",
            "qty": 2,
            "lmtPrice": 4.00,
        }],
        "orderType": "LMT",
        "tif": "DAY",
        "stopLoss": sample_stop_loss,
    }

    result = await handle_place_order(mock_ib, app_state, payload)

    orders = mock_ib.get_placed_orders()
    assert len(orders) >= 2
    parent = orders[0]
    stop = orders[1]

    assert parent.order.action == "SELL"
    assert stop.order.action == "BUY"
    assert stop.order.totalQuantity == 2
    assert stop.order.parentId == parent.order.orderId


# ---------------------------------------------------------------------------
# 21. Both parent and stop persist in active_trades
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stop_order_persists_in_active_trades(mock_ib, app_state, sample_stop_loss):
    """Both parent and stop order IDs should exist in state.active_trades."""
    payload = {
        "legs": [{
            "symbol": "SPX",
            "expiry": "20260410",
            "strike": 5200.0,
            "right": "C",
            "action": "BUY",
            "qty": 1,
            "lmtPrice": 3.50,
        }],
        "orderType": "LMT",
        "tif": "DAY",
        "stopLoss": sample_stop_loss,
    }

    result = await handle_place_order(mock_ib, app_state, payload)
    orders = mock_ib.get_placed_orders()
    parent = orders[0]
    stop = orders[1]

    assert parent.order.orderId in app_state.active_trades
    assert stop.order.orderId in app_state.active_trades


# ---------------------------------------------------------------------------
# 22A. Combo parent PreSubmitted should still settle to Submitted
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_combo_parent_waits_through_presubmitted_for_initial_ack():
    trade = SimpleNamespace(
        order=SimpleNamespace(orderId=1234),
        orderStatus=SimpleNamespace(
            status="PreSubmitted",
            filled=0,
            remaining=1,
            avgFillPrice=0.0,
        ),
        log=[],
    )

    async def simulate_submit():
        await asyncio.sleep(0.2)
        trade.orderStatus.status = "Submitted"

    task = asyncio.create_task(simulate_submit())
    status = await await_order_status(trade, timeout=1.0, include_presubmitted=True)
    await task

    assert status == "Submitted"


@pytest.mark.asyncio
async def test_combo_parent_ws_status_push_waits_through_presubmitted(mock_ws):
    trade = SimpleNamespace(
        order=SimpleNamespace(orderId=1235),
        orderStatus=SimpleNamespace(
            status="PreSubmitted",
            filled=0,
            remaining=1,
            avgFillPrice=0.0,
        ),
        log=[],
    )

    async def simulate_submit():
        await asyncio.sleep(0.2)
        trade.orderStatus.status = "Submitted"

    task = asyncio.create_task(simulate_submit())
    await watch_and_push_status(
        mock_ws,
        trade,
        timeout=1.0,
        include_presubmitted=True,
    )
    await task

    statuses = mock_ws.get_order_statuses()
    assert len(statuses) == 1
    assert statuses[0]["orderId"] == 1235
    assert statuses[0]["status"] == "Submitted"


# ---------------------------------------------------------------------------
# 22. WS status push for stop trade on parent fill (bracket lifecycle)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stop_order_ws_status_push_on_parent_fill(mock_ib_bracket, app_state, mock_ws, sample_stop_loss):
    """watch_and_push_status for bracket child should push once stop leaves PreSubmitted."""
    payload = {
        "legs": [{
            "symbol": "SPX",
            "expiry": "20260410",
            "strike": 5200.0,
            "right": "C",
            "action": "BUY",
            "qty": 1,
            "lmtPrice": 3.50,
        }],
        "orderType": "LMT",
        "tif": "DAY",
        "stopLoss": sample_stop_loss,
    }

    result = await handle_place_order(mock_ib_bracket, app_state, payload)
    orders = mock_ib_bracket.get_placed_orders()
    parent = orders[0]
    stop = orders[1]

    assert stop.orderStatus.status == "PreSubmitted"

    # Start watching the stop trade (bracket_child=True)
    async def simulate_fill():
        await asyncio.sleep(0.2)
        mock_ib_bracket.simulate_parent_fill(parent.order.orderId)

    task = asyncio.create_task(simulate_fill())
    await watch_and_push_status(mock_ws, stop, timeout=3.0, bracket_child=True)
    await task

    statuses = mock_ws.get_order_statuses()
    assert len(statuses) >= 1
    stop_push = [s for s in statuses if s["orderId"] == stop.order.orderId]
    assert len(stop_push) == 1
    assert stop_push[0]["status"] == "Submitted"


# ---------------------------------------------------------------------------
# 23. WS status push for stop cancellation via parent cancel watcher
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stop_order_ws_status_push_on_parent_cancel(mock_ib_bracket, app_state, mock_ws, sample_stop_loss):
    """watch_parent_and_cancel_child should cancel stop and push WS message
    when parent is cancelled."""
    payload = {
        "legs": [{
            "symbol": "SPX",
            "expiry": "20260410",
            "strike": 5200.0,
            "right": "C",
            "action": "BUY",
            "qty": 1,
            "lmtPrice": 3.50,
        }],
        "orderType": "LMT",
        "tif": "DAY",
        "stopLoss": sample_stop_loss,
    }

    result = await handle_place_order(mock_ib_bracket, app_state, payload)
    orders = mock_ib_bracket.get_placed_orders()
    parent = orders[0]
    stop = orders[1]

    # Parent fills immediately in bracket_mode — set it back to Submitted
    # so the watcher can observe the cancel transition
    parent.orderStatus.status = "Submitted"

    # Cancel parent after a short delay
    async def cancel_parent():
        await asyncio.sleep(0.2)
        mock_ib_bracket.cancelOrder(parent.order)

    task = asyncio.create_task(cancel_parent())
    await watch_parent_and_cancel_child(
        mock_ib_bracket, mock_ws, parent, stop, timeout=3.0
    )
    await task

    assert stop.orderStatus.status == "Cancelled"
    statuses = mock_ws.get_order_statuses()
    cancel_pushes = [s for s in statuses if s["orderId"] == stop.order.orderId]
    assert len(cancel_pushes) >= 1
    assert cancel_pushes[0]["status"] == "Cancelled"
    assert "parent" in cancel_pushes[0]["message"].lower()


# ---------------------------------------------------------------------------
# 24. Parent fill → watcher exits without cancelling stop
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_watcher_exits_on_parent_fill_without_cancelling_stop(mock_ib_bracket, app_state, mock_ws, sample_stop_loss):
    """When parent fills, watch_parent_and_cancel_child should exit
    without cancelling the stop."""
    payload = {
        "legs": [{
            "symbol": "SPX",
            "expiry": "20260410",
            "strike": 5200.0,
            "right": "C",
            "action": "BUY",
            "qty": 1,
            "lmtPrice": 3.50,
        }],
        "orderType": "LMT",
        "tif": "DAY",
        "stopLoss": sample_stop_loss,
    }

    result = await handle_place_order(mock_ib_bracket, app_state, payload)
    orders = mock_ib_bracket.get_placed_orders()
    parent = orders[0]
    stop = orders[1]

    # Parent is already Filled in bracket_mode
    assert parent.orderStatus.status == "Filled"

    # Watcher should exit immediately since parent is already terminal
    await watch_parent_and_cancel_child(
        mock_ib_bracket, mock_ws, parent, stop, timeout=2.0
    )

    # Stop should NOT be cancelled — it's still PreSubmitted (waiting for activation)
    assert stop.orderStatus.status == "PreSubmitted"
    # No cancel WS messages should have been sent
    cancel_msgs = [s for s in mock_ws.get_order_statuses() if s["status"] == "Cancelled"]
    assert len(cancel_msgs) == 0


# ===========================================================================
# MULTI-LEG COMBO STOP LIMIT LIFECYCLE TESTS
# ===========================================================================

# ---------------------------------------------------------------------------
# 25. Debit spread (bull call) + stop lifecycle
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_combo_debit_spread_stop_lifecycle(mock_ib_bracket, app_state, sample_stop_loss):
    """Bull call spread + stop loss: parent fills → stop starts PreSubmitted,
    transitions to Submitted after simulate_parent_fill."""
    payload = {
        "legs": [
            {
                "symbol": "SPX",
                "expiry": "20260410",
                "strike": 5200.0,
                "right": "C",
                "action": "BUY",
                "qty": 1,
                "lmtPrice": 5.00,
            },
            {
                "symbol": "SPX",
                "expiry": "20260410",
                "strike": 5210.0,
                "right": "C",
                "action": "SELL",
                "qty": 1,
                "lmtPrice": 3.00,
            },
        ],
        "orderType": "LMT",
        "tif": "DAY",
        "comboAction": "BUY",
        "stopLoss": sample_stop_loss,
    }

    result = await handle_place_order(mock_ib_bracket, app_state, payload)
    orders = mock_ib_bracket.get_placed_orders()
    assert len(orders) >= 2

    parent_bag = orders[0]
    stop_bag = orders[1]

    assert parent_bag.orderStatus.status == "Filled"
    assert stop_bag.orderStatus.status == "PreSubmitted"
    assert stop_bag.order.parentId == parent_bag.order.orderId
    assert stop_bag.order.orderType == "STP LMT"
    assert stop_bag.order.action == "SELL"  # opposite of BUY parent

    # Simulate IB fill propagation
    mock_ib_bracket.simulate_parent_fill(parent_bag.order.orderId)
    assert stop_bag.orderStatus.status == "Submitted"


# ---------------------------------------------------------------------------
# 26. Credit spread + stop lifecycle
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_combo_credit_spread_stop_lifecycle(mock_ib_bracket, app_state, sample_stop_loss):
    """Bear call credit spread + stop: SELL parent → BUY stop."""
    payload = {
        "legs": [
            {
                "symbol": "SPX",
                "expiry": "20260410",
                "strike": 5200.0,
                "right": "P",
                "action": "SELL",
                "qty": 1,
                "lmtPrice": 5.00,
            },
            {
                "symbol": "SPX",
                "expiry": "20260410",
                "strike": 5190.0,
                "right": "P",
                "action": "BUY",
                "qty": 1,
                "lmtPrice": 3.00,
            },
        ],
        "orderType": "LMT",
        "tif": "DAY",
        "comboAction": "SELL",
        "comboLmtPrice": -2.00,
        "stopLoss": sample_stop_loss,
    }

    result = await handle_place_order(mock_ib_bracket, app_state, payload)
    orders = mock_ib_bracket.get_placed_orders()
    assert len(orders) >= 2

    parent_bag = orders[0]
    stop_bag = orders[1]

    assert parent_bag.order.action == "SELL"
    assert stop_bag.order.action == "BUY"  # opposite
    assert stop_bag.order.orderType == "STP LMT"
    assert stop_bag.orderStatus.status == "PreSubmitted"

    # Verify stop prices
    assert stop_bag.order.auxPrice == 1.50  # stopPrice
    assert stop_bag.order.lmtPrice == 1.40  # limitPrice


# ---------------------------------------------------------------------------
# 27. Combo stop cancelled when parent cancelled
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_combo_stop_cancelled_when_parent_cancelled(mock_ib_bracket, app_state, sample_stop_loss):
    """BAG parent cancel should cascade to stop BAG child."""
    payload = {
        "legs": [
            {
                "symbol": "SPX",
                "expiry": "20260410",
                "strike": 5200.0,
                "right": "C",
                "action": "BUY",
                "qty": 1,
                "lmtPrice": 5.00,
            },
            {
                "symbol": "SPX",
                "expiry": "20260410",
                "strike": 5210.0,
                "right": "C",
                "action": "SELL",
                "qty": 1,
                "lmtPrice": 3.00,
            },
        ],
        "orderType": "LMT",
        "tif": "DAY",
        "comboAction": "BUY",
        "stopLoss": sample_stop_loss,
    }

    result = await handle_place_order(mock_ib_bracket, app_state, payload)
    orders = mock_ib_bracket.get_placed_orders()
    parent_bag = orders[0]
    stop_bag = orders[1]

    # Cancel parent — cascade to child
    mock_ib_bracket.cancelOrder(parent_bag.order)
    assert parent_bag.orderStatus.status == "Cancelled"
    assert stop_bag.orderStatus.status == "Cancelled"


# ---------------------------------------------------------------------------
# 28. Combo stop has reversed legs (opposite actions)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_combo_stop_has_reversed_legs(mock_ib, app_state, sample_stop_loss):
    """Each ComboLeg in the stop BAG should have the opposite action of the
    parent BAG legs."""
    payload = {
        "legs": [
            {
                "symbol": "SPX",
                "expiry": "20260410",
                "strike": 5200.0,
                "right": "C",
                "action": "BUY",
                "qty": 1,
                "lmtPrice": 5.00,
            },
            {
                "symbol": "SPX",
                "expiry": "20260410",
                "strike": 5210.0,
                "right": "C",
                "action": "SELL",
                "qty": 1,
                "lmtPrice": 3.00,
            },
        ],
        "orderType": "LMT",
        "tif": "DAY",
        "comboAction": "BUY",
        "stopLoss": sample_stop_loss,
    }

    result = await handle_place_order(mock_ib, app_state, payload)
    orders = mock_ib.get_placed_orders()
    assert len(orders) >= 2

    parent_bag = orders[0]
    stop_bag = orders[1]

    parent_legs = parent_bag.contract.comboLegs
    stop_legs = stop_bag.contract.comboLegs

    assert len(parent_legs) == len(stop_legs)
    for p_leg, s_leg in zip(parent_legs, stop_legs):
        # Opposite actions
        if p_leg.action == "BUY":
            assert s_leg.action == "SELL"
        else:
            assert s_leg.action == "BUY"
        # Same conId and ratio
        assert p_leg.conId == s_leg.conId
        assert p_leg.ratio == s_leg.ratio


# ---------------------------------------------------------------------------
# 29. Combo stop has smart routing params
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_combo_stop_direct_cboe_routing(mock_ib, app_state, sample_stop_loss):
    """SPX combo stop BAG should route directly to CBOE without SMART combo params."""
    payload = {
        "legs": [
            {
                "symbol": "SPX",
                "expiry": "20260410",
                "strike": 5200.0,
                "right": "C",
                "action": "BUY",
                "qty": 1,
                "lmtPrice": 5.00,
            },
            {
                "symbol": "SPX",
                "expiry": "20260410",
                "strike": 5210.0,
                "right": "C",
                "action": "SELL",
                "qty": 1,
                "lmtPrice": 3.00,
            },
        ],
        "orderType": "LMT",
        "tif": "DAY",
        "comboAction": "BUY",
        "stopLoss": sample_stop_loss,
    }

    result = await handle_place_order(mock_ib, app_state, payload)
    orders = mock_ib.get_placed_orders()
    stop_bag = orders[1]

    assert stop_bag.contract.exchange == "CBOE"
    assert all(getattr(cl, "exchange", None) == "CBOE" for cl in stop_bag.contract.comboLegs)
    assert getattr(stop_bag.order, "smartComboRoutingParams", None) in (None, [])


# ===========================================================================
# STOP LIMIT EDGE CASE TESTS
# ===========================================================================

# ---------------------------------------------------------------------------
# 30. Invalid stop price (zero) — stop order should not be created
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stop_order_invalid_stop_price_zero(mock_ib, app_state):
    """Stop price of 0 should gracefully skip stop order creation."""
    payload = {
        "legs": [{
            "symbol": "SPX",
            "expiry": "20260410",
            "strike": 5200.0,
            "right": "C",
            "action": "BUY",
            "qty": 1,
            "lmtPrice": 3.50,
        }],
        "orderType": "LMT",
        "tif": "DAY",
        "stopLoss": {"stopPrice": 0, "limitPrice": 0},
    }

    result = await handle_place_order(mock_ib, app_state, payload)
    assert result["data"]["status"] == "Filled"

    orders = mock_ib.get_placed_orders()
    # The first placeOrder had transmit=False, then re-submitted with transmit=True
    # So we may have 2 placed orders (same parent, retransmitted) or just 1 if re-submitted
    # Either way, no STP LMT order should exist
    stp_orders = [o for o in orders if o.order.orderType == "STP LMT"]
    assert len(stp_orders) == 0
    # The last placed order should have transmit=True (re-submitted)
    last = orders[-1]
    assert last.order.transmit is True


# ---------------------------------------------------------------------------
# 31. Stop order dict vs scalar format
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stop_order_scalar_format(mock_ib, app_state):
    """Scalar stopLoss value should use same price for both stop and limit."""
    payload = {
        "legs": [{
            "symbol": "SPX",
            "expiry": "20260410",
            "strike": 5200.0,
            "right": "C",
            "action": "BUY",
            "qty": 1,
            "lmtPrice": 3.50,
        }],
        "orderType": "LMT",
        "tif": "DAY",
        "stopLoss": 2.50,
    }

    result = await handle_place_order(mock_ib, app_state, payload)
    assert result["data"]["status"] == "Filled"

    orders = mock_ib.get_placed_orders()
    assert len(orders) == 2
    stop = orders[1]
    assert stop.order.orderType == "STP LMT"
    assert stop.order.auxPrice == 2.50  # stopPrice = scalar value
    assert stop.order.lmtPrice == 2.50  # limitPrice = same scalar


@pytest.mark.asyncio
async def test_stop_order_dict_format(mock_ib, app_state, sample_stop_loss):
    """Dict stopLoss should split stopPrice and limitPrice correctly."""
    payload = {
        "legs": [{
            "symbol": "SPX",
            "expiry": "20260410",
            "strike": 5200.0,
            "right": "C",
            "action": "BUY",
            "qty": 1,
            "lmtPrice": 3.50,
        }],
        "orderType": "LMT",
        "tif": "DAY",
        "stopLoss": sample_stop_loss,
    }

    result = await handle_place_order(mock_ib, app_state, payload)
    orders = mock_ib.get_placed_orders()
    stop = orders[1]
    assert stop.order.auxPrice == 1.50  # stopPrice
    assert stop.order.lmtPrice == 1.40  # limitPrice


# ---------------------------------------------------------------------------
# 32. Stop order tick rounding (SPX rules)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stop_order_tick_rounding_spx(mock_ib, app_state):
    """Stop and limit prices should follow SPX tick rules."""
    payload = {
        "legs": [{
            "symbol": "SPX",
            "expiry": "20260410",
            "strike": 5200.0,
            "right": "C",
            "action": "BUY",
            "qty": 1,
            "lmtPrice": 3.50,
        }],
        "orderType": "LMT",
        "tif": "DAY",
        # Prices that need rounding: 1.53 → 1.55 (0.05 tick, ≤$2), 2.63 → 2.60 (0.10 tick, >$2)
        "stopLoss": {"stopPrice": 2.63, "limitPrice": 1.53},
    }

    result = await handle_place_order(mock_ib, app_state, payload)
    orders = mock_ib.get_placed_orders()
    stop = orders[1]
    # 2.63 > $2.00 → 0.10 tick → round to 2.60
    assert stop.order.auxPrice == 2.60
    # 1.53 ≤ $2.00 → 0.05 tick → round to 1.55
    assert stop.order.lmtPrice == 1.55


# ---------------------------------------------------------------------------
# 33. Stop not cancelled when parent already filled (no false cascade)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stop_not_cancelled_when_parent_already_filled(mock_ib, app_state, sample_stop_loss):
    """If parent is already Filled and user cancels parent, stop should remain
    active (IB doesn't cascade from filled orders)."""
    payload = {
        "legs": [{
            "symbol": "SPX",
            "expiry": "20260410",
            "strike": 5200.0,
            "right": "C",
            "action": "BUY",
            "qty": 1,
            "lmtPrice": 3.50,
        }],
        "orderType": "LMT",
        "tif": "DAY",
        "stopLoss": sample_stop_loss,
    }

    result = await handle_place_order(mock_ib, app_state, payload)
    orders = mock_ib.get_placed_orders()
    parent = orders[0]
    stop = orders[1]

    # Parent is already Filled (mock_ib fills immediately)
    assert parent.orderStatus.status == "Filled"

    # Trying to cancel a filled parent should not affect the stop
    # (In real IB, filled orders can't be cancelled; here we test the watcher logic)
    stop_status_before = stop.orderStatus.status
    # watcher should exit immediately without cancelling stop
    await watch_parent_and_cancel_child(
        mock_ib, None, parent, stop, timeout=1.0
    )
    assert stop.orderStatus.status == stop_status_before


# ---------------------------------------------------------------------------
# 34. Multiple quantity stop order matches parent quantity
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_multiple_qty_stop_order_single_leg(mock_ib, app_state, sample_stop_loss):
    """Stop order quantity should match parent quantity for single-leg."""
    payload = {
        "legs": [{
            "symbol": "SPX",
            "expiry": "20260410",
            "strike": 5200.0,
            "right": "C",
            "action": "BUY",
            "qty": 5,
            "lmtPrice": 3.50,
        }],
        "orderType": "LMT",
        "tif": "DAY",
        "stopLoss": sample_stop_loss,
    }

    result = await handle_place_order(mock_ib, app_state, payload)
    orders = mock_ib.get_placed_orders()
    parent = orders[0]
    stop = orders[1]

    assert parent.order.totalQuantity == 5
    assert stop.order.totalQuantity == 5


@pytest.mark.asyncio
async def test_multiple_qty_stop_order_combo(mock_ib, app_state, sample_stop_loss):
    """Stop order quantity should match combo quantity for multi-leg BAG."""
    payload = {
        "legs": [
            {
                "symbol": "SPX",
                "expiry": "20260410",
                "strike": 5200.0,
                "right": "C",
                "action": "BUY",
                "qty": 1,
                "lmtPrice": 5.00,
            },
            {
                "symbol": "SPX",
                "expiry": "20260410",
                "strike": 5210.0,
                "right": "C",
                "action": "SELL",
                "qty": 1,
                "lmtPrice": 3.00,
            },
        ],
        "orderType": "LMT",
        "tif": "DAY",
        "comboAction": "BUY",
        "comboQuantity": 3,
        "stopLoss": sample_stop_loss,
    }

    result = await handle_place_order(mock_ib, app_state, payload)
    orders = mock_ib.get_placed_orders()
    parent = orders[0]
    stop = orders[1]

    assert parent.order.totalQuantity == 3
    assert stop.order.totalQuantity == 3


# ---------------------------------------------------------------------------
# 35. Iron condor (4-leg) + stop lifecycle
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_iron_condor_stop_lifecycle(mock_ib_bracket, app_state, sample_stop_loss):
    """4-leg iron condor with stop loss. Verify bracket lifecycle and reversed legs."""
    payload = {
        "legs": [
            {   # Short call
                "symbol": "SPX",
                "expiry": "20260410",
                "strike": 5250.0,
                "right": "C",
                "action": "SELL",
                "qty": 1,
                "lmtPrice": 3.00,
            },
            {   # Long call (wing)
                "symbol": "SPX",
                "expiry": "20260410",
                "strike": 5260.0,
                "right": "C",
                "action": "BUY",
                "qty": 1,
                "lmtPrice": 2.00,
            },
            {   # Short put
                "symbol": "SPX",
                "expiry": "20260410",
                "strike": 5150.0,
                "right": "P",
                "action": "SELL",
                "qty": 1,
                "lmtPrice": 3.50,
            },
            {   # Long put (wing)
                "symbol": "SPX",
                "expiry": "20260410",
                "strike": 5140.0,
                "right": "P",
                "action": "BUY",
                "qty": 1,
                "lmtPrice": 2.50,
            },
        ],
        "orderType": "LMT",
        "tif": "DAY",
        "comboAction": "SELL",
        "comboLmtPrice": -2.00,
        "stopLoss": sample_stop_loss,
    }

    result = await handle_place_order(mock_ib_bracket, app_state, payload)
    orders = mock_ib_bracket.get_placed_orders()
    assert len(orders) >= 2

    parent_bag = orders[0]
    stop_bag = orders[1]

    # Parent is iron condor SELL
    assert parent_bag.order.action == "SELL"
    assert parent_bag.orderStatus.status == "Filled"

    # Stop is the reverse (BUY to close)
    assert stop_bag.order.action == "BUY"
    assert stop_bag.order.orderType == "STP LMT"
    assert stop_bag.orderStatus.status == "PreSubmitted"
    assert stop_bag.order.parentId == parent_bag.order.orderId

    # Verify all 4 legs reversed
    parent_legs = parent_bag.contract.comboLegs
    stop_legs = stop_bag.contract.comboLegs
    assert len(stop_legs) == 4
    for p_leg, s_leg in zip(parent_legs, stop_legs):
        assert p_leg.conId == s_leg.conId
        expected_action = "SELL" if p_leg.action == "BUY" else "BUY"
        assert s_leg.action == expected_action

    # Simulate parent fill → stop activates
    mock_ib_bracket.simulate_parent_fill(parent_bag.order.orderId)
    assert stop_bag.orderStatus.status == "Submitted"


# ---------------------------------------------------------------------------
# 36. Combo stop WS push on parent cancel
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_combo_stop_ws_push_on_parent_cancel(mock_ib_bracket, app_state, mock_ws, sample_stop_loss):
    """Multi-leg combo: watch_parent_and_cancel_child pushes cancellation
    for the stop BAG when parent is cancelled."""
    payload = {
        "legs": [
            {
                "symbol": "SPX",
                "expiry": "20260410",
                "strike": 5200.0,
                "right": "C",
                "action": "BUY",
                "qty": 1,
                "lmtPrice": 5.00,
            },
            {
                "symbol": "SPX",
                "expiry": "20260410",
                "strike": 5210.0,
                "right": "C",
                "action": "SELL",
                "qty": 1,
                "lmtPrice": 3.00,
            },
        ],
        "orderType": "LMT",
        "tif": "DAY",
        "comboAction": "BUY",
        "stopLoss": sample_stop_loss,
    }

    result = await handle_place_order(mock_ib_bracket, app_state, payload)
    orders = mock_ib_bracket.get_placed_orders()
    parent_bag = orders[0]
    stop_bag = orders[1]

    # Parent fills immediately in bracket_mode — set it back to Submitted
    # so the watcher can observe the cancel transition
    parent_bag.orderStatus.status = "Submitted"

    async def cancel_parent():
        await asyncio.sleep(0.2)
        mock_ib_bracket.cancelOrder(parent_bag.order)

    task = asyncio.create_task(cancel_parent())
    await watch_parent_and_cancel_child(
        mock_ib_bracket, mock_ws, parent_bag, stop_bag, timeout=3.0
    )
    await task

    assert stop_bag.orderStatus.status == "Cancelled"
    statuses = mock_ws.get_order_statuses()
    cancel_pushes = [s for s in statuses if s["orderId"] == stop_bag.order.orderId]
    assert len(cancel_pushes) >= 1
    assert cancel_pushes[0]["status"] == "Cancelled"


# ---------------------------------------------------------------------------
# 37. outsideRth propagated to stop order
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stop_order_outside_rth_propagated(mock_ib, app_state, sample_stop_loss):
    """outsideRth flag should propagate to both parent and stop orders."""
    payload = {
        "legs": [{
            "symbol": "SPX",
            "expiry": "20260410",
            "strike": 5200.0,
            "right": "C",
            "action": "BUY",
            "qty": 1,
            "lmtPrice": 3.50,
        }],
        "orderType": "LMT",
        "tif": "DAY",
        "outsideRth": True,
        "stopLoss": sample_stop_loss,
    }

    result = await handle_place_order(mock_ib, app_state, payload)
    orders = mock_ib.get_placed_orders()
    parent = orders[0]
    stop = orders[1]

    assert parent.order.outsideRth is True
    assert stop.order.outsideRth is True


# ---------------------------------------------------------------------------
# 38. Stop TIF matches parent TIF
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stop_order_tif_matches_parent(mock_ib, app_state, sample_stop_loss):
    """Stop order TIF should match the parent order TIF."""
    payload = {
        "legs": [{
            "symbol": "SPX",
            "expiry": "20260410",
            "strike": 5200.0,
            "right": "C",
            "action": "BUY",
            "qty": 1,
            "lmtPrice": 3.50,
        }],
        "orderType": "LMT",
        "tif": "DAY",
        "stopLoss": sample_stop_loss,
    }

    result = await handle_place_order(mock_ib, app_state, payload)
    orders = mock_ib.get_placed_orders()
    parent = orders[0]
    stop = orders[1]

    assert parent.order.tif == "DAY"
    assert stop.order.tif == "DAY"
