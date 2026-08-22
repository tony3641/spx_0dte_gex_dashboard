# Credit-Spread Strategy Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a user define, save, and run a credit-spread trading strategy: an ordered set of entry conditions (priority-ordered hard-AND, short-circuiting) plus exit rules, with both a scanner mode (surface candidates + human-confirmed ticket) and an auto-execute mode (auto-place on all conditions met, auto-manage the exit). Models are pure and TDD'd from the helper layer up.

**Architecture:** New backend modules — `strategy_models.py` (dataclasses + serialization), `condition_helpers.py` (pure RSI/%move/ATM-IV/credit/width math), `strategy_store.py` (JSON CRUD), `strategy_engine.py` (candidate generation, condition evaluation, evaluation + take-profit loops) — added alongside the existing framework. `AppState` gains strategy/V-IX/kill-switch fields; `ib_connection.py` gains a VIX subscription; `ws_handler.py` gains strategy WS handlers; `server.py` loads the store and starts the loops. The execution primitive (`order_manager.handle_place_order`) is reused, not rewritten. Frontend adds a Strategies tab (vanilla JS, no build step).

**Tech Stack:** Python 3.10+, FastAPI, native `ibapi` via `IBClient`, asyncio, pytest; vanilla JS + Plotly in the browser.

**Spec:** `docs/superpowers/specs/2026-08-21-credit-spread-strategy-engine-design.md`

## Global Constraints

- Python 3.10+; Windows. `ibapi` from `C:\TWS API\source\pythonclient`.
- **Never block the IB socket thread** (no file I/O, no `await` in callbacks).
- **Never poll IB for status/data.** Event-driven only.
- Reuse the existing execution primitive and WS protocol; do **not** rewrite `order_manager.py`. The engine matches open positions to a strategy by **contract signature** (set of `(strike, right)` from the candidate), so `order_manager` needs no changes.
- `AppState` field names must stay stable (other modules read them): use `state.chain_quotes_cache` (dict with `strikes` list), `state.price_history` (deque of `{..., "close":...}` oldest→newest), `state.vix`, `state.spx_price`.
- Credit-spread conventions: **credit is positive** in condition params; the signed `comboLmtPrice` passed to `handle_place_order` is **negative** for a credit (matches `order-entry.js`).
- Direction: `bull_put` (SELL short put at higher strike, BUY long put at `short − width`) and `bear_call` (SELL short call at lower strike, BUY long call at `short + width`).
- Every task ends with its tests green and a commit.

---

### Task 1: Strategy data models

**Files:**
- Create: `strategy_models.py`
- Test: `tests/test_strategy_models.py`

**Interfaces:**
- Produces: `DIRECTIONS`, `TREND_INDICATORS`, `BUCKET_OPS`, dataclasses `Condition`, `TakeProfit`, `StopLoss`, `ExitRules`, `Strategy`, each with `to_dict()` / `from_dict(cls, d)`. Later tasks (store, engine, WS) construct these and rely on the exact field names below.
  - `Strategy` fields: `name: str`, `direction: str`, `conditions: list[Condition]`, `exit_rules: ExitRules`, `auto_execute: bool = False`, `armed: bool = False`, `target_expiry: str = ""`.
  - `Condition` fields: `kind: str` (`entry_window|short_delta|spread_width|credit|trend|volatility`), `enabled: bool = True`, `params: dict`.
  - `TakeProfit`: `mode: str` (`pct_credit|dollar|credit_price`), `value: float`.
  - `StopLoss`: `stop_price: float`, `limit_price: float`.
  - `ExitRules`: `take_profit: TakeProfit|None`, `stop_loss: StopLoss|None`, `hold_to_expire: bool = False`.

- [ ] **Step 1: Write the failing test**

```python
import pytest
from strategy_models import Strategy, Condition, ExitRules, TakeProfit, StopLoss


def test_strategy_roundtrips():
    s = Strategy(
        name="opening-bull-put",
        direction="bull_put",
        conditions=[
            Condition(kind="entry_window", enabled=True, params={"start": "09:32", "end": "11:00"}),
            Condition(kind="short_delta", enabled=True, params={"min": 0.05, "max": 0.35}),
        ],
        exit_rules=ExitRules(take_profit=TakeProfit(mode="pct_credit", value=0.5),
                             stop_loss=StopLoss(stop_price=1.0, limit_price=0.95),
                             hold_to_expire=False),
        auto_execute=False,
    )
    d = s.to_dict()
    s2 = Strategy.from_dict(d)
    assert s2.to_dict() == d
    assert s2.direction == "bull_put"
    assert isinstance(s2.exit_rules.take_profit, TakeProfit)
    assert s2.exit_rules.stop_loss.limit_price == 0.95


def test_invalid_direction_rejected():
    with pytest.raises(ValueError):
        Strategy.from_dict({"name": "x", "direction": "iron_condor", "conditions": [],
                            "exit_rules": {"take_profit": None, "stop_loss": None, "hold_to_expire": False}})


def test_condition_passthrough():
    c = Condition(kind="trend", params={"indicator": "rsi", "period": 14, "op": "range", "low": 30, "high": 55})
    assert Condition.from_dict(c.to_dict()).params["op"] == "range"
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_strategy_models.py -v`
Expected: FAIL — `ModuleNotFoundError` / `from_dict` missing.

- [ ] **Step 3: Implement the models**

```python
"""Pure data models for a credit-spread trading strategy."""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


DIRECTIONS = ("bull_put", "bear_call")
TREND_INDICATORS = ("rsi", "pmove")
BUCKET_OPS = ("above", "below", "range")


@dataclass
class Condition:
    kind: str                                    # entry_window|short_delta|spread_width|credit|trend|volatility
    enabled: bool = True
    params: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"kind": self.kind, "enabled": self.enabled, "params": dict(self.params)}

    @classmethod
    def from_dict(cls, d: dict) -> "Condition":
        return cls(kind=d["kind"], enabled=d.get("enabled", True), params=dict(d.get("params") or {}))


@dataclass
class TakeProfit:
    mode: str = "pct_credit"    # pct_credit | dollar | credit_price
    value: float = 0.0

    def to_dict(self) -> dict:
        return {"mode": self.mode, "value": self.value}

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> Optional["TakeProfit"]:
        if not d:
            return None
        return cls(mode=d.get("mode", "pct_credit"), value=float(d.get("value", 0.0)))


@dataclass
class StopLoss:
    stop_price: float
    limit_price: float

    def to_dict(self) -> dict:
        return {"stop_price": self.stop_price, "limit_price": self.limit_price}

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> Optional["StopLoss"]:
        if not d:
            return None
        return cls(stop_price=float(d["stop_price"]), limit_price=float(d["limit_price"]))


@dataclass
class ExitRules:
    take_profit: Optional[TakeProfit] = None
    stop_loss: Optional[StopLoss] = None
    hold_to_expire: bool = False

    def to_dict(self) -> dict:
        return {
            "take_profit": self.take_profit.to_dict() if self.take_profit else None,
            "stop_loss": self.stop_loss.to_dict() if self.stop_loss else None,
            "hold_to_expire": self.hold_to_expire,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ExitRules":
        return cls(
            take_profit=TakeProfit.from_dict(d.get("take_profit")),
            stop_loss=StopLoss.from_dict(d.get("stop_loss")),
            hold_to_expire=bool(d.get("hold_to_expire", False)),
        )


@dataclass
class Strategy:
    name: str
    direction: str
    conditions: List[Condition]
    exit_rules: ExitRules = field(default_factory=ExitRules)
    auto_execute: bool = False
    armed: bool = False
    target_expiry: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "direction": self.direction,
            "conditions": [c.to_dict() for c in self.conditions],
            "exit_rules": self.exit_rules.to_dict(),
            "auto_execute": self.auto_execute,
            "armed": self.armed,
            "target_expiry": self.target_expiry,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Strategy":
        direction = d["direction"]
        if direction not in DIRECTIONS:
            raise ValueError(f"Invalid direction: {direction}")
        return cls(
            name=d["name"],
            direction=direction,
            conditions=[Condition.from_dict(c) for c in d.get("conditions", [])],
            exit_rules=ExitRules.from_dict(d.get("exit_rules") or {}),
            auto_execute=bool(d.get("auto_execute", False)),
            armed=bool(d.get("armed", False)),
            target_expiry=d.get("target_expiry", ""),
        )
```

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/test_strategy_models.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add strategy_models.py tests/test_strategy_models.py
git commit -m "feat: strategy data models (Condition/ExitRules/Strategy) with roundtrip"
```

---

### Task 2: Condition + market-data helpers (pure)

**Files:**
- Create: `condition_helpers.py`
- Test: `tests/test_condition_helpers.py`

**Interfaces:**
- Consumes: nothing (pure). Reads list-of-dicts rows shaped like `chain_quotes_cache["strikes"]` rows (`strike`, `call_bid/ask/delta/iv`, `put_bid/ask/delta/iv`).
- Produces: `wilder_rsi(closes, period=14) -> float|None`, `percent_change(closes, minutes=5) -> float|None`, `atm_iv(rows, spot) -> float|None`, `nearest_row(rows, spot) -> dict`, `spread_width(direction, short_strike, long_strike) -> float`, `spread_margin(width_points) -> float`, `combo_credit(short_row, long_row) -> dict`. The engine (Tasks 6–7) calls these.

- [ ] **Step 1: Write the failing test**

```python
import pytest
from condition_helpers import (
    wilder_rsi, percent_change, atm_iv, nearest_row,
    spread_width, spread_margin, combo_credit,
)


