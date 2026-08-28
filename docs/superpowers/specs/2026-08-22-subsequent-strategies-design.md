# Design — Subsequent (Nested) Credit-Spread Strategies

**Date:** 2026-08-22
**Status:** Approved for implementation planning

## 1. Problem statement

The strategy engine (see `2026-08-21-credit-spread-strategy-engine-design.md`)
lets a user define a standalone credit-spread strategy: an ordered set of entry
conditions, exit rules, a scanner/auto mode, and a take-profit watch loop. Each
strategy is independent — it opens its own vertical, manages its own exit, and
does not know about any other strategy.

The user wants **subsequent (nested) strategies**: a strategy can be declared as
a *child* of a given *master* strategy and, instead of entering on its own
schedule, it waits for the master to meet one or more **trigger conditions**, then
takes over. A master may have several subsequent strategies, and a subsequent
strategy may itself be a master of further strategy — an arbitrary-depth tree.
This enables workflows such as: *"run this bull put; when it stops out, launch the
recovery follow-up; when that take-profits, expand more broadly,"* or *"when the
first trade is up N× the credit it actually collected, roll it into a wider
structure."*

Today nothing in the engine models parent/child relationships, trigger
conditions, or a single-use per-arm lifecycle.

## 2. Goals

1. Let a user declare a strategy as a **subsequent** of another strategy (a
   parent/child tree of any depth).
2. Let a subsequent strategy be gated by **one or more trigger conditions** on the
   *parent's* lifecycle: time-of-day, the parent's exit reason, and the parent's
   unrealized PnL in multiples of the credit it actually collected.
3. Let the triggers combine as **any** or **all** (configurable per child).
4. Enforce a **one-position-per-arming-cycle** lifecycle on every strategy
   (masters and children), reset by manual re-arm and by each trading day.
5. Reuse the existing entry-condition / exit-rules / candidate / account-margin
   machinery for the child's *own* entry: a subsequent strategy is a full
   `Strategy`; only its *activation* is gated by the parent.

## 3. Non-goals

- **No concurrency inside a branch.** A child only enters after its parent is
  flat (confirmed).
- **No multi-parent / DAG.** A strategy has at most one parent (confirmed).
- **No backtesting, no non-0DTE expiries**, no strategies beyond 2-leg verticals
  (unchanged from the base spec).
- **No new persistence technology** — strategies stay in
  `config/strategies.json`.
- **No new broker subscriptions** — no new data feeds; triggers read live
  `AppState` (clock, positions PnL, executions) only.

## 4. Model

`Strategy` gains three optional fields; everything else is unchanged.

```python
TRIGGER_KINDS = ("time_of_day", "parent_exit_reason", "parent_unrealized_pnl")
TRIGGER_LOGIC  = ("any", "all")

@dataclass
class TriggerSpec:
    kind: str                      # one of TRIGGER_KINDS
    enabled: bool = True
    params: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"kind": self.kind, "enabled": self.enabled, "params": dict(self.params)}

    @classmethod
    def from_dict(cls, d: dict) -> "TriggerSpec":
        return cls(kind=d["kind"], enabled=d.get("enabled", True),
                   params=dict(d.get("params") or {}))

@dataclass
class Strategy:
    name: str
    direction: str
    conditions: List[Condition]
    exit_rules: ExitRules = field(default_factory=ExitRules)
    auto_execute: bool = False
    armed: bool = False
    target_expiry: str = ""
    budget: Optional[float] = None
    run_days: List[int] = field(default_factory=lambda: list(DEFAULT_RUN_DAYS))
    short_day_enabled: bool = False
    run_on_fomc: bool = True
    run_on_nfp: bool = True
    # --- NEW ---
    parent_name: str = ""                                # "" = master (independent)
    subsequent_triggers: List[TriggerSpec] = field(default_factory=list)
    trigger_logic: str = "any"                           # "any" | "all"
```

- A strategy with `parent_name == ""` is a **master** and behaves exactly as the
  base spec describes (scanned/entered on its own conditions).
- A strategy with `parent_name != ""` is a **child / subsequent**. It is a full
  `Strategy` (its own direction, conditions, exit rules, budget, `armed`,
  `auto_execute`, run days), but the main loop **skips it until it is eligible**.
