"""Throwaway latency probe: native ibapi vs ib_insync (paper, TWS 7497).

Spike — measures three round-trip latencies against TWS paper (127.0.0.1:7497):
  1. connect -> nextValidId (native) / connect -> ready (ib_insync)
  2. reqContractDetails
  3. placeOrder -> orderStatus leaving PendingSubmit/Submitted

Run native probe:  python tests/spikes/spike_native_latency.py
Run ib_insync:     python tests/spikes/spike_native_latency.py --insync

Nothing from this file is wired into the app. Throwaway code.
"""
import sys
import threading
import time

sys.path.insert(0, r"C:\TWS API\source\pythonclient")
from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract
from ibapi.order import Order
from ibapi.order_cancel import OrderCancel

HOST = "127.0.0.1"
PORT = 7497

# Window before we abandon the far-OTM SPXW qual and fall back to a liquid name.
QUAL_TIMEOUT = 5.0
# Upper bound on the whole run (qualification + order + orderStatus). The brief's
# time.sleep(8) was raised to this so the 3 latencies always fit inside.
TOTAL_TIMEOUT = 12.0

# Contracts: a buy-limit far below market that can never fill (paper).
#   SPXW far-OTM call at strike 100 — likely unlisted, hence the AAPL fallback.
#   AAPL ~$200 -> resting GTC buy-limit at $5.00 can never trade.
SPXW_LMT_PRICE = 1.0
AAPL_LMT_PRICE = 5.0


class Probe(EWrapper, EClient):
    """Native ibapi latency probe (clientId 99)."""

    def __init__(self):
        EClient.__init__(self, self)
        self.t0 = time.perf_counter()
        self.nvi_ms = None
        self.details_ms = None
        self.order_ms = None
        self.cancel_ms = None
        self.order_t0 = None
        self.req_t0 = None
        self.order_id = 0
        self.qual_con_id = None
        self.qual_kind = None
        self.expected_req = 1
        self.using_fallback = False
        self.order_placed = False
        self.done = False
        self._lock = threading.Lock()

    def _now_ms(self):
        return (time.perf_counter() - self.t0) * 1000

    # ---- connection / request flow -----------------------------------------

    def nextValidId(self, orderId):
        self.nvi_ms = self._now_ms()
        self.order_id = orderId
        print(f"[native] connect->nextValidId: {self.nvi_ms:.1f} ms")
        # qualify a far-OTM SPXW contract (100 strike, never traded)
        threading.Thread(target=self._watchdog_spxw, daemon=True).start()
        c = Contract()
        c.symbol = "SPX"
        c.secType = "OPT"
        c.exchange = "SMART"
        c.currency = "USD"
        c.lastTradeDateOrContractMonth = "20260918"
        c.strike = 100.0
        c.right = "C"
        c.multiplier = "100"
        c.tradingClass = "SPXW"
        self.req_t0 = time.perf_counter()
        EClient.reqContractDetails(self, 1, c)

    def _watchdog_spxw(self):
        time.sleep(QUAL_TIMEOUT)
        with self._lock:
            if self.qual_con_id is None and not self.using_fallback:
                print("[native] SPXW qual timed out — falling back to AAPL")
                self._fallback_to_aapl()

    def _fallback_to_aapl(self):
        self.using_fallback = True
        self.expected_req = 2
        threading.Thread(target=self._watchdog_aapl, daemon=True).start()
        c = Contract()
        c.symbol = "AAPL"
        c.secType = "STK"
        c.exchange = "SMART"
        c.currency = "USD"
        self.req_t0 = time.perf_counter()
        EClient.reqContractDetails(self, 2, c)

    def _watchdog_aapl(self):
        time.sleep(QUAL_TIMEOUT)
        with self._lock:
            if self.qual_con_id is None:
                print("[native] AAPL qual also timed out — no order placed")

    def contractDetails(self, reqId, cd):
        if reqId != self.expected_req:
            return
        with self._lock:
            if self.qual_con_id is None:
                self.details_ms = (time.perf_counter() - self.req_t0) * 1000
                self.qual_con_id = cd.contract.conId
                self.qual_kind = "AAPL" if self.using_fallback else "SPXW"
                print(f"[native] reqContractDetails: {self.details_ms:.1f} ms "
                      f"(conId={self.qual_con_id}, {self.qual_kind})")

    def contractDetailsEnd(self, reqId):
        with self._lock:
            if self.qual_con_id is None and not self.using_fallback:
                print("[native] no SPXW details (strike 100 not listed) — falling back to AAPL")
                self._fallback_to_aapl()
            elif self.qual_con_id is not None and not self.order_placed:
                self._place_order()

    # ---- order flow ---------------------------------------------------------

    def _place_order(self):
        self.order_placed = True
        lmt = AAPL_LMT_PRICE if self.using_fallback else SPXW_LMT_PRICE
        c = Contract()
        c.conId = self.qual_con_id
        c.exchange = "SMART"
        c.currency = "USD"  # conId suffices; no symbol needed
        o = Order()
        o.action = "BUY"
        o.totalQuantity = 1
        o.orderType = "LMT"
        o.lmtPrice = lmt
        o.tif = "GTC"
        o.transmit = True
        self.order_t0 = time.perf_counter()
        print(f"[native] placing BUY 1 LMT ${lmt} GTC (conId={self.qual_con_id})")
        EClient.placeOrder(self, self.order_id, c, o)

    def orderStatus(self, orderId, status, filled, remaining, avgFillPrice, permId,
                    parentId, lastFillPrice, clientId, whyHeld, mktCapPrice):
        if status in ("Submitted", "PreSubmitted") and self.order_ms is None:
            self.order_ms = (time.perf_counter() - self.order_t0) * 1000
            self.done = True
            print(f"[native] placeOrder->orderStatus({status}): {self.order_ms:.1f} ms")
            EClient.cancelOrder(self, orderId, OrderCancel())
        elif status in ("Cancelled", "Canceled", "PendingCancel") and self.cancel_ms is None:
            self.cancel_ms = (time.perf_counter() - self.order_t0) * 1000
            print(f"[native] orderStatus({status}) after cancel: {self.cancel_ms:.1f} ms")

    # ---- misc ---------------------------------------------------------------

    def error(self, reqId, errorTime, errorCode, errorString, advancedOrderRejectJson=""):
        # 2104/2106 are connection notices; 200 = no security definition (expected for
        # an unlisted SPXW strike). Print everything for transparency.
        print(f"[native] error(reqId={reqId}, code={errorCode}): {errorString}")
        if errorCode == 200 and reqId == 1 and not self.using_fallback:
            with self._lock:
                if self.qual_con_id is None and not self.using_fallback:
                    print("[native] no SPXW details (strike 100 not listed) — falling back to AAPL")
                    self._fallback_to_aapl()
        elif errorCode == 200 and reqId == 2:
            print("[native] AAPL qualification also failed — no order placed")