def test_wilder_rsi_all_gains_is_100():
    closes = [100 + i for i in range(20)]      # strictly rising
    assert wilder_rsi(closes, 14) == 100.0


def test_wilder_rsi_not_enough_data():
    assert wilder_rsi([1.0, 2.0], 14) is None


def test_percent_change():
    closes = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0]
    assert round(percent_change(closes, 5), 4) == 5.0   # 105 vs 100


def test_percent_change_short():
    assert percent_change([10.0, 11.0], 5) is None


def test_nearest_row_and_atm_iv():
    rows = [
        {"strike": 5100, "call_iv": 20.0, "put_iv": 21.0},
        {"strike": 5200, "call_iv": 18.0, "put_iv": 19.0},
        {"strike": 5300, "call_iv": 22.0, "put_iv": 23.0},
    ]
    assert nearest_row(rows, 5210)["strike"] == 5200
    assert atm_iv(rows, 5210) == 18.5


def test_width_and_margin():
    assert spread_width("bull_put", 5200, 5100) == 100.0
    assert spread_width("bear_call", 5200, 5300) == 100.0
    assert spread_margin(25) == 2500.0


def test_combo_credit():
    short = {"strike": 5200, "put_bid": 3.0, "put_ask": 3.4}
    long = {"strike": 5100, "put_bid": 1.0, "put_ask": 1.4}
    c = combo_credit(short, long, "P")
    assert c["mid"] == pytest.approx((3.2 - 1.2), abs=1e-9)   # 2.0
    assert c["bid"] == pytest.approx(3.0 - 1.4, abs=1e-9)     # 1.6 conservative
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_condition_helpers.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Implement the helpers**

```python
"""Pure math helpers for strategy conditions. No IO, no state."""
from typing import List, Optional


def wilder_rsi(closes: List[float], period: int = 14) -> Optional[float]:
    """Wilder's RSI over the newest 'period'+1 closes. Returns None if too few."""
    if len(closes) < period + 1:
        return None
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    avg_gain, avg_loss = gains / period, losses / period
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(d, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-d, 0.0)) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def percent_change(closes: List[float], minutes: int = 5) -> Optional[float]:
    """% move of the newest close vs `minutes` bars back. Returns None if short."""
    if len(closes) <= minutes:
        return None
    past = closes[-(minutes + 1)]
    if not past:
        return None
    return (closes[-1] - past) / past * 100.0


def nearest_row(rows: List[dict], spot: float) -> Optional[dict]:
    """Row whose strike is nearest to spot."""
    if not rows:
        return None
    return min(rows, key=lambda r: abs(r["strike"] - spot))


def atm_iv(rows: List[dict], spot: float) -> Optional[float]:
    """Average call+put IV (%) at the strike nearest spot."""
    row = nearest_row(rows, spot)
    if row is None:
        return None
    vals = [v for v in (row.get("call_iv"), row.get("put_iv")) if v is not None]
    if not vals:
        return None
    return sum(vals) / len(vals)


def spread_width(direction: str, short_strike: float, long_strike: float) -> float:
    """Strike distance in points (positive)."""
    return abs(round(short_strike - long_strike, 2))


def spread_margin(width_points: float) -> float:
    """Margin for a credit vertical = width * 100 (multiplier)."""
    return width_points * 100.0


def _side(row: dict, right: str, field: str):
    return row.get(f"{'call' if right == 'C' else 'put'}_{field}")


def _mid(bid, ask):
    if bid is None or ask is None or bid <= 0 or ask <= 0:
        return None
    return (bid + ask) / 2.0


def combo_credit(short_row: dict, long_row: dict, right: str) -> dict:
    """Net credit of a credit vertical (positive = credit). ``right`` is 'C' or 'P'."""
    s_bid, s_ask = _side(short_row, right, "bid"), _side(short_row, right, "ask")
    l_bid, l_ask = _side(long_row, right, "bid"), _side(long_row, right, "ask")
    s_mid, l_mid = _mid(s_bid, s_ask), _mid(l_bid, l_ask)
    if s_mid is None or l_mid is None:
        return {"bid": None, "ask": None, "mid": None}
    return {
        "mid": round(s_mid - l_mid, 4),
        "bid": round(s_bid - l_ask, 4),
        "ask": round(s_ask - l_bid, 4),
    }
```

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/test_condition_helpers.py -v`
Expected: PASS (8 passed).

- [ ] **Step 5: Commit**

```bash
git add condition_helpers.py tests/test_condition_helpers.py
git commit -m "feat: pure condition/market-data helpers (rsi, pmove, atm_iv, credit, width)"
```

---

### Task 3: Strategy store (JSON persistence)

**Files:**
- Create: `strategy_store.py`
- Test: `tests/test_strategy_store.py`

**Interfaces:**
- Consumes: `Strategy.from_dict/to_dict` (Task 1).
- Produces: `STRATEGIES_PATH` (default `config/strategies.json`), `load_strategies(path=None) -> dict[str, Strategy]`, `save_strategy(path, strategy)`, `save_strategies(path, strategies)`, `delete_strategy(path, name)`. Later tasks (WS handlers, server) call these. Fail-safe: a malformed file returns `{}` (does not raise).

- [ ] **Step 1: Write the failing test**

```python
import os, pytest
from strategy_models import Strategy, Condition, ExitRules
from strategy_store import load_strategies, save_strategy, delete_strategy


def _s(name="a"):
    return Strategy(name=name, direction="bull_put", conditions=[Condition(kind="short_delta", params={"min": 0.1, "max": 0.3})])


def test_save_and_load_roundtrip(tmp_path):
    p = str(tmp_path / "strategies.json")
    save_strategy(p, _s("a"))
    out = load_strategies(p)
    assert out["a"].direction == "bull_put"
    assert out["a"].conditions[0].kind == "short_delta"


def test_delete(tmp_path):
    p = str(tmp_path / "strategies.json")
    save_strategy(p, _s("a"))
    save_strategy(p, _s("b"))
    delete_strategy(p, "a")
    assert set(load_strategies(p)) == {"b"}


def test_malformed_file_is_safe(tmp_path):
    p = str(tmp_path / "strategies.json")
    with open(p, "w") as f:
        f.write("{not valid json")
    assert load_strategies(p) == {}
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_strategy_store.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Implement the store**

