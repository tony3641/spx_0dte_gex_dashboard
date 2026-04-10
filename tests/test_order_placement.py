"""
Order placement unit tests — highest priority test module.

Exercises single-leg, multi-leg BAG, stop-loss brackets, dynamic fill,
tick rounding, validation errors, and IB disconnected scenarios.

All tests use MockIB (from conftest) — no live IB connection required.
"""

import asyncio
import sys
import os

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from order_manager import handle_place_order, handle_cancel_order, _PENDING_STATUSES
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
    assert trade.order.action == "BUY"
    assert trade.order.orderType == "LMT"
    assert getattr(trade.order, "smartComboRoutingParams", None), "BAG orders must set smart combo routing params"
    tag = trade.order.smartComboRoutingParams[0]
    assert getattr(tag, "tag", None) == "NonGuaranteed"
    assert getattr(tag, "value", None) == "1"

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
