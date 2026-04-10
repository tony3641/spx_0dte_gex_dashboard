"""
Shared test fixtures and MockIB class for AI-driven autonomous testing.

MockIB is an explicit class (not unittest.mock) so that AI agents can
inspect return values, state transitions, and order flows without opaque
mock internals.
"""

import asyncio
import sys
import os
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import pytest

# Ensure project root is on sys.path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app_state import AppState, create_app_state


# ---------------------------------------------------------------------------
# Lightweight stand-ins for ib_insync data classes used in order_manager
# ---------------------------------------------------------------------------

@dataclass
class MockOrderStatus:
    status: str = ""
    filled: float = 0
    remaining: float = 0
    avgFillPrice: float = 0.0


@dataclass
class MockOrder:
    orderId: int = 0
    permId: int = 0
    clientId: int = 1
    action: str = ""
    totalQuantity: float = 0
    orderType: str = "LMT"
    lmtPrice: float = 0.0
    auxPrice: float = 0.0
    tif: str = "DAY"
    outsideRth: bool = False
    transmit: bool = True
    parentId: int = 0


@dataclass
class MockLogEntry:
    message: str = ""


@dataclass
class MockTrade:
    contract: Any = None
    order: MockOrder = field(default_factory=MockOrder)
    orderStatus: MockOrderStatus = field(default_factory=MockOrderStatus)
    log: List[MockLogEntry] = field(default_factory=list)


@dataclass
class MockContractDetails:
    minTick: float = 0.05


@dataclass
class MockContract:
    conId: int = 0
    symbol: str = ""
    secType: str = ""
    lastTradeDateOrContractMonth: str = ""
    strike: float = 0.0
    right: str = ""
    multiplier: str = "100"
    currency: str = "USD"
    exchange: str = "SMART"
    localSymbol: str = ""
    tradingClass: str = ""
    comboLegs: list = field(default_factory=list)


@dataclass
class MockTicker:
    bid: float = -1
    ask: float = -1
    last: float = -1
    bidSize: float = 0
    askSize: float = 0
    volume: float = 0
    callOpenInterest: float = -1
    putOpenInterest: float = -1
    modelGreeks: Any = None
    lastGreeks: Any = None
    contract: Any = None


# ---------------------------------------------------------------------------
# MockIB — explicit class implementing the subset of ib_insync.IB used
# by order_manager, account_manager, and other modules
# ---------------------------------------------------------------------------

