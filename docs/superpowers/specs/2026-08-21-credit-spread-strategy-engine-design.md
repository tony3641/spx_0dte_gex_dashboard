# Design — User-Defined Credit-Spread Trading Strategy Engine

**Date:** 2026-08-21
**Status:** Approved for implementation planning

## 1. Problem statement

The SPX 0DTE GEX Dashboard already has a full option-trading framework: a
native `ibapi` broker bridge (`ib_client.py`), a tested order-execution layer
(`order_manager.py`) that places single-leg and multi-leg **BAG** combo orders
with optional stop-limit brackets, live streaming chain data with greeks
(`chain_manager.py`, `gex_calculator.py`), SPX 1-minute bars (`price_bars.py`),
and account/position streaming (`account_manager.py`).

The user wants a new capability: **let a user define an option-trading strategy
(credit spreads prioritized) by composing entry and exit conditions, and have
that strategy drive trade selection and management.** Today the "Strategy
Builder" is a disposable legs/pricing widget in the frontend — it hand-picks
legs, computes combo credit/delta/payoff, and submits a `place_order` payload.
There is **no strategy model, no condition engine, no scanning, no strategy
persistence, and no position management toward an exit** on the backend.

## 2. Goals

1. Let a user define, save, and reuse a credit-spread strategy as an **ordered
   set of entry conditions** plus exit rules.
2. Evaluate entry conditions against live in-process data (**no new data
   fetching** for most conditions) on a cadence.
3. Support **two behavior modes per strategy**: a **scanner** (surface matching
   candidates + a human-confirmed order ticket) and **fully automated**
   (auto-place on all conditions met, auto-manage the exit).
4. Prioritize **credit spreads**: Bull Put (sell put / buy lower put) and Bear
   Call (sell call / buy higher call).
5. Manage exits: **stop-loss = existing IB stop-limit bracket**; **take-profit =
   a server-side loop that flattens the position at a target**; plus hold-to-expire.
6. Drive VIX and ATM option IV as two independent, optional volatility conditions.

## 3. Non-goals

- **No backtesting / historical simulation.**
- **No other expiries beyond 0DTE SPXW** for the MVP (monthly is a deferred
  extension; the engine reads per-expiry chain data so it can be added later).
- **No multi-leg strategies beyond 2-leg vertical credit spreads** (no iron
  condors, butterflies, etc.) in the MVP.
- **No indicators beyond RSI and %-move** for the intraday-trend condition.
- **No new persistence technology** (strategies live in a JSON file, no DB).

## 4. Strategy model

A strategy is a saved, named blueprint. Stored as JSON in
`config/strategies.json` (no DB — matches the project). In-memory on
`AppState`.

```python
@dataclass
class Strategy:
    name: str
    direction: str                    # 'bull_put' | 'bear_call'
    entry_conditions: list[Condition] # ordered; priority == list order (min 1)
    exit_rules: ExitRules
    auto_execute: bool = False        # off by default
    armed: bool = False               # auto only fires when armed
    target_expiry: str = ""           # SPXW expiry (defaults to current 0DTE)
```

**Priority semantics (user-confirmed):** conditions are a **hard AND chain,
evaluated only in the given order, short-circuiting**. If a higher-priority
condition fails, the engine stops and reports that condition as the current
blocker — it does not evaluate the remaining conditions. All enabled conditions
must pass for an order to go in. There is **no weighting / scoring / threshold**.

```python
@dataclass
class Condition:
    kind: str                         # see table §5
    enabled: bool = True
    params: dict[str, Any] = field(default_factory=dict)
```

```python
@dataclass
class ExitRules:
    take_profit: TakeProfit | None    # server-side flatten target
    stop_loss: StopLoss | None        # IB stop-limit bracket (existing)
    hold_to_expire: bool = False      # close-by rule or hold to expiry
```

Take-profit / stop-loss are both optional; at least one is recommended (though a
naked "hold to expire" strategy is allowed).

## 5. Entry conditions

Each condition reads only from already-live `AppState` (no new IO), so
evaluation is fast and unit-testable. `kind` maps to a small isolated evaluator.

