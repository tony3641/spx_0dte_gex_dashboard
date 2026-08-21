"""
Shared test fixtures and MockIBClient class for AI-driven autonomous testing.

MockIBClient is an explicit class (not unittest.mock) so that AI agents can
inspect return values, state transitions, and order flows without opaque
mock internals. It mirrors the native ``IBClient`` surface (ib_client.py):
``place_order`` returns a real event-driven ``OrderHandle``, ``subscribe_tick``
returns a real ``TickStream``, and account data uses the real
``AccountValue``/``PortfolioItem``/``ExecutionRecord`` records.
"""

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

# Reuse the real bridge types — never re-implement them here.
from ib_client import (  # noqa: E402  (sys.path insert above)
    AccountValue,
    ExecutionRecord,
    Greeks,
    OrderHandle,
    PortfolioItem,
    SecDefOptParams,
    TickStream,
    _PENDING_STATUSES,
    _TERMINAL_STATUSES,
)


# ---------------------------------------------------------------------------
# Lightweight stand-ins for ibapi data classes used by ported tests
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
class MockContractDetails:
    minTick: float = 0.05
    contract: Any = field(default_factory=lambda: MockContract())


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


# ---------------------------------------------------------------------------
# MockIBClient — explicit class implementing the native IBClient surface used
# by order_manager, account_manager, chain_fetcher, and other modules
# ---------------------------------------------------------------------------