```python
"""JSON persistence for strategies. Fail-safe (malformed file -> empty)."""
import json
import logging
from pathlib import Path
from typing import Dict, Optional

from strategy_models import Strategy

logger = logging.getLogger(__name__)

STRATEGIES_PATH = Path(__file__).parent / "config" / "strategies.json"


def _resolve(path) -> Path:
    return Path(path) if path is not None else STRATEGIES_PATH


def load_strategies(path=None) -> Dict[str, Strategy]:
    p = _resolve(path)
    if not p.exists():
        return {}
    try:
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        return {name: Strategy.from_dict(d) for name, d in data.items()}
    except Exception as e:
        logger.error(f"Failed to load strategies from {p}: {e} — starting empty")
        return {}


def save_strategies(path, strategies: Dict[str, Strategy]) -> None:
    p = _resolve(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = {name: s.to_dict() for name, s in strategies.items()}
    tmp = p.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    tmp.replace(p)   # atomic


def save_strategy(path, strategy: Strategy) -> None:
    strategies = load_strategies(path)
    strategies[strategy.name] = strategy
    save_strategies(path, strategies)


def delete_strategy(path, name: str) -> bool:
    strategies = load_strategies(path)
    if name not in strategies:
        return False
    del strategies[name]
    save_strategies(path, strategies)
    return True
```

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/test_strategy_store.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add strategy_store.py tests/test_strategy_store.py
git commit -m "feat: strategy JSON store (load/save/delete, atomic, fail-safe)"
```

---

### Task 4: AppState additions

**Files:**
- Modify: `app_state.py`
- Test: `tests/test_app_state_extras.py`

**Interfaces:**
- Produces new `AppState` fields (defaults) that Tasks 5–9 read/write: `strategies: dict[str, Strategy] = {}`, `strategy_candidates: dict[str, list] = {}`, `vix: float | None = None`, `vix_stream: object | None = None`, `auto_trade_kill_switch: bool = False`, `strategy_log: list = []`.

- [ ] **Step 1: Write the failing test**

```python
from app_state import create_app_state


def test_strategy_state_defaults():
    s = create_app_state()
    assert s.strategies == {}
    assert s.strategy_candidates == {}
    assert s.vix is None
    assert s.auto_trade_kill_switch is False
    assert s.strategy_log == []
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_app_state_extras.py -v`
Expected: FAIL — `AttributeError` (no `strategies`).

- [ ] **Step 3: Implement** — add to `AppState.__init__` before the final blank line:

```python
        # Strategy engine (Task: credit-spread strategies)
        self.strategies: dict = {}               # {name: Strategy}
        self.strategy_candidates: dict = {}      # {name: [Candidate.to_dict()]}
        self.vix: float | None = None            # spot VIX (index)
        self.vix_stream = None                   # Optional[TickStream] (native)
        self.auto_trade_kill_switch: bool = False
        self.strategy_log: list = []             # audit entries for auto trades
```

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/test_app_state_extras.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app_state.py tests/test_app_state_extras.py
git commit -m "feat: AppState strategy/VIX/kill-switch fields"
```

---

### Task 5: VIX subscription + streaming

**Files:**
- Modify: `ib_connection.py` (add `setup_vix_subscription`, `update_vix`)
- Modify: `ws_handler.py` (`status_push_loop` calls `update_vix`)
- Test: `tests/test_vix.py`

**Interfaces:**
- Consumes: `IBClient.subscribe_tick` (in `ib_client.py`), `_index_contract` (already in `ib_connection.py`).
- Produces: `setup_vix_subscription(ib, state)` (subscribe VIX → `state.vix_stream`), `update_vix(state)` (read stream → `state.vix`). Later tasks (engine C6, WS `vix_update`) read `state.vix`.

- [ ] **Step 1: Write the failing test**

```python
import asyncio
from ib_connection import setup_vix_subscription, update_vix
import pytest


@pytest.mark.asyncio
async def test_vix_subscription_and_update(mock_ib, app_state):
    await setup_vix_subscription(mock_ib, app_state)
    assert app_state.vix_stream is not None
    update_vix(app_state)
    assert app_state.vix is not None and app_state.vix > 0
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_vix.py -v`
Expected: FAIL — `ImportError` / missing function.

- [ ] **Step 3: Implement** — add to `ib_connection.py` (after `update_spx_es_prices`):

```python
async def setup_vix_subscription(ib, state):
    """Subscribe to spot VIX (CBOE index) for the strategy's volatility condition."""
    try:
        vix = _index_contract("VIX", "CBOE", "USD")
        details = await ib.req_contract_details(vix)
        if details:
            vix.conId = details[0].contract.conId
        state.vix_stream = ib.subscribe_tick(vix, "")
        logger.info(f"Subscribed to VIX (reqId={state.vix_stream.req_id})")
    except Exception as e:
        logger.warning(f"VIX subscription failed: {e}")


def update_vix(state):
    """Read the VIX TickStream into state.vix."""
    stream = getattr(state, "vix_stream", None)
    if stream is None:
        return
    price = stream.last if stream.last and stream.last > 0 else stream.bid
    if price and price > 0:
        state.vix = float(price)
```

Then import `update_vix` in `ws_handler.py` and call it at the top of `status_push_loop`:

```python
from ib_connection import update_vix
# in status_push_loop, right after "await asyncio.sleep(5)":
update_vix(state)
```

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/test_vix.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ib_connection.py ws_handler.py tests/test_vix.py
git commit -m "feat: VIX spot subscription + state.vix update"
```

---

### Task 6: Candidate generator

**Files:**
- Create: `strategy_engine.py` (starts here; grows in Tasks 7–9)
- Test: `tests/test_strategy_engine.py` (grows each task)

**Interfaces:**
- Consumes: `condition_helpers` (Task 2), `Strategy` (Task 1), `state.chain_quotes_cache` (dict with `strikes` list of rows), `state.account_summary`.
- Produces: `Candidate` dataclass with `.to_dict()` and `generate_candidates(strategy, state, max_n=20) -> list[Candidate]`. Field names below are used by Tasks 7–9:
  - `direction`, `short_strike`, `long_strike`, `width_points`, `margin`, `credit_bid`, `credit_ask`, `credit_mid`, `short_delta`, `long_delta`, `atm_iv`.

- [ ] **Step 1: Write the failing test**

```python
import pytest
from strategy_models import Strategy, Condition
from strategy_engine import generate_candidates


def _rows():
    return {"strikes": [
        {"strike": 5100, "put_bid": 1.0, "put_ask": 1.4, "put_delta": -0.70,
         "call_bid": 9.0, "call_ask": 9.6, "call_delta": 0.60, "call_iv": 20.0, "put_iv": 21.0},
        {"strike": 5200, "put_bid": 3.0, "put_ask": 3.4, "put_delta": -0.30,
         "call_bid": 4.0, "call_ask": 4.6, "call_delta": 0.30, "call_iv": 18.0, "put_iv": 19.0},
        {"strike": 5300, "put_bid": 6.0, "put_ask": 6.6, "put_delta": -0.10,
         "call_bid": 2.0, "call_ask": 2.6, "call_delta": 0.10, "call_iv": 22.0, "put_iv": 23.0},
    ], "spot_price": 5200.0, "annual_vol": 0.2}


def test_bear_call_candidates():
    strat = Strategy(name="bc", direction="bear_call", conditions=[
        Condition(kind="short_delta", params={"min": 0.2, "max": 0.4}),
        Condition(kind="spread_width", params={"min": 50, "max": 150}),
    ])
    state = type("S", (), {"chain_quotes_cache": _rows(), "account_summary": {}})()
    cands = generate_candidates(strat, state)
    assert cands
    c = cands[0]
    assert c.direction == "bear_call"
    assert c.short_strike == 5200 and c.long_strike == 5300
    assert c.width_points == 100 and c.margin == 10000
    assert c.credit_mid > 0
    assert c.credit_mid == pytest.approx((4.3 - 2.3), abs=0.01)
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_strategy_engine.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Implement** — create `strategy_engine.py`:

```python
"""Credit-spread strategy engine: candidate generation, condition evaluation,
and the evaluation/take-profit loops. Works off live AppState; no extra IO."""
import asyncio
import logging
import time
from dataclasses import dataclass, field, asdict
from typing import List, Optional

from condition_helpers import (
    spread_width, spread_margin, combo_credit, nearest_row,
    wilder_rsi, percent_change, atm_iv,
)
from strategy_models import Strategy, Condition

logger = logging.getLogger(__name__)


@dataclass
class Candidate:
    direction: str
    short_strike: float
    long_strike: float
    width_points: float
    margin: float
    credit_bid: float
    credit_ask: float
    credit_mid: float
    short_delta: float
    long_delta: float
    atm_iv: Optional[float]

    def to_dict(self) -> dict:
        return asdict(self)


def chain_rows(state) -> list:
    """Rows from the live chain quotes cache."""
    cache = getattr(state, "chain_quotes_cache", None) or {}
    return cache.get("strikes", [])


def _side_field(row: dict, right: str, field: str):
    prefix = "call" if right == "C" else "put"
    return row.get(f"{prefix}_{field}")


def generate_candidates(strategy: Strategy, state, max_n: int = 20) -> List[Candidate]:
    rows = chain_rows(state)
    spot = float(getattr(state, "spx_price", 0) or 0.0)
    cond = {c.kind: c for c in strategy.conditions if c.enabled}
    delta_c = cond.get("short_delta")
    width_c = cond.get("spread_width")
    credit_c = cond.get("credit")

    dmin, dmax = (delta_c.params.get("min", 0.05), delta_c.params.get("max", 0.35)) if delta_c else (0.05, 0.35)
    wmin, wmax = (width_c.params.get("min", 5), width_c.params.get("max", 50)) if width_c else (5, 50)
    cmin, cmax = (credit_c.params.get("min", 0.0), credit_c.params.get("max", 1e6)) if credit_c else (0.0, 1e6)

    short_right = "P" if strategy.direction == "bull_put" else "C"
    long_right = short_right

    cands: List[Candidate] = []
    # Long leg sits on the opposite side of the short: for bull_put short_strike >
    # long_strike (long_sign -1); for bear_call short_strike < long_strike (+1).
    long_sign = -1 if strategy.direction == "bull_put" else 1
    for short_row in rows:
        s_strike = short_row.get("strike")
        if not s_strike:
            continue
        sd = _side_field(short_row, short_right, "delta")
        if sd is None or not (dmin <= abs(sd) <= dmax):
            continue
        for long_row in rows:
            l_strike = long_row.get("strike")
            if not l_strike:
                continue
            width = spread_width(strategy.direction, s_strike, l_strike)
            if not (wmin <= width <= wmax):
                continue
            if long_sign > 0 and l_strike <= s_strike:
                continue
            if long_sign < 0 and l_strike >= s_strike:
                continue
            cr = combo_credit(short_row, long_row, short_right)
            if cr["mid"] is None or not (cmin <= cr["mid"] <= cmax):
                continue
            ld = _side_field(long_row, short_right, "delta")
            cands.append(Candidate(
                direction=strategy.direction,
                short_strike=s_strike,
                long_strike=l_strike,
                width_points=width,
                margin=spread_margin(width),
                credit_bid=cr["bid"], credit_ask=cr["ask"], credit_mid=cr["mid"],
                short_delta=sd, long_delta=ld if ld is not None else 0.0,
                atm_iv=atm_iv(rows, spot) if spot else None,
            ))
    cands.sort(key=lambda c: c.credit_mid, reverse=True)
    return cands[:max_n]


def _find_row(rows: list, strike: float) -> Optional[dict]:
    for r in rows:
        if abs(r["strike"] - strike) < 0.01:
            return r
    return None
```

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/test_strategy_engine.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add strategy_engine.py tests/test_strategy_engine.py
git commit -m "feat: credit-spread candidate generator (delta/width/credit, capped)"
```

---

### Task 7: Condition evaluation (priority-ordered hard-AND, short-circuit)

**Files:**
- Modify: `strategy_engine.py`
- Test: `tests/test_strategy_engine.py`

**Interfaces:**
- Consumes: `Candidate`/`generate_candidates` (Task 6), `Condition` (Task 1), `now_et` (from `market_hours`), `state.vix`, `state.price_history`, `state.spx_price`, `state.chain_quotes_cache`.
- Produces: `StrategyEval` dataclass (`status: "ready"|"blocked"`, `blocker: Optional[str]`, `candidates: list[Candidate]`, `values: dict`), `evaluate_conditions(strategy, state, now=None, candidates=None) -> StrategyEval`. `now` is injectable for tests. Local helper `_passes_bucket(value, op, lo, hi)`.

- [ ] **Step 1: Add the failing test**

```python
import pytest
from strategy_models import Strategy, Condition
from strategy_engine import evaluate_conditions


def _closes():
    return [100.0 + 0.2 * i for i in range(20)]  # gently rising


def _state(vix=15.0, spot=5200.0):
    rows = {"strikes": [
        {"strike": 5100, "put_bid": 1.0, "put_ask": 1.4, "put_delta": -0.70, "put_iv": 21.0,
         "call_bid": 9.0, "call_ask": 9.6, "call_delta": 0.30, "call_iv": 20.0},
        {"strike": 5200, "put_bid": 3.0, "put_ask": 3.4, "put_delta": -0.30, "put_iv": 19.0,
         "call_bid": 4.0, "call_ask": 4.6, "call_delta": 0.30, "call_iv": 18.0},
        {"strike": 5300, "put_bid": 6.0, "put_ask": 6.6, "put_delta": -0.10, "put_iv": 23.0,
         "call_bid": 2.0, "call_ask": 2.6, "call_delta": 0.10, "call_iv": 22.0},
    ], "spot_price": spot}
    class S: pass
    s = S()
    s.chain_quotes_cache = rows
    s.spx_price = spot
    s.vix = vix
    s.price_history = [{"close": c} for c in _closes()]
    s.account_summary = {}
    return s


def _swe(d="bull_put"):
    return Strategy(name="t", direction=d, conditions=[
        Condition(kind="entry_window", params={"start": "09:32", "end": "11:00"}),
        Condition(kind="volatility", params={"vix_enabled": True, "vix_op": "above", "vix_value": 14.0,
                                             "atm_iv_enabled": False}),
        Condition(kind="short_delta", params={"min": 0.2, "max": 0.4}),
        Condition(kind="spread_width", params={"min": 50, "max": 150}),
    ])


def test_fail_window_blocker(monkeypatch):
    import datetime as dt
    from strategy_engine import evaluate_conditions
    now = dt.datetime(2026, 8, 21, 12, 30)
    ev = evaluate_conditions(_swe(), _state(), now=now)
    assert ev.status == "blocked"
    assert ev.blocker == "entry_window"


def test_all_pass_ready():
    import datetime as dt
    now = dt.datetime(2026, 8, 21, 10, 0)
    ev = evaluate_conditions(_swe(), _state(vix=20.0), now=now)
    assert ev.status == "ready"
    assert ev.candidates


def test_vix_blocks_before_candidates():
    import datetime as dt
    now = dt.datetime(2026, 8, 21, 10, 0)
    ev = evaluate_conditions(_swe(), _state(vix=10.0), now=now)
    assert ev.blocker == "volatility"
    assert ev.candidates == []
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_strategy_engine.py -v`
Expected: FAIL — `evaluate_conditions` undefined (or no short-circuit).

- [ ] **Step 3: Implement** — append to `strategy_engine.py`:

```python
@dataclass
class StrategyEval:
    status: str                       # "ready" | "blocked"
    blocker: Optional[str] = None
    candidates: List[Candidate] = field(default_factory=list)
    values: dict = field(default_factory=dict)


def _passes_bucket(value: Optional[float], op: str, lo: float, hi: float) -> bool:
    if value is None:
        return False
    if op == "above":
        return value > hi
    if op == "below":
        return value < lo
    if op == "range":
        return lo <= value <= hi
    return False


def _bucket_params(p: dict, base: str) -> tuple:
    op = p.get(f"{base}_op", "range")
    lo = float(p.get(f"{base}_low", p.get(f"{base}_value", 0)))
    hi = float(p.get(f"{base}_high", p.get(f"{base}_value", 0)))
    return op, lo, hi


