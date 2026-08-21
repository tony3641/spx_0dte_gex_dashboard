# Design — Migrate broker access from `ib_insync` to the native IB Python API (TWS API)

**Date:** 2026-08-20
**Status:** Approved for implementation planning

## 1. Problem statement

The SPX 0DTE GEX Dashboard talks to Interactive Brokers through `ib_insync` (an
asyncio wrapper over the TWS API wire protocol). The user reports unacceptable
latency in two areas:

1. **Order flow** — place → acknowledgment → fill feedback.
2. **Data fetching** — the full-chain option snapshot refresh.

Because this is an options-trading framework, **API latency is the primary
success criterion.** The user wants to move to the native Python API
(`ibapi`, installed at `C:\TWS API\source\pythonclient`) in the expectation
that it is faster.

### Reality check (why native helps)

`ib_insync` speaks the same wire protocol as `ibapi`, so native is not
automatically faster. The genuine wins come from four changes, enabled by a
native bridge:

- **Event-driven order acknowledgment.** Today `await_order_status` polls
  `trade.orderStatus.status` every 100 ms and re-queries `reqOpenOrders()` on a
  slow path (`order_manager.py`). Native `orderStatus` callbacks can resolve an
  `asyncio.Event` instantly.
- **First-tick snapshot completion.** The chain fetch sleeps a fixed **12 s per
  batch** (`chain_fetcher.py:269`) plus 0.5 s between batches — the bulk of the
  ~60 s refresh — regardless of library. A native bridge can cancel each
  subscription the moment its first tick arrives.
- **Socket-thread decoupling.** `ib_insync` processes all IB messages on the
  same asyncio loop as FastAPI and the WebSocket broadcasts, so broadcast load
  delays inbound ticks. A dedicated socket thread removes that contention.
- **No keepalive pump.** `ib_keepalive_loop` (`ws_handler.py`, `ib.sleep(0.1)`)
  is deleted; the socket thread reads continuously.

## 2. Goals / non-goals

### Goals

1. Replace every `ib_insync` usage with the native `ibapi` client.
2. Reduce order place→ack→fill latency (event-driven, zero polling).
3. Reduce full-chain snapshot refresh time (first-tick completion).
4. Decouple IB message processing from the FastAPI asyncio loop.
5. Keep the app's business logic (GEX math, chain building, WS payloads) intact.
6. Keep the test suite: `MockIB` is reworked to implement the new surface so
   existing tests port rather than get deleted.

### Non-goals

- No dual backend: this is a **hard cut**. `ib_insync` is removed from
  `requirements.txt` and the codebase once the native driver passes parity.
- No change to the frontend, GEX computation, or the WebSocket message
  protocol. The module boundaries (`ib_connection`, `chain_fetcher`,
  `chain_manager`, `order_manager`, `account_manager`, `price_bars`,
  `ws_handler`, `server.py`) are preserved.
- IB's server-side pacing limits (e.g., 100 concurrent market-data lines) are
  not lifted; we respect them.

## 3. Latency contract (design rules)

1. **Event-driven inbound, zero polling.** Every IB callback resolves an
   asyncio `Future`/`Event` via `loop.call_soon_threadsafe` the instant it
   arrives.
2. **One socket thread does the minimum.** It parses, writes into a shared
   object, and signals. Serialization, GEX math, and WS broadcast stay on the
   asyncio loop.
3. **First-tick snapshot completion.** Chain-fetch subscriptions are cancelled
   as soon as their data is in.
4. **Three bridge patterns, chosen by data shape:**
   - *One-shot requests* (contract details, historical bars, sec-def-opt-params)
     → `asyncio.Future` per reqId, resolved by the matching `…End` callback.
   - *Continuous streams* (SPX/ES/chain quotes) → persistent per-reqId
     `TickStream` object updated on the socket thread; asyncio reads it at its
     own cadence. No per-tick Future (would churn the loop).
   - *Discrete events* (order status, account dirty) → `asyncio.Event` /
     dirty flag, set thread-safely.

## 4. Architecture — `ib_client.py`

One new module subclasses `EWrapper` + `EClient`.