class MockIBClient:
    """Mock bridge implementing the native ``IBClient`` surface.

    Parameters
    ----------
    connected : bool
        Whether isConnected() returns True.
    fill_immediately : bool
        If True, placed orders get status='Filled' immediately.
        If False, orders stay in 'Submitted'.
    reject : bool
        If True, placed orders get status='Cancelled' (simulating IB reject).
    bracket_mode : bool
        If True, child orders (parentId != 0) start as 'PreSubmitted' and
        transition to 'Submitted' on ``simulate_parent_fill``.
    """

    def __init__(self, connected: bool = True,
                 fill_immediately: bool = True,
                 reject: bool = False,
                 bracket_mode: bool = False):
        self.connected = connected
        self._fill_immediately = fill_immediately
        self._reject = reject
        self._bracket_mode = bracket_mode
        self._next_order_id = 100
        self._next_con_id = 10000
        self._next_req_id = 1
        self._placed_orders: List[OrderHandle] = []
        self._cancelled_orders: List[int] = []
        self._bracket_children: Dict[int, List[OrderHandle]] = {}  # parentId → [child handles]
        # Public ``orders`` (used by account_manager) aliases ``_orders``
        # (used internally / by the brief's example body).
        self.orders: Dict[int, OrderHandle] = {}
        self._orders = self.orders
        self._streams: Dict[int, TickStream] = {}
        self.account_values: List[AccountValue] = []
        self.portfolio: List[PortfolioItem] = []
        self.executions: List[ExecutionRecord] = []
        self.account_dirty = False
        self.on_account_dirty = None
        self.call_log: List[Dict] = []  # records every method call for AI analysis

    # -- Connection ----------------------------------------------------------

    def isConnected(self) -> bool:
        return self.connected

    async def connect(self, host, port, client_id=1, timeout=15.0):
        self.call_log.append({"method": "connect", "host": host, "port": port,
                              "client_id": client_id, "timeout": timeout})
        self.connected = True

    def disconnect(self):
        self.call_log.append({"method": "disconnect"})
        self.connected = False

    # -- Contract qualification / one-shot requests ---------------------------

    async def req_contract_details(self, contract):
        """Return one MockContractDetails carrying a fresh conId + minTick.

        The returned contract mirrors the input's symbol/secType/etc. so
        ported tests see the same shape the real bridge produces.
        """
        mc = MockContract(
            conId=self._next_con_id,
            symbol=getattr(contract, "symbol", "SPX"),
            secType=getattr(contract, "secType", "OPT"),
            lastTradeDateOrContractMonth=getattr(contract, "lastTradeDateOrContractMonth", ""),
            strike=getattr(contract, "strike", 0.0),
            right=getattr(contract, "right", ""),
            multiplier=getattr(contract, "multiplier", "100"),
            currency=getattr(contract, "currency", "USD"),
            exchange=getattr(contract, "exchange", "SMART"),
            localSymbol=getattr(contract, "localSymbol", ""),
            tradingClass=getattr(contract, "tradingClass", ""),
            comboLegs=list(getattr(contract, "comboLegs", [])),
        )
        self._next_con_id += 1
        self.call_log.append({"method": "req_contract_details", "symbol": mc.symbol,
                              "secType": mc.secType, "conId": mc.conId})
        return [MockContractDetails(minTick=0.05, contract=mc)]

    async def req_sec_def_opt_params(self, symbol, fut_fop_exchange="", sec_type="OPT", con_id=0):
        """Minimal fake — SPXW (exchange SMART) plus an SPX monthly chain."""
        self.call_log.append({"method": "req_sec_def_opt_params", "symbol": symbol,
                              "fut_fop_exchange": fut_fop_exchange, "sec_type": sec_type,
                              "con_id": con_id})
        return [SecDefOptParams(
            exchange="SMART", tradingClass="SPXW", multiplier="100",
            expirations=["20260410", "20260417", "20260619"],
            strikes=[5000.0, 5100.0, 5200.0, 5300.0, 5400.0, 5500.0],
        ), SecDefOptParams(
            exchange="SMART", tradingClass="SPX", multiplier="100",
            expirations=["20260619"],
            strikes=[5000.0, 5200.0, 5400.0],
        )]

    async def req_historical_bars(self, contract, end_date_time="", duration="1 D",
                                  bar_size="1 min", what_to_show="TRADES",
                                  use_rth=True, format_date=2):
        """Minimal fake — returns no bars by default."""
        self.call_log.append({"method": "req_historical_bars",
                              "symbol": getattr(contract, "symbol", ""),
                              "duration": duration, "bar_size": bar_size,
                              "what_to_show": what_to_show})
        return []

    # -- Market data ---------------------------------------------------------

    def subscribe_tick(self, contract, generic=""):
        """Subscribe a real TickStream.

        Bare quote subscriptions (generic="") seed an immediate quote so the
        dynamic-liquidation mid-price fetch (order_manager._get_mid_price) has
        data to read — mirroring the pre-migration mock's reqMktData. Batch /
        filtered subscriptions (e.g. generic="101") stay tick-free so the mock
        can also exercise "no quote yet" paths.
        """
        req_id = self._next_req_id
        self._next_req_id += 1
        stream = TickStream(req_id, contract)
        if generic == "":
            stream.bid = 3.40
            stream.ask = 3.60
            stream.last = 3.50
            stream._mark(True)
        self._streams[req_id] = stream
        self.call_log.append({"method": "subscribe_tick", "reqId": req_id,
                              "symbol": getattr(contract, "symbol", ""), "generic": generic})
        return stream

    def unsubscribe_tick(self, req_id):
        self.call_log.append({"method": "unsubscribe_tick", "reqId": req_id})
        self._streams.pop(req_id, None)

    async def fetch_snapshot(self, contracts, generic="101", timeout=5.0):
        """Subscribe a batch, seed a minimal quote + greeks, then cancel it.

        The seed mirrors ``subscribe_tick``'s bare-quote seed (bid/ask/last +
        model greeks) so ported ``chain_fetcher`` tests have data to convert
        into ``OptionData``. Unlike ``subscribe_tick`` the streams are not
        marked tick-received (``received_any_tick()`` stays False) — the mock
        returns with data already populated.
        """
        streams = [self.subscribe_tick(c, generic) for c in contracts]
        for s in streams:
            s.bid = 3.40
            s.ask = 3.60
            s.last = 3.50
            s.bid_size = 5
            s.ask_size = 5
            s.model_greeks.update(0.18, 0.5, 0.02, 0.0, 0.0, 3.50, 0.0)
        self.call_log.append({"method": "fetch_snapshot", "count": len(streams),
                              "generic": generic, "timeout": timeout})
        for s in streams:
            self.unsubscribe_tick(s.req_id)
        return streams

    # -- Order placement / lifecycle ------------------------------------------

    def place_order(self, contract, order, order_id=None) -> OrderHandle:
        if order_id is None:
            order_id = self._next_order_id
            self._next_order_id += 1
        order.orderId = order_id
        handle = self.orders.get(order_id)
        if handle is None:
            # Fresh placement
            if self._reject:
                status = "Cancelled"; filled = 0; remaining = float(order.totalQuantity)
            elif self._bracket_mode and getattr(order, "parentId", 0) != 0:
                status = "PreSubmitted"; filled = 0; remaining = float(order.totalQuantity)
            elif self._fill_immediately:
                status = "Filled"; filled = float(order.totalQuantity); remaining = 0
            else:
                status = "Submitted"; filled = 0; remaining = float(order.totalQuantity)
            handle = OrderHandle(order_id, contract, order)
            self._apply_status(handle, status, filled=filled, remaining=remaining,
                               avg_fill_price=float(order.lmtPrice or 3.50))
            self.orders[order_id] = handle
            self._placed_orders.append(handle)
            if getattr(order, "parentId", 0) != 0:
                self._bracket_children.setdefault(order.parentId, []).append(handle)
        else:
            # Modify existing order (same orderId) — update contract/order in
            # place; do NOT append a new handle to _placed_orders.
            handle.contract = contract
            handle.order = order
        self.call_log.append({"method": "place_order", "orderId": order_id,
                              "action": order.action, "totalQuantity": float(order.totalQuantity),
                              "orderType": order.orderType, "lmtPrice": getattr(order, "lmtPrice", None),
                              "auxPrice": getattr(order, "auxPrice", None),
                              "transmit": order.transmit, "parentId": getattr(order, "parentId", 0),
                              "status": handle.status})
        return handle

    def cancel_order(self, order_id):
        """Cancel an order and cascade to its bracket children (real IB behaviour)."""
        self.call_log.append({"method": "cancel_order", "orderId": order_id})
        handle = self.orders.get(order_id)
        if handle is not None:
            self._apply_status(handle, "Cancelled")
            self._cancelled_orders.append(order_id)
        for child in self._bracket_children.get(order_id, []):
            if child.status not in ("Filled", "Cancelled"):
                self._apply_status(child, "Cancelled")
                self._cancelled_orders.append(child.order_id)

    def req_open_orders(self):
        self.call_log.append({"method": "req_open_orders"})

    # -- Account queries -----------------------------------------------------

    def req_account_updates(self, subscribe, account=""):
        self.call_log.append({"method": "req_account_updates",
                              "subscribe": subscribe, "account": account})

    def req_executions(self, exec_filter=None):
        self.call_log.append({"method": "req_executions"})

    # -- Internal helpers -----------------------------------------------------

    def _apply_status(self, handle, status, filled=None, remaining=None, avg_fill_price=None):
        """Mirror IBClient.orderStatus event semantics on an OrderHandle.

        Sets the handle fields and fires the matching ack/fill/terminal events,
        exactly like the real bridge's ``orderStatus`` callback.
        """
        prev_status = handle.status
        handle.status = status
        if filled is not None:
            handle.filled = float(filled)
        if remaining is not None:
            handle.remaining = float(remaining)
        if avg_fill_price is not None:
            handle.avg_fill_price = avg_fill_price
        handle.status_event.set()
        if prev_status == "PreSubmitted" and status != "PreSubmitted":
            handle.activated_event.set()
        if status not in _PENDING_STATUSES:
            handle.ack_event.set()
        if status == "Filled" and handle.remaining <= 0:
            handle.fill_event.set()
        if status in _TERMINAL_STATUSES:
            handle.terminal_event.set()

    # -- Utility for tests ---------------------------------------------------

    def get_placed_orders(self) -> List[OrderHandle]:
        """Return all orders placed during the test."""
        return list(self._placed_orders)

    def get_last_trade(self) -> Optional[OrderHandle]:
        """Return the most recently placed order handle."""
        return self._placed_orders[-1] if self._placed_orders else None

    def get_call_log_json(self) -> str:
        """Return call log as JSON for AI analysis."""
        return json.dumps(self.call_log, indent=2, default=str)

    def set_fill_status(self, order_id: int, status: str = "Filled",
                        filled: float = 0, avg_price: float = 0.0):
        """Manually transition an order's status (for async tests)."""
        handle = self.orders.get(order_id)
        if handle is None:
            return
        remaining = float(getattr(handle.order, "totalQuantity", 0.0)) - filled
        self._apply_status(handle, status, filled=filled,
                           remaining=remaining, avg_fill_price=avg_price)
        self.call_log.append({"method": "set_fill_status", "orderId": order_id,
                              "status": status, "filled": filled, "avg_price": avg_price})

    def simulate_parent_fill(self, parent_id: int, avg_price: float = 0.0):
        """Simulate IB filling a parent order and activating bracket children.

        Transitions parent → Filled (with events) and all bracket children
        PreSubmitted → Submitted (IB's real lifecycle).
        """
        parent = self.orders.get(parent_id)
        if parent is not None:
            total = float(getattr(parent.order, "totalQuantity", 0.0))
            price = avg_price or float(getattr(parent.order, "lmtPrice", None) or 3.50)
            self._apply_status(parent, "Filled", filled=total,
                               remaining=0, avg_fill_price=price)
        for child in self._bracket_children.get(parent_id, []):
            if child.status == "PreSubmitted":
                self._apply_status(child, "Submitted")
        self.call_log.append({"method": "simulate_parent_fill", "parentId": parent_id,
                              "avg_price": avg_price})


