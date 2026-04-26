"""Focused tests for websocket-side IB error forwarding."""

import asyncio
import sys
import os
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ws_handler import make_ib_error_handler


@pytest.mark.asyncio
async def test_ib_error_handler_ignores_informational_codes():
    messages = []

    async def broadcast_fn(message):
        messages.append(message)

    state = SimpleNamespace(active_trades={})
    handler = make_ib_error_handler(state, broadcast_fn)

    handler(-1, 2104, "Market data farm connection is OK", None)
    await asyncio.sleep(0)

    assert messages == []


@pytest.mark.asyncio
async def test_ib_error_handler_broadcasts_actionable_error_with_trade_contract():
    messages = []

    async def broadcast_fn(message):
        messages.append(message)

    bag_contract = SimpleNamespace(
        conId=0,
        symbol="SPX",
        secType="BAG",
        exchange="SMART",
        currency="USD",
        lastTradeDateOrContractMonth="",
        strike=0.0,
        right="",
        localSymbol="",
        tradingClass="",
        comboLegs=[
            SimpleNamespace(conId=1, ratio=1, action="BUY", exchange="SMART"),
            SimpleNamespace(conId=2, ratio=1, action="SELL", exchange="SMART"),
        ],
    )
    trade = SimpleNamespace(contract=bag_contract)
    state = SimpleNamespace(active_trades={123: trade})
    handler = make_ib_error_handler(state, broadcast_fn)

    handler(123, 10043, "Missing or invalid NonGuaranteed value.", None)
    await asyncio.sleep(0)

    assert len(messages) == 1
    payload = messages[0]
    assert payload["type"] == "ib_error"
    assert payload["data"]["orderId"] == 123
    assert payload["data"]["errorCode"] == 10043
    assert payload["data"]["contract"]["secType"] == "BAG"
    assert payload["data"]["contract"]["comboLegs"][0]["action"] == "BUY"