| # | `kind` | Reads from | `params` | Pass when |
|---|---|---|---|---|
| C1 | `entry_window` | `market_hours` ET clock | `start`, `end` (HH:MM) | now_et within [start, end] |
| C2 | `short_delta` | chain greeks (`abs(delta)`) | `min`, `max` | short-leg `abs(delta)` in [min,max] |
| C3 | `spread_width` | candidate construction | `min`, `max` | width in points in [min,max] (margin = width × 100) |
| C4 | `credit` | combo credit (reuse existing net-credit calc) | `min`, `max` ($/contract) | net credit in [min,max] |
| C5 | `trend` | `state.price_history` closes | `indicator` ∈ {`rsi`, `pmove`}, `op` ∈ {`above`,`below`,`range`}, thresholds | indicator vs op/threshold |
| C6 | `volatility` | **VIX feed** + **chain ATM IV** | `vix_enabled`, `vix_op`, `vix_*`; `atm_iv_enabled`, `atm_iv_op`, `atm_iv_*` | each enabled sub-factor passes; at least one enabled |

Notes:

- **C5 `rsi`**: Wilder's RSI over a rolling window of closes (e.g. `period=14`).
  `pmove` = `(last_close − close_minutes_ago) / close_minutes_ago × 100`, where
  `close_minutes_ago` is the close `minutes` bars back in the 1-minute series
  (`params.minutes`, default 5).
- **C6 is the only split condition.** Two independent sub-factors — **VIX spot**
  (new feed) and **ATM option IV** (derived from the chain). Each can be on/off
  and has its own op + thresholds. Either, both, or neither may be enabled; if
  any enabled sub-factor fails, the condition fails.
- `short_delta` and `spread_width` and `credit` are evaluated **per candidate**
  (they depend on the constructed vertical), whereas `entry_window`, `trend`,
  and `volatility` are **market-global** and evaluated once per tick (they do not
  depend on which candidate). The engine evaluates the global conditions first
  (cheapest, most decoupled), then builds candidates and evaluates the
  per-candidate conditions. Priority ordering is applied within the global set
  and within the candidate set, preserving the user's listed order. The spec
  keeps the user-visible order as the single source of truth; the global/candidate
  split is an internal optimization and is documented so it is not surprising.

## 6. Candidate generation & scanner

`strategy_engine` enumerates candidate verticals from the live `state.chain_data` /
`state.chain_quotes_cache` per strategy direction:

- **Bull put:** short put strikes whose `abs(delta)` ∈ C2 range; long leg =
  short strike − width (width ∈ C3 range).
- **Bear call:** short call strikes whose `abs(delta)` ∈ C2 range; long leg =
  short strike + width.

Each candidate computes (reusing existing pricing math where possible):
credit (bid/ask/mid, signed), width in points, margin (`= width × 100`), ATM IV,
short-leg delta, and a **buying-power / margin check** against
`state.account_summary` (ExcessLiquidity / MaintMarginReq).

Candidate set is **capped** (top-N by credit, configurable) to avoid flooding
the UI or the auto-path.

When conditions pass and a candidate is accepted:

- **Scanner mode** (`auto_execute` false): broadcast the candidate(s) to the UI
  with metrics + a "place order" ticket; the human confirms through the existing
  order flow. No order is placed until the human confirms.
- **Auto mode** (`auto_execute` true and `armed`): construct the `place_order`
  payload and hand it to the existing `order_manager.handle_place_order`,
  tagging the order with `strategy_id` (backend-side only).

## 7. Exit management

- **Stop-loss:** reused unchanged — the existing IB stop-limit bracket attached
  on entry (`order_manager`). Config: signed stop / limit prices.
- **Take-profit:** one **server-side watch loop** in `strategy_engine`. It tracks
  strategy-tagged positions and flattens (closes the vertical) when the TP target
  is reached. Config as a **% of max credit**, **$ per contract**, or an
  **absolute credit target**.
- **Expire:** optional rule — close before expiry on a DTE/time (e.g., a time on
  the expiry day), or hold to close.
- **Reconciliation (main correctness risk):** the TP loop resolves against
  `state.positions` / `state.open_orders` so it never double-closes when the
  broker's stop-loss fires first (broker SL races server TP). Handles: partial
  fills, a cancelled/unfilled entry (no position → nothing to manage), and the
  reverse-close of the correct leg actions. The spec requires the exit loop to
  be idempotent and guarded by position state.

## 8. Data additions

- **VIX spot feed:** new subscription in `ib_client.py` / `ib_connection.py`
  (symbol `VIX`, secType `IND`, exchange `CBOE`, cash USD), surfaced as
  `state.vix` + WS push. This is the **only non-trivial new broker plumbing**.
- **ATM IV:** derived from the chain — take the call and put implied-volatility
  at the strike nearest to spot and average them (nearest strike wins on ties,
  falling to nearest available strike if the ATM strike has no quote). No new
  subscription.