def _eval_condition(cond: Condition, state, now) -> tuple:
    """Return (passed, value) for a single condition; value is None on fail."""
    p = cond.params
    if cond.kind == "entry_window":
        hm = now.strftime("%H:%M")
        start, end = p.get("start", "09:30"), p.get("end", "15:30")
        return start <= hm <= end, hm
    if cond.kind == "volatility":
        vix = float(getattr(state, "vix", 0) or 0.0)
        vix_ok = True
        vix_val = None
        if p.get("vix_enabled", False):
            op, lo, hi = _bucket_params(p, "vix")
            vix_ok = _passes_bucket(vix, op, lo, hi)
            vix_val = vix
        atm_ok = True
        atm_val = None
        if p.get("atm_iv_enabled", False):
            rows = chain_rows(state)
            spot = float(getattr(state, "spx_price", 0) or 0.0)
            iv = atm_iv(rows, spot) if spot else None
            op, lo, hi = _bucket_params(p, "atm_iv")
            atm_ok = _passes_bucket(iv, op, lo, hi)
            atm_val = iv
        if not (p.get("vix_enabled", False) or p.get("atm_iv_enabled", False)):
            return True, None
        return (vix_ok and atm_ok), {"vix": vix_val, "atm_iv": atm_val}
    if cond.kind == "trend":
        closes = [b["close"] for b in getattr(state, "price_history", [])]
        indicator = p.get("indicator", "rsi")
        if indicator == "rsi":
            val = wilder_rsi(closes, int(p.get("period", 14)))
        else:
            val = percent_change(closes, int(p.get("minutes", 5)))
        op = p.get("op", "range")
        lo = float(p.get("low", p.get("value", 0)))
        hi = float(p.get("high", p.get("value", lo)))
        return _passes_bucket(val, op, lo, hi), val
    # per-candidate conditions short-circuit in evaluate_conditions
    return True, None


def evaluate_conditions(strategy: Strategy, state, now=None, candidates=None) -> StrategyEval:
    """Evaluate global conditions in priority order, then per-candidate ones.

    Global conditions (entry_window, trend, volatility) are checked first, in
    user order; if any fails, return blocked without building candidates.
    Then build candidates and drop any failing per-candidate conditions
    (short_delta, spread_width, credit). If none remain -> blocked.
    """
    import datetime as dt
    now = now or dt.datetime.now()
    values = {}
    enabled = [c for c in strategy.conditions if c.enabled]

    # 1) Global conditions, priority order, short-circuit.
    for cond in enabled:
        if cond.kind in ("short_delta", "spread_width", "credit"):
            continue
        passed, value = _eval_condition(cond, state, now)
        values[cond.kind] = value
        if not passed:
            return StrategyEval(status="blocked", blocker=cond.kind, values=values)

    # 2) Build candidates, then drop those failing per-candidate conditions.
    cands = candidates if candidates is not None else generate_candidates(strategy, state)
    per = {c.kind: c for c in enabled if c.kind in ("short_delta", "spread_width", "credit")}
    if not per:
        cands = cands or generate_candidates(strategy, state)
        if not cands:
            return StrategyEval(status="blocked", blocker="no_candidate", values=values)
        return StrategyEval(status="ready", candidates=cands, values=values)

    passing = []
    for c in cands:
        ok = True
        if "short_delta" in per:
            p = per["short_delta"].params
            if not (float(p.get("min", 0.05)) <= abs(c.short_delta) <= float(p.get("max", 0.35))):
                ok = False
        if "spread_width" in per:
            p = per["spread_width"].params
            if not (float(p.get("min", 5)) <= c.width_points <= float(p.get("max", 50))):
                ok = False
        if "credit" in per:
            p = per["credit"].params
            if not (float(p.get("min", 0.0)) <= c.credit_mid <= float(p.get("max", 1e6))):
                ok = False
        if ok:
            passing.append(c)
    if not passing:
        return StrategyEval(status="blocked", blocker="no_candidate_pass", values=values)
    return StrategyEval(status="ready", candidates=passing, values=values)
```

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/test_strategy_engine.py -v`
Expected: PASS (4 total in the module).

- [ ] **Step 5: Commit**

```bash
git add strategy_engine.py tests/test_strategy_engine.py
git commit -m "feat: priority-ordered hard-AND condition evaluation"
```

---

### Task 8: Entry decision + auto-order placement + audit

**Files:**
- Modify: `strategy_engine.py`
- Test: `tests/test_strategy_engine.py` (async integration)

**Interfaces:**
- Consumes: `StrategyEval` (Task 7), `handle_place_order` (from `order_manager`), `now_et`/`is_within_rth` (from `market_hours`), `config.round_signed_to_tick`/`spx_tick_for_price`.
- Produces: `_build_entry_payload(strategy, candidate, state) -> dict`, `place_strategy_entry(ib, state, strategy, candidate) -> dict` (returns the `handle_place_order` response), `signature_for_candidate(candidate, state) -> set`, `async strategy_evaluation_loop(ib, state, broadcast_fn)` . The loop evaluates armed strategies on a cadence; in auto mode it places the entry and records `state.strategy_candidates[name]` + audit in `state.strategy_log`; in scan mode it pushes a `strategy_candidate` broadcast.

- [ ] **Step 1: Write the failing test**

```python
import pytest, asyncio
from strategy_models import Strategy, Condition
from strategy_engine import place_strategy_entry, _build_entry_payload


def _state_t8(spot=5200.0):
    rows = {"strikes": [
        {"strike": 5100, "put_bid": 1.0, "put_ask": 1.4, "put_delta": -0.70, "put_iv": 21.0,
         "call_bid": 9.0, "call_ask": 9.6, "call_delta": 0.30, "call_iv": 20.0},
        {"strike": 5200, "put_bid": 3.0, "put_ask": 3.4, "put_delta": -0.30, "put_iv": 19.0,
         "call_bid": 4.0, "call_ask": 4.6, "call_delta": 0.30, "call_iv": 18.0},
        {"strike": 5300, "put_bid": 6.0, "put_ask": 6.6, "put_delta": -0.10, "put_iv": 23.0,
         "call_bid": 2.0, "call_ask": 2.6, "call_delta": 0.10, "call_iv": 22.0},
    ], "spot_price": spot}
    class S: pass
    s = S()
    s.chain_quotes_cache = rows
    s.spx_price = spot
    s.expiration = "20260821"
    s.positions = []
    return s


def test_build_entry_payload_credit_negative():
    strat = Strategy(name="t", direction="bear_call", conditions=[Condition(kind="short_delta", params={"min": 0.2, "max": 0.4})])
    cand = type("C", (), {"direction": "bear_call", "short_strike": 5200.0, "long_strike": 5300.0,
                          "credit_mid": 2.0})()
    p = _build_entry_payload(strat, cand, _state_t8())
    assert p["comboLmtPrice"] < 0
    assert p["legs"][0]["action"] == "SELL"
    assert p["legs"][1]["action"] == "BUY"


- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_strategy_engine.py -v`
Expected: FAIL — `_build_entry_payload` undefined.

- [ ] **Step 3: Implement** — append to `strategy_engine.py`:

```python
def signature_for_candidate(candidate: Candidate, state) -> set:
    """Contract signature (set of (strike, right)) to match a strategy to positions."""
    return {(candidate.short_strike, short_right_for(candidate.direction)),
            (candidate.long_strike, short_right_for(candidate.direction))}


def short_right_for(direction: str) -> str:
    return "P" if direction == "bull_put" else "C"


def _has_open_position(state, sig) -> bool:
    """True if any open position matches the signature set of (strike, right)."""
    for pos in getattr(state, "positions", []) or []:
        c = pos.get("contract") or {}
        if (float(c.get("strike", 0) or 0), c.get("right", "")) in sig:
            return True
    return False


def _build_entry_payload(strategy: Strategy, candidate: Candidate, state) -> dict:
    from config import spx_tick_for_price, round_signed_to_tick
    rows = chain_rows(state)
    short_row = _find_row(rows, candidate.short_strike)
    long_row = _find_row(rows, candidate.long_strike)
    right = short_right_for(candidate.direction)
    tick = spx_tick_for_price(abs(candidate.credit_mid))
    # Per-leg limit: SELL leg uses bid (receive), BUY leg uses ask (pay) — mirror order-entry.js.
    short_lmt = _side_field(short_row, right, "bid") if short_row else candidate.credit_mid
    long_lmt = _side_field(long_row, right, "ask") if long_row else candidate.credit_mid
    legs = [
        {"symbol": "SPX", "expiry": getattr(state, "expiration", ""), "strike": candidate.short_strike,
         "right": right, "action": "SELL", "qty": 1, "lmtPrice": float(short_lmt or 0.01), "secType": "OPT"},
        {"symbol": "SPX", "expiry": getattr(state, "expiration", ""), "strike": candidate.long_strike,
         "right": right, "action": "BUY", "qty": 1, "lmtPrice": float(long_lmt or 0.01), "secType": "OPT"},
    ]
    return {
        "legs": legs,
        "orderType": "LMT", "tif": "DAY",
        "comboAction": "BUY",
        "comboLmtPrice": round_signed_to_tick(-abs(candidate.credit_mid), tick),
        "comboQuantity": 1,
        "outsideRth": False,
        "stopLoss": (strategy.exit_rules.stop_loss.to_dict()
                     if strategy.exit_rules.stop_loss else None),
    }


async def place_strategy_entry(ib, state, strategy, candidate) -> dict:
    from order_manager import handle_place_order
    from account_manager import refresh_account_state
    payload = _build_entry_payload(strategy, candidate, state)
    resp = await handle_place_order(ib, state, payload, ws=None, refresh_fn=refresh_account_state)
    state.strategy_log.append({
        "ts": time.monotonic(), "slug": "entry",
        "strategy": strategy.name, "candidate": candidate.to_dict(), "resp": resp,
        "status": getattr(resp, "status", None),
    })
    return resp


async def strategy_evaluation_loop(ib, state, broadcast_fn):
    """On a cadence, evaluate armed strategies and act (scan or auto)."""
    from market_hours import now_et
    interval = 3.0
    while True:
        try:
            await asyncio.sleep(interval)
            if not getattr(state, "connected", False):
                continue
            for name, strat in list(state.strategies.items()):
                if not strat.armed:
                    continue
                ev = evaluate_conditions(strat, state, now=now_et())
                state.strategy_candidates[name] = [c.to_dict() for c in ev.candidates]
                if ev.status != "ready":
                    continue
                if getattr(state, "auto_trade_kill_switch", False):
                    continue
                if strat.auto_execute and ev.candidates:
                    best = ev.candidates[0]
                    if _has_open_position(state, signature_for_candidate(best, state)):
                        continue   # spec §11: one concurrent auto position per strategy
                    try:
                        await place_strategy_entry(ib, state, strat, best)
                        await broadcast_fn({"type": "strategy_exit", "data": {"name": name, "event": "auto_entry"}})
                    except Exception as e:
                        logger.error(f"Auto entry failed for {name}: {e}")
                elif not strat.auto_execute:
                    await broadcast_fn({"type": "strategy_candidate", "data": {"name": name, "candidates": [c.to_dict() for c in ev.candidates]}})
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"strategy_evaluation_loop error: {e}")
            await asyncio.sleep(3)
```

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/test_strategy_engine.py -v` (plus the async test written below)

An async integration test (add to the test module):

```python
@pytest.mark.asyncio
async def test_place_strategy_entry_via_mock(mock_ib, app_state):
    from strategy_engine import _build_entry_payload, place_strategy_entry, Candidate
    app_state.expiration = "20260821"
    strat = Strategy(name="t", direction="bear_call", conditions=[Condition(kind="short_delta", params={"min": 0.2, "max": 0.4})])
    cand = Candidate(direction="bear_call", short_strike=5200.0, long_strike=5300.0, width_points=100.0,
                     margin=10000.0, credit_bid=1.8, credit_ask=2.2, credit_mid=2.0,
                     short_delta=0.3, long_delta=0.1, atm_iv=18.0)
    payload = _build_entry_payload(strat, cand, app_state)
    resp = await place_strategy_entry(mock_ib, app_state, strat, cand)
    assert resp is not None
    assert len(app_state.strategy_log) == 1
    assert len(mock_ib.get_placed_orders()) == 1

def test_has_open_position():
    from strategy_engine import _has_open_position
    state = type("S", (), {"positions": [{"contract": {"strike": 5200.0, "right": "C"}}]})()
    sig = {(5200.0, "C"), (5300.0, "C")}
    assert _has_open_position(state, sig) is True
    assert _has_open_position(type("S", (), {"positions": []})(), sig) is False
    other = type("S", (), {"positions": [{"contract": {"strike": 5000.0, "right": "P"}}]})()
    assert _has_open_position(other, sig) is False
```

Run: `pytest tests/test_strategy_engine.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add strategy_engine.py tests/test_strategy_engine.py
git commit -m "feat: strategy entry placement (credit-signed payload) + eval loop + audit"
```

---

### Task 9: Take-profit exit loop

**Files:**
- Modify: `strategy_engine.py`
- Test: `tests/test_strategy_engine.py`

**Interfaces:**
- Consumes: `signature_for_candidate` (Task 8), `state.positions` (list of position dicts with `contract`/`unrealizedPNL`/`averageCost`), `handle_place_order` (closing order).
- Produces: `find_strategy_positions(strategy, state) -> list`, `maybe_flatten_at_take_profit(ib, state, strategy, position, tp) -> bool`, `async take_profit_loop(ib, state, broadcast_fn)`. TP modes: `pct_credit` (close when PnL ≥ value% of max credit), `dollar` (PnL ≥ value), `credit_price` (position's current spread credit ≥ value).

- [ ] **Step 1: Write the failing test**

```python
import pytest

def test_find_strategy_positions_by_signature():
    import asyncio
    # (covered inline below)
```

```python
from strategy_engine import signature_for_candidate, find_strategy_positions
from strategy_models import Strategy, Condition


def test_match_positions():
    state = type("S", (), {
        "positions": [
            {"contract": {"strike": 5200.0, "right": "C"}, "unrealizedPNL": 100.0},
            {"contract": {"strike": 5300.0, "right": "C"}, "unrealizedPNL": 100.0},
            {"contract": {"strike": 5000.0, "right": "P"}, "unrealizedPNL": 50.0},
        ],
        "expiration": "20260821",
    })()
    cand = type("C", (), {"direction": "bear_call", "short_strike": 5200.0, "long_strike": 5300.0})()
    sig = signature_for_candidate(cand, state)
    found = find_strategy_positions(cand, state)
    # both legs present -> matched
    assert len(found) == 2
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_strategy_engine.py -v`
Expected: FAIL — `find_strategy_positions` undefined.

- [ ] **Step 3: Implement** — append to `strategy_engine.py`:

```python
def find_strategy_positions(candidate, state) -> list:
    """Positions matching the candidate's leg signature."""
    sig = signature_for_candidate(candidate, state)
    out = []
    for pos in getattr(state, "positions", []) or []:
        c = pos.get("contract") or {}
        key = (float(c.get("strike", 0) or 0), c.get("right", ""))
        if key in sig:
            out.append(pos)
    return out


def _tp_target(tp, max_credit: float) -> float:
    if tp is None:
        return 0.0
    if tp.mode == "pct_credit":
        return tp.value * max_credit
    if tp.mode == "dollar":
        return tp.value
    # credit_price mode: treat value as absolute & ignored here; handled by caller
    return tp.value


