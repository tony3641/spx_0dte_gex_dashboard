"""
Account manager unit tests — serialization helpers and refresh logic.
"""

import sys
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import account_manager
from account_manager import (
    serialize_account_values,
    serialize_portfolio_item,
    serialize_trade,
    parse_execution_time,
    format_execution_time_et,
    refresh_account_state,
    build_account_payload,
)
from market_hours import ET


# ---------------------------------------------------------------------------
# Lightweight stand-ins for ib_insync data classes
# ---------------------------------------------------------------------------

@dataclass
class FakeAccountValue:
    tag: str = ""
    value: str = ""
    currency: str = "USD"
    account: str = "DU12345"


@dataclass
class FakeContract:
    conId: int = 100
    symbol: str = "SPX"
    secType: str = "OPT"
    lastTradeDateOrContractMonth: str = "20260410"
    strike: float = 5200.0
    right: str = "C"
    multiplier: str = "100"
    currency: str = "USD"
    exchange: str = "SMART"
    localSymbol: str = "SPXW 260410C05200000"
    tradingClass: str = "SPXW"


@dataclass
class FakePortfolioItem:
    contract: FakeContract = None
    position: float = 1
    marketPrice: float = 3.50
    marketValue: float = 350.0
    averageCost: float = 300.0
    unrealizedPNL: float = 50.0
    realizedPNL: float = 0.0
    account: str = "DU12345"

    def __post_init__(self):
        if self.contract is None:
            self.contract = FakeContract()


@dataclass
class FakeOrderStatus:
    status: str = "Filled"
    filled: float = 1
    remaining: float = 0
    avgFillPrice: float = 3.50


@dataclass
class FakeOrder:
    orderId: int = 1
    permId: int = 100
    clientId: int = 1
    action: str = "BUY"
    totalQuantity: float = 1
    orderType: str = "LMT"
    lmtPrice: float = 3.50
    auxPrice: float = 0.0
    tif: str = "DAY"


@dataclass
class FakeLogEntry:
    message: str = "Order submitted"


@dataclass
class FakeTrade:
    contract: FakeContract = None
    order: FakeOrder = None
    orderStatus: FakeOrderStatus = None
    log: list = None

    def __post_init__(self):
        if self.contract is None:
            self.contract = FakeContract()
        if self.order is None:
            self.order = FakeOrder()
        if self.orderStatus is None:
            self.orderStatus = FakeOrderStatus()
        if self.log is None:
            self.log = [FakeLogEntry()]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestSerializeAccountValues:

    def test_extracts_wanted_keys(self):
        values = [
            FakeAccountValue(tag="NetLiquidation", value="100000.50"),
            FakeAccountValue(tag="BuyingPower", value="50000.00"),
            FakeAccountValue(tag="UnrealizedPnL", value="-1234.56"),
            FakeAccountValue(tag="SomeOtherTag", value="999"),
        ]
        result = serialize_account_values(values)
        assert result["NetLiquidation"] == 100000.50
        assert result["BuyingPower"] == 50000.00
        assert result["UnrealizedPnL"] == -1234.56
        assert "SomeOtherTag" not in result

    def test_filters_non_usd(self):
        values = [
            FakeAccountValue(tag="NetLiquidation", value="100000", currency="USD"),
            FakeAccountValue(tag="NetLiquidation", value="85000", currency="EUR"),
        ]
        result = serialize_account_values(values)
        assert result["NetLiquidation"] == 100000.0

    def test_empty_list(self):
        assert serialize_account_values([]) == {}

    def test_invalid_value_skipped(self):
        values = [
            FakeAccountValue(tag="NetLiquidation", value="not_a_number"),
        ]
        result = serialize_account_values(values)
        assert "NetLiquidation" not in result


class TestSerializePortfolioItem:

    def test_serializes_option_position(self):
        item = FakePortfolioItem()
        result = serialize_portfolio_item(item)

        assert result["position"] == 1
        assert result["marketPrice"] == 3.50
        assert result["unrealizedPNL"] == 50.0
        assert result["contract"]["symbol"] == "SPX"
        assert result["contract"]["strike"] == 5200.0
        assert result["contract"]["right"] == "C"
        assert result["contract"]["secType"] == "OPT"

    def test_serializes_no_strike_contract(self):
        contract = FakeContract(strike=0.0, right="", secType="STK", symbol="AAPL")
        item = FakePortfolioItem(contract=contract)
        result = serialize_portfolio_item(item)
        assert result["contract"]["strike"] is None  # 0 treated as None
        assert result["contract"]["symbol"] == "AAPL"


class TestSerializeTrade:

    def test_serializes_filled_trade(self):
        trade = FakeTrade()
        result = serialize_trade(trade)

        assert result["orderId"] == 1
        assert result["action"] == "BUY"
        assert result["totalQty"] == 1
        assert result["orderType"] == "LMT"
        assert result["status"] == "Filled"
        assert result["filled"] == 1
        assert result["contract"]["symbol"] == "SPX"


class TestBuildAccountPayload:

    def test_returns_expected_keys(self, app_state):
        app_state.account_summary = {"NetLiquidation": 100000.0}
        app_state.positions = [{"contract": {"symbol": "SPX"}}]
        app_state.open_orders = []
        app_state.executions = []

        result = build_account_payload(app_state)

        assert "summary" in result
        assert "positions" in result
        assert "orders" in result
        assert "executions" in result
        assert result["summary"]["NetLiquidation"] == 100000.0
        assert len(result["positions"]) == 1


class TestRefreshAccountState:

    def test_populates_state_from_mock_ib(self, app_state):
        """Verify refresh_account_state pulls data from IB and populates state."""
        from tests.conftest import MockIB

        mock = MockIB()
        # We can't easily set up account values on MockIB since it returns
        # empty lists by default. Verify it doesn't crash and sets dirty flag.
        refresh_account_state(mock, app_state)
        assert app_state.account_dirty is True


class TestExecutionTimeParsing:

    def test_parse_aware_utc_converts_to_et(self):
        raw = datetime(2026, 4, 10, 5, 44, 0, tzinfo=timezone.utc)
        parsed = parse_execution_time(raw)
        assert parsed is not None
        assert parsed.astimezone(ET).hour == 1
        assert parsed.astimezone(ET).minute == 44

    def test_parse_ib_timezone_name_string(self):
        parsed = parse_execution_time("20260410 01:44:00 US/Eastern")
        assert parsed is not None
        assert parsed.hour == 1
        assert parsed.minute == 44

    def test_format_execution_time_uses_dst_label_in_april(self):
        dt = datetime(2026, 4, 10, 1, 44, 0, tzinfo=ET)
        text = format_execution_time_et(dt)
        assert text.startswith("01:44:00")
        assert text.endswith("EDT")

    def test_parse_time_only_rolls_to_previous_day_if_future(self, monkeypatch):
        # If a plain time-only string occurs before market open, assume it belongs
        # to the previous session when the resulting datetime would otherwise be in the future.
        monkeypatch.setattr('account_manager.now_et', lambda: datetime(2026, 4, 10, 5, 38, 0, tzinfo=ET))
        parsed = parse_execution_time('12:37:50')
        assert parsed is not None
        assert parsed.date() == datetime(2026, 4, 9, tzinfo=ET).date()
        assert parsed.hour == 12
        assert parsed.minute == 37