- **RSI:** new pure function (Wilder) over `state.price_history`; **%move** from
  closes. No new library dependencies.

## 9. Persistence & WS protocol

- `strategy_store.py`: load/save/CRUD strategies to `config/strategies.json`;
  atomic writes; unchanged strategies left intact on malformed file (fail-safe
  load). Strategies survive restarts.
- **`AppState` additions:** `strategies: dict[str, Strategy]`, `strategy_candidates`,
  `vix`, plus per-strategy order/position tag mapping.
- **New WS messages** (`ws_handler.py`): `strategy_save`, `strategy_delete`,
  `strategy_arm` / `strategy_disarm`, `strategy_list` reply, `strategy_candidate`
  (scanner scout), `strategy_exit` (TP/stop event), `vix_update`, `strategy_update`.
  Reuses the existing `place_order:` message for actual fills. New message
  handlers are added alongside the existing `place_order:` / `cancel_order:`
  dispatch without disturbing them.

## 10. UI

A new **Strategies** tab (the app already has Dashboard / Option Chain / Account):

- **Strategy list:** saved strategies, each with arm/disarm action + auto-execute
  toggle, live status (blocker condition or "ready").
- **Strategy editor:** name, direction (Bull Put / Bear Call), add / remove /
  reorder entry conditions (up/down priority arrows), condition params (ranges,
  operators, indicators), exit rules, auto-execute + arm.
- **Live scanner results:** current matching candidates with metrics + a
  "place order" button (hidden when auto-execute is on).
- Condition values update live from the existing WS feed (no refresh button).

Frontend is vanilla JS (no build step), matching the existing `state.js` /
`ws.js` pattern; the editor and results panel dispatch the WS messages above.

## 11. Safety & governance (auto mode)

- `auto_execute` off by default; **authoring is explicit** per strategy and
  requires the strategy to be **armed** before auto fires.
- Global **kill switch** to disable all auto-trading.
- Guards: only within RTH (configurable override), max concurrent auto positions
  / max auto orders per day, margin / buying-power check before placing.
- Every auto decision is **logged** with the full condition/value snapshot
  (evaluated condition, value, pass/fail, candidate, chosen order) for audit.
- Scanner mode still shows the confirmation modal in the existing order flow.

## 12. Architecture & module boundaries

New backend modules (single purpose, testable in isolation):

- `strategy_engine.py` — condition evaluators, candidate generator, evaluation
  loop, take-profit watch loop. Reuses `order_manager.handle_place_order` for
  entry and for TP closing.
- `strategy_store.py` — JSON persistence / CRUD.
- `condition_helpers.py` — pure functions: RSI, %move, ATM-IV pick, credit/width
  math (kept pure and unit-testable).

Modified:

- `app_state.py` — add `strategies`, `strategy_candidates`, `vix`, tag map.
- `ib_client.py` / `ib_connection.py` — add VIX subscription.
- `ws_handler.py` — add strategy WS handlers.
- `server.py` — start the strategy evaluation + exit loops; wire the store.

The existing `order_manager.py`, `chain_manager.py`, `account_manager.py`, and
`gex_calculator.py` are **used, not modified**, except where the VIX subscription
and position tracking need a small hook. The execution primitive is reused, not
rewritten.

## 13. Testing

- Pure helpers (RSI, %move, credit, width, ATM-IV) → unit tests.
- Condition evaluators, including priority short-circuit order and the
  global-vs-candidate split → unit tests with seeded `AppState`.
- Candidate generator (delta/width/credit range, direction, margin check,
  top-N cap) → unit tests.
- The evaluation-to-entry decision (scanner vs auto) → unit tests (+ integration
  with mocked `place_order`).
- Take-profit loop safety (no double-close when SL wins, partial fills,
  unfilled entry) → unit tests with mocked positions.
- WS protocol handlers → unit tests via the existing `MockIBClient` /
  `MockWs` fixtures (`tests/conftest.py`).
- Strategy store: save/load/CRUD + malformed-file fail-safe → unit tests.

## 14. MVP scope (single implementation phase)

Store + model + engine (C1–C6) + direction (Bull Put / Bear Call) + candidate
scanner + scanner UI + stop-loss (existing) + server TP loop + VIX feed + auto
toggle/arm + kill switch + audit logging.

Deferred (YAGNI): backtesting, monthly / non-0DTE expiries, indicators beyond
RSI/%move, strategies beyond 2-leg verticals, multi-candidate batching, any
database.