- `subsequent_triggers` is a list of `TriggerSpec`. Validation rejects a child
  whose trigger list is empty or whose triggers are all disabled, so a subsequent
  strategy always has ≥1 enabled trigger. (An empty list would otherwise mean
  "never fires" — a silent no-op; we require it to be explicit.)

`{to_dict,from_dict}` for `Strategy` gain the three new fields; missing fields
decode to defaults so existing `strategies.json` files stay valid.

### Validation at load time

A tree-validation pass runs over all loaded strategies. A strategy list is
rejected, or the offending children dropped (fail-safe, like the current store):

- every `parent_name` must reference an existing strategy name;
- **no cycles**: a strategy cannot be its own ancestor (walk parents; reject if
  the walk returns to the start);
- `trigger_logic` must be in `TRIGGER_LOGIC`;
- `subsequent_triggers` must have valid `kind`s and `params` shapes (see §5);
- a child's trigger list must not be empty if `parent_name` is set (see above).

## 5. Trigger conditions

Each trigger reads only live / already-tracked state (no new IO). `kind` maps to
a small isolated evaluator, mirroring the base spec's `Condition` table.

| # | `kind` | reads from | `params` | triggers (latches `fired`) when |
|---|---|---|---|---|
| T1 | `time_of_day` | `market_hours` ET clock | `start`, `end` (HH:MM; default `09:30`/`15:30`) | `start ≤ now_et ≤ end`. Latched; for a precise time set `start == end`. |
| T2 | `parent_exit_reason` | parent's recorded close event | `reason` ∈ {`take_profit`, `stop_loss`} | parent's trade closed with that reason. |
| T3 | `parent_unrealized_pnl` | parent's running unrealized PnL | `gain_multiple` (≥0, optional), `loss_multiple` (≥0, optional); at least one set | parent's high-water multiplier ≥ `gain_multiple` **or** low-water multiplier ≤ −`loss_multiple`, where multiplier = `unrealized_pnl / actual_credit`. |

Notes:

- **T1 `time_of_day`** is evaluated continuously (not only at close) and latched
  the first tick it holds. Combined with "parent flat", this means a follow-up
  can launch at a set clock time even after the parent closed earlier in the day.
- **T2 `parent_exit_reason`** is inherently a *close event* — it cannot hold until
  the parent's trade has closed, so it naturally waits for the parent to be flat.
- **T3 `parent_unrealized_pnl`** tracks **high-water / low-water** multipliers
  *during* the trade (each tick `mult = net_pnl / actual_credit`, keep max/min).
  The trigger stays latched once a water mark crosses, so a threshold reached
  mid-trade still fires even if PnL normalized before close. Configuring only
  `gain_multiple` fires on a winning follow-up; only `loss_multiple` fires a
  recovery; both is allowed (independent).
- The **"actual credit"** used by T3 is the *filled* credit, not the candidate
  mid (see §7).

### Combination

`trigger_logic ∈ {"any", "all"}` is stored **per child**. Enabled triggers are
combined accordingly: `any` fires on the first satisfied trigger; `all` fires only
if every enabled trigger is satisfied. `any` is recommended (T2 + T1 means the
follow-up fires on a take-profit OR at 2pm — a disjunction is the natural reading);
`all` can be self-contradictory (e.g. T2 `stop_loss` + a time window the exit can
never be inside), and that is the user's responsibility.

## 6. Lifecycle: one position per arming cycle

Every strategy (master and child) is a three-state machine. A strategy opens **at
most one position per arming cycle** — confirmed global rule.

```
armed  →  WAITING   (entry conditions not yet met)
           │  conditions pass
           ▼
        ENTERED     (the ONE position this cycle; will not open another)
           │  manage exit: TP / SL / expire; also feed parent triggers
           ▼
        DONE        (trade closed; as a parent it has fired its children)
```

- After entering, the strategy is *done opening*: it only manages the exit and,
  as a parent, waits-for / fires its subsequent strategies. It does **not**
  re-enter.

### Runtime state (per strategy)

```python
@dataclass
class RuntimeState:
    cycle: int = 0                          # current arming cycle
    entered: bool = False                   # opened this cycle's one position
    done: bool = False                      # that position closed; idle until a reset
    trade: Optional[dict] = None            # the single trade (see §7)
    trigger_fired: bool = False             # this child triggered for the current parent cycle
    parent_cycle: int = 0                   # the parent cycle it latched against
```

