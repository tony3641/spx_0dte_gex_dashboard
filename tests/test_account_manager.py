"""
Account manager unit tests — serialization helpers and refresh logic (native bridge).

Uses the real native records (``AccountValue``/``PortfolioItem``/
``ExecutionRecord``/``OrderHandle``) and the ``MockIBClient`` surface so the
serializers are tested against exactly the objects ``IBClient`` produces.
"""

import sys
import os
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import account_manager
from account_manager import (
    serialize_account_values,
    serialize_portfolio_item,
    serialize_order_handle,
    serialize_execution,
    parse_execution_time,
    format_execution_time_et,
    refresh_account_state,
    build_account_payload,
)
from ib_client import AccountValue, ExecutionRecord, OrderHandle, PortfolioItem
from tests.conftest import MockContract, MockIBClient, MockOrder
from market_hours import ET, now_et


# ---------------------------------------------------------------------------
# Builders for native records
# ---------------------------------------------------------------------------

def _contract(**kw):
    """A MockContract mirroring a native option contract (SPXW 5200 C)."""
    defaults = dict(
        conId=100,
        symbol="SPX",
        secType="OPT",
        lastTradeDateOrContractMonth="20260410",
        strike=5200.0,
        right="C",
        multiplier="100",
        currency="USD",
        exchange="SMART",
        localSymbol="SPXW 260410C05200000",
        tradingClass="SPXW",
    )
    defaults.update(kw)
    return MockContract(**defaults)


def _order(**kw):
    """A MockOrder with a BUY 1 SPX option LMT DAY defaults."""
    defaults = dict(orderId=1, action="BUY", totalQuantity=1, orderType="LMT",
                    lmtPrice=3.50, auxPrice=0.0, tif="DAY", clientId=1)
    defaults.update(kw)
    return MockOrder(**defaults)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestSerializeAccountValues:

    def test_extracts_wanted_keys(self):
        values = [
            AccountValue("NetLiquidation", "100000.50", "USD"),
            AccountValue("BuyingPower", "50000.00", "USD"),
            AccountValue("UnrealizedPnL", "-1234.56", "USD"),
            AccountValue("SomeOtherTag", "999", "USD"),
        ]
        result = serialize_account_values(values)
        assert result["NetLiquidation"] == 100000.50
        assert result["BuyingPower"] == 50000.00
        assert result["UnrealizedPnL"] == -1234.56
        assert "SomeOtherTag" not in result

    def test_filters_non_usd(self):
        values = [
            AccountValue("NetLiquidation", "100000", "USD"),
            AccountValue("NetLiquidation", "85000", "EUR"),
        ]
        result = serialize_account_values(values)
        assert result["NetLiquidation"] == 100000.0

    def test_empty_list(self):
        assert serialize_account_values([]) == {}

    def test_invalid_value_skipped(self):
        values = [
            AccountValue("NetLiquidation", "not_a_number", "USD"),
        ]
        result = serialize_account_values(values)
        assert "NetLiquidation" not in result


class TestSerializePortfolioItem:

    def test_serializes_option_position(self):
        item = PortfolioItem(_contract(), 1, 3.50, 350.0, 300.0, 50.0, 0.0, "DU12345")
        result = serialize_portfolio_item(item)

        assert result["position"] == 1
        assert result["marketPrice"] == 3.50
        assert result["unrealizedPNL"] == 50.0
        assert result["contract"]["symbol"] == "SPX"
        assert result["contract"]["strike"] == 5200.0
        assert result["contract"]["right"] == "C"
        assert result["contract"]["secType"] == "OPT"

    def test_serializes_no_strike_contract(self):
        contract = _contract(strike=0.0, right="", secType="STK", symbol="AAPL")
        item = PortfolioItem(contract, 1, 3.50, 350.0, 300.0, 50.0, 0.0, "DU12345")
        result = serialize_portfolio_item(item)
        assert result["contract"]["strike"] is None  # 0 treated as None
        assert result["contract"]["symbol"] == "AAPL"


class TestSerializeOrderHandle:

    def test_serializes_filled_order(self):
        handle = OrderHandle(1, _contract(), _order())
        handle.status = "Filled"
        handle.filled = 1
        handle.remaining = 0
        handle.avg_fill_price = 3.50
        handle.perm_id = 100

        result = serialize_order_handle(handle)

        assert result["orderId"] == 1
        assert result["permId"] == 100
        assert result["clientId"] == 1
        assert result["action"] == "BUY"
        assert result["totalQty"] == 1
        assert result["orderType"] == "LMT"
        assert result["lmtPrice"] == 3.50
        assert result["status"] == "Filled"
        assert result["filled"] == 1
        assert result["remaining"] == 0
        assert result["avgFillPrice"] == 3.50
        assert result["contract"]["symbol"] == "SPX"
        assert result["lastLogMsg"] == ""

    def test_unset_prices_serialize_as_none(self):
        unset = 1.7976931348623157e+308
        handle = OrderHandle(2, _contract(),
                             _order(orderId=2, orderType="MKT",
                                    lmtPrice=unset, auxPrice=unset))
        handle.status = "Submitted"
        handle.remaining = 1

        result = serialize_order_handle(handle)

        assert result["lmtPrice"] is None
        assert result["auxPrice"] is None

    def test_zero_aux_price_is_preserved(self):
        handle = OrderHandle(3, _contract(),
                             _order(orderId=3, orderType="STP LMT",
                                    lmtPrice=1.50, auxPrice=1.40))
        handle.status = "Submitted"
        handle.remaining = 1

        result = serialize_order_handle(handle)

        assert result["auxPrice"] == 1.40
        assert result["lmtPrice"] == 1.50