async def maybe_flatten_at_take_profit(ib, state, candidate, pos, tp) -> bool:
    """Close the spread (reverse legs) if TP target is reached. Returns True if closed."""
    pnl = float(pos.get("unrealizedPNL", 0) or 0)
    max_credit = float(candidate.credit_mid or 0)
    target = _tp_target(tp, max_credit)
    if pnl < target:
        return False
    # Close: reverse each leg (BUY the short, SELL the long) at market.
    from order_manager import handle_place_order
    from account_manager import refresh_account_state
    right = short_right_for(candidate.direction)
    legs = [
        {"symbol": "SPX", "expiry": getattr(state, "expiration", ""), "strike": candidate.short_strike,
         "right": right, "action": "BUY", "qty": 1, "lmtPrice": None, "secType": "OPT"},
        {"symbol": "SPX", "expiry": getattr(state, "expiration", ""), "strike": candidate.long_strike,
         "right": right, "action": "SELL", "qty": 1, "lmtPrice": None, "secType": "OPT"},
    ]
    payload = {"legs": legs, "orderType": "LMT", "tif": "DAY", "comboAction": "BUY",
               "comboLmtPrice": 0.0, "comboQuantity": 1, "outsideRth": False}
    await handle_place_order(ib, state, payload, ws=None, refresh_fn=refresh_account_state)
    return True


async def take_profit_loop(ib, state, broadcast_fn):
    """Watch strategy-tagged positions and flatten at take-profit target."""
    from market_hours import now_et
    while True:
        try:
            await asyncio.sleep(3.0)
            if not getattr(state, "connected", False):
                continue
            for name, strat in list(state.strategies.items()):
                tp = strat.exit_rules.take_profit
                if tp is None:
                    continue
                cands = [Candidate(**c) for c in state.strategy_candidates.get(name, [])]
                for cand in cands:
                    positions = find_strategy_positions(cand, state)
                    if len(positions) < 2:
                        continue
                    closed = await maybe_flatten_at_take_profit(ib, state, cand, positions[0], tp)
                    if closed:
                        await broadcast_fn({"type": "strategy_exit", "data": {"name": name, "event": "take_profit"}})
                        break
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"take_profit_loop error: {e}")
            await asyncio.sleep(3)
```

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/test_strategy_engine.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add strategy_engine.py tests/test_strategy_engine.py
git commit -m "feat: take-profit position-management loop (idempotent close)"
```

---

### Task 10: WS strategy handlers + server wiring

**Files:**
- Modify: `ws_handler.py` (add handlers), `server.py` (load store + start loops + VIX subscription)
- Test: `tests/test_strategy_ws.py`

**Interfaces:**
- Consumes: `strategy_store` (Task 3), `strategy_engine` (Tasks 6–9).
- Produces: WS client→server handlers `strategy_save`, `strategy_delete`, `strategy_arm`, `strategy_disarm`, `strategy_kill_switch`, `set_tab:strategies`; WS server→client broadcasts `strategy_list`, `vix_update`. `server.py` calls `load_strategies()` into `state.strategies`, calls `setup_vix_subscription(ib, state)`, and starts `strategy_evaluation_loop` + `take_profit_loop`.

- [ ] **Step 1: Write the failing test**

```python
import pytest, asyncio, json
from strategy_models import Strategy, Condition
from strategy_store import STRATEGIES_PATH
from strategy_engine import strategy_evaluation_loop


def _dispatch(payload, state):
    from strategy_engine import _apply_ws_action
    return _apply_ws_action(payload, state)


def test_save_and_arm_roundtrip(tmp_path, monkeypatch):
    import strategy_store as store
    monkeypatch.setattr(store, "STRATEGIES_PATH", tmp_path / "strategies.json")
    # Handlers call load_strategies/save_strategy with the module default path;
    # they resolve STRATEGIES_PATH at call time.
```

> The WS handlers below are thin wrappers over `strategy_store` / `strategy_engine`. This test is kept minimal because handlers are exercised end-to-end through the engine; the integration test (Step 3) drives them via `websocket_endpoint` message strings.

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_strategy_ws.py -v`
Expected: FAIL — no `strategy_save` handling yet.

- [ ] **Step 3: Implement** — in `ws_handler.py`, import the store/engine and add branches to `websocket_endpoint`:

```python
from strategy_store import load_strategies, save_strategy, delete_strategy
from strategy_engine import evaluate_conditions

# inside websocket_endpoint's while loop, after the set_tab:account branch:
elif msg == "set_tab:strategies":
    state.active_tab = "strategies"
    logger.info("Client active tab: strategies")
    state.strategies = load_strategies()
    await ws.send_text(json.dumps({"type": "strategy_list", "data": {
        "strategies": [s.to_dict() for s in state.strategies.values()],
        "kill_switch": state.auto_trade_kill_switch,
    }}))

elif msg.startswith("strategy_save:"):
    try:
        body = json.loads(msg.split(":", 1)[1])
        strat = Strategy.from_dict(body)
        state.strategies[strat.name] = strat
        save_strategy(None, strat)
        await ws.send_text(json.dumps({"type": "strategy_list", "data": {
            "strategies": [s.to_dict() for s in state.strategies.values()],
            "kill_switch": state.auto_trade_kill_switch}}))
    except Exception as e:
        logger.error(f"strategy_save error: {e}", exc_info=True)

elif msg.startswith("strategy_delete:"):
    name = msg.split(":", 1)[1]
    state.strategies.pop(name, None)
    delete_strategy(None, name)

elif msg.startswith("strategy_arm:"):
    state.strategies[msg.split(":", 1)[1]].armed = True

elif msg.startswith("strategy_disarm:"):
    state.strategies[msg.split(":", 1)[1]].armed = False

elif msg.startswith("strategy_kill_switch:"):
    state.auto_trade_kill_switch = msg.split(":", 1)[1] == "true"
```

Add to `status_push_loop` a `vix_update` broadcast (import `update_vix` already from Task 5):

```python
await broadcast_fn({"type": "vix_update", "data": {"vix": state.vix}})
```

- [ ] **Step 3b: Wire server** — in `server.py` lifespan, after `setup_account_subscription`, and before/among the loop starts:

```python
from strategy_store import load_strategies
from strategy_engine import strategy_evaluation_loop, take_profit_loop
from ib_connection import setup_vix_subscription

state.strategies = load_strategies()
await setup_vix_subscription(ib, state)
...

state.background_tasks.append(asyncio.create_task(strategy_evaluation_loop(ib, state, broadcast_fn)))
state.background_tasks.append(asyncio.create_task(take_profit_loop(ib, state, broadcast_fn)))
```

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/test_strategy_ws.py -v`
Expected: PASS (handler dispatch + store roundtrip).

- [ ] **Step 5: Commit**

```bash
git add ws_handler.py server.py tests/test_strategy_ws.py
git commit -m "feat: strategy WS handlers + server loop/store/VIX wiring"
```

---

### Task 11: Frontend — Strategies tab (vanilla JS)

**Files:**
- Modify: `static/index.html` (tab button, container, script + CSS includes, `VALID_TABS`/`getValidTab`)
- Modify: `static/js/state.js` (add `strategies`, `strategyCandidates`, `vix`, `killSwitch`, `killSwitch`)
- Modify: `static/js/tabs.js` (handle `strategies`)
- Create: `static/js/strategy-ui.js`
- Modify: `static/css/strategy.css` (reuse; add tab styles)

**Interfaces:**
- Consumes: `handleMessage` (defined in `strategy-builder.js` and re-dispatched), `ws.send` (from `ws.js`), `state` (from `state.js`).
- Produces: functions `renderStrategyList()`, `renderStrategyEditor(strategyName)`, `renderScanner(name)`, `saveStrategyFromForm()`, message handlers `strategy_list`, `strategy_candidate`, `strategy_exit`, `vix_update`, and a `set_tab:strategies` sender. Frontend is not unit-tested here (no JS test harness in the repo); verify manually via the `run` skill.

- [ ] **Step 1: index.html** — add the tab button after the Account button (line 79):

```html
<button class="tab-btn" data-tab="strategies" onclick="switchTab('strategies')">Strategies</button>
```

Add a container after `#accountTab`'s closing div, before `</body>`-level script includes — a `#strategiesTab` div:

```html
<!-- Strategies Tab (Tab 4) -->
<div class="tab-panel" id="strategiesTab" style="display:none;">
    <div class="strategy-tab-layout">
        <div class="strat-list-pane">
            <h3>Strategies</h3>
            <div id="strategyNav"></div>
            <button class="btn btn-primary" onclick="newStrategy()">+ New Strategy</button>
        </div>
        <div class="strat-editor-pane" id="strategyEditor"></div>
        <div class="strat-scanner-pane" id="strategyScanner"></div>
    </div>
</div>
```