# ---------------------------------------------------------------------------
# Pytest fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_ib():
    """A connected MockIBClient that fills immediately."""
    return MockIBClient(connected=True, fill_immediately=True)


@pytest.fixture
def mock_ib_pending():
    """A connected MockIBClient that keeps orders in Submitted."""
    return MockIBClient(connected=True, fill_immediately=False)


@pytest.fixture
def mock_ib_reject():
    """A connected MockIBClient that rejects all orders."""
    return MockIBClient(connected=True, reject=True)


@pytest.fixture
def mock_ib_disconnected():
    """A disconnected MockIBClient."""
    return MockIBClient(connected=False)


@pytest.fixture
def mock_ib_bracket():
    """A connected MockIBClient with bracket order simulation.

    - Parent orders fill immediately.
    - Child orders (parentId != 0) start as PreSubmitted.
    - Use mock_ib_bracket.simulate_parent_fill(id) to transition children.
    - cancel_order cascades to bracket children.
    """
    return MockIBClient(connected=True, fill_immediately=True, bracket_mode=True)


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
# MockWebSocket — captures sent messages for assertion
# ---------------------------------------------------------------------------

class MockWebSocket:
    """Minimal mock WebSocket that records sent messages."""

    def __init__(self):
        self.sent: List[str] = []
        self._closed = False

    async def send_text(self, text: str):
        if self._closed:
            raise RuntimeError("WebSocket closed")
        self.sent.append(text)

    def get_messages(self) -> List[dict]:
        """Return all sent messages parsed as JSON dicts."""
        return [json.loads(s) for s in self.sent]

    def get_order_statuses(self) -> List[dict]:
        """Return only order_status messages' data payloads."""
        return [
            m["data"] for m in self.get_messages()
            if m.get("type") == "order_status"
        ]

    def close(self):
        self._closed = True


@pytest.fixture
def mock_ws():
    """A MockWebSocket for capturing WS messages in tests."""
    return MockWebSocket()


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