`state.runtime: dict[str, RuntimeState]` and `state.day_key: str` ("YYYY-MM-DD").

### Cycle reset

Two reset triggers (confirmed "both"):

1. **Manual re-arm** — `strategy_arm:<name>` resets that strategy's runtime
   (`cycle += 1`, `entered = done = False`, `trade = None`, `trigger_fired = False`).
   Because the child latch is keyed to `parent_cycle` (see §6 "Latch invalidation"),
   re-arming a master automatically re-primes its entire subtree.
2. **Daily auto-reset** — on a day-roll the eval loop runs `_daily_reset()`:
   reset every idle/`done` strategy's runtime so it may trade once that day. An
   **open position is preserved** (its `trade` record and `entered=True` stay, so
   no second entry is allowed until it exits) — 0DTE positions are normally closed
   same-day, but the guard protects a carry.

### Latch invalidation

A child's `trigger_fired` is meaningful only while
`parent_cycle == parent.runtime.cycle`. When the parent re-arms (new cycle) or the
day rolls, the comparison is stale and the child re-evaluates its triggers fresh
for the new parent cycle. This removes the need for an explicit cascade on re-arm.

## 7. Trade record ("actual credit" and water marks)

On entry, the engine records on `runtime.trade`:

```python
{
  "credit": candidate.credit_mid,        # replaced by the actual fill credit when observed
  "open_ts": time.monotonic(),
  "close_ts": None,
  "close_reason": None,                   # "take_profit" | "stop_loss" | "expire" | "manual"
  "high_water_mult": 0.0,                 # max(net_pnl / credit) while open
  "low_water_mult": 0.0,                  # min(net_pnl / credit) while open
}
```

- **`credit`**: seeded with `candidate.credit_mid` immediately, then **updated to
  the actual fill credit** when the combo fill is observed in `state.executions`
  (realized credit = −combo fill price). T3 multiples use whichever is current.
  The spec requires the fill-update path (see §14 tests) because the user is
  explicit that multiples are against the *actual* collected credit.
- **Water marks**: each tick while the parent position is open, compute
  `net_pnl = Σ state.positions[...]["unrealizedPNL"]` over the trade's legs and
  update `high_water_mult` / `low_water_mult`.

## 8. Parent-close detection & reason classification

The server initiates exactly one closer: `take_profit_loop`. For every *other*
close the position simply disappears from `state.positions`. So:

- If `maybe_flatten_at_take_profit` returned true for the trade → reason
  `take_profit` (the loop already knows it closed it).
- Else if the strategy has `exit_rules.stop_loss` → the IB stop bracket fired →
  `stop_loss`.
- Else → `expire` if the trade's expiry date has passed, otherwise `manual`.

`classify_parent_close(parent, state)` is a small pure-ish helper tested against
the mock positions/executions fixtures.

Detecting a close: the eval loop (and TP loop) already resolve a strategy's
positions via `find_strategy_positions(candidate, state)`. When a strategy with
`entered == True` and a known `trade` has fewer than 2 matching positions, the
trade has closed: record `close_ts`, `close_reason = classify_parent_close(...)`,
set `done = True`, then `fire_children(parent)`. This is idempotent — it only fires
once because `done` is then set.

## 9. Eligibility & firing

**Continuous latching vs. close-time evaluation.** T1/T3 are latched **every tick
while the parent's trade is live** (inside `_update_parent_role`, which also
updates the water marks), so a time window or PnL threshold reached mid-trade
stays latched even if the value normalizes later. `fire_children` runs at the
parent's close (idempotent via `done`); it evaluates T2 (only knowable at close)
and finalizes the per-child aggregate across already-latched T1/T3.

### `fire_children(parent)`

For each strategy `c` with `c.parent_name == parent.name`:

1. Evaluate `c`'s enabled triggers against `parent.runtime.trade` and the current
   clock (`close_reason` for T2, `now_et` for T1, water marks / credit for T3).
   Triggers that were already latched (T1/T3) stay latched; T2 is evaluated now.
2. Combine per `c.trigger_logic` → `fired`.
3. If `fired` and not already set for this parent cycle: set
   `c.runtime.trigger_fired = True`, `c.runtime.parent_cycle = parent.runtime.cycle`.