class TestSerializeExecution:

    def test_filters_to_today_and_reads_commission(self):
        mock = MockIBClient()
        today = now_et()
        yesterday = today - timedelta(days=1)

        exec_today = SimpleNamespace(
            execId="E1", time=today.strftime("%Y%m%d %H:%M:%S"),
            side="BUY", shares=1, price=3.50, orderId=100)
        commission = SimpleNamespace(execId="E1", commissionAndFees=1.02)
        exec_old = SimpleNamespace(
            execId="E2", time=yesterday.strftime("%Y%m%d %H:%M:%S"),
            side="SELL", shares=2, price=5.00, orderId=101)

        mock.executions = [
            ExecutionRecord(_contract(symbol="SPX"), exec_old, None),
            ExecutionRecord(_contract(symbol="SPX"), exec_today, commission),
        ]

        result = serialize_execution(mock)

        assert len(result) == 1
        entry = result[0]
        assert entry["execId"] == "E1"
        assert entry["side"] == "BUY"
        assert entry["shares"] == 1
        assert entry["price"] == 3.50
        assert entry["orderId"] == 100
        assert entry["commission"] == 1.02
        assert entry["symbol"] == "SPX"

    def test_oversized_commission_treated_as_none(self):
        mock = MockIBClient()
        exec_rec = SimpleNamespace(
            execId="E3", time=now_et().strftime("%Y%m%d %H:%M:%S"),
            side="BUY", shares=1, price=3.50, orderId=102)
        commission = SimpleNamespace(execId="E3", commissionAndFees=2.5e9)
        mock.executions = [ExecutionRecord(_contract(symbol="SPX"), exec_rec, commission)]

        result = serialize_execution(mock)

        assert len(result) == 1
        assert result[0]["commission"] is None

    def test_empty_list(self):
        assert serialize_execution(MockIBClient()) == []


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
        """Verify refresh_account_state pulls data from the native bridge state."""
        mock = MockIBClient(connected=True, fill_immediately=False)
        mock.account_values = [
            AccountValue("NetLiquidation", "100000.50", "USD"),
            AccountValue("NetLiquidation", "85000.00", "EUR"),
        ]
        mock.portfolio = [
            PortfolioItem(_contract(), 1, 3.50, 350.0, 300.0, 50.0, 0.0, "DU12345"),
        ]
        handle = mock.place_order(_contract(), _order())  # status "Submitted"

        refresh_account_state(mock, app_state)

        assert app_state.account_dirty is True
        assert app_state.account_summary["NetLiquidation"] == 100000.50
        assert len(app_state.positions) == 1
        assert app_state.positions[0]["contract"]["symbol"] == "SPX"
        # Only non-terminal orders appear in open_orders; active_trades keeps all.
        assert len(app_state.open_orders) == 1
        assert app_state.open_orders[0]["orderId"] == handle.order_id
        assert app_state.open_orders[0]["status"] == "Submitted"
        assert app_state.active_trades[handle.order_id] is handle

    def test_terminal_orders_excluded_from_open_orders(self, app_state):
        mock = MockIBClient(connected=True, fill_immediately=True)  # Filled
        filled = mock.place_order(_contract(), _order())
        refresh_account_state(mock, app_state)
        assert app_state.account_dirty is True
        assert app_state.open_orders == []
        assert filled.order_id in app_state.active_trades

    def test_empty_mock_does_not_crash(self, app_state):
        mock = MockIBClient()
        refresh_account_state(mock, app_state)
        assert app_state.account_dirty is True
        assert app_state.account_summary == {}
        assert app_state.positions == []


class TestExecutionTimeParsing:

    def test_parse_aware_utc_converts_to_et(self):
        raw = datetime(2026, 4, 10, 5, 44, 0, tzinfo=timezone.utc)
        parsed = parse_execution_time(raw)
        assert parsed is not None
        assert parsed.astimezone(ET).hour == 1
        assert parsed.astimezone(ET).minute == 44

    def test_parse_native_ib_execution_time_string(self):
        # Native ibapi execution.time format: "YYYYMMDD HH:MM:SS" (no tz suffix).
        parsed = parse_execution_time("20260820 14:30:00")
        assert parsed is not None
        assert parsed.year == 2026
        assert parsed.month == 8
        assert parsed.day == 20
        assert parsed.hour == 14
        assert parsed.minute == 30
        assert parsed.tzinfo is ET

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