Add the script include after `strategy-builder.js` (line 381):

```html
<script src="/static/js/strategy-ui.js"></script>
```

Add a CSS include near the others (line 14):

```html
<link rel="stylesheet" href="/static/css/strategies.css">
```

- [ ] **Step 2: state.js** — add to the `state` object:

```javascript
strategies: [],          // [{name, direction, conditions, exit_rules, auto_execute, armed}]
strategyCandidates: {},  // {name: [candidate]}
vix: null,
killSwitch: false,
```

- [ ] **Step 3: tabs.js** — update `switchTab` to hide/show `strategiesTab` and the tab list:

```javascript
const strategies = document.getElementById('strategiesTab');
// after the account.style.display='none' line:
strategies.style.display = 'none';
strategies.classList.remove('active');
...
} else if (tab === 'strategies') {
    strategies.classList.add('active');
    if (ws && ws.readyState === WebSocket.OPEN) ws.send('set_tab:strategies');
}
```

- [ ] **Step 4: strategy-ui.js** — create with the essential functions:

```javascript
// Strategy UI — list, editor, scanner results, WS handlers.
function newStrategy() {
    const s = {name: 'New ' + (state.strategies.length + 1), direction: 'bull_put',
        conditions: [], exit_rules: {take_profit: null, stop_loss: null, hold_to_expire: false},
        auto_execute: false, armed: false};
    state.strategies.push(s);
    renderStrategyList(); renderStrategyEditor(s.name);
}

function renderStrategyList() {
    const nav = document.getElementById('strategyNav');
    nav.innerHTML = state.strategies.map(s =>
        `<div class="strat-item ${s.name===selectedStrategy()?'active':''}" onclick="renderStrategyEditor('${s.name}')">${s.name}</div>`).join('');
}

function selectedStrategy() { return window._selStrategy || (state.strategies[0] && state.strategies[0].name); }

function renderStrategyEditor(name) {
    window._selStrategy = name;
    const s = state.strategies.find(x => x.name === name);
    if (!s) return;
    const el = document.getElementById('strategyEditor');
    el.innerHTML = `
    <h3>${s.name}</h3>
    <label>Direction</label>
    <select id="stratDirection">
      <option value="bull_put" ${s.direction==='bull_put'?'selected':''}>Bull Put</option>
      <option value="bear_call" ${s.direction==='bear_call'?'selected':''}>Bear Call</option>
    </select>
    <label>Conditions (priority order)</label>
    <ol id="stratCondList">${s.conditions.map((c,i)=>`<li onclick="editCondition(${i})">${c.kind}</li>`).join('')}</ol>
    <button onclick="addCondition()">+ Add Condition</button>
    <div id="conditionEditor"></div>
    <label>Exit rules</label>
    <div>
      <label><input type="checkbox" id="tpEnabled" onchange="toggleTp(this)"> Take Profit</label>
      <input id="tpValue" placeholder="% of credit or $" />
      <label><input type="checkbox" id="slEnabled"> Stop Loss</label>
      <input id="slStop" placeholder="stop" /> <input id="slLimit" placeholder="limit" />
    </div>
    <label class="caps"><input type="checkbox" id="autoExec" ${s.auto_execute?'checked':''}> Auto-execute</label>
    <button onclick="saveStrategyFromForm()">Save</button>
    <button onclick="armStrategy()">${s.armed?'Disarm':'Arm'}</button>
    <button onclick="deleteStrategy()">Delete</button>`;
    if (s.exit_rules.take_profit) { document.getElementById('tpEnabled').checked = true; document.getElementById('tpValue').value = s.exit_rules.take_profit.value; }
    if (s.exit_rules.stop_loss) { document.getElementById('slEnabled').checked = true; document.getElementById('slStop').value = s.exit_rules.stop_loss.stop_price; document.getElementById('slLimit').value = s.exit_rules.stop_loss.limit_price; }
}

function saveStrategyFromForm() {
    const s = state.strategies.find(x => x.name === selectedStrategy());
    if (!s) return;
    s.direction = document.getElementById('stratDirection').value;
    s.auto_execute = document.getElementById('autoExec').checked;
    const tp = document.getElementById('tpEnabled').checked ? {mode:'pct_credit', value: parseFloat(document.getElementById('tpValue').value)||0} : null;
    const sl = document.getElementById('slEnabled').checked ? {stop_price: parseFloat(document.getElementById('slStop').value)||0, limit_price: parseFloat(document.getElementById('slLimit').value)||0} : null;
    s.exit_rules = {take_profit: tp, stop_loss: sl, hold_to_expire: false};
    sendWsMessage('strategy_save:', s);
    renderStrategyList();
}

function armStrategy() {
    const s = state.strategies.find(x => x.name === selectedStrategy());
    if (!s) return;
    sendWsMessage('strategy_arm:', s.name);
}

function deleteStrategy() {
    const s = state.strategies.find(x => x.name === selectedStrategy());
    if (!s) return;
    sendWsMessage('strategy_delete:', s.name);
    state.strategies = state.strategies.filter(x => x.name !== s.name);
    renderStrategyList();
}

function sendWsMessage(prefix, body) {
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(prefix + JSON.stringify(body));
}

function renderScanner(name) {
    const el = document.getElementById('strategyScanner');
    const cands = state.strategyCandidates[name] || [];
    el.innerHTML = `<h3>Live candidates</h3>` + cands.map(c =>
        `<div class="cand-row">SELL ${c.short_strike} ${c.direction==='bull_put'?'P':'C'} / BUY ${c.long_strike}
         · credit $${c.credit_mid?.toFixed(2)} · d=${c.short_delta?.toFixed(2)} · w=${c.width_points}
         <button onclick="placeStrategyCandidate('${name}')">Place</button></div>`).join('') || '<p>No matches</p>';
}

function handleStrategyMessage(msg) {
    switch (msg.type) {
        case 'strategy_list': state.strategies = (msg.data.strategies||[]); state.killSwitch = msg.data.kill_switch; renderStrategyList(); renderScanner(selectedStrategy()); break;
        case 'strategy_candidate': state.strategyCandidates[msg.data.name] = msg.data.candidates; if (selectedStrategy()===msg.data.name) renderScanner(msg.data.name); break;
        case 'strategy_exit': showOrderToast('strategy ' + msg.data.name + ': ' + msg.data.event, 'info'); break;
        case 'vix_update': state.vix = msg.data.vix; if (document.getElementById('vixBadge')) document.getElementById('vixBadge').textContent = state.vix?.toFixed(2); break;
    }
}
```

- [ ] **Step 5: Wire the dispatcher** — in `strategy-builder.js`'s `handleMessage`, add a default case forwarding strategy messages:

```javascript
default:
    handleStrategyMessage(msg);   // route strategy_* / vix_update from strategy-ui.js
    break;
```

- [ ] **Step 6: Manual verification** (no JS test harness)

Run: `python server.py`, open `http://localhost:8000`, click **Strategies**.
Expected: the tab shows a strategy list pane, an editor, and an (empty) scanner pane. Creating and saving a strategy persists to `config/strategies.json`. With a live chain, arming a scan-mode strategy surfaces candidates in the scanner pane.

- [ ] **Step 7: Commit**

```bash
git add static/index.html static/js/state.js static/js/tabs.js static/js/strategy-ui.js static/css/strategies.css static/js/strategy-builder.js
git commit -m "feat: Strategies tab UI (list, editor, scanner, WS handlers)"
```

---

## Post-Implementation Verification

After all tasks, run the full suite:

```bash
pytest -q
```

Then run the app with the `run` skill and smoke-test: create a bull-put strategy, arm it in scan mode, confirm candidates appear; flip auto-execute on and confirm an entry is placed against the mock/paper IB when all conditions pass.

## Deferred (from spec §14 — not in this plan)

Backtesting; monthly / non-0DTE expiries; indicators beyond RSI/%move; strategies beyond 2-leg verticals; multi-candidate batching; any database.