4. Broadcast `strategy_trigger {name: c.name, event: "eligible"}`.

### `_child_is_eligible(child, state)`

Returns `True` when all of:

- `child.runtime.trigger_fired` **and** `child.runtime.parent_cycle == parent.runtime.cycle`
  *(the trigger was latched for the parent's current cycle)*;
- the parent is **flat** (its position has closed) — this is the "child waits for
  parent flat" rule;
- the child is not already `entered`/`done` this cycle.

When eligible, the child is evaluated by the *normal* pipeline (its own entry
conditions, budget, `_strategy_should_run_today`, margin) and may enter exactly
once, exactly like a master — it just was gated to this point.

## 10. Engine flow changes (`strategy_evaluation_loop`)

```python
today = now_et().date()
if state.day_key != today.isoformat():
    _daily_reset(state)                 # keep open positions; reset done/idle
    state.day_key = today.isoformat()

for name, strat in state.strategies.items():
    if not strat.armed:
        continue
    if not _strategy_should_run_today(strat, state):
        state.strategy_candidates[name] = []
        continue
    rt = get_runtime(state, name)

    if rt.entered or rt.done:
        # already used this cycle's one trade; just manage parent role + exit
        _update_parent_role(name, state)   # water marks while open; on close -> classify + fire_children
        state.strategy_candidates[name] = []
        continue

    if strat.parent_name:
        if not _child_is_eligible(strat, state):
            state.strategy_candidates[name] = []
            continue

    ev = evaluate_conditions(strat, state, now=now_et())
    state.strategy_candidates[name] = [c.to_dict() for c in ev.candidates]
    if ev.status != "ready":
        continue
    if getattr(state, "auto_trade_kill_switch", False):
        continue
    if strat.auto_execute and ev.candidates:
        best = ev.candidates[0]
        if not _has_margin(state, best, budget=strat.budget):
            continue
        resp = await place_strategy_entry(ib, state, strat, best)
        if (resp.get("data") or {}).get("status", "") not in ("Error", ""):
            rt.entered = True
            rt.trade = {...credit=best.credit_mid, open_ts=time.monotonic(), ...}
            await broadcast_fn({"type": "strategy_exit", "data": {"name": name, "event": "auto_entry"}})
    elif not strat.auto_execute:
        await broadcast_fn({"type": "strategy_candidate",
                            "data": {"name": name, "candidates": [c.to_dict() for c in ev.candidates]}})
```

Key changes from the base loop:

- **One-shot across the whole cycle**: `entered`/`done` skips a strategy entirely
  (no re-entry after exit). This supersedes the old behavior where a strategy
  could re-enter once its position closed.
- **Child gate** before evaluation.
- **Parent role** is maintained out-of-band: `_update_parent_role` ticks water
  marks while open and, on detecting a close, classifies the reason and fires
  children (idempotent via `done`).

`take_profit_loop` is changed minimally: it already flattens at TP and pops the
position; it must also record `close_reason = "take_profit"` and call
`fire_children` on the parent instead of only broadcasting `strategy_exit`.

## 11. Persistence

- `strategy_store.load_strategies` runs the **tree-validation** pass (§4) after
  `from_dict`; offending children are dropped and logged (fail-safe), the rest
  load.
- `save_strategies` writes the new fields via `Strategy.to_dict`; atomic write
  unchanged.
- Back-compat: files without the new fields decode to `parent_name=""`,
  `subsequent_triggers=[]`, `trigger_logic="any"`.

## 12. WS protocol

- `strategy_save:<json>` — the body now carries `parent_name`, `subsequent_triggers`,
  `trigger_logic`. No new message handler; the existing `Strategy.from_dict` +
  `strategy_list` reply covers it.
- `strategy_list` reply — includes the new fields (the UI derives the tree).
- **New broadcast `strategy_trigger`** `{name, event: "eligible"}` — pushed when a
  child becomes eligible so the UI can light up the child's status. (A child that
  is latched but still gated on the parent being flat is not yet broadcast.)
- `strategy_exit` — the parent's close now also drives children (the engine calls
  `fire_children` internally; existing clients still receive `strategy_exit`).

