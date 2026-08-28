# Subsequent (Nested) Strategies Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a strategy be declared as a *subsequent* (child) of another strategy, gated by trigger conditions (time-of-day, parent-exit-reason, parent unrealized PnL in multiples of actual collected credit), with a one-position-per-arming-cycle lifecycle.

**Architecture:** Extend `Strategy` with `parent_name` + `subsequent_triggers` (`TriggerSpec` list) + `trigger_logic`. Children live in the same flat `config/strategies.json`, validated acyclically on load. `strategy_engine` gains a `RuntimeState` per strategy (cycle/entered/done/trade), a trigger-aggregate evaluator, parent-role maintenance (water marks, close reason classification, `fire_children`), and child eligibility checks. The eval loop treats a master as today but skips a strategy that has already used its one trade, and only evaluates a child once its parent's trigger fired and the parent is flat.

**Tech Stack:** Python (dataclasses, asyncio, pytest), vanilla-JS frontend, FastAPI WebSocket.

**Spec:** `docs/superpowers/specs/2026-08-22-subsequent-strategies-design.md`

## Global Constraints

- Conditions are a hard AND chain in list order (base spec §4); do not change this.
- `stop_loss` is a single credit multiplier; `comboLmtPrice` signed negative on entry (base spec §7).
- One position per strategy per arming cycle (spec §6) — applies to masters globally.
- Cycle resets on manual re-arm and each trading day (spec §6).
- A child enters only after its parent is flat; no concurrent positions in a branch (spec §4, confirmed).
- No new broker subscriptions; triggers read live `AppState`/`state.executions` only.
- Persistence stays in `config/strategies.json` (fail-safe load, atomic write).
- Tests run with `pytest`; project root is on `sys.path` via `tests/conftest.py`.
- `side` in `state.executions` entries is `"SLD"` (sold/short leg) or `"BOT"` (bought/long leg).

---

## File Structure

- `strategy_models.py` — `TriggerSpec`, `RuntimeState`, new `Strategy` fields + validation.
- `app_state.py` — `runtime: dict[str, RuntimeState]`, `day_key: str`.
- `strategy_store.py` — `validate_strategy_tree()`; run on load.
- `strategy_engine.py` — trigger aggregate, runtime helpers, parent-role maintenance, reworked eval loop + TP loop.
- `ws_handler.py` — re-arm resets runtime.
- `static/js/strategy-ui.js` — tree list, parent selector, trigger editor, logic radio.
- Tests: `tests/test_strategy_models.py`, `tests/test_strategy_store.py`, `tests/test_strategy_engine.py`, `tests/test_strategy_ws.py`, `tests/test_app_state_extras.py`.

---

### Task 1: Model — `TriggerSpec`, `RuntimeState`, `Strategy` fields

**Files:**
- Modify: `strategy_models.py`
- Test: `tests/test_strategy_models.py`

**Interfaces:**
- Produces: `TRIGGER_KINDS: tuple`, `TRIGGER_LOGIC: tuple`, `TriggerSpec(kind, enabled, params)` w/ `to_dict`/`from_dict`, `RuntimeState(cycle, entered, done, trade, time_met, parent_cycle)`, `Strategy.parent_name`, `Strategy.subsequent_triggers: list[TriggerSpec]`, `Strategy.trigger_logic`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_strategy_models.py`:

```python
from strategy_models import TriggerSpec, RuntimeState, TRIGGER_KINDS, TRIGGER_LOGIC


def test_trigger_spec_roundtrips():
    t = TriggerSpec(kind="time_of_day", enabled=True, params={"start": "13:00", "end": "15:00"})
    assert TriggerSpec.from_dict(t.to_dict()).to_dict() == t.to_dict()


def test_child_strategy_roundtrip_with_triggers():
    s = Strategy(
        name="followup", direction="bull_put", conditions=[],
        parent_name="master",
        subsequent_triggers=[TriggerSpec(kind="parent_exit_reason", params={"reason": "stop_loss"})],
        trigger_logic="any",
    )
    d = s.to_dict()
    assert d["parent_name"] == "master"
    assert d["subsequent_triggers"][0]["kind"] == "parent_exit_reason"
    assert d["trigger_logic"] == "any"
    s2 = Strategy.from_dict(d)
    assert s2.parent_name == "master"
    assert s2.subsequent_triggers[0].kind == "parent_exit_reason"
    assert s2.trigger_logic == "any"


def test_child_strategy_defaults_when_absent():
    d = {"name": "x", "direction": "bull_put", "conditions": [],
         "exit_rules": {"take_profit": None, "stop_loss": None, "hold_to_expire": False}}
    s = Strategy.from_dict(d)
    assert s.parent_name == ""
    assert s.subsequent_triggers == []
    assert s.trigger_logic == "any"


def test_invalid_trigger_logic_rejected():
    d = {"name": "x", "direction": "bull_put", "conditions": [],
         "exit_rules": {"take_profit": None, "stop_loss": None, "hold_to_expire": False},
         "trigger_logic": "or"}
    with pytest.raises(ValueError):
        Strategy.from_dict(d)


def test_invalid_trigger_kind_rejected():
    d = {"name": "x", "direction": "bull_put", "conditions": [],
         "exit_rules": {"take_profit": None, "stop_loss": None, "hold_to_expire": False},
         "subsequent_triggers": [{"kind": "parent_price", "enabled": True, "params": {}}]}
    with pytest.raises(ValueError):
        Strategy.from_dict(d)


def test_runtime_state_defaults():
    rt = RuntimeState()
    assert rt.cycle == 0
    assert rt.entered is False
    assert rt.done is False
    assert rt.trade is None
    assert rt.time_met is False
    assert rt.parent_cycle == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_strategy_models.py::test_trigger_spec_roundtrips -v`
Expected: FAIL (`AttributeError`/`ImportError` — `TriggerSpec` not defined)

- [ ] **Step 3: Implement model changes**

`strategy_models.py` — add after the existing constants and before `Strategy`:

```python
TRIGGER_KINDS = ("time_of_day", "parent_exit_reason", "parent_unrealized_pnl")
TRIGGER_LOGIC = ("any", "all")


@dataclass
class TriggerSpec:
    kind: str
    enabled: bool = True
    params: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"kind": self.kind, "enabled": self.enabled, "params": dict(self.params)}

    @classmethod
    def from_dict(cls, d: dict) -> "TriggerSpec":
        return cls(kind=d["kind"], enabled=d.get("enabled", True), params=dict(d.get("params") or {}))


