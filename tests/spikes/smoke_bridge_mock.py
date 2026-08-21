"""Throwaway mock-based check of the ported ib_connection functions.

Complements tests/spikes/smoke_bridge.py (live). Verifies the native surface
consumption of connect_ib / setup_spx_subscription / update_spx_es_prices /
setup_es_subscription / fetch_es_baseline / setup_chain_info /
setup_monthly_chain_info against MockIBClient + real TickStream.

Run:  python tests/spikes/smoke_bridge_mock.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from tests.conftest import MockIBClient, MockContract, MockContractDetails
from ibapi.contract import Contract
from ib_client import TickStream
from app_state import AppState
import ib_connection as conn

CHECKS = []
FAILURES = []


def check(name, cond, extra=""):
    CHECKS.append(name)
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {extra}")
        FAILURES.append(name)


class _FutureESMock(MockIBClient):
    """Mock that returns ES contract details with a future expiry."""

    async def req_contract_details(self, contract):
        mc = MockContract(
            conId=50001, symbol="ES", secType="FUT", exchange="CME",
            currency="USD", localSymbol="ESU6",
            lastTradeDateOrContractMonth="20260918",
        )
        return [MockContractDetails(minTick=0.25, contract=mc)]

    async def req_historical_bars(self, contract, end_date_time="", duration="1 D",
                                  bar_size="1 min", what_to_show="TRADES",
                                  use_rth=True, format_date=2):
        from types import SimpleNamespace
        bar = SimpleNamespace(date="20260819 16:20:00", close=6120.50)
        return [bar]


async def main():
    # -- connect ---------------------------------------------------------
    ib = MockIBClient()
    state = AppState()
    await conn.connect_ib(ib, state)
    check("connect_ib sets state.connected", state.connected is True)
    check("connect_ib called ib.connect", any(c["method"] == "connect" for c in ib.call_log))

    # -- spx subscription -------------------------------------------------
    await conn.setup_spx_subscription(ib, state)
    check("state.spx_contract is native Contract IND/SPX/CBOE",
          isinstance(state.spx_contract, Contract)
          and state.spx_contract.symbol == "SPX"
          and state.spx_contract.secType == "IND"
          and state.spx_contract.exchange == "CBOE"
          and state.spx_contract.currency == "USD")
    check("state.spx_stream is TickStream", isinstance(state.spx_stream, TickStream))
    check("spx subscription used generic 233",
          any(c["method"] == "subscribe_tick" and c.get("generic") == "233"
              for c in ib.call_log))
    check("spx_stream.req_id matches subscribe_tick log", any(
        c["method"] == "subscribe_tick" and c["reqId"] == state.spx_stream.req_id
        for c in ib.call_log))

    # -- update_spx_es_prices: RTH path -----------------------------------
    # Mock seeds quotes only for generic=""; seed manually for generic="233".
    state.spx_stream.bid = 6110.25
    state.spx_stream.ask = 6110.30
    state.spx_stream.last = 6110.25
    state.data_mode = "initializing"
    with _patch_rth(True):
        await conn.update_spx_es_prices(state)
    check("RTH: spx_price/live_price set from stream", state.spx_price == 6110.25
          and state.live_price == 6110.25)
    check("RTH: data_mode switched to live", state.data_mode == "live")
    check("RTH: es_derived cleared", state.es_derived is False)

    # -- update_spx_es_prices: off-RTH path --------------------------------
    state.data_mode = "live"
    with _patch_rth(False):
        await conn.update_spx_es_prices(state)
    check("off-RTH: data_mode switched to historical", state.data_mode == "historical")

    # -- ES-derived path ---------------------------------------------------
    state.es_stream = TickStream(99, None)
    state.es_stream.last = 6115.75
    state.es_at_spx_close = 6100.0
    state.spx_last_close = 6090.0
    state.data_mode = "historical"
    state.spx_stream = None  # no live SPX so derivation is the only price
    with _patch_rth(False):
        await conn.update_spx_es_prices(state)
    expected = round(6090.0 * (1.0 + (6115.75 - 6100.0) / 6100.0), 2)
    check("ES-derived: spx_price computed from es delta",
          state.spx_price == expected and state.live_price == expected)
    check("ES-derived: es_derived True", state.es_derived is True)
    check("ES-derived: es_price captured", state.es_price == 6115.75)
    check("ES-derived: es_at_spx_close bootstrapped only if 0",
          state.es_at_spx_close == 6100.0)

    # -- setup_es_subscription ---------------------------------------------
    es_ib = _FutureESMock()
    es_state = AppState()
    await conn.setup_es_subscription(es_ib, es_state)
    check("ES: front-month contract selected", es_state.es_contract is not None
          and es_state.es_contract.localSymbol == "ESU6")
    check("ES: es_stream is TickStream", isinstance(es_state.es_stream, TickStream))
    check("ES: used native FUT contract secType/exchange",
          es_state.es_contract.secType == "FUT" and es_state.es_contract.exchange == "CME")

    # -- fetch_es_baseline ---------------------------------------------------
    es_state.es_at_spx_close = 0.0
    await conn.fetch_es_baseline(es_ib, es_state)
    check("fetch_es_baseline: TRADES bar close captured", es_state.es_at_spx_close == 6120.50)

    # -- empty historical bars falls through to bootstrap warning -----------
    empty_ib = MockIBClient()  # req_historical_bars returns []
    es_state2 = AppState()
    es_state2.es_contract = MockContract(symbol="ES", secType="FUT",
                                         lastTradeDateOrContractMonth="20260918")
    es_state2.es_at_spx_close = 0.0
    await conn.fetch_es_baseline(empty_ib, es_state2)
    check("fetch_es_baseline: no bars leaves baseline 0 (bootstrap)", es_state2.es_at_spx_close == 0.0)

    # -- setup_chain_info / setup_monthly_chain_info -------------------------
    # These two entry points aren't exercised against MockIBClient here
    # (chain_fetcher drives them through the native surface; see test_chain_fetcher).
    print("  SKIP  setup_chain_info / setup_monthly_chain_info"
          " (chain_fetcher ported in Task 12)")

    print()
    print(f"RESULT: {len(CHECKS) - len(FAILURES)}/{len(CHECKS)} passed")
    if FAILURES:
        print("FAILURES:", FAILURES)
        sys.exit(1)
    print("ALL CHECKS PASSED")


class _patch_rth:
    """Temporarily force is_within_rth() to return a fixed value."""

    def __init__(self, value):
        self.value = value
        self.orig = conn.is_within_rth

    def __enter__(self):
        conn.is_within_rth = lambda: self.value

    def __exit__(self, *exc):
        conn.is_within_rth = self.orig


if __name__ == "__main__":
    asyncio.run(main())