## 13. UI (`static/js/strategy-ui.js`)

In the Strategies tab:

- **Editor**: a "This is a subsequent strategy" toggle reveals:
  - **Parent selector** — a dropdown of all other strategies (excluding self and
    descendants, to prevent cycles client-side).
  - **Trigger list editor** — add / remove / reorder `TriggerSpec`s, reusing the
    existing condition-editor pattern (`CONDITION_DEFS`) with three new kinds
    (`time_of_day`, `parent_exit_reason`, `parent_unrealized_pnl`).
  - **Trigger logic** radio — Any / All.
- **List**: render the tree (children indented under their parent) with per-node
  status. For masters: `off` / `scan` / `auto` (as today). For children, add a sub
  status: `waiting <parent>` / `eligible` / `in position` / `done`.
- Live: `strategy_trigger` events flip a child to `eligible`; `strategy_exit` /
  `strategy_candidate` update as today.

## 14. Safety & governance

- **Tree must be armed**: the whole tree only runs when the **master** is armed. A
  disarmed master freezes all descendants (children of a disarmed master are
  skipped because the child gate requires the parent's role/lifecycle to run).
- **Own gates per child**: once eligible, a child uses its own `armed`,
  `auto_execute` (off ⇒ scanner mode, human confirms), `budget`, `run_days`, and
  day gates. A child is independent of the master's `auto_execute` toggle.
- **One position per branch**: a child waits for its parent to be flat, so a single
  branch holds at most one position at a time. Different branches (siblings) may
  each hold one; each is checked against its own `budget` and account excess via
  the existing `_has_margin`. The kill switch, RTH rules, and audit logging from
  the base spec continue to apply.

## 15. Module boundaries

- `strategy_models.py` — `TriggerSpec`, new `Strategy` fields, `to_dict/from_dict`.
- `strategy_store.py` — tree-validation pass on load.
- `strategy_engine.py` — trigger evaluators (T1–T3), `RuntimeState`, `get_runtime`,
  `_daily_reset`, `_update_parent_role`, `fire_children`, `_child_is_eligible`,
  `classify_parent_close`, the reworked `strategy_evaluation_loop`, and the
  TP-loop tweak.
- `app_state.py` — `runtime: dict[str, RuntimeState]`, `day_key: str`.
- `ws_handler.py` — `strategy_trigger` broadcast; pass-through of new fields.
- `static/js/strategy-ui.js` — tree list, trigger editor, parent selector.
- `order_manager.py` / `account_manager.py` — **used, not modified**, except
  reading `state.executions` to update the fill credit (already present).

## 16. Testing

- **Trigger evaluators**: T1 window (in/out, latched), T2 reason match, T3
  gain/loss multiple against fill credit, high/low water persistence after PnL
  normalizes.
- **Fill-credit update**: `credit_mid` → actual fill credit from `state.executions`;
  T3 uses the updated value.
- **Eligibility**: child not eligible while parent open; eligible when `fired` and
  parent flat; latch invalidation on parent re-arm / day roll; `any` vs `all`;
  empty trigger list never fires; nesting depth ≥ 2.
- **Parent-close classification**: TP (via loop) vs SL (bracket, position gone) vs
  manual/expire, against mock positions/executions.
- **One-shot lifecycle**: a strategy enters once per cycle; does not re-enter after
  exit; resets on re-arm and on day roll; preserves an open position across a
  day-roll.
- **Tree validation**: orphan parent, cycle, invalid `trigger_logic`, invalid
  trigger kind; fail-safe drop of offending children; back-compat decode.
- **WS**: `strategy_save` with children round-trips; `strategy_trigger` broadcast;
  `strategy_list` includes new fields.
- **Engine integration**: child (auto + scanner) enters once eligible; sibling
  concurrency permitted; master armed gates the subtree.

## 17. MVP scope

**In:** model + trigger evaluators (T1–T3) + tree validation + RuntimeState/lifecycle
(one-shot, re-arm + daily reset) + parent-close detection & classification +
child eligibility/firing + fill-credit update + `strategy_trigger` + tree UI /
trigger editor / parent selector.

**Deferred (YAGNI):** multi-parent (DAG), sibling-to-sibling triggers,
backtesting, non-0DTE expiries, strategies beyond 2-leg verticals, a database.