@dataclass
class RuntimeState:
    cycle: int = 0                 # current arming cycle (increments on re-arm / day roll)
    entered: bool = False          # opened the cycle's one position
    done: bool = False             # that position closed; idle until a reset
    trade: Optional[dict] = None   # the single trade record (see strategy_engine)
    time_met: bool = False         # T1: the time window was reached during the parent's current trade
    parent_cycle: int = 0          # the parent cycle time_met was latched against
```

Add three fields to `Strategy` (after `run_on_nfp`), and extend `to_dict`/`from_dict`:

```python
# inside Strategy dataclass body, after run_on_nfp:
    parent_name: str = ""                                # "" = master (standalone)
    subsequent_triggers: List[TriggerSpec] = field(default_factory=list)
    trigger_logic: str = "any"                           # "any" | "all"
```

In `to_dict`, add:

```python
            "parent_name": self.parent_name,
            "subsequent_triggers": [t.to_dict() for t in self.subsequent_triggers],
            "trigger_logic": self.trigger_logic,
```

In `from_dict`, replace the `return cls(...)` construction by computing validation first, then build:

```python
        trigger_logic = d.get("trigger_logic", "any")
        if trigger_logic not in TRIGGER_LOGIC:
            raise ValueError(f"Invalid trigger_logic: {trigger_logic}")
        triggers = [TriggerSpec.from_dict(t) for t in d.get("subsequent_triggers", [])]
        for t in triggers:
            if t.kind not in TRIGGER_KINDS:
                raise ValueError(f"Invalid trigger kind: {t.kind}")
        return cls(
            ...
            parent_name=d.get("parent_name", ""),
            subsequent_triggers=triggers,
            trigger_logic=trigger_logic,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_strategy_models.py -v`
Expected: PASS (all model tests including the pre-existing ones)

- [ ] **Step 5: Commit**

```bash
git add strategy_models.py tests/test_strategy_models.py
git commit -m "feat: TriggerSpec + RuntimeState + child Strategy fields"
```

---

### Task 2: AppState — `runtime` and `day_key`

**Files:**
- Modify: `app_state.py`
- Test: `tests/test_app_state_extras.py`

**Interfaces:**
- Consumes: `RuntimeState` from `strategy_models`.
- Produces: `state.runtime: dict[str, RuntimeState]`, `state.day_key: str`.

- [ ] **Step 1: Write the failing test**

Replace `tests/test_app_state_extras.py` with:

```python
from app_state import create_app_state


def test_strategy_state_defaults():
    s = create_app_state()
    assert s.strategies == {}
    assert s.strategy_candidates == {}
    assert s.vix is None
    assert s.auto_trade_kill_switch is False
    assert s.strategy_log == []
    assert s.strategy_open_positions == {}


def test_subsequent_runtime_state_defaults():
    s = create_app_state()
    assert s.runtime == {}
    assert s.day_key == ""
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_app_state_extras.py::test_subsequent_runtime_state_defaults -v`
Expected: FAIL (`AttributeError: 'AppState' object has no attribute 'runtime'`)

- [ ] **Step 3: Implement**

`app_state.py` — add import and fields:

```python
from strategy_models import RuntimeState
```

In `AppState.__init__`, after the strategy-engine block (`self.strategy_open_positions = {}`):

```python
        # Subsequent-strategy runtime (per-strategy lifecycle + parent trades)
        self.runtime: dict = {}      # {strategy_name: RuntimeState}
        self.day_key: str = ""       # "YYYY-MM-DD" (drives daily cycle reset)
```

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/test_app_state_extras.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app_state.py tests/test_app_state_extras.py
git commit -m "feat: AppState runtime + day_key for subsequent strategies"
```

---

### Task 3: Store — tree validation on load

**Files:**
- Modify: `strategy_store.py`
- Test: `tests/test_strategy_store.py`

**Interfaces:**
- Consumes: `Strategy` with `parent_name`/`subsequent_triggers`.
- Produces: `validate_strategy_tree(strategies: dict[str, Strategy]) -> dict[str, Strategy]`; `load_strategies` now returns only the validated subset.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_strategy_store.py`:

```python
from strategy_store import validate_strategy_tree


def _child(name, parent, enabled=True, logic="any"):
    return Strategy(name=name, direction="bull_put", conditions=[],
                    parent_name=parent,
                    subsequent_triggers=[TriggerSpec(kind="parent_exit_reason",
                                                     params={"reason": "stop_loss"},
                                                     enabled=enabled)],
                    trigger_logic=logic)


def test_validate_accepts_clean_tree():
    strat = {"master": Strategy(name="master", direction="bull_put", conditions=[]),
             "child": _child("child", "master")}
    assert set(validate_strategy_tree(strat)) == {"master", "child"}


def test_validate_drops_orphan_child():
    strat = {"child": _child("child", "nowhere")}
    assert validate_strategy_tree(strat) == {}


def test_validate_drops_child_with_no_enabled_trigger():
    strat = {"master": Strategy(name="master", direction="bull_put", conditions=[]),
             "child": _child("child", "master", enabled=False)}
    assert set(validate_strategy_tree(strat)) == {"master"}


def test_validate_drops_cycle():
    a = _child("a", "b")
    b = _child("b", "a")
    assert validate_strategy_tree({"a": a, "b": b}) == {}


def test_load_applies_validation(tmp_path):
    # A saved child whose parent is deleted on disk should not survive load.
    p = tmp_path / "strategies.json"
    from strategy_store import save_strategies
    save_strategies(str(p), {
        "master": Strategy(name="master", direction="bull_put", conditions=[]),
        "orphan": _child("orphan", "gone"),
    })
    assert set(load_strategies(str(p))) == {"master"}
```

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/test_strategy_store.py::test_validate_accepts_clean_tree -v`
Expected: FAIL (`ImportError` — `validate_strategy_tree` not defined)

- [ ] **Step 3: Implement**

`strategy_store.py` — add the validator and call it from `load_strategies`:

```python
def validate_strategy_tree(strategies) -> dict:
    """Keep only strategies that are valid in the parent/child tree.

    Drops (fail-safe, logged): a child whose parent is missing, a child with no
    enabled trigger, and any strategy in a parent cycle. A master is always kept.
    """
    ok = dict(strategies)

    def own_invalid(s):
        if s.parent_name == "":
            return False
        return not any(t.enabled for t in s.subsequent_triggers)

    changed = True
    while changed:
        changed = False
        names = set(ok)
        for n in list(ok):
            s = ok[n]
            if own_invalid(s):
                logger.error(f"Dropping strategy '{n}': child must have >=1 enabled trigger")
                del ok[n]; changed = True; continue
            if s.parent_name != "" and s.parent_name not in names:
                logger.error(f"Dropping strategy '{n}': parent '{s.parent_name}' not found")
                del ok[n]; changed = True; continue
        names = set(ok)
        for n in list(ok):
            seen = set(); cur = n
            while cur in ok and cur != "":
                if cur in seen:
                    logger.error(f"Dropping strategy '{n}': parent cycle detected")
                    del ok[n]; changed = True; break
                seen.add(cur)
                cur = ok[cur].parent_name
    return ok
```

In `load_strategies`, replace the final return:

```python
        data = {name: Strategy.from_dict(d) for name, d in data.items()}
        return validate_strategy_tree(data)
```

`strategy_store.py` already defines `logger = logging.getLogger(__name__)`; reuse it (no new logger).

Then in `load_strategies`, the only failure path that must stay `{}` is the malformed/absent file — validation happens on the decoded dict. Keep the `except` block unchanged.

- [ ] **Step 4: Run to verify they pass**

Run: `pytest tests/test_strategy_store.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add strategy_store.py tests/test_strategy_store.py
git commit -m "feat: acyclic tree validation on strategy load"
```

---

### Task 4: Engine — trigger aggregate, runtime helpers, eligibility

**Files:**
- Modify: `strategy_engine.py`
- Test: `tests/test_strategy_engine.py`

**Interfaces:**
- Consumes: `RuntimeState`, `TriggerSpec`, `Strategy.parent_name`/`subsequent_triggers`/`trigger_logic`, `Candidate`, `short_right_for`, `find_strategy_positions`.
- Produces: `get_runtime(state, name) -> RuntimeState`, `reset_strategy_runtime(state, name)`, `_trigger_aggregate(child, state, now=None) -> bool`, `_child_is_eligible(child, state, now=None) -> bool`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_strategy_engine.py`:

```python
import datetime as dt
import time
from strategy_models import TriggerSpec, RuntimeState
from strategy_engine import get_runtime, reset_strategy_runtime, _child_is_eligible


def _parent_rt(entered=True, done=True, credit=0.30, high=1.5, low=-2.5, close_reason="stop_loss"):
    rt = RuntimeState(entered=entered, done=done)
    rt.trade = {"candidate": {"direction": "bull_put", "short_strike": 5100.0, "long_strike": 5000.0,
                              "credit_mid": 0.30},
                "credit": credit, "high_water_mult": high, "low_water_mult": low,
                "close_reason": close_reason}
    return rt


def _populate():
    state = type("S", (), {})()
    state.strategies = {
        "master": Strategy(name="master", direction="bull_put", conditions=[]),
        "recovery": Strategy(
            name="recovery", direction="bull_put", conditions=[],
            parent_name="master",
            subsequent_triggers=[TriggerSpec(kind="parent_exit_reason", params={"reason": "stop_loss"})],
            trigger_logic="any",
        ),
    }
    state.runtime = {"master": _parent_rt(), "recovery": RuntimeState()}
    return state


def test_get_runtime_creates_on_demand():
    st = _populate()
    rt = get_runtime(st, "master")
    assert rt is st.runtime["master"]
    assert get_runtime(st, "new").cycle == 0


def test_trigger_exit_reason_fires_when_parent_closed():
    from strategy_engine import _trigger_aggregate
    st = _populate()
    assert _trigger_aggregate(st.strategies["recovery"], st) is True


def test_child_not_eligible_while_parent_open():
    st = _populate()
    st.runtime["master"].done = False           # parent still open
    assert _child_is_eligible(st.strategies["recovery"], st) is False


def test_child_eligible_when_parent_done_and_trigger_fired():
    st = _populate()
    assert _child_is_eligible(st.strategies["recovery"], st) is True


def test_child_not_eligible_after_it_entered():
    st = _populate()
    st.runtime["recovery"].entered = True
    assert _child_is_eligible(st.strategies["recovery"], st) is False


def test_trigger_pnl_gain_multiple():
    from strategy_engine import _trigger_aggregate
    st = _populate()
    st.strategies["recovery"].subsequent_triggers = [
        TriggerSpec(kind="parent_unrealized_pnl", params={"gain_multiple": 1.0, "loss_multiple": 2.0})]
    assert _trigger_aggregate(st.strategies["recovery"], st) is True    # high 1.5 >= 1.0


def test_trigger_time_of_day_window():
    from strategy_engine import _trigger_aggregate
    st = _populate()
    st.strategies["recovery"].subsequent_triggers = [
        TriggerSpec(kind="time_of_day", params={"start": "13:00", "end": "15:00"})]
    now = dt.datetime(2026, 8, 21, 14, 0)
    assert _trigger_aggregate(st.strategies["recovery"], st, now=now) is True
    assert _trigger_aggregate(st.strategies["recovery"], st, now=dt.datetime(2026, 8, 21, 12, 0)) is False


def test_reset_strategy_runtime_increments_cycle():
    st = _populate()
    reset_strategy_runtime(st, "master")
    assert st.runtime["master"].cycle == 1
    assert st.runtime["master"].entered is False
    assert st.runtime["master"].trade is None
```

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/test_strategy_engine.py::test_get_runtime_creates_on_demand -v`
Expected: FAIL (`ImportError` — `get_runtime` not defined)

- [ ] **Step 3: Implement**

`strategy_engine.py` — add imports and functions (place after `short_right_for`):

```python
from strategy_models import TriggerSpec, RuntimeState
```

Functions:

```python
def get_runtime(state, name: str) -> RuntimeState:
    rt = getattr(state, "runtime", {}).get(name)
    if rt is None:
        rt = RuntimeState()
        state.runtime[name] = rt
    return rt


def reset_strategy_runtime(state, name: str) -> None:
    """Start a fresh arming cycle for a strategy and re-prime its children."""
    rt = get_runtime(state, name)
    rt.cycle += 1
    rt.entered = False
    rt.done = False
    rt.trade = None
    rt.time_met = False
    rt.parent_cycle = 0
    parent = state.strategies.get(name)
    if parent is not None:
        for child in state.strategies.values():
            if child.parent_name == name:
                crt = get_runtime(state, child.name)
                crt.time_met = False
                crt.parent_cycle = 0


def _trigger_aggregate(child: Strategy, state, now=None):
    """Aggregate a child's enabled triggers against the parent's current trade."""
    import datetime as dt
    now = now or dt.datetime.now()
    prt = get_runtime(state, child.parent_name)
    crt = get_runtime(state, child.name)
    ptrade = prt.trade
    enabled = [t for t in child.subsequent_triggers if t.enabled]
    if not enabled:
        return False

    def one(t):
        if t.kind == "time_of_day":
            if crt.time_met and crt.parent_cycle == prt.cycle:
                return True
            hm = now.strftime("%H:%M")
            start, end = t.params.get("start", "09:30"), t.params.get("end", "15:30")
            return start <= hm <= end
        if ptrade is None:
            return False
        if t.kind == "parent_exit_reason":
            return ptrade.get("close_reason") == t.params.get("reason")
        if t.kind == "parent_unrealized_pnl":
            credit = float(ptrade.get("credit") or 0.0)
            if credit <= 0:
                return False
            gain = t.params.get("gain_multiple")
            loss = t.params.get("loss_multiple")
            if gain is None and loss is None:
                return False
            hi = float(ptrade.get("high_water_mult") or 0.0)
            lo = float(ptrade.get("low_water_mult") or 0.0)
            if gain is not None and hi >= float(gain):
                return True
            if loss is not None and lo <= -float(loss):
                return True
            return False
        return False

    results = [one(t) for t in enabled]
    return all(results) if child.trigger_logic == "all" else any(results)


def _child_is_eligible(child: Strategy, state, now=None):
    """A child may be evaluated/entered only if its trigger fired AND its
    parent has closed its trade this cycle (child waits for parent flat)."""
    parent = state.strategies.get(child.parent_name)
    if parent is None:
        return False
    prt = get_runtime(state, parent.name)
    crt = get_runtime(state, child.name)
    if crt.entered or crt.done:
        return False
    if not prt.entered or not prt.done:
        return False   # parent must have traded AND closed (flat)
    return _trigger_aggregate(child, state, now)
```

- [ ] **Step 4: Run to verify they pass**

Run: `pytest tests/test_strategy_engine.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add strategy_engine.py tests/test_strategy_engine.py
git commit -m "feat: trigger aggregate + runtime helpers + child eligibility"
```

---

### Task 5: Engine — parent role (water marks, close classify, fire_children, daily reset)

**Files:**
- Modify: `strategy_engine.py`
- Test: `tests/test_strategy_engine.py`

**Interfaces:**
- Consumes: `_trigger_aggregate`, `get_runtime`, `find_strategy_positions`, `Candidate`, `short_right_for`, `state.executions`.
- Produces: `classify_parent_close(strat, cand, state) -> str`, `_refresh_trade_credit(state, trade)`, `_latch_time_triggers(strat, state)`, `fire_children(parent, state, broadcast_fn=None)`, `_update_parent_role(name, state, broadcast_fn=None)`, `_daily_reset(state)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_strategy_engine.py`:

```python
import asyncio
from strategy_engine import (classify_parent_close, _refresh_trade_credit,
                             _update_parent_role, _daily_reset, fire_children)


def _pos_state(positions=None, executions=None):
    class S:
        pass
    s = S()
    s.positions = positions or []
    s.executions = executions or []
    s.expiration = "20260821"
    return s


def _cand():
    return Candidate(direction="bull_put", short_strike=5100.0, long_strike=5000.0, width_points=100.0,
                     margin=10000.0, credit_bid=0.2, credit_ask=0.4, credit_mid=0.30,
                     short_delta=0.3, long_delta=0.1, atm_iv=18.0)


def test_classify_stop_loss_when_configured():
    strat = Strategy(name="p", direction="bull_put", conditions=[],
                     exit_rules=ExitRules(stop_loss=StopLoss(multiplier=5.0)))
    assert classify_parent_close(strat, _cand(), _pos_state()) == "stop_loss"


def test_classify_manual_when_no_stop_and_future_expiry():
    from datetime import timedelta
    from market_hours import now_et
    strat = Strategy(name="p", direction="bull_put", conditions=[])
    st = _pos_state()
    st.expiration = (now_et().date() + timedelta(days=1)).strftime("%Y%m%d")  # future -> not expired
    assert classify_parent_close(strat, _cand(), st) == "manual"


def test_classify_expire_when_past_expiry():
    from datetime import timedelta
    from market_hours import now_et
    strat = Strategy(name="p", direction="bull_put", conditions=[])
    st = _pos_state()
    st.expiration = (now_et().date() - timedelta(days=1)).strftime("%Y%m%d")  # past -> expired
    assert classify_parent_close(strat, _cand(), st) == "expire"


def test_refresh_credit_from_executions():
    trade = {"candidate": {"direction": "bull_put", "short_strike": 5100.0, "long_strike": 5000.0,
                           "credit_mid": 0.30}, "credit": 0.30}
    execs = [
        {"strike": 5100.0, "right": "P", "side": "SLD", "price": 0.35},
        {"strike": 5000.0, "right": "P", "side": "BOT", "price": 0.05},
    ]
    _refresh_trade_credit(_pos_state(executions=execs), trade)
    assert trade["credit"] == pytest.approx(0.30, abs=0.001)


def test_update_parent_role_marks_done_and_fires_children():
    from strategy_models import ExitRules, StopLoss
    state = type("S", (), {})()
    state.strategies = {
        "master": Strategy(name="master", direction="bull_put", conditions=[],
                           exit_rules=ExitRules(stop_loss=StopLoss(multiplier=5.0))),
        "child": Strategy(name="child", direction="bull_put", conditions=[],
                          parent_name="master",
                          subsequent_triggers=[TriggerSpec(kind="parent_exit_reason", params={"reason": "stop_loss"})]),
    }
    state.positions = []   # no matching positions -> parent is closed
    state.executions = []
    state.expiration = "20260821"
    state.runtime = {"master": RuntimeState(entered=True)}
    state.runtime["master"].trade = {"candidate": _cand().to_dict(), "credit": 0.30,
                                     "high_water_mult": 0.0, "low_water_mult": 0.0}
    state.runtime["child"] = RuntimeState()
    fired = []
    async def bcast(m):
        fired.append(m)
    _update_parent_role("master", state, bcast)
    assert state.runtime["master"].done is True
    assert state.runtime["master"].trade["close_reason"] == "stop_loss"
    assert state.runtime["child"].time_met is False   # no time trigger
    assert any(m.get("type") == "strategy_trigger" and m["data"]["name"] == "child"
               for m in fired)


def test_update_parent_role_updates_water_marks_while_open():
    state = type("S", (), {})()
    state.strategies = {"master": Strategy(name="master", direction="bull_put", conditions=[]),
                        "child": Strategy(name="child", direction="bull_put", conditions=[],
                                          parent_name="master")}
    state.positions = [{"contract": {"strike": 5100.0, "right": "P"}, "unrealizedPNL": 0.45},
                       {"contract": {"strike": 5000.0, "right": "P"}, "unrealizedPNL": 0.15}]
    state.executions = []
    state.expiration = "20260821"
    cd = _cand().to_dict()
    state.runtime = {"master": RuntimeState(entered=True)}
    state.runtime["master"].trade = {"candidate": cd, "credit": 0.30,
                                     "high_water_mult": 0.0, "low_water_mult": 0.0}
    state.runtime["child"] = RuntimeState()
    _update_parent_role("master", state)   # still open -> no close
    assert state.runtime["master"].done is False
    mult = (0.45 + 0.15) / 0.30
    assert state.runtime["master"].trade["high_water_mult"] == pytest.approx(mult)


def test_daily_reset_preserves_open_position():
    state = type("S", (), {})()
    state.strategies = {"master": Strategy(name="master", direction="bull_put", conditions=[])}
    state.positions = [{"contract": {"strike": 5100.0, "right": "P"}, "unrealizedPNL": 0.0},
                       {"contract": {"strike": 5000.0, "right": "P"}, "unrealizedPNL": 0.0}]
    state.executions = []
    state.expiration = "20260821"
    cd = _cand().to_dict()
    state.runtime = {"master": RuntimeState(entered=True)}
    state.runtime["master"].trade = {"candidate": cd, "credit": 0.30,
                                     "high_water_mult": 0.0, "low_water_mult": 0.0}
    _daily_reset(state)
    assert state.runtime["master"].entered is True       # open -> preserved
    assert state.runtime["master"].trade is not None


def test_daily_reset_resets_closed() :
    state = type("S", (), {})()
    state.strategies = {"master": Strategy(name="master", direction="bull_put", conditions=[])}
    state.positions = []   # closed
    state.executions = []
    state.expiration = "20260821"
    cd = _cand().to_dict()
    state.runtime = {"master": RuntimeState(entered=True, done=True)}
    state.runtime["master"].trade = {"candidate": cd, "credit": 0.30,
                                     "high_water_mult": 0.0, "low_water_mult": 0.0}
    _daily_reset(state)
    assert state.runtime["master"].entered is False
    assert state.runtime["master"].trade is None
```

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/test_strategy_engine.py::test_classify_stop_loss_when_configured -v`
Expected: FAIL (`ImportError`)

- [ ] **Step 3: Implement**

`strategy_engine.py` — add (near `find_strategy_positions` / before `strategy_evaluation_loop`):

```python
def classify_parent_close(strat: Strategy, cand: Candidate, state) -> str:
    """Why did the parent's trade close? TP is set by take_profit_loop; here we
    infer the fallback: the IB stop bracket if configured, else expire/manual."""
    if strat.exit_rules.stop_loss is not None:
        return "stop_loss"
    exp = getattr(state, "expiration", "")
    if exp and exp <= now_et().strftime("%Y%m%d"):
        return "expire"
    return "manual"


def _refresh_trade_credit(state, trade: dict) -> None:
    """Best-effort actual-credit update from fills (side SLD = short leg)."""
    cand = trade.get("candidate") or {}
    if not cand:
        return
    right = short_right_for(cand.get("direction", "bull_put"))
    short_strike = cand.get("short_strike")
    long_strike = cand.get("long_strike")
    short_fill = long_fill = None
    for ex in getattr(state, "executions", []) or []:
        if ex.get("strike") == short_strike and ex.get("right") == right and ex.get("side") == "SLD":
            short_fill = ex.get("price")
        if ex.get("strike") == long_strike and ex.get("right") == right and ex.get("side") == "BOT":
            long_fill = ex.get("price")
    if short_fill is not None and long_fill is not None:
        trade["credit"] = round(float(short_fill) - float(long_fill), 4)


def _latch_time_triggers(strat: Strategy, state) -> None:
    """While the parent is open, latch each child's time-of-day window."""
    prt = get_runtime(state, strat.name)
    hm = now_et().strftime("%H:%M")
    for child in state.strategies.values():
        if child.parent_name != strat.name:
            continue
        crt = get_runtime(state, child.name)
        if crt.parent_cycle != prt.cycle:
            crt.time_met = False
            crt.parent_cycle = prt.cycle
        for t in child.subsequent_triggers:
            if t.kind == "time_of_day" and t.enabled:
                start, end = t.params.get("start", "09:30"), t.params.get("end", "15:30")
                if start <= hm <= end:
                    crt.time_met = True


def fire_children(parent: Strategy, state, broadcast_fn=None) -> None:
    """Evaluate children's triggers at the parent's close and announce eligible ones."""
    for child in state.strategies.values():
        if child.parent_name != parent.name:
            continue
        if _trigger_aggregate(child, state):
            if broadcast_fn is not None:
                broadcast_fn({"type": "strategy_trigger",
                              "data": {"name": child.name, "event": "eligible"}})


def _update_parent_role(name: str, state, broadcast_fn=None) -> None:
    """Maintain a strategy's parent role while positioned; on close, classify the
    reason and fire children. Idempotent via done."""
    strat = state.strategies.get(name)
    rt = get_runtime(state, name)
    if strat is None or rt.done or rt.trade is None:
        return
    trade = rt.trade
    cand = Candidate(**trade["candidate"])
    positions = find_strategy_positions(cand, state)
    if len(positions) >= 2:
        # still open: refresh actual credit + water marks + time latches
        _refresh_trade_credit(state, trade)
        net_pnl = sum(float(p.get("unrealizedPNL", 0) or 0) for p in positions)
        credit = float(trade.get("credit") or 0.0)
        if credit > 0:
            mult = net_pnl / credit
            trade["high_water_mult"] = max(float(trade.get("high_water_mult", 0.0)), mult)
            trade["low_water_mult"] = min(float(trade.get("low_water_mult", 0.0)), mult)
        _latch_time_triggers(strat, state)
        return
    # closed
    trade["close_ts"] = time.monotonic()
    trade["close_reason"] = classify_parent_close(strat, cand, state)
    rt.done = True
    fire_children(strat, state, broadcast_fn)


def _daily_reset(state) -> None:
    """Start a fresh cycle each day, preserving any still-open position."""
    for name, strat in state.strategies.items():
        rt = get_runtime(state, name)
        if rt.trade is None:
            continue
        cand = Candidate(**rt.trade["candidate"])
        if len(find_strategy_positions(cand, state)) >= 2:
            continue   # open -> preserve
        rt.cycle += 1
        rt.entered = False
        rt.done = False
        rt.trade = None
        rt.time_met = False
        rt.parent_cycle = 0
```

Make sure `time`, `now_et`, `find_strategy_positions`, `short_right_for`, `Candidate`, `ExitRules`, `StopLoss` are imported at the top of `strategy_engine.py` (add the model imports if missing).

- [ ] **Step 4: Run to verify they pass**

Run: `pytest tests/test_strategy_engine.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add strategy_engine.py tests/test_strategy_engine.py
git commit -m "feat: parent-role maintenance + close classification + fire_children"
```

---

### Task 6: Engine — rework eval loop + TP loop

**Files:**
- Modify: `strategy_engine.py`
- Test: `tests/test_strategy_engine.py`

**Interfaces:**
- Consumes: `_child_is_eligible`, `_update_parent_role`, `_daily_reset`, `get_runtime`, `reset_strategy_runtime`, `find_strategy_positions`.
- Produces: reworked `strategy_evaluation_loop` (one-shot + child gate + parent-role tick), `take_profit_loop` sets `close_reason="take_profit"` and calls `fire_children`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_strategy_engine.py`:

```python
@pytest.mark.asyncio
async def test_eval_loop_child_enters_once_eligible(monkeypatch):
    import asyncio
    from datetime import date
    from strategy_engine import strategy_evaluation_loop, StrategyEval, Candidate
    from strategy_models import TriggerSpec, RuntimeState

    state = type("S", (), {})()
    state.connected = True
    state.account_summary = {"ExcessLiquidity": 100000.0}
    state.expirations = [date.today().strftime("%Y%m%d")]
    state.strategy_open_positions = {}
    state.strategy_log = []
    state.auto_trade_kill_switch = False
    state.runtime = {}
    state.vix = 20.0
    state.spx_price = 5200.0
    state.chain_quotes_cache = {"strikes": []}

    parent = Strategy(name="master", direction="bull_put", conditions=[], armed=True, auto_execute=True,
                      run_days=[date.today().weekday()], short_day_enabled=True, run_on_fomc=True, run_on_nfp=True)
    child = Strategy(name="child", direction="bull_put", conditions=[], armed=True, auto_execute=True,
                     parent_name="master",
                     subsequent_triggers=[TriggerSpec(kind="parent_exit_reason", params={"reason": "stop_loss"})],
                     run_days=[date.today().weekday()], short_day_enabled=True, run_on_fomc=True, run_on_nfp=True)
    state.strategies = {"master": parent, "child": child}

    # Parent has traded and CLOSED (flat) this cycle; the stop-loss trigger fired.
    state.runtime["master"] = RuntimeState(entered=True, done=True)
    state.runtime["master"].trade = {"candidate": {"direction": "bull_put", "short_strike": 5100.0,
                                                   "long_strike": 5000.0, "credit_mid": 0.30},
                                     "credit": 0.30, "high_water_mult": 0.0, "low_water_mult": 0.0,
                                     "close_reason": "stop_loss"}
    state.runtime["child"] = RuntimeState()

    placed = []
    cand = Candidate(direction="bull_put", short_strike=5100.0, long_strike=5000.0, width_points=100.0,
                     margin=10000.0, credit_bid=0.2, credit_ask=0.4, credit_mid=0.30,
                     short_delta=0.3, long_delta=0.1, atm_iv=18.0)
    async def fake_place(ib, s, strat, candidate):
        placed.append(strat.name)
        return {"type": "order_status", "data": {"status": "Filled"}}
    def fake_eval(strategy, s, now=None, candidates=None):
        return StrategyEval(status="ready", candidates=[cand])
    async def fake_bcast(message):
        pass
    monkeypatch.setattr("strategy_engine.place_strategy_entry", fake_place)
    monkeypatch.setattr("strategy_engine.evaluate_conditions", fake_eval)

    # The master must NOT enter (already done/entered); the child must NOT be
    # double-entered after entry (one-shot). Each arm/child gate enforced in-loop.
    # We run two cadences: first builds candidates, only the child places.
    # To prove one-shot, we manually mark the child entered after the first run
    # is prevented by the loop guard itself — assert only one placement.

    # First, suppress _strategy_should_run_today so gate is a no-op:
    monkeypatch.setattr("strategy_engine._strategy_should_run_today", lambda strat, s, today=None: True)
    monkeypatch.setattr("strategy_engine._daily_reset", lambda s: None)

    task = asyncio.create_task(strategy_evaluation_loop(None, state, fake_bcast))
    await asyncio.sleep(3.5)
    await asyncio.sleep(3.5)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert placed == ["child"]   # only the eligible child placed; parent skipped; child once
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_strategy_engine.py::test_eval_loop_child_enters_once_eligible -v`
Expected: FAIL (loop currently places both the parent and has no child gate)

- [ ] **Step 3: Implement — rework `strategy_evaluation_loop`**

Replace the body of `strategy_evaluation_loop` as follows (keep the outer `while True` / `asyncio.sleep(interval)` / `CancelledError` / generic exception handling):

```python
async def strategy_evaluation_loop(ib, state, broadcast_fn):
    interval = 3.0
    while True:
        try:
            await asyncio.sleep(interval)
            if not getattr(state, "connected", False):
                continue
            today = now_et().date()
            day_key = today.isoformat()
            if getattr(state, "day_key", None) != day_key:
                _daily_reset(state)
                state.day_key = day_key
            for name, strat in list(state.strategies.items()):
                if not strat.armed:
                    continue
                if not _strategy_should_run_today(strat, state):
                    state.strategy_candidates[name] = []
                    continue
                rt = get_runtime(state, name)
                if rt.entered or rt.done or name in state.strategy_open_positions:
                    # this cycle's one trade is used (or a position is awaiting
                    # close); keep the parent role live and do not re-enter
                    _update_parent_role(name, state, broadcast_fn)
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
                        logger.info(f"{strat.name}: insufficient margin/budget, skipping auto entry")
                        continue
                    try:
                        resp = await place_strategy_entry(ib, state, strat, best)
                        if (resp.get("data") or {}).get("status", "") not in ("Error", ""):
                            rt.entered = True
                            rt.trade = {
                                "candidate": best.to_dict(),
                                "credit": float(best.credit_mid),
                                "open_ts": time.monotonic(),
                                "close_ts": None,
                                "close_reason": None,
                                "high_water_mult": 0.0,
                                "low_water_mult": 0.0,
                            }
                            await broadcast_fn({"type": "strategy_exit", "data": {"name": name, "event": "auto_entry"}})
                    except Exception as e:
                        logger.error(f"Auto entry failed for {name}: {e}")
                elif not strat.auto_execute:
                    await broadcast_fn({"type": "strategy_candidate",
                                        "data": {"name": name, "candidates": [c.to_dict() for c in ev.candidates]}})
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"strategy_evaluation_loop error: {e}")
            await asyncio.sleep(3)
```

**Note:** the one-position guard is now enforced by `rt.entered or rt.done` (not `state.strategy_open_positions` alone). Keep the existing `state.strategy_open_positions` bookkeeping in `place_strategy_entry` as-is (it also broadcasts/locks against the old guard path used elsewhere).

- [ ] **Step 4: Implement — TP loop tweak**

In `take_profit_loop`, replace the close branch so it records the reason and fires children:

```python
                closed = await maybe_flatten_at_take_profit(ib, state, cand, positions, tp)
                if closed:
                    rt = get_runtime(state, name)
                    rt.done = True
                    if rt.trade is not None:
                        rt.trade["close_ts"] = time.monotonic()
                        rt.trade["close_reason"] = "take_profit"
                    state.strategy_open_positions.pop(name, None)
                    await broadcast_fn({"type": "strategy_exit", "data": {"name": name, "event": "take_profit"}})
                    fire_children(strat, state, broadcast_fn)
```

- [ ] **Step 5: Run to verify they pass**

Run: `pytest tests/test_strategy_engine.py -v`
Expected: PASS (including the pre-existing `test_eval_loop_skips_strategy_with_open_position`)

- [ ] **Step 6: Commit**

```bash
git add strategy_engine.py tests/test_strategy_engine.py
git commit -m "feat: one-shot eval loop + child gate + TP reasons + fire children"
```

---

### Task 7: WS — re-arm resets runtime

**Files:**
- Modify: `ws_handler.py`
- Test: `tests/test_strategy_ws.py`

**Interfaces:**
- Consumes: `reset_strategy_runtime` from `strategy_engine`.
- Produces: `strategy_arm` resets the strategy's runtime cycle.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_strategy_ws.py`:

```python
@pytest.mark.asyncio
async def test_strategy_arm_resets_runtime(app_state):
    from strategy_engine import get_runtime
    app_state.strategies["a"] = Strategy(name="a", direction="bull_put", conditions=[])
    rt = get_runtime(app_state, "a")
    rt.entered = True
    rt.done = True
    ws = FakeWS(["strategy_arm:a"])

    await websocket_endpoint(ws, None, app_state, _noop_broadcast)

    assert app_state.strategies["a"].armed is True
    assert rt.entered is False      # re-arm resets the one-shot latch
    assert rt.trade is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_strategy_ws.py::test_strategy_arm_resets_runtime -v`
Expected: FAIL (`rt.entered` still True)

- [ ] **Step 3: Implement**

In `ws_handler.py`, add the import and update the arm handler:

```python
from strategy_engine import reset_strategy_runtime
```

In the `elif msg.startswith("strategy_arm:")` branch:

```python
                elif msg.startswith("strategy_arm:"):
                    s = state.strategies.get(_unquote_name(msg.split(":", 1)[1]))
                    if s is not None:
                        s.armed = True
                        reset_strategy_runtime(state, s.name)
```

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/test_strategy_ws.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add ws_handler.py tests/test_strategy_ws.py
git commit -m "feat: re-arm resets strategy runtime cycle"
```

---

### Task 8: Frontend — tree list, parent selector, trigger editor, logic

**Files:**
- Modify: `static/js/strategy-ui.js`
- Test: no JS test runner exists; verify manually (see Step 4).

**Interfaces:**
- Consumes: `strategy_save` body now carrying `parent_name`, `subsequent_triggers`, `trigger_logic`. `strategy_trigger` WS event.
- Produces: a tree-ordered list, a parent dropdown, a trigger list editor, an Any/All radio.

- [ ] **Step 1: Add trigger field schema**

Near the top of `strategy-ui.js` next to `CONDITION_DEFS`, add:

```js
const TRIGGER_DEFS = {
    time_of_day: {
        help: 'Time window (ET) in which the follow-up may fire after the parent closes.',
        opDriven: false,
        fields: [
            { key: 'start', label: 'Start (HH:MM)', type: 'text', default: '09:30', required: false },
            { key: 'end', label: 'End (HH:MM)', type: 'text', default: '15:30', required: false },
        ],
    },
    parent_exit_reason: {
        help: 'Fire when the parent closes for this reason.',
        opDriven: false,
        fields: [
            { key: 'reason', label: 'Parent exit', type: 'enum', options: ['stop_loss', 'take_profit'], default: 'stop_loss', required: true },
        ],
    },
    parent_unrealized_pnl: {
        help: 'Fire when the parent trade is up gain_multiple× or down loss_multiple× the credit it actually collected.',
        opDriven: false,
        fields: [
            { key: 'gain_multiple', label: 'Gain × credit', type: 'number', step: '0.1', optional: true },
            { key: 'loss_multiple', label: 'Loss × credit', type: 'number', step: '0.1', optional: true },
        ],
    },
};

let _triggerEditing = null;   // {index} while a subsequent-trigger editor is open
```

- [ ] **Step 2: Render the subsection in the editor**

Inside `renderStrategyEditor(name)`, after the `Conditions` block and before `Exit rules`, add a subsequent-strategy panel:

```js
            <hr/>
            <label class="caps"><input type="checkbox" id="isSubsequent" ${s.parent_name ? 'checked' : ''} onchange="toggleSubsequent()"> This is a subsequent strategy</label>
            <div id="subsequentPanel" style="${s.parent_name ? '' : 'display:none'}">
              <label>Parent strategy</label>
              <select id="subParent">${state.strategies.filter(x => x.name !== s.name && !isDescendant(s.name, x.name))
                  .map(x => `<option value="${x.name}" ${s.parent_name === x.name ? 'selected' : ''}>${esc(x.name)}</option>`).join('')}</select>
              <label>Trigger logic</label>
              <select id="subLogic">
                <option value="any" ${s.trigger_logic === 'any' ? 'selected' : ''}>Any</option>
                <option value="all" ${s.trigger_logic === 'all' ? 'selected' : ''}>All</option>
              </select>
              <label>Subsequent triggers</label>
              <ol id="subTriggerList">${(s.subsequent_triggers || []).map((t, i) =>
                  `<li>${esc(t.kind)} ${t.enabled ? '' : '(disabled)'}
                     <button onclick="removeTrigger(${i})">x</button>
                     <button onclick="editTrigger(${i})">edit</button></li>`).join('')}</ol>
              <button onclick="addTrigger()">+ Add Trigger</button>
              <div id="triggerEditor"></div>
            </div>
```

- [ ] **Step 3: Add the handlers**

Add to `strategy-ui.js`:

```js
function isDescendant(name, maybeChild) {
    // True if maybeChild is a descendant of name (prevents parent=own child cycles in the dropdown).
    if (!state.strategies) return false;
    let cur = state.strategies.find(x => x.name === maybeChild);
    while (cur && cur.parent_name) {
        if (cur.parent_name === name) return true;
        cur = state.strategies.find(x => x.name === cur.parent_name);
    }
    return false;
}

function toggleSubsequent() {
    const on = document.getElementById('isSubsequent').checked;
    const panel = document.getElementById('subsequentPanel');
    if (panel) panel.style.display = on ? '' : 'none';
}

function addTrigger() {
    const s = state.strategies.find(x => x.name === selectedStrategy());
    if (!s) return;
    s.subsequent_triggers = s.subsequent_triggers || [];
    s.subsequent_triggers.push({ kind: 'time_of_day', enabled: true, params: { start: '09:30', end: '15:30' } });
    renderStrategyEditor(s.name);
}

function removeTrigger(i) {
    const s = state.strategies.find(x => x.name === selectedStrategy());
    if (!s) return;
    s.subsequent_triggers.splice(i, 1);
    renderStrategyEditor(s.name);
}

function renderTriggerFields(i, kind, params) {
    const container = document.getElementById('triggerEditor');
    const def = TRIGGER_DEFS[kind];
    if (!container || !def) return;
    const p = params || {};
    let html = def.help ? `<div class="cond-help">${esc(def.help)}</div>` : '';
    html += def.fields.map(f => _condFieldHtml(f, p[f.key], f.default, 'trg_')).join('');
    container.innerHTML = html;
}

function editTrigger(i) {
    const s = state.strategies.find(x => x.name === selectedStrategy());
    if (!s) return;
    const t = s.subsequent_triggers[i];
    const el = document.getElementById('triggerEditor');
    if (!t || !el) return;
    _triggerEditing = { index: i };
    el.innerHTML = `
        <label>Trigger type</label>
        <select id="trgKind" onchange="triggerKindChanged(${i})">
          ${Object.keys(TRIGGER_DEFS).map(k => `<option value="${k}" ${t.kind === k ? 'selected' : ''}>${esc(k)}</option>`).join('')}
        </select>
        <div><label>Enabled</label><input type="checkbox" id="trgEnabled" ${t.enabled ? 'checked' : ''} /></div>
        <div id="triggerFields"></div>
        <button onclick="applyTrigger(${i})">Apply</button>`;
    renderTriggerFields(i, t.kind, t.params);
}

function triggerKindChanged(i) {
    const kind = document.getElementById('trgKind').value;
    const s = state.strategies.find(x => x.name === selectedStrategy());
    if (!s) return;
    _triggerEditing = { index: i };
    renderTriggerFields(i, kind, s.subsequent_triggers[i].params);
}

function applyTrigger(i) {
    const s = state.strategies.find(x => x.name === selectedStrategy());
    if (!s) return;
    const t = s.subsequent_triggers[i];
    if (!t) return;
    const kind = document.getElementById('trgKind').value;
    const def = TRIGGER_DEFS[kind];
    if (!def) return;
    const params = {};
    for (const f of def.fields) {
        const el = document.getElementById('cf_trg_' + f.key);
        if (el) {
            const raw = String(el.value || '').trim();
            if (raw === '' || /^n\/?a$/i.test(raw)) continue;
            params[f.key] = (f.type === 'number') ? Number(raw) : raw;
        }
    }
    t.kind = kind;
    t.enabled = document.getElementById('trgEnabled').checked;
    t.params = params;
    _triggerEditing = null;
    renderStrategyEditor(s.name);
}
```

- [ ] **Step 4: Read the new fields on save + hide orphan children**

In `readFormStrategy`, after building `strategy`, add (so `strategy_save` carries the child fields):

```js
            parent_name: document.getElementById('isSubsequent') && document.getElementById('isSubsequent').checked
                ? (document.getElementById('subParent').value || '') : '',
            subsequent_triggers: (s.subsequent_triggers || []).map(t => Object.assign({}, t)),
            trigger_logic: document.getElementById('subLogic') ? document.getElementById('subLogic').value : 'any',
```

In `renderStrategyList`, show children indented and indicate status:

```js
        nav.innerHTML = state.strategies.map(s => {
            const isChild = s.parent_name ? 'strat-child' : '';
            const sub = s.parent_name
                ? `<span class="strat-sub"> of ${esc(s.parent_name)}</span>` : '';
            return `<div class="strat-item ${isChild} ${s.name === _selStrategy ? 'active' : ''}" onclick="selectStrategy('${s.name}')">
                <span class="strat-name">${s.name}${sub}</span>
                <span class="strat-status">${s.armed ? (s.auto_execute ? 'auto' : 'scan') : 'off'}</span>
                <button onclick="event.stopPropagation(); armStrategy('${s.name}')">${s.armed ? 'Disarm' : 'Arm'}</button>
                <button onclick="event.stopPropagation(); deleteStrategy('${s.name}')">Del</button>
            </div>`; }).join('') || '<p>No strategies yet. Create one.</p>';
```

Handle the `strategy_trigger` message in `handleStrategyMessage`:

```js
            case 'strategy_trigger':
                showOrderToast('strategy ' + msg.data.name + ': ' + msg.data.event, 'info');
                break;
```

- [ ] **Step 5: Manual verification**

No JS test runner exists. Verify in the running app:

```bash
# from the project root
python -m uvicorn server:app --reload
```

Open `http://localhost:<port>`, go to Strategies: add a master strategy, then a second strategy with "This is a subsequent strategy" checked, pick the master as parent, add a trigger (e.g. `parent_exit_reason: stop_loss`), set logic Any, and Save. Confirm the list shows the child indented under its parent and that Save persists `parent_name`/`subsequent_triggers`/`trigger_logic` (visible in `config/strategies.json`). With the master armed + a stop-loss exit, confirm a `strategy_trigger` toast appears when the child becomes eligible.

- [ ] **Step 6: Commit**

```bash
git add static/js/strategy-ui.js
git commit -m "feat: subsequent-strategy tree UI + trigger editor"
```

---

## Post-Plan Verification

- [ ] Run the full suite: `pytest tests -v` — all green.
- [ ] Confirm `config/strategies.json` back-compat: loading the existing file (with no new fields) still yields valid strategies (defaults applied).

## Self-Review Notes

- **Spec coverage:** §4 model → Task 1; §5 triggers → Tasks 1/4; §6 lifecycle → Tasks 1/2/4/5/6/7; §7 trade record → Tasks 5/6; §8 close classification → Task 5; §9 eligibility/firing → Tasks 4/5; §10 engine flow → Task 6; §11 persistence/validation → Task 3; §12 WS → Tasks 7 (+ §12 in Task 6); §13 UI → Task 8; §14 safety → Tasks 4/6 (armed gates, margin, kill switch reused); §16 testing → all tasks.
- **Placeholder scan:** none — every code step carries real code.
- **Type consistency:** `get_runtime`/`reset_strategy_runtime`/`_child_is_eligible`/`_trigger_aggregate`/`classify_parent_close`/`_refresh_trade_credit`/`_latch_time_triggers`/`fire_children`/`_update_parent_role`/`_daily_reset` are defined once (Task 4/5) and consumed consistently (Tasks 6/7). `RuntimeState` fields (`cycle/entered/done/trade/time_met/parent_cycle`) are set/reset consistently. `trade["candidate"]` uses `Candidate.to_dict()`, rebuilt with `Candidate(**rt.trade["candidate"])`.
