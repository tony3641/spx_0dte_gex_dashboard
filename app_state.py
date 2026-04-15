"""
Shared application state container.

Extracted from server.py so every module can import and type-check against
AppState without pulling in the entire server.
"""

import asyncio
from collections import deque
from typing import Dict, List, Optional, Set

from gex_calculator import GEXResult, OptionData


class AppState:
    """Mutable singleton holding all runtime state for the dashboard."""

    def __init__(self):
        # SPX index
        self.spx_contract = None          # Optional[Contract]
        self.spx_price: float = 0.0       # latest known SPX price (live or historical)
        self.live_price: float = 0.0      # latest live streaming price (0 when not streaming)
        self.price_history: deque = deque(maxlen=28800)  # OHLC bars (1-min)

        # GEX
        self.latest_gex: Optional[dict] = None
        self.gex_result: Optional[GEXResult] = None

        # Chain parameters
        self.expiration: str = ""
        self.expirations: List[str] = []
        self.strikes: List[float] = []

        # Connection
        self.connected: bool = False
        self.chain_fetching: bool = False
        self.last_chain_update: str = ""
        self.ws_clients: set = set()     # Set[WebSocket]
        self.background_tasks: List[asyncio.Task] = []

        # Mode tracking
        self.data_mode: str = "initializing"  # "live" | "historical" | "initializing"
        self.historical_date: str = ""

        # Volatility
        self.annual_vol: float = 0.20
        self.risk_free_rate: float = 0.043

        # ES futures (off-hours derived SPX price)
        self.es_contract = None           # Optional[Contract]
        self.es_price: float = 0.0
        self.es_at_spx_close: float = 0.0
        self.spx_last_close: float = 0.0
        self.es_derived: bool = False

        # Option chain data for Tab 2
        self.chain_data: List[OptionData] = []
        self.chain_quotes_cache: Optional[dict] = None
        self.chain_fetch_active: Optional[asyncio.Event] = None
        self.chain_stream_tickers: dict = {}
        self.chain_stream_contracts: dict = {}
        self.chain_stream_unknown_keys: set = set()
        self.force_chain_fetch_event: Optional[asyncio.Event] = None
        self.active_tab: str = "dashboard"
        self.manual_refresh_requested: bool = False
        self.viewport_center_strike: float = 0.0
        self.viewport_center_last_ts: float = 0.0

        # Monthly GEX (Dashboard toggle: 0DTE ↔ Monthly)
        self.gex_mode: str = "0dte"                   # "0dte" | "monthly"
        self.monthly_expiration: str = ""
        self.monthly_expirations: List[str] = []
        self.monthly_strikes: List[float] = []
        self.monthly_gex_result: Optional[GEXResult] = None
        self.monthly_latest_gex: Optional[dict] = None
        self.monthly_chain_data: List[OptionData] = []
        self.monthly_last_fetch_ts: float = 0.0       # time.monotonic()

        # Account / portfolio / orders (Tab 3)
        self.account_summary: dict = {}
        self.positions: List[dict] = []
        self.open_orders: List[dict] = []
        self.executions: List[dict] = []
        self.account_dirty: bool = False
        self.active_trades: dict = {}     # {orderId: Trade}


def create_app_state() -> AppState:
    """Factory for clean AppState instances (useful in tests)."""
    return AppState()