```python
class IBClient(EWrapper, EClient):
    def __init__(self):
        EClient.__init__(self, self)
        self._loop = None                    # asyncio loop, captured at connect
        self._req_id = itertools.count(1)
        self._next_order_id: Optional[int] = None
        self._account_code: Optional[str] = None
        self._futures: dict[int, asyncio.Future]   = {}   # one-shot requests
        self._streams: dict[int, TickStream]        = {}   # persistent quote streams
        self._orders:   dict[int, OrderHandle]      = {}   # placed orders
```

### 4.1 Threading model

- `connect(host, port, client_id)` calls `EClient.connect(...)`, captures the
  running asyncio loop, and spawns `threading.Thread(target=self.run, daemon=True)`.
- All `EWrapper` callbacks execute on that socket thread.
- All `EClient` request methods (`placeOrder`, `reqMktData`, …) are called from
  the asyncio thread; they are internally lock-protected, so this is safe.
- Futures are created on the asyncio loop and resolved via
  `loop.call_soon_threadsafe(future.set_result, ...)` / `event.set()`.
- `TickStream`/`OrderHandle` mutable state is written on the socket thread and
  read from asyncio; GIL makes field reads/writes atomic, no extra locks.
- Disconnect: `EClient.disconnect()` from asyncio; the `run()` loop exits; the
  thread ends. Reconnect creates a **fresh `IBClient`** (like today's
  `server.py` reconnect, which rebuilds state).

### 4.2 Connection sequence

1. `connect(host, port, client_id)` → spawn socket thread.
2. Wait (with timeout) for the `nextValidId` callback; store
   `self._next_order_id`. This happens **during connect** so order submission is
   never blocked waiting for an ID.
3. Capture `self._account_code` from `managedAccounts`.

### 4.3 reqId registry

Monotonic counter (`itertools.count`). Every request/source gets a unique id;
callbacks dispatch purely by reqId, unambiguous under hundreds of concurrent
streams.

### 4.4 Exposed async surface

| Method | Backing callback(s) |
|---|---|
| `connect(...)` / `disconnect()` | `nextValidId`, `managedAccounts` |
| `req_contract_details(contract) -> list[ContractDetails]` | `contractDetails`, `contractDetailsEnd` |
| `req_sec_def_opt_params(...)` | `securityDefinitionOptionParameter(…End)` |
| `req_historical_bars(contract, ...) -> list[BarData]` | `historicalData`, `historicalDataEnd` |
| `subscribe_tick(contract, generic) -> TickStream` | `tickPrice`, `tickSize`, `tickOptionComputation` |
| `unsubscribe_tick(req_id)` | `cancelMktData` |
| `fetch_snapshot(contracts, generic, criteria, timeout)` | `tickPrice`, `tickSize`, `tickOptionComputation` |
| `place_order(contract, order) -> OrderHandle` | `orderStatus`, `openOrder`, `execDetails` |
| `cancel_order(order_id)` | `orderStatus` (cancel confirmation) |
| `req_open_orders()` | `openOrder`, `orderStatus` |
| `req_account_updates(subscribe, account)` | `updateAccountValue`, `updatePortfolio`, `accountDownloadEnd` |
| `req_executions(exec_filter)` | `execDetails`, `commissionReport` |
| `error(...)` (callback) | forwarded to WS error handler |

## 5. Order flow (priority #1)

### 5.1 `OrderHandle`

```python
class OrderHandle:
    order_id, perm_id, parent_id
    status, filled, remaining, avg_fill_price, last_fill_price
    ack_event:  asyncio.Event   # set when status leaves PendingSubmit/ApiPending
    fill_event: asyncio.Event   # set on execDetails / status == "Filled"
    status_event: asyncio.Event # set on ANY status change (WS pushes)
```

### 5.2 Path

1. `place_order(contract, order)` allocates `order_id` from the connect-time
   counter and calls `EClient.placeOrder(order_id, contract, order)`.
2. `orderStatus(orderId, status, filled, remaining, avgFillPrice, permId,
   parentId, lastFillPrice, clientId, whyHeld, mktCapPrice)` updates the handle
   and sets `ack_event`/`status_event` immediately.