def main_native():
    app = Probe()
    print("[native] connecting...")
    app.connect(HOST, PORT, clientId=99)
    threading.Thread(target=app.run, daemon=True).start()
    deadline = time.time() + TOTAL_TIMEOUT
    while time.time() < deadline and not app.done:
        time.sleep(0.1)
    app.disconnect()
    print("[native] disconnected")
    if app.order_ms is None:
        print(f"[native] WARNING: orderStatus never observed within {TOTAL_TIMEOUT:.0f}s "
              f"(qual_con_id={app.qual_con_id})")


def main_insync():
    from ib_insync import IB, Option, Stock, LimitOrder

    ib = IB()
    ib.requestTimeout = 10
    t0 = time.perf_counter()
    ib.run(ib.connectAsync(HOST, PORT, clientId=98))  # completes after nextValidId
    nvi_ms = (time.perf_counter() - t0) * 1000
    print(f"[insync] connect->nextValidId: {nvi_ms:.1f} ms")

    # qualify a far-OTM SPXW contract (100 strike, never traded)
    t = time.perf_counter()
    c = Option("SPX", "20260918", 100.0, "C", "SMART",
               multiplier="100", tradingClass="SPXW")
    details = ib.run(ib.reqContractDetailsAsync(c))
    fallback = False
    if not details:
        print("[insync] no SPXW details (strike 100 not listed) — falling back to AAPL")
        t = time.perf_counter()
        c = Stock("AAPL", "SMART", "USD")
        details = ib.run(ib.reqContractDetailsAsync(c))
        fallback = True
    details_ms = (time.perf_counter() - t) * 1000
    contract = details[0].contract if details else None
    kind = "AAPL" if fallback else "SPXW"
    if contract is None:
        print(f"[insync] reqContractDetails: {details_ms:.1f} ms — NO details, aborting")
        ib.disconnect()
        return
    print(f"[insync] reqContractDetails: {details_ms:.1f} ms (conId={contract.conId}, {kind})")

    # place a GTC buy-limit far below market (never fills), then cancel
    lmt = AAPL_LMT_PRICE if fallback else SPXW_LMT_PRICE
    order = LimitOrder("BUY", 1, lmt, tif="GTC")
    t = time.perf_counter()
    trade = ib.placeOrder(contract, order)
    deadline = time.time() + TOTAL_TIMEOUT
    status = trade.orderStatus.status
    while time.time() < deadline and status in ("PendingSubmit", ""):
        ib.sleep(0.05)  # pumps ib_insync's event loop so orderStatus events are processed
        status = trade.orderStatus.status
    order_ms = (time.perf_counter() - t) * 1000
    print(f"[insync] placeOrder->orderStatus({status}): {order_ms:.1f} ms")
    if status in ("PendingSubmit", ""):
        print("[insync] WARNING: orderStatus never left PendingSubmit within "
              f"{TOTAL_TIMEOUT:.0f}s")
    ib.cancelOrder(trade.order)
    ib.sleep(0.5)
    ib.disconnect()
    print("[insync] disconnected")


if __name__ == "__main__":
    if "--insync" in sys.argv:
        main_insync()
    else:
        main_native()