class MockIB:
    """Mock IB client with configurable behaviour.

    Parameters
    ----------
    connected : bool
        Whether isConnected() returns True.
    fill_immediately : bool
        If True, placed orders get status='Filled' immediately.
        If False, orders stay in PendingSubmit.
    reject : bool
        If True, placed orders get status='Cancelled' (simulating IB reject).
    """

    def __init__(self, connected: bool = True,
                 fill_immediately: bool = True,
                 reject: bool = False):
        self._connected = connected
        self._fill_immediately = fill_immediately
        self._reject = reject
        self._next_order_id = 100
        self._next_con_id = 10000
        self._placed_orders: List[MockTrade] = []
        self._cancelled_orders: List[int] = []
        self._open_trades: List[MockTrade] = []
        self._account_values: list = []
        self._portfolio: list = []
        self._fills: list = []
        self._mkt_data_tickers: Dict[int, MockTicker] = {}
        self.call_log: List[Dict] = []  # records every method call for AI analysis

    # -- Connection ----------------------------------------------------------

    def isConnected(self) -> bool:
        return self._connected

    async def connectAsync(self, host, port, clientId=1, timeout=15):
        self.call_log.append({"method": "connectAsync", "host": host,
                              "port": port, "clientId": clientId})
        if not self._connected:
            raise ConnectionError("MockIB: not connected")

    def disconnect(self):
        self.call_log.append({"method": "disconnect"})
        self._connected = False

    # -- Contract qualification ----------------------------------------------

    async def qualifyContractsAsync(self, *contracts):
        self.call_log.append({
            "method": "qualifyContractsAsync",
            "contracts": [getattr(c, "symbol", str(c)) for c in contracts],
        })
        result = []
        for c in contracts:
            mc = MockContract(
                conId=self._next_con_id,
                symbol=getattr(c, "symbol", "SPX"),
                secType=getattr(c, "secType", "OPT"),
                lastTradeDateOrContractMonth=getattr(c, "lastTradeDateOrContractMonth", ""),
                strike=getattr(c, "strike", 0.0),
                right=getattr(c, "right", ""),
                multiplier=getattr(c, "multiplier", "100"),
                currency=getattr(c, "currency", "USD"),
                exchange=getattr(c, "exchange", "SMART"),
                tradingClass=getattr(c, "tradingClass", ""),
            )
            self._next_con_id += 1
            result.append(mc)
        return result

    async def reqContractDetailsAsync(self, contract):
        self.call_log.append({
            "method": "reqContractDetailsAsync",
            "symbol": getattr(contract, "symbol", ""),
        })
        return [MockContractDetails(minTick=0.05)]

    # -- Market data ---------------------------------------------------------

    def reqMktData(self, contract, genericTickList="", snapshot=False):
        self.call_log.append({
            "method": "reqMktData",
            "symbol": getattr(contract, "symbol", ""),
        })
        ticker = MockTicker(bid=3.40, ask=3.60, last=3.50, contract=contract)
        con_id = getattr(contract, "conId", id(contract))
        self._mkt_data_tickers[con_id] = ticker
        return ticker

    def cancelMktData(self, contract):
        self.call_log.append({
            "method": "cancelMktData",
            "symbol": getattr(contract, "symbol", ""),
        })
        con_id = getattr(contract, "conId", id(contract))
        self._mkt_data_tickers.pop(con_id, None)

    # -- Order placement -----------------------------------------------------

    def placeOrder(self, contract, order) -> MockTrade:
        order_id = self._next_order_id
        self._next_order_id += 1
        order.orderId = order_id

        if self._reject:
            status = MockOrderStatus(
                status="Cancelled", filled=0,
                remaining=order.totalQuantity, avgFillPrice=0.0,
            )
        elif self._fill_immediately:
            fill_price = order.lmtPrice if order.lmtPrice else 3.50
            status = MockOrderStatus(
                status="Filled",
                filled=order.totalQuantity,
                remaining=0,
                avgFillPrice=fill_price,
            )
        else:
            status = MockOrderStatus(
                status="Submitted",
                filled=0,
                remaining=order.totalQuantity,
                avgFillPrice=0.0,
            )

        trade = MockTrade(
            contract=contract,
            order=order,
            orderStatus=status,
            log=[MockLogEntry(message=f"MockIB: order {order_id} placed")],
        )
        self._placed_orders.append(trade)
        self._open_trades.append(trade)

        self.call_log.append({
            "method": "placeOrder",
            "orderId": order_id,
            "action": order.action,
            "totalQuantity": order.totalQuantity,
            "orderType": order.orderType,
            "lmtPrice": order.lmtPrice,
            "auxPrice": order.auxPrice,
            "transmit": order.transmit,
            "parentId": order.parentId,
            "status": status.status,
        })
        return trade

    def cancelOrder(self, order):
        self.call_log.append({
            "method": "cancelOrder",
            "orderId": order.orderId,
        })
        self._cancelled_orders.append(order.orderId)
        for trade in self._open_trades:
            if trade.order.orderId == order.orderId:
                trade.orderStatus.status = "Cancelled"
                break

    def reqOpenOrders(self):
        self.call_log.append({"method": "reqOpenOrders"})

    # -- Account queries -----------------------------------------------------

    def accountValues(self):
        return self._account_values

    def portfolio(self):
        return self._portfolio

    def openTrades(self):
        return [t for t in self._open_trades
                if t.orderStatus.status not in ("Filled", "Cancelled")]

    def fills(self):
        return self._fills

    # -- Utility for tests ---------------------------------------------------

    def get_placed_orders(self) -> List[MockTrade]:
        """Return all orders placed during the test."""
        return list(self._placed_orders)

    def get_last_trade(self) -> Optional[MockTrade]:
        """Return the most recently placed trade."""
        return self._placed_orders[-1] if self._placed_orders else None

    def get_call_log_json(self) -> str:
        """Return call log as JSON for AI analysis."""
        return json.dumps(self.call_log, indent=2, default=str)

    def set_fill_status(self, order_id: int, status: str = "Filled",
                        filled: float = 0, avg_price: float = 0.0):
        """Manually transition an order's status (for async tests)."""
        for trade in self._placed_orders:
            if trade.order.orderId == order_id:
                trade.orderStatus.status = status
                trade.orderStatus.filled = filled
                trade.orderStatus.avgFillPrice = avg_price
                trade.orderStatus.remaining = trade.order.totalQuantity - filled
                break


# ---------------------------------------------------------------------------
# Pytest fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_ib():
    """A connected MockIB that fills immediately."""
    return MockIB(connected=True, fill_immediately=True)


@pytest.fixture
def mock_ib_pending():
    """A connected MockIB that keeps orders in PendingSubmit."""
    return MockIB(connected=True, fill_immediately=False)


@pytest.fixture
def mock_ib_reject():
    """A connected MockIB that rejects all orders."""
    return MockIB(connected=True, reject=True)


@pytest.fixture
def mock_ib_disconnected():
    """A disconnected MockIB."""
    return MockIB(connected=False)


@pytest.fixture
def app_state():
    """Fresh AppState instance."""
    return create_app_state()


@pytest.fixture
def sample_legs_single():
    """Single-leg BUY call payload."""
    return {
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
    }


@pytest.fixture
def sample_legs_combo():
    """Two-leg vertical spread payload (bull call spread)."""
    return {
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
    }


@pytest.fixture
def sample_stop_loss():
    """Stop-loss dict payload."""
    return {"stopPrice": 1.50, "limitPrice": 1.40}


# ---------------------------------------------------------------------------
# JSON log capture (for AI-friendly structured output)
# ---------------------------------------------------------------------------

class JSONLogCapture(logging.Handler):
    """Captures log records as structured dicts for AI analysis."""

    def __init__(self):
        super().__init__()
        self.records: List[Dict] = []

    def emit(self, record):
        self.records.append({
            "level": record.levelname,
            "logger": record.name,
            "message": self.format(record),
        })

    def get_messages(self) -> List[str]:
        return [r["message"] for r in self.records]

    def to_json(self) -> str:
        return json.dumps(self.records, indent=2, default=str)


@pytest.fixture
def log_capture():
    """Attach a JSON log capture handler to all loggers for the test."""
    handler = JSONLogCapture()
    handler.setLevel(logging.DEBUG)
    root = logging.getLogger()
    root.addHandler(handler)
    yield handler
    root.removeHandler(handler)