3. `execDetails` updates fills and sets `fill_event`.
4. **No polling, no `reqOpenOrders()` re-poll.**

### 5.3 Module changes (`order_manager.py`)

- `await_order_status` (100 ms poll) → `await handle.ack(timeout)`.
- `watch_and_push_status` → task that `await`s `status_event` and broadcasts the
  `order_status` WS message (same payload shape).
- Bracket stop-loss: identical construction (`STP LMT`, `parentId`); monitoring
  uses the same event-driven handles. `watch_parent_and_cancel_child` becomes
  `await parent.status_event`-driven.
- Dynamic-fill reprice: `asyncio.wait_for(handle.fill_event.wait(),
  timeout=reprice_interval)`; reprice on timeout; exit on fill/cancel.
- `cancel_order`: `EClient.cancelOrder(order_id)` — note native takes only the
  id (not `trade.order`).
- Contract qualification: `req_contract_details` per contract before placement
  (as today via `qualifyContractsAsync`).
- `handle_cancel_order`, `active_trades`, `open_orders` reworked to hold
  `OrderHandle`s.

## 6. Market data

### 6.1 `TickStream` (per reqId)

```
bid, ask, last, high, low, close
bid_size, ask_size, last_size, volume
call_oi, put_oi, open_interest
bid_greeks / ask_greeks / last_greeks / model_greeks
implied_volatility
```

### 6.2 Callback → field mapping (verified against local `ibapi`)

| Callback | Tick type | Field |
|---|---|---|
| `tickPrice` | 1 / 2 / 4 | bid / ask / last |
| `tickPrice` | 6 / 7 / 9 | high / low / close |
| `tickPrice` | 24 | implied volatility (generic `'106'`) |
| `tickSize` | 0 / 3 / 5 | bid_size / ask_size / last_size |
| `tickSize` | 8 | volume |
| `tickSize` | 27 / 28 | call_oi / put_oi (generic `'101'`) |
| `tickSize` | 22 | open_interest |
| `tickOptionComputation` | 10 | bid_greeks |
| `tickOptionComputation` | 11 | ask_greeks |
| `tickOptionComputation` | 12 | last_greeks |
| `tickOptionComputation` | 13 | model_greeks |

IB sends `-1.0` / `1.7976931348623157e+308` for unavailable values — normalized
to `None`. Existing `_safe_float` / `-1`-sentinel logic carries over.

`tickOptionComputation` supplies `impliedVol, delta, gamma, vega, theta,
optPrice, undPrice` per greek bucket.

### 6.3 SPX / ES

`subscribe_tick(contract, generic='233')` once each; `ib_connection` reads the
streams. `make_pending_tickers_handler` becomes a pure read of the SPX/ES
streams at loop cadence — same RTH / ES-derived logic, no `pendingTickersEvent`
wiring.

### 6.4 Chain stream (`chain_manager.chain_stream_loop`)

Unchanged subscription-set management (add/remove by strike, viewport
centering) but `state.chain_stream_tickers[key]` holds `TickStream`s. Greeks
extraction keeps the model→last→bid→ask fallback (`_extract_stream_greeks`).

### 6.5 Snapshot fetch (`chain_fetcher.py`)

`fetch_snapshot(contracts, generic='101', ...)`:

1. Subscribe a batch (cap ~50 requests to stay safely under the 100-line
   pacing; iterate batches).
2. **First-tick completion:** a completion counter decrements as each
   `TickStream` receives its first relevant tick (quote, greek, or OI), with a
   short per-batch grace window (~1–2 s, configurable) so greeks/OI land before
   cancel.
3. Cancel the batch immediately on completion (or a global timeout fallback).

The fixed 12 s sleep is removed. Qualification-cache logic (conId-keyed) and
strike filtering are unchanged.

### 6.6 Historical bars / volatility (`price_bars.py`)

`req_historical_bars` accumulates `historicalData` chunks until
`historicalDataEnd`, returns `list[BarData]`. `compute_annual_vol` and
`fetch_historical_bars` only swap the call; bar→state logic untouched.

### 6.7 sec-def-opt-params

`get_chain_params` / `get_monthly_chain_params` wrap
`securityDefinitionOptionParameter(…End)`; same SPXW/SPX filtering and
return shape.

## 7. Account (`account_manager.py`)

The ib_insync *event subscriptions* (`updatePortfolioEvent += …`, etc.) are
replaced by bridge callbacks writing state + setting an internal dirty flag
thread-safely:

| Native callback | State written |
|---|---|
| `updateAccountValue(key, val, currency, accountName)` | `account_summary` (same key filter, USD only) |
| `updatePortfolio(contract, position, marketPrice, marketValue, averageCost, unrealizedPNL, realizedPNL, accountName)` | `positions` |
| `openOrder` / `orderStatus` | `open_orders` + `active_trades` (`OrderHandle`s) |
| `execDetails` + `commissionReport` | `executions` |
| `accountDownloadEnd` | signals a complete snapshot |

- `account_push_loop` unchanged (polls dirty flag, broadcasts).
- `serialize_trade` → `serialize_order_handle`.
- `parse_execution_time` drops the `ib_insync` import; keep the regex fallback.
- Account tab re-requests `req_open_orders` / `req_executions` on demand.

## 8. Error handling (`ws_handler.py`)

- `error(reqId, errorCode, errorString, advancedOrderRejectJson)` replaces
  `ib.errorEvent`.
- Same `_IGNORED_IB_ERROR_CODES` set; contract resolved from reqId→`OrderHandle`.
- `advancedOrderRejectJson` surfaced in the log.
- `ib_keepalive_loop` is **deleted**.

## 9. Connection lifecycle (`server.py`)

- `ib = IBClient()` replaces `ib = IB()`.
- `connect_ib` → `await ib.connect(...)`; `setup_*` functions call the new
  surface.
- Reconnect endpoint: disconnect, create a fresh `IBClient`, re-run
  `setup_spx_subscription` / `setup_chain_info` / `setup_monthly_chain_info`,
  cancel/re-establish stream subscriptions (as today).
- Shutdown: cancel background tasks, `await ib.disconnect()`.

## 10. Tests

- `MockIB` → `MockIBClient` implementing the new async surface:
  `req_contract_details`, `subscribe_tick`, `fetch_snapshot`, `place_order →
  OrderHandle` (existing immediate-fill / pending / reject / bracket modes),
  `cancel_order`, `req_open_orders`, account callbacks, `req_historical_bars`,
  `req_sec_def_opt_params`.
- `call_log`, fixtures, and `MockWebSocket` carry over.
- Port: `test_order_placement`, `test_account_manager`, `test_chain_fetcher`,
  `test_ws_handler`, `test_config`, `test_market_hours`, `test_risk_free`.
- New tests assert **event-driven** semantics (status set via callback → event
  resolves; no polling).

## 11. Dependency

- `ibapi` is not on PyPI under a stable name. Install from local source:
  `pip install -e "C:\TWS API\source\pythonclient"` (add as a requirements line
  + setup note).
- `ib_insync` removed from `requirements.txt` at cutover.
- Python 3.10 (per README) is supported by `ibapi`.

## 12. Milestones (each ends green + smoke-tested in paper)

1. **Spike:** socket thread connects, captures `nextValidId`, measures
   `orderStatus` latency vs `ib_insync` — validates the premise before the full
   build.
2. `ib_client.py` bridge: connect/disconnect, contract details, historical bars.
3. **Order flow** (priority): `OrderHandle`, place/cancel/ack/fill, brackets,
   dynamic-fill.
4. Market data: SPX/ES streams, snapshot fetch, chain stream, greeks.
5. Account: values, positions, open orders, executions.
6. `server.py` wiring, reconnect, shutdown; WS error path; delete keepalive.
7. Tests ported + parity; full suite green.
8. **Hard cut:** remove `ib_insync` everywhere.

## 13. Risks

- **reqId→future correlation under load** is the highest-risk area; the design
  confines it to `ib_client.py` and covers it with dedicated tests.
- **Hard cut** means no rollback; Milestone-1 spike and paper smoke tests
  mitigate.
- **Tick-type subtleties** (greek buckets, OI sentinels) are the most likely
  source of silent wrong-data bugs; covered by `MockTicker` unit tests and
  manual chain-tab verification.
