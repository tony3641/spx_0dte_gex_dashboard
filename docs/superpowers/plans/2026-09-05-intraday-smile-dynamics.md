# Intraday Smile Dynamics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add three orthogonal intraday dynamics to the simulator's SVI smile — sigma-driven skew tilt (A), expiry amplification (B), and a variance-budget ATM anchor (C') — each independently gated before the next is stacked.

**Architecture:** All dynamics live behind one new value object, `SmileDynamics` (sim_calibrate.py), built once per run from `SimRunConfig` dials. `smile_iv()` (sim_pricing.py) evaluates `clip(smile.iv(m) + level + tilt, 0.01, 5.0)` from `dyn` + bar index; neutral dial values reproduce the legacy formula bit-for-bit. Four `sim_engine.py` call sites converge on the new signature; `execute_pipeline` (sim_jobs.py) builds `dyn` per run and threads it through.

**Tech Stack:** Python 3, numpy, scipy (existing), pytest (`python -m pytest tests/ -v`; `tests/run_tests.py` also works).

**Spec:** `docs/superpowers/specs/2026-09-05-intraday-smile-dynamics-design.md` — read §4 (unified formula), §7 (budget math), §8 (gates) before starting. The plan argues from the spec.

## Global Constraints

- **Bit-identical chains (hard gate):** with neutral dials (`skew_beta=0.0`, `skew_t_gamma=0.0`, `atm_budget=False`) every output must satisfy `np.array_equal` vs the committed baselines. Chains: master -> A(off) -> B(gamma=0) -> C'(budget off). A broken chain is a release blocker.
- **Closed-form responses only.** Never refit SVI at runtime (spec §3 cost rule).
- **`dyn` is never cached** in `sim_jobs._CALIB_CACHE` — that cache key covers the data source only; dials vary per run. Build in `execute_pipeline`, O(steps).
- **Validation goes in `SimRunConfig.validate()`** (repo pattern), not `__post_init__`.
- **All repo documents in English** (README sections, docstrings committed to the repo).
- Repo commit style: `feat:`, `fix:`, `test:`, `docs:` prefixes.
- Baseline npz files depend on whatever smile snapshot (`config/sim_smile.json`) is present at capture time. Capture all four baselines in one sitting on the same machine; never regenerate one baseline alone after the snapshot changes.
- SimRunConfig is a plain (non-frozen) dataclass; tests set dials via keyword or `setattr`.

## File Structure

| File | Responsibility |
|---|---|
| `sim_config.py` | Run dials + validation (modify) |
| `sim_calibrate.py` | `SmileDynamics` value object + `build_dynamics(model, cfg)` precompute (modify) |
| `sim_pricing.py` | Pure IV/pricing math: new `smile_iv` signature, tilt, clamp (modify) |
| `sim_engine.py` | Pricing orchestration: thread `dyn` through `run_entry` / `_spread_rows` / `run_exits` / `run_cell` / `run_family` (modify) |
| `sim_jobs.py` | Pipeline: build `dyn` per run; export dials (modify) |
| `tests/fixtures/generate_sim_baseline.py` | Create: deterministic baseline capture script |
| `tests/test_sim_regression.py` | Create: bit-identical chain tests vs committed npz |
| `tests/test_sim_config.py` | Create: dial validation tests |
| `tests/test_sim_pricing.py` | Create: tilt/clamp/flat-iv/budget-branch unit tests |
| `tests/test_sim_calibrate.py` | Extend: `build_dynamics` table tests |
| `tests/test_sim_engine.py` | Extend: lazy-vs-explicit dyn, behavioral tests |
| `README.md` | Model documentation (modify) |

---

## Phase A — sigma-driven skew tilt

### Task 1: Legacy regression baseline (capture BEFORE any code change)

**Files:**
- Create: `tests/fixtures/generate_sim_baseline.py`
- Create: `tests/test_sim_regression.py`
- Create: `tests/fixtures/sim_baseline_legacy.npz` (generated, committed)

**Interfaces:**
- Consumes: `load_bars(cfg)` (sim_data.py:111), `calibrate(bars, cfg)` (sim_calibrate.py:335), `simulate_chunk(model, cfg, s0, n_paths, seed_seq)` (sim_paths.py:16), `run_entry`/`run_exits` (sim_engine.py:132/292), fixture CSV `tests/fixtures/sim_bars_5m.csv` (78 bars/day, 5-min, 10 days).
- Produces: `tests/fixtures/sim_baseline_<tag>.npz` with arrays `spots, sigmas, entered, entry_minute, short_idx, long_idx, fill, pnl, mtm0` and scalars `skew_beta, t_gamma, budget`; test helpers `_cell(dials)` / `_assert_cell_matches(z, entry, trials)` that Tasks 6, 8, 12 reuse. The npz path convention `<tag>` is fixed: `legacy`, `A`, `AB`, `ABC`.

- [ ] **Step 1: Write the baseline capture script**

```python
"""Capture deterministic baseline outputs for the smile-dynamics bit-identical chain.

Run from the repo root:
    python tests/fixtures/generate_sim_baseline.py --tag legacy
    python tests/fixtures/generate_sim_baseline.py --tag A --skew-beta 1.0
    python tests/fixtures/generate_sim_baseline.py --tag AB --skew-beta 1.0 --t-gamma 0.4
    python tests/fixtures/generate_sim_baseline.py --tag ABC --skew-beta 1.0 --t-gamma 0.4 --budget

Writes tests/fixtures/sim_baseline_<tag>.npz. Deterministic given the fixture CSV and
whatever smile snapshot (config/sim_smile.json) is present; capture all tags in one
sitting and never regenerate a single tag after the snapshot changes.
"""
import argparse
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from sim_calibrate import calibrate
from sim_config import SimRunConfig
from sim_data import load_bars
from sim_engine import run_entry, run_exits
from sim_paths import simulate_chunk
from strategy_models import Condition, ExitRules, StopLoss, Strategy

FIXTURE = os.path.join(os.path.dirname(__file__), "sim_bars_5m.csv")
LADDER = np.arange(5100.0, 6900.0 + 2.5, 5.0)


def _strategy():
    """Guarantees entries across the day: wide delta band, tiny credit floor, long window."""
    return Strategy(
        name="T", direction="bull_put",
        conditions=[
            Condition(kind="short_delta", params={"min": 0.05, "max": 0.60}),
            Condition(kind="spread_width", params={"min": 5, "max": 50}),
            Condition(kind="credit", params={"min": 0.05}),
            Condition(kind="entry_window", params={"start": "09:35", "end": "14:00"}),
        ],
        exit_rules=ExitRules(stop_loss=StopLoss(multiplier=6.0)), budget=None)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True, choices=["legacy", "A", "AB", "ABC"])
    ap.add_argument("--skew-beta", type=float, default=0.0)
    ap.add_argument("--t-gamma", type=float, default=0.0)
    ap.add_argument("--budget", action="store_true")
    a = ap.parse_args()

    cfg = SimRunConfig(strategy_name="T", source="csv", csv_path=FIXTURE,
                       bar_size="5m", n_paths=60, seed=42)
    cfg.validate()
    bars = load_bars(cfg)
    model = calibrate(bars, cfg)
    spot0 = float(bars.closes[-1])
    paths = simulate_chunk(model, cfg, spot0, cfg.n_paths,
                           np.random.SeedSequence(entropy=cfg.seed))
    entry = run_entry(model, cfg, _strategy(), paths, LADDER)
    trials = run_exits(model, cfg, _strategy(), paths, LADDER, entry)
    mtm0 = next((t.mtm for t in trials if t.mtm is not None),
                np.full(paths.spots.shape[1], np.nan))
    out = os.path.join(os.path.dirname(__file__), f"sim_baseline_{a.tag}.npz")
    np.savez(out, spots=paths.spots, sigmas=paths.sigmas,
             entered=entry.entered, entry_minute=entry.entry_minute,
             short_idx=entry.short_idx, long_idx=entry.long_idx,
             fill=entry.fill, pnl=np.array([t.pnl for t in trials]), mtm0=mtm0,
             skew_beta=a.skew_beta, t_gamma=a.t_gamma, budget=np.array(int(a.budget)))
    print(f"wrote {out}: {int(entry.entered.sum())}/{cfg.n_paths} paths entered")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Generate the legacy baseline and sanity-check it**

Run: `python tests/fixtures/generate_sim_baseline.py --tag legacy`
Expected: `wrote ...sim_baseline_legacy.npz: NN/60 paths entered` with NN >= 20 (the loose conditions guarantee most paths enter; if NN < 20 STOP and investigate — a degenerate baseline is useless as a regression anchor).

- [ ] **Step 3: Write the regression test harness**

```python
# tests/test_sim_regression.py
"""Bit-identical chain for the smile-dynamics work (spec 2026-09-05 §8).

Chains: legacy -> A(off) -> AB(gamma=0) -> ABC(budget off). Each gate task adds its
npz + test; every link must hold with np.array_equal (exact float equality), not
allclose. Baselines are captured by tests/fixtures/generate_sim_baseline.py.
"""
import os

import numpy as np

from sim_calibrate import calibrate
from sim_config import SimRunConfig
from sim_data import load_bars
from sim_engine import run_entry, run_exits
from sim_paths import simulate_chunk
from strategy_models import Condition, ExitRules, StopLoss, Strategy

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "sim_bars_5m.csv")
LADDER = np.arange(5100.0, 6900.0 + 2.5, 5.0)

LEGACY = {}   # neutral dials are the dataclass defaults; extended by gate tasks


def _strategy():
    return Strategy(
        name="T", direction="bull_put",
        conditions=[
            Condition(kind="short_delta", params={"min": 0.05, "max": 0.60}),
            Condition(kind="spread_width", params={"min": 5, "max": 50}),
            Condition(kind="credit", params={"min": 0.05}),
            Condition(kind="entry_window", params={"start": "09:35", "end": "14:00"}),
        ],
        exit_rules=ExitRules(stop_loss=StopLoss(multiplier=6.0)), budget=None)


def _cell(dials=None):
    """Deterministic pipeline slice: calibrate -> paths -> entry -> exits."""
    cfg = SimRunConfig(strategy_name="T", source="csv", csv_path=FIXTURE,
                       bar_size="5m", n_paths=60, seed=42)
    for k, v in (dials or {}).items():
        setattr(cfg, k, v)
    cfg.validate()
    bars = load_bars(cfg)
    model = calibrate(bars, cfg)
    spot0 = float(bars.closes[-1])
    paths = simulate_chunk(model, cfg, spot0, cfg.n_paths,
                           np.random.SeedSequence(entropy=cfg.seed))
    entry = run_entry(model, cfg, _strategy(), paths, LADDER)
    trials = run_exits(model, cfg, _strategy(), paths, LADDER, entry)
    return entry, trials


def _npz(tag):
    return np.load(os.path.join(os.path.dirname(__file__), "fixtures",
                                f"sim_baseline_{tag}.npz"))


def _assert_cell_matches(z, entry, trials):
    assert np.array_equal(entry.entered, z["entered"])
    assert np.array_equal(entry.entry_minute, z["entry_minute"])
    assert np.array_equal(entry.short_idx, z["short_idx"])
    assert np.array_equal(entry.long_idx, z["long_idx"])
    assert np.array_equal(entry.fill, z["fill"])
    assert np.array_equal(np.array([t.pnl for t in trials]), z["pnl"])
    if not np.isnan(z["mtm0"]).all():
        mtm = next(t.mtm for t in trials if t.mtm is not None)
        assert np.array_equal(mtm, z["mtm0"])


def test_legacy_baseline_unchanged():
    _assert_cell_matches(_npz("legacy"), *_cell(LEGACY))
```

- [ ] **Step 4: Run the test to verify it passes on unmodified code**

Run: `python -m pytest tests/test_sim_regression.py -v`
Expected: PASS (this is the anchor — if it fails here, determinism is broken; debug before any code change).

- [ ] **Step 5: Commit**

```bash
git add tests/fixtures/generate_sim_baseline.py tests/test_sim_regression.py tests/fixtures/sim_baseline_legacy.npz
git commit -m "test: legacy baseline for smile-dynamics bit-identical chain"
```

### Task 2: `skew_beta` config dial

**Files:**
- Modify: `sim_config.py` (field after `ladder_range_pct` at :39; validation in `validate()` after the `gamma_mult`/`vol_beta` check at :73-74)
- Create: `tests/test_sim_config.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `SimRunConfig.skew_beta: float = 0.0` with `validate()` raising `ValueError("skew_beta must be >= 0")` on negatives. Later tasks read `cfg.skew_beta`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_sim_config.py
import pytest

from sim_config import SimRunConfig


def _cfg(**kw):
    return SimRunConfig(strategy_name="T", **kw)


def test_skew_beta_default_is_neutral():
    assert _cfg().skew_beta == 0.0


def test_negative_skew_beta_rejected():
    with pytest.raises(ValueError, match="skew_beta"):
        _cfg(skew_beta=-0.1).validate()


def test_positive_skew_beta_accepted():
    _cfg(skew_beta=1.0).validate()   # must not raise
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_sim_config.py -v`
Expected: FAIL — `SimRunConfig` has no attribute `skew_beta` (`TypeError` on the first test).

- [ ] **Step 3: Implement**

In `sim_config.py`, add the field after `ladder_range_pct: float = 0.15`:

```python
    skew_beta: float = 0.0              # smile tilt per unit vol-shock (>=0); 0 = legacy
```

In `validate()`, immediately after the `gamma_mult`/`vol_beta` check:

```python
        if self.skew_beta < 0:
            raise ValueError("skew_beta must be >= 0")
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_sim_config.py -v`
Expected: 3 PASS.

- [ ] **Step 5: Commit**

```bash
git add sim_config.py tests/test_sim_config.py
git commit -m "feat(sim-config): skew_beta dial (default 0 = legacy)"
```

### Task 3: `SmileDynamics` value object + neutral `build_dynamics`

**Files:**
- Modify: `sim_calibrate.py` (insert after the `CalibratedModel` class, ~line 168)
- Modify: `tests/test_sim_calibrate.py` (append)

**Interfaces:**
- Consumes: `CalibratedModel` (sigma0, smile, ushape, garch), `SimRunConfig` (vol_beta, flat_iv, skew_beta, steps_per_day()).
- Produces (final shape — Tasks 7/10 only change `build_dynamics` internals, never this class):

```python
@dataclass(frozen=True)
class SmileDynamics:
    sigma0: float
    vol_beta: float
    flat_iv: bool
    iv0: float
    skew_beta: float = 0.0
    t_scale: object = None      # (steps,) T_ref/max(T_t,T_floor); ones = neutral
    skew_t_gamma: float = 0.0
    atm_budget: bool = False
    budget_beta: float = 1.0
    v_bar: float = 0.0
    a_tab: object = None        # (steps,) A(t) = v_bar*(S(t)-P(t))
    b_tab: object = None        # (steps,) B(t) = P(t)
    v0: float = 0.0


def build_dynamics(model: CalibratedModel, cfg: SimRunConfig) -> SmileDynamics:
    ...
```

- [ ] **Step 1: Write the failing tests** (append to `tests/test_sim_calibrate.py`)

```python
def test_build_dynamics_neutral_fields():
    from sim_calibrate import build_dynamics
    from sim_config import SimRunConfig
    bars = _make_bars()
    cfg = SimRunConfig(strategy_name="T", source="csv", bar_size="5m")
    model = calibrate(bars, cfg)
    dyn = build_dynamics(model, cfg)
    assert dyn.sigma0 == model.sigma0
    assert dyn.vol_beta == cfg.vol_beta == 0.75
    assert dyn.flat_iv is False
    assert dyn.iv0 == float(model.smile.iv(0.0))
    assert dyn.skew_beta == 0.0
    assert dyn.skew_t_gamma == 0.0
    assert dyn.atm_budget is False
    assert dyn.a_tab is None and dyn.b_tab is None
    assert dyn.t_scale.shape == (cfg.steps_per_day(),)
    assert np.array_equal(dyn.t_scale, np.ones(cfg.steps_per_day()))
```

(`_make_bars` / `calibrate` already exist in this test module.)

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_sim_calibrate.py::test_build_dynamics_neutral_fields -v`
Expected: FAIL — `ImportError: cannot import name 'build_dynamics'`.

- [ ] **Step 3: Implement** (insert into `sim_calibrate.py` after `CalibratedModel`)

```python
@dataclass(frozen=True)
class SmileDynamics:
    """Run-level smile response dials + precomputed tables (smile-dynamics spec §4).

    Neutral values (skew_beta=0, skew_t_gamma=0, atm_budget=False) reproduce the
    legacy IV formula clip(smile.iv(m) + vol_beta*(sigma - sigma0), 0.01, 5.0)
    bit-for-bit — tests/test_sim_regression.py enforces the chain.
    """
    sigma0: float
    vol_beta: float
    flat_iv: bool
    iv0: float
    skew_beta: float = 0.0
    t_scale: object = None      # (steps,) T_ref/max(T_t,T_floor); ones = neutral
    skew_t_gamma: float = 0.0
    atm_budget: bool = False
    budget_beta: float = 1.0
    v_bar: float = 0.0
    a_tab: object = None        # (steps,) A(t) = v_bar*(S(t)-P(t))
    b_tab: object = None        # (steps,) B(t) = P(t)
    v0: float = 0.0


def build_dynamics(model: CalibratedModel, cfg: SimRunConfig) -> SmileDynamics:
    """Per-run smile dynamics from cfg dials. O(steps) precompute.

    Never cache this with the model: sim_jobs._CALIB_CACHE keys on the data source
    only, while these dials vary per run.
    """
    steps = cfg.steps_per_day()
    return SmileDynamics(
        sigma0=model.sigma0, vol_beta=cfg.vol_beta, flat_iv=cfg.flat_iv,
        iv0=float(model.smile.iv(0.0)), skew_beta=cfg.skew_beta,
        t_scale=np.ones(steps))
```

(`np`, `dataclass`, `CalibratedModel`, `SimRunConfig` are already imported in this module.)

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_sim_calibrate.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add sim_calibrate.py tests/test_sim_calibrate.py
git commit -m "feat(sim-calibrate): SmileDynamics value object + neutral build_dynamics"
```

### Task 4: `smile_iv` new signature + tilt + clamp

**Files:**
- Modify: `sim_pricing.py:41-47` (replace the whole `smile_iv` function)
- Create: `tests/test_sim_pricing.py`

**Interfaces:**
- Consumes: `SmileDynamics` (Task 3).
- Produces (final Phase A signature — Tasks 11/12 call it identically):
  `smile_iv(m, smile, sigma_t, dyn, t) -> np.ndarray`, broadcast over `m` (…, M) and `sigma_t` (…, 1). Legacy positional args are gone; all callers updated in Task 5.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_sim_pricing.py
"""Unit tests for the smile-dynamics IV math (spec 2026-09-05 §4-§6)."""
import numpy as np

from sim_calibrate import DEFAULT_SMILE, SmileDynamics
from sim_pricing import bar_year_frac, bsm_put, smile_iv

SIGMA0 = 0.0005
R = 0.043


def _dyn(**kw):
    d = dict(sigma0=SIGMA0, vol_beta=0.75, flat_iv=False,
             iv0=float(DEFAULT_SMILE.iv(0.0)), skew_beta=0.0,
             t_scale=np.ones(4), skew_t_gamma=0.0)
    d.update(kw)
    return SmileDynamics(**d)


def test_neutral_dyn_is_bit_identical_to_legacy_formula():
    m = np.linspace(-0.15, 0.15, 31)
    sigma = np.array([[0.0007], [0.0004]])
    out = smile_iv(m, DEFAULT_SMILE, sigma, _dyn(), 1)
    legacy = np.clip(DEFAULT_SMILE.iv(m) + 0.75 * (sigma - SIGMA0), 0.01, 5.0)
    assert np.array_equal(out, legacy)


def test_tilt_zero_at_atm_and_moves_wings():
    m = np.array([-0.10, 0.0, 0.10])
    base = smile_iv(m, DEFAULT_SMILE, np.array([[SIGMA0]]), _dyn(), 0)
    up = smile_iv(m, DEFAULT_SMILE, np.array([[SIGMA0 * 1.5]]), _dyn(skew_beta=1.0), 0)
    assert up[0] > base[0]            # put wing richer when vol rises
    assert up[2] < base[2]            # call wing cheaper
    assert up[1] == base[1]           # ATM exactly untouched (tilt == 0.0 at m == 0)


def test_down_shock_flattens_wing():
    m = np.array([-0.10, 0.0, 0.10])
    base = smile_iv(m, DEFAULT_SMILE, np.array([[SIGMA0]]), _dyn(), 0)
    down = smile_iv(m, DEFAULT_SMILE, np.array([[SIGMA0 * 0.5]]), _dyn(skew_beta=1.0), 0)
    assert down[0] < base[0]          # vol collapse -> put wing cheaper
    assert down[2] > base[2]          # call wing richer


def test_ratio_clamped_for_uncapped_tails():
    m = np.array([-0.15, 0.15])
    dyn = _dyn(skew_beta=1.0, vol_beta=0.0)   # level off so the tilt is isolated
    iv10 = smile_iv(m, DEFAULT_SMILE, np.array([[SIGMA0 * 11.0]]), dyn, 0)  # raw +10
    iv4 = smile_iv(m, DEFAULT_SMILE, np.array([[SIGMA0 * 5.0]]), dyn, 0)    # raw +4
    assert np.array_equal(iv10, iv4)  # both clamp to ratio +3


def test_flat_iv_short_circuit_ignores_dynamics():
    dyn = _dyn(flat_iv=True, skew_beta=1.0)
    out = smile_iv(np.array([-0.1, 0.1]), DEFAULT_SMILE, np.array([[0.001]]), dyn, 2)
    assert np.all(out == dyn.iv0)


def test_put_prices_monotone_in_strike_with_tilt():
    ladder = np.arange(5700.0, 6300.0 + 2.5, 5.0)
    m = np.log(ladder / 6000.0)
    for t, mult in [(0, 1.8), (3, 0.6)]:
        iv = smile_iv(m, DEFAULT_SMILE, np.array([[SIGMA0 * mult]]), _dyn(skew_beta=1.0), t)
        put = bsm_put(6000.0, ladder, bar_year_frac(300) * (10 - t), R, iv)
        assert np.all(np.diff(put) >= -1e-12)   # put rises with strike, no butterfly flip
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_sim_pricing.py -v`
Expected: FAIL/ERROR — `smile_iv() takes 7 positional arguments but 5 were given`.

- [ ] **Step 3: Implement** — replace `smile_iv` and add the clamp constants in `sim_pricing.py`

```python
_VOL_RATIO_MIN = -1.0
_VOL_RATIO_MAX = 3.0


def smile_iv(m, smile, sigma_t, dyn, t):
    """IV at bar t from log-moneyness m + per-path per-bar sigma_t (smile-dynamics spec §4).

    dyn is a sim_calibrate.SmileDynamics carrying the run's dials/tables; neutral dyn
    reproduces the legacy formula exactly:
        clip(smile.iv(m) + vol_beta*(sigma - sigma0), 0.01, 5.0)
    """
    m = np.asarray(m, dtype=float)
    sigma = np.asarray(sigma_t, dtype=float)
    if dyn.flat_iv:
        return np.full(np.broadcast_shapes(m.shape, sigma.shape), dyn.iv0)
    base = smile.iv(m)
    level = dyn.vol_beta * (sigma - dyn.sigma0)
    ratio = np.clip(sigma / dyn.sigma0 - 1.0, _VOL_RATIO_MIN, _VOL_RATIO_MAX)
    tilt = -dyn.skew_beta * (float(dyn.t_scale[t]) ** dyn.skew_t_gamma) * ratio * m
    return np.clip(base + level + tilt, 0.01, 5.0)
```

- [ ] **Step 4: Run to verify pass + regression intact**

Run: `python -m pytest tests/test_sim_pricing.py tests/test_sim_regression.py -v`
Expected: all PASS (the neutral-dyn test and the legacy baseline both hold — tilt is `±0.0` when `skew_beta == 0`, and `x + 0.0` preserves bits below the clip floor).

- [ ] **Step 5: Commit**

```bash
git add sim_pricing.py tests/test_sim_pricing.py
git commit -m "feat(sim-pricing): smile_iv dyn signature + sigma-driven skew tilt"
```

### Task 5: Thread `dyn` through sim_engine + sim_jobs

**Files:**
- Modify: `sim_engine.py` — import (line 14), `run_entry` (:132), call sites :170 and :200, `_spread_rows` (:282, call site :285), `run_exits` (:292, call site :325), `run_cell` (:356), `run_family` (:410, call sites :414 and :424-427)
- Modify: `sim_jobs.py` — import + `execute_pipeline` (build after `ladder = build_ladder(...)` at :154, pass at :171 and :178)

**Interfaces:**
- Consumes: `SmileDynamics`, `build_dynamics(model, cfg)` (Task 3), `smile_iv(m, smile, sigma_t, dyn, t)` (Task 4).
- Produces: every engine entry point gains a trailing `dyn: Optional[SmileDynamics] = None` parameter and lazily builds when `None`; `execute_pipeline(cfg, bars, progress_cb, spot0, ...)` builds once and passes it down. Existing callers/tests unchanged.

- [ ] **Step 1: Make the edits (no new tests yet — existing suite is the safety net)**

`sim_engine.py` — extend the import at line 14:

```python
from sim_calibrate import CalibratedModel, SmileDynamics, build_dynamics
```

Add after `_qty_for` (line ~129):

```python
def _ensure_dyn(model: CalibratedModel, cfg: SimRunConfig,
                dyn: Optional[SmileDynamics]) -> SmileDynamics:
    return dyn if dyn is not None else build_dynamics(model, cfg)
```

`run_entry` — signature becomes:

```python
def run_entry(model: CalibratedModel, cfg: SimRunConfig, strategy: Strategy,
              paths, ladder: np.ndarray, per_path_start=None, k: Optional[float] = None,
              dyn: Optional[SmileDynamics] = None) -> EntryState:
```

with `dyn = _ensure_dyn(model, cfg, dyn)` as the first statement after `n, steps = paths.spots.shape`, and both call sites (:170, :200) become:

```python
        iv_t = smile_iv(m, model.smile, paths.sigmas[:, t:t + 1], dyn, t)
```

`_spread_rows` — signature gains `dyn: Optional[SmileDynamics] = None`, first line `dyn = _ensure_dyn(model, cfg, dyn)`, call site (:285):

```python
    iv_t = smile_iv(m, model.smile, paths.sigmas[:, t:t + 1], dyn, t)
```

`run_exits` — signature gains `dyn: Optional[SmileDynamics] = None`; after `n, steps = paths.spots.shape` add `dyn = _ensure_dyn(model, cfg, dyn)`; call site (:325):

```python
            iv_t = smile_iv(m, model.smile, np.array([[paths.sigmas[p, t]]]), dyn, t)[0]
```

`run_cell` — signature gains `dyn: Optional[SmileDynamics] = None`; body:

```python
    entry = run_entry(model, cfg, strategy, paths, ladder, k=k, dyn=dyn)
    return run_exits(model, cfg, strategy, paths, ladder, entry,
                     sl_multiplier=sl_multiplier, dyn=dyn)
```

`run_family` — signature gains `dyn: Optional[SmileDynamics] = None`; the root call (:414) becomes `run_cell(model, cfg, root, paths, ladder, dyn=dyn)`; the child block (:424-427) becomes:

```python
        child_results = run_exits(
            model, cfg, child, paths, ladder,
            run_entry(model, cfg, child, paths, ladder, per_path_start=starts_c, dyn=dyn),
            sl_multiplier=None, dyn=dyn)                               # child keeps its own stop
```

`sim_jobs.py` — add to the imports: `from sim_calibrate import build_dynamics`. In `execute_pipeline`, after `ladder = build_ladder(spot0, cfg.ladder_range_pct)`:

```python
    dyn = build_dynamics(model, cfg)   # per-run dials; NOT cached with the model
```

and pass `dyn=dyn` into the `run_family(...)` call (:171) and the `run_cell(...)` call (:178).

- [ ] **Step 2: Run the existing suites (they must pass unchanged via the lazy path)**

Run: `python -m pytest tests/test_sim_engine.py tests/test_sim_regression.py tests/test_sim_pricing.py -v`
Expected: all PASS.

- [ ] **Step 3: Add the lazy-vs-explicit equivalence test** (append to `tests/test_sim_engine.py`)

```python
def test_explicit_dyn_matches_lazy_build():
    from sim_calibrate import build_dynamics
    from sim_data import parse_csv
    import os
    cfg = SimRunConfig(strategy_name="T", source="csv",
                       csv_path=os.path.join("tests", "fixtures", "sim_bars_5m.csv"),
                       bar_size="5m")
    bars = parse_csv(cfg.csv_path, 300)
    model = calibrate(bars, cfg)
    es_lazy = run_entry(model, cfg, _strategy(), _paths(), _ladder())
    es_explicit = run_entry(model, cfg, _strategy(), _paths(), _ladder(),
                            dyn=build_dynamics(model, cfg))
    assert np.array_equal(es_lazy.entered, es_explicit.entered)
    assert np.array_equal(es_lazy.fill, es_explicit.fill)
```

with `from sim_calibrate import calibrate` added to that file's imports (extend line 5). Note `_paths()` rebuilds identical synthetic paths per call (seeded rng inside), so the two runs are comparable.

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_sim_engine.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add sim_engine.py sim_jobs.py tests/test_sim_engine.py
git commit -m "feat(sim-engine): thread SmileDynamics through all pricing call sites"
```

### Task 6: Gate A — behavioral proof, baseline A, export, README

**Files:**
- Modify: `sim_jobs.py:195-199` (export dials), `README.md`, `tests/test_sim_regression.py`, `tests/test_sim_engine.py`
- Create: `tests/fixtures/sim_baseline_A.npz`

**Interfaces:**
- Consumes: everything from Tasks 2-5.
- Produces: committed `sim_baseline_A.npz` (skew_beta=1.0) — Task 8's `gamma=0` chain link compares against it.

- [ ] **Step 1: Behavioral test — credits rise on down-drift paths with elevated sigma** (append to `tests/test_sim_engine.py`)

```python
def test_skew_tilt_raises_put_credits_on_down_drift():
    from sim_calibrate import DEFAULT_SMILE, CalibratedModel, GarchParams
    model = CalibratedModel(
        garch=GarchParams(omega=2e-10, alpha=0.05, gamma=0.10, beta=0.85, nu=6.0,
                          converged=True),
        ushape=np.ones(78), sigma0=0.0005, smile=DEFAULT_SMILE, vix0=15.0, source="test")
    cfg0 = SimRunConfig(strategy_name="T", bar_size="5m")
    cfg1 = SimRunConfig(strategy_name="T", bar_size="5m", skew_beta=1.0)
    paths = _paths()
    paths.spots = paths.spots * np.linspace(1.0, 0.97, paths.spots.shape[1])[None, :]
    paths.sigmas = np.full_like(paths.sigmas, 0.0005 * 1.8)   # vol shock -> tilt active
    strat = _strategy()
    f0 = run_entry(model, cfg0, strat, paths, _ladder()).fill
    f1 = run_entry(model, cfg1, strat, paths, _ladder()).fill
    assert (f1 >= f0 - 1e-12).all()
    assert f1.sum() > f0.sum()
```

(`_paths()` spots are a fixed-seed random walk; the `*0.97` drift makes short puts richer, and the elevated constant sigma activates the tilt. `f1 >= f0` holds per-path because a richer put wing raises every put mid; the sum asserts it is active.)

- [ ] **Step 2: Run it**

Run: `python -m pytest tests/test_sim_engine.py::test_skew_tilt_raises_put_credits_on_down_drift -v`
Expected: PASS.

- [ ] **Step 3: Export the dial** — in `sim_jobs.py` the `dials=dict(...)` at :195-199 gains one entry:

```python
                                                    vol_cap_mult=cfg.vol_cap_mult,
                                                    skew_beta=cfg.skew_beta),
```

- [ ] **Step 4: Capture baseline A and add its chain test**

Run: `python tests/fixtures/generate_sim_baseline.py --tag A --skew-beta 1.0`
Expected: NN/60 entered, same NN as the legacy capture (dials must not change path generation).

Append to `tests/test_sim_regression.py`:

```python
A_STATE = {"skew_beta": 1.0, "skew_t_gamma": 0.0, "atm_budget": False}


def test_phase_A_activated_baseline_unchanged():
    """Gate A: skew_beta=1.0 outputs are pinned; Task 8 re-links to this npz."""
    _assert_cell_matches(_npz("A"), *_cell(A_STATE))
```

- [ ] **Step 5: README section** (append to the simulator section of `README.md`, English):

```markdown
### Intraday smile dynamics (sim)

The simulated smile now responds to the path's own volatility state. With
`skew_beta = 0` (default) behavior is unchanged. `skew_beta > 0` tilts the IV curve
when a path's GARCH sigma deviates from its calibrated mean: put wings get richer,
call wings cheaper, ATM unchanged (`iv_t(m) += -skew_beta * clamp(sigma_t/sigma0 - 1,
-1, +3) * m`). The response is closed-form — the SVI is never refit at runtime.
```

- [ ] **Step 6: Full suite + commit**

Run: `python -m pytest tests/ -v`
Expected: all PASS (this closes **Gate A**: units + property + bit-identical legacy + behavioral signature + export).

```bash
git add sim_jobs.py README.md tests/test_sim_regression.py tests/test_sim_engine.py tests/fixtures/sim_baseline_A.npz
git commit -m "feat(sim): Gate A closed — sigma-driven skew tilt with pinned baseline"
```

## Phase B — expiry amplification

### Task 7: `skew_t_gamma` dial + real `t_scale` table

**Files:**
- Modify: `sim_config.py` (field + validation), `sim_calibrate.py` (`build_dynamics`)
- Modify: `tests/test_sim_config.py`, `tests/test_sim_calibrate.py`

**Interfaces:**
- Produces: `SimRunConfig.skew_t_gamma: float = 0.0` (validated `0 <= gamma <= 1`); `build_dynamics` now fills `t_scale` with the real time table and `skew_t_gamma=cfg.skew_t_gamma`. `smile_iv` (Task 4) picks this up unchanged via `t_scale[t] ** skew_t_gamma`.

- [ ] **Step 1: Failing tests**

Append to `tests/test_sim_config.py`:

```python
def test_t_gamma_default_is_neutral():
    assert _cfg().skew_t_gamma == 0.0


def test_t_gamma_out_of_range_rejected():
    with pytest.raises(ValueError, match="skew_t_gamma"):
        _cfg(skew_t_gamma=1.5).validate()
```

Append to `tests/test_sim_calibrate.py`:

```python
def test_build_dynamics_t_scale_table():
    from sim_calibrate import build_dynamics
    from sim_config import SimRunConfig
    model = calibrate(_make_bars(), SimRunConfig(strategy_name="T", source="csv",
                                                 bar_size="5m"))
    cfg0 = SimRunConfig(strategy_name="T", source="csv", bar_size="5m")
    assert np.array_equal(build_dynamics(model, cfg0).t_scale, np.ones(78))
    cfg4 = SimRunConfig(strategy_name="T", source="csv", bar_size="5m",
                        skew_t_gamma=0.4)
    ts = build_dynamics(model, cfg4).t_scale
    assert ts.shape == (78,)
    assert ts[0] == 1.0                                  # anchored at the first bar
    assert np.all(np.diff(ts) > 0.0)                     # grows monotonically to expiry
    assert ts[-1] == pytest.approx((77 / 0.5) ** 0.4)    # T_floor = half a 5-min bar
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_sim_config.py tests/test_sim_calibrate.py -v`
Expected: FAIL — no `skew_t_gamma` attribute / `ValueError` not raised.

- [ ] **Step 3: Implement**

`sim_config.py` field (after `skew_beta`):

```python
    skew_t_gamma: float = 0.0           # expiry amplification exponent (0..1); 0 = Phase-A-only tilt
```

`validate()` after the `skew_beta` check:

```python
        if not (0 <= self.skew_t_gamma <= 1):
            raise ValueError("skew_t_gamma must be in [0, 1]")
```

`sim_calibrate.py` — replace the `build_dynamics` body:

```python
def build_dynamics(model: CalibratedModel, cfg: SimRunConfig) -> SmileDynamics:
    """Per-run smile dynamics from cfg dials. O(steps) precompute.

    Never cache this with the model: sim_jobs._CALIB_CACHE keys on the data source
    only, while these dials vary per run.
    """
    steps = cfg.steps_per_day()
    t_scale = np.ones(steps)
    if cfg.skew_t_gamma > 0.0:
        # Same bar fraction as sim_pricing.bar_year_frac (avoided here to keep
        # sim_calibrate free of a sim_pricing import): 252 RTH days x 6.5 h.
        barf = BAR_SECONDS[cfg.bar_size] / (252 * 6.5 * 3600.0)
        t_left = np.arange(steps - 1, -1, -1) * barf     # years to expiry after bar t
        t_scale = t_left[0] / np.maximum(t_left, 0.5 * barf)
    return SmileDynamics(
        sigma0=model.sigma0, vol_beta=cfg.vol_beta, flat_iv=cfg.flat_iv,
        iv0=float(model.smile.iv(0.0)), skew_beta=cfg.skew_beta,
        t_scale=t_scale, skew_t_gamma=cfg.skew_t_gamma)
```

and add `from sim_config import BAR_SECONDS` to `sim_calibrate.py`'s `sim_config` import (line 107 becomes `from sim_config import BAR_SECONDS, SimRunConfig`).

- [ ] **Step 4: Run to verify pass + regression intact**

Run: `python -m pytest tests/test_sim_config.py tests/test_sim_calibrate.py tests/test_sim_regression.py -v`
Expected: all PASS (gamma defaults to 0 -> `t_scale` ones -> bit-identical chain holds).

- [ ] **Step 5: Commit**

```bash
git add sim_config.py sim_calibrate.py tests/test_sim_config.py tests/test_sim_calibrate.py
git commit -m "feat(sim): skew_t_gamma dial + per-run t_scale table"
```

### Task 8: Gate B — baseline AB, per-bar monotonicity, late-window report

**Files:**
- Modify: `tests/test_sim_regression.py`, `tests/test_sim_pricing.py`, `README.md`
- Create: `tests/fixtures/sim_baseline_AB.npz`

**Interfaces:**
- Produces: committed `sim_baseline_AB.npz` (skew_beta=1.0, skew_t_gamma=0.4) — Task 12's `atm_budget=False` chain link compares against it.

- [ ] **Step 1: Chain test** (append to `tests/test_sim_regression.py`)

```python
AB_STATE = {"skew_beta": 1.0, "skew_t_gamma": 0.4, "atm_budget": False}


def test_phase_B_gamma0_matches_phase_A():
    """Gate B chain link: gamma=0 must reproduce the Phase-A baseline exactly."""
    _assert_cell_matches(_npz("A"),
                         *_cell({"skew_beta": 1.0, "skew_t_gamma": 0.0,
                                 "atm_budget": False}))


def test_phase_B_activated_baseline_unchanged():
    _assert_cell_matches(_npz("AB"), *_cell(AB_STATE))
```

- [ ] **Step 2: Per-bar strike monotonicity at gamma=0.4** (append to `tests/test_sim_pricing.py`)

```python
def test_strike_monotonicity_every_bar_at_gamma_04():
    from sim_calibrate import CalibratedModel, GarchParams, build_dynamics
    from sim_config import SimRunConfig
    model = CalibratedModel(
        garch=GarchParams(omega=2e-10, alpha=0.05, gamma=0.10, beta=0.85, nu=6.0,
                          converged=True),
        ushape=np.ones(78), sigma0=SIGMA0, smile=DEFAULT_SMILE, vix0=15.0, source="test")
    cfg = SimRunConfig(strategy_name="T", bar_size="5m", skew_beta=1.0, skew_t_gamma=0.4)
    dyn = build_dynamics(model, cfg)
    ladder = np.arange(5700.0, 6300.0 + 2.5, 5.0)
    m = np.log(ladder / 6000.0)
    barf = 300 / (252 * 6.5 * 3600.0)
    for t in range(78):
        iv = smile_iv(m, DEFAULT_SMILE, np.array([[SIGMA0 * 1.8]]), dyn, t)
        put = bsm_put(6000.0, ladder, barf * (77 - t), R, iv)
        assert np.all(np.diff(put) >= -1e-12), f"butterfly flip at bar {t}"
```

- [ ] **Step 3: Late-window quantified report test** (append to `tests/test_sim_pricing.py`)

```python
def test_late_window_credits_steeper_with_gamma():
    """Gate B deliverable: quantify the last-window credit shift (spec §8)."""
    from sim_calibrate import CalibratedModel, GarchParams, build_dynamics
    from sim_config import SimRunConfig
    rng = np.random.default_rng(0)
    n, steps = 60, 78
    spots = np.full((n, steps), 6000.0) + rng.normal(0, 2.0, (n, steps)).cumsum(1) * 0.1
    spots *= np.linspace(1.0, 0.97, steps)[None, :]          # down drift
    sigmas = np.full((n, steps), SIGMA0 * 1.8)               # vol shock
    model = CalibratedModel(
        garch=GarchParams(omega=2e-10, alpha=0.05, gamma=0.10, beta=0.85, nu=6.0,
                          converged=True),
        ushape=np.ones(steps), sigma0=SIGMA0, smile=DEFAULT_SMILE, vix0=15.0,
        source="test")
    ladder = np.arange(5100.0, 6900.0 + 2.5, 5.0)
    fills, minutes = {}, {}
    for tag, gamma in [("gamma0", 0.0), ("gamma04", 0.4)]:
        cfg = SimRunConfig(strategy_name="T", bar_size="5m", skew_beta=1.0,
                           skew_t_gamma=gamma)
        es = run_entry_sim(model, cfg, spots, sigmas, ladder, dyn=build_dynamics(model, cfg))
        fills[tag], minutes[tag] = es.fill, es.entry_minute
    late = minutes["gamma0"] >= 47                            # last hour of the 09:35-14:00 window
    print(f"late-window mean credit gamma=0: {fills['gamma0'][late].mean():.4f} "
          f"gamma=0.4: {fills['gamma04'][late].mean():.4f}")
    assert fills["gamma04"][late].mean() > fills["gamma0"][late].mean()
```

with a small adapter placed above the test in the same file (sim_pricing must not import sim_engine — keep pricing importable standalone):

```python
def run_entry_sim(model, cfg, spots, sigmas, ladder, dyn=None):
    """Thin stand-in for sim_engine.run_entry on prebuilt paths (avoids the import)."""
    from types import SimpleNamespace
    from sim_engine import run_entry
    return run_entry(model, cfg, _strategy_sim(), SimpleNamespace(spots=spots, sigmas=sigmas),
                     ladder, dyn=dyn)


def _strategy_sim():
    from strategy_models import Condition, ExitRules, StopLoss, Strategy
    return Strategy(
        name="T", direction="bull_put",
        conditions=[
            Condition(kind="short_delta", params={"min": 0.05, "max": 0.60}),
            Condition(kind="spread_width", params={"min": 5, "max": 50}),
            Condition(kind="credit", params={"min": 0.05}),
            Condition(kind="entry_window", params={"start": "09:35", "end": "14:00"}),
        ],
        exit_rules=ExitRules(stop_loss=StopLoss(multiplier=6.0)), budget=None)
```

- [ ] **Step 4: Run the new tests**

Run: `python -m pytest tests/test_sim_regression.py tests/test_sim_pricing.py -v -s`
Expected: all PASS; the printed late-window means show the shift (record the numbers in the commit message).

- [ ] **Step 5: README + commit**

Append one sentence to the README section: `skew_t_gamma` (0..1, literature anchor ~0.4) scales this tilt by `(T0/T)^gamma`, steepening wings toward expiry.

```bash
git add tests/test_sim_regression.py tests/test_sim_pricing.py README.md tests/fixtures/sim_baseline_AB.npz
git commit -m "feat(sim): Gate B closed — expiry amplification, AB baseline pinned (late-window credit X.XXXX -> X.XXXX)"
```

## Phase C' — variance-budget ATM anchor

### Task 9: `atm_budget` + `budget_beta` dials

**Files:**
- Modify: `sim_config.py` (two fields + validation), `tests/test_sim_config.py`

**Interfaces:**
- Produces: `SimRunConfig.atm_budget: bool = False`, `SimRunConfig.budget_beta: float = 1.0` (validated `>= 0`).

- [ ] **Step 1: Failing tests** (append to `tests/test_sim_config.py`)

```python
def test_budget_dials_default_neutral():
    c = _cfg()
    assert c.atm_budget is False
    assert c.budget_beta == 1.0


def test_negative_budget_beta_rejected():
    with pytest.raises(ValueError, match="budget_beta"):
        _cfg(budget_beta=-1.0).validate()
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_sim_config.py -v`
Expected: FAIL — no `atm_budget` attribute.

- [ ] **Step 3: Implement** — `sim_config.py` fields (after `skew_t_gamma`):

```python
    atm_budget: bool = False            # variance-budget ATM anchor (spec §7); False = legacy level shift
    budget_beta: float = 1.0            # budget state-sensitivity scale (1.0 = theory)
```

`validate()` after the `skew_t_gamma` check:

```python
        if self.budget_beta < 0:
            raise ValueError("budget_beta must be >= 0")
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_sim_config.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add sim_config.py tests/test_sim_config.py
git commit -m "feat(sim-config): atm_budget/budget_beta dials"
```

### Task 10: Budget tables in `build_dynamics`

**Files:**
- Modify: `sim_calibrate.py` (`build_dynamics`)
- Modify: `tests/test_sim_calibrate.py`

**Interfaces:**
- Consumes: `model.garch` (omega/alpha/gamma/beta), `cfg.gamma_mult`, `model.ushape` (length == `cfg.steps_per_day()` — the `_CALIB_CACHE` key includes `bar_size`, which pins `steps_per_day`).
- Produces: `dyn.v_bar, dyn.a_tab, dyn.b_tab, dyn.v0` filled when `cfg.atm_budget`; `t_scale` also computed when `atm_budget` is on even with `skew_t_gamma == 0` (the anchor annualizes by `t_scale`).

- [ ] **Step 1: Failing tests** (append to `tests/test_sim_calibrate.py`)

```python
def _budget_cfg(**kw):
    from sim_config import SimRunConfig
    return SimRunConfig(strategy_name="T", source="csv", bar_size="5m",
                        atm_budget=True, **kw)


def test_budget_tables_match_direct_summation():
    from sim_calibrate import build_dynamics
    bars = _make_bars()
    cfg = _budget_cfg()
    model = calibrate(bars, cfg)
    dyn = build_dynamics(model, cfg)
    steps = 78
    barf = 300 / (252 * 6.5 * 3600.0)
    u2 = np.asarray(model.ushape, dtype=float)[:steps] ** 2 * barf
    p_eff = (model.garch.alpha + model.garch.gamma * cfg.gamma_mult / 2.0
             + model.garch.beta)
    v_bar = model.garch.omega / (1.0 - p_eff)
    for t in (0, 1, 39, 76):
        ks = np.arange(t + 1, steps)
        s_direct = float(np.sum(u2[t + 1:]))
        p_direct = float(np.sum(p_eff ** (ks - t) * u2[ks]))
        assert dyn.a_tab[t] + dyn.b_tab[t] * v_bar == pytest.approx(v_bar * s_direct,
                                                                    rel=1e-12)
        assert dyn.b_tab[t] == pytest.approx(p_direct, rel=1e-12)
    assert dyn.v0 == pytest.approx(v_bar * float(np.sum(u2[1:])), rel=1e-12)
    assert dyn.v_bar == pytest.approx(v_bar, rel=1e-15)


def test_conditional_expectation_closed_form():
    """E[sigma^2_{t+k}] = v_bar + p^k (sigma^2 - v_bar): iterate the exact E-map."""
    from sim_config import SimRunConfig
    bars = _make_bars()
    cfg = SimRunConfig(strategy_name="T", source="csv", bar_size="5m")
    model = calibrate(bars, cfg)
    g = model.garch
    p_eff = g.alpha + g.gamma / 2.0 + g.beta
    v_bar = g.omega / (1.0 - p_eff)
    s2 = 3.0 * v_bar
    for k in range(1, 60):
        s2 = g.omega + p_eff * s2                      # exact expectation recursion
        assert s2 == pytest.approx(v_bar + p_eff ** k * (3.0 * v_bar - v_bar),
                                   rel=1e-12)


def test_budget_off_leaves_tables_empty():
    from sim_config import SimRunConfig
    cfg = SimRunConfig(strategy_name="T", source="csv", bar_size="5m")
    model = calibrate(_make_bars(), cfg)
    dyn = build_dynamics(model, cfg)
    assert dyn.a_tab is None and dyn.b_tab is None and dyn.v0 == 0.0
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_sim_calibrate.py -v -k budget`
Expected: FAIL — `dyn.a_tab is None` with the current builder.

- [ ] **Step 3: Implement** — replace `build_dynamics` in `sim_calibrate.py`:

```python
def build_dynamics(model: CalibratedModel, cfg: SimRunConfig) -> SmileDynamics:
    """Per-run smile dynamics from cfg dials. O(steps) precompute.

    Never cache this with the model: sim_jobs._CALIB_CACHE keys on the data source
    only, while these dials vary per run.
    """
    steps = cfg.steps_per_day()
    # Same bar fraction as sim_pricing.bar_year_frac (avoided here to keep
    # sim_calibrate free of a sim_pricing import): 252 RTH days x 6.5 h.
    barf = BAR_SECONDS[cfg.bar_size] / (252 * 6.5 * 3600.0)
    t_scale = np.ones(steps)
    if cfg.skew_t_gamma > 0.0 or cfg.atm_budget:
        t_left = np.arange(steps - 1, -1, -1) * barf     # years to expiry after bar t
        t_scale = t_left[0] / np.maximum(t_left, 0.5 * barf)
    v_bar = a_tab = b_tab = None
    v0 = 0.0
    if cfg.atm_budget:
        g = model.garch
        # p_eff must mirror sim_paths.py:21 (gamma_mult scales the GJR term); the
        # 1/2 comes from E[neg * eps^2] = E[eps^2]/2 by symmetry of the shocks.
        p_eff = g.alpha + g.gamma * cfg.gamma_mult / 2.0 + g.beta
        v_bar_val = g.omega / (1.0 - p_eff)
        u2 = np.asarray(model.ushape, dtype=float)[:steps] ** 2 * barf
        # S(t) = sum_{k>t} u2[k];  P(t) = sum_{k>t} p_eff^(k-t) u2[k]
        S = np.zeros(steps)
        P = np.zeros(steps)
        for t in range(steps - 2, -1, -1):
            S[t] = S[t + 1] + u2[t + 1]
            P[t] = u2[t + 1] + p_eff * P[t + 1]
        v_bar = v_bar_val
        a_tab = v_bar_val * (S - P)      # A(t) = v_bar*(S(t)-P(t))
        b_tab = P                        # B(t) = P(t)
        v0 = v_bar_val * float(S[0])     # V(0, initial state sigma^2 = v_bar)
    return SmileDynamics(
        sigma0=model.sigma0, vol_beta=cfg.vol_beta, flat_iv=cfg.flat_iv,
        iv0=float(model.smile.iv(0.0)), skew_beta=cfg.skew_beta,
        t_scale=t_scale, skew_t_gamma=cfg.skew_t_gamma,
        atm_budget=cfg.atm_budget, budget_beta=cfg.budget_beta,
        v_bar=v_bar if v_bar is not None else 0.0,
        a_tab=a_tab, b_tab=b_tab, v0=v0)
```

- [ ] **Step 4: Run to verify pass + regression intact**

Run: `python -m pytest tests/test_sim_calibrate.py tests/test_sim_regression.py -v`
Expected: all PASS (`atm_budget` defaults False -> tables empty -> chain untouched).

- [ ] **Step 5: Commit**

```bash
git add sim_calibrate.py tests/test_sim_calibrate.py
git commit -m "feat(sim-calibrate): variance-budget tables (S/P backward recursions)"
```

### Task 11: `smile_iv` budget branch

**Files:**
- Modify: `sim_pricing.py` (replace `smile_iv` — full final function below)
- Modify: `tests/test_sim_pricing.py` (append)

**Interfaces:**
- Consumes: `dyn.atm_budget / budget_beta / v_bar / a_tab / b_tab / v0 / t_scale` (Task 10).
- Produces: final `smile_iv` — no further signature changes.

- [ ] **Step 1: Failing tests** (append to `tests/test_sim_pricing.py`)

```python
def _budget_model(ushape):
    from sim_calibrate import CalibratedModel, GarchParams
    return CalibratedModel(
        garch=GarchParams(omega=2e-10, alpha=0.05, gamma=0.10, beta=0.85, nu=6.0,
                          converged=True),
        ushape=ushape, sigma0=SIGMA0, smile=DEFAULT_SMILE, vix0=15.0, source="test")


def _ubars(**kw):
    """Budget dyn over a 78-bar 5-min day with the given ATM sigma path."""
    from sim_calibrate import build_dynamics
    from sim_config import SimRunConfig
    u = np.interp(np.arange(78), [0, 6, 39, 72, 77], [2.0, 0.7, 0.6, 1.6, 2.4])
    u /= u.mean()
    model = _budget_model(u if kw.pop("u_shape", True) else np.ones(78))
    cfg = SimRunConfig(strategy_name="T", bar_size="5m", atm_budget=True, **kw)
    return build_dynamics(model, cfg), model


def test_budget_quiet_flat_ushape_gives_flat_atm_iv():
    """Flat U-shape: S(t)/S(0) == T_t/T_ref cancels t_scale -> level == 0."""
    dyn, model = _ubars(u_shape=False)
    m = np.array([0.0])
    ivs = np.array([smile_iv(m, model.smile, np.array([[np.sqrt(dyn.v_bar)]]), dyn, t)[0]
                    for t in range(77)])
    assert np.allclose(ivs, dyn.iv0, atol=1e-10)


def test_budget_quiet_ushape_early_dip_then_firms():
    """U-shape IV signature: early burn-off dips the level below the anchor, then the
    remaining close-bucket variance firms it back up into the expiry (spec §7). The
    dip's depth/timing depends on the close-bucket weight — assert the robust ordering,
    not a midday sign."""
    dyn, model = _ubars()
    v_atm = np.sqrt(dyn.v_bar)
    lvl = lambda t: smile_iv(np.array([0.0]), model.smile,
                             np.array([[v_atm]]), dyn, t)[0] - dyn.iv0
    assert lvl(0) == pytest.approx(0.0, abs=1e-12)     # L(0) == iv0 exactly anchored
    assert lvl(6) < 0.0                                # open bucket burned off -> dip
    assert lvl(39) > lvl(6)                            # recovery as close ramp dominates
    assert lvl(70) > lvl(39)                           # keeps firming into the close


def test_budget_stress_rises_into_close():
    dyn, model = _ubars()
    v_stress = 2.0 * np.sqrt(dyn.v_bar)
    lvl = lambda t: smile_iv(np.array([0.0]), model.smile,
                             np.array([[v_stress]]), dyn, t)[0] - dyn.iv0
    assert lvl(39) > 0.0                               # stress lifts IV above anchor
    assert lvl(70) > lvl(39)                           # and it rises into the close


def test_budget_off_branch_bit_identical_to_legacy_expression():
    from sim_calibrate import build_dynamics
    from sim_config import SimRunConfig
    model = _budget_model(np.ones(78))
    cfg = SimRunConfig(strategy_name="T", bar_size="5m")
    dyn = build_dynamics(model, cfg)
    m = np.linspace(-0.15, 0.15, 21)
    sigma = np.array([[0.0007], [0.0004]])
    out = smile_iv(m, model.smile, sigma, dyn, 3)
    legacy = np.clip(model.smile.iv(m) + 0.75 * (sigma - SIGMA0), 0.01, 5.0)
    assert np.array_equal(out, legacy)
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_sim_pricing.py -v -k budget`
Expected: FAIL — `dyn.atm_budget` is never read; quiet U-shape signature absent (legacy branch is a pure sigma shift, `lvl(39) == 0.0` fails).

- [ ] **Step 3: Implement** — replace `smile_iv` in `sim_pricing.py` (final form):

```python
def smile_iv(m, smile, sigma_t, dyn, t):
    """IV at bar t from log-moneyness m + per-path per-bar sigma_t (smile-dynamics spec §4).

    dyn is a sim_calibrate.SmileDynamics carrying the run's dials/tables; neutral dyn
    reproduces the legacy formula exactly:
        clip(smile.iv(m) + vol_beta*(sigma - sigma0), 0.01, 5.0)
    With atm_budget the level is the variance-budget anchor: annualized remaining
    expected variance conditioned on the path's sigma state, reattached to the
    snapshot's iv0 at t=0.
    """
    m = np.asarray(m, dtype=float)
    sigma = np.asarray(sigma_t, dtype=float)
    if dyn.flat_iv:
        return np.full(np.broadcast_shapes(m.shape, sigma.shape), dyn.iv0)
    base = smile.iv(m)
    if dyn.atm_budget:
        sig2t = dyn.v_bar + dyn.budget_beta * (sigma * sigma - dyn.v_bar)
        v = dyn.a_tab[t] + dyn.b_tab[t] * sig2t
        level = dyn.iv0 * (np.sqrt(v / dyn.v0 * float(dyn.t_scale[t])) - 1.0)
    else:
        level = dyn.vol_beta * (sigma - dyn.sigma0)
    ratio = np.clip(sigma / dyn.sigma0 - 1.0, _VOL_RATIO_MIN, _VOL_RATIO_MAX)
    tilt = -dyn.skew_beta * (float(dyn.t_scale[t]) ** dyn.skew_t_gamma) * ratio * m
    return np.clip(base + level + tilt, 0.01, 5.0)
```

- [ ] **Step 4: Run to verify pass + full regression**

Run: `python -m pytest tests/test_sim_pricing.py tests/test_sim_regression.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add sim_pricing.py tests/test_sim_pricing.py
git commit -m "feat(sim-pricing): variance-budget ATM anchor level branch"
```

### Task 12: Gate C' — baseline ABC, bias report, export, README

**Files:**
- Modify: `tests/test_sim_regression.py`, `sim_jobs.py` (export), `README.md`
- Create: `tests/fixtures/sim_baseline_ABC.npz`

**Interfaces:**
- Produces: committed `sim_baseline_ABC.npz` (full stack) — the terminal link of the chain.

- [ ] **Step 1: Chain tests** (append to `tests/test_sim_regression.py`)

```python
ABC_STATE = {"skew_beta": 1.0, "skew_t_gamma": 0.4, "atm_budget": True,
             "budget_beta": 1.0}


def test_phase_C_budget_off_matches_phase_B():
    """Gate C' chain link: atm_budget=False must reproduce the Phase-B baseline."""
    _assert_cell_matches(_npz("AB"), *_cell(AB_STATE))


def test_phase_C_full_stack_baseline_unchanged():
    _assert_cell_matches(_npz("ABC"), *_cell(ABC_STATE))
```

The Gate C' bias report lives in `tests/test_sim_pricing.py` (Step 2 below) — it is a
deterministic directional comparison, not a fixture chain test, because the fixture's
U-shape is nearly flat (there the anchor correctly degenerates to the legacy level).

- [ ] **Step 2: Deterministic bias report test** (append to `tests/test_sim_pricing.py`)

```python
def test_late_window_entry_bias_report():
    """Gate C' deliverable (spec §8 item 6): the original motivation, quantified.

    Deterministic directional case: close-heavy U-shape + elevated vol state. The
    budget anchor's annualized remaining variance concentrates in the close bucket,
    so late-window entries price RICHER than under the legacy flat level. The real
    market's late-day IV crush comes from variance-premium burn-off, which is an
    accepted residual (spec §7) — expect the anchor to move the other way from the
    premium on quiet real days. Print the numbers for the Gate C' report.
    """
    from types import SimpleNamespace

    from sim_calibrate import build_dynamics
    from sim_config import SimRunConfig
    from sim_engine import run_entry

    u = np.interp(np.arange(78), [0, 6, 39, 72, 77], [2.0, 0.7, 0.6, 1.6, 2.4])
    u /= u.mean()
    g = GarchParams(omega=2e-10, alpha=0.05, gamma=0.10, beta=0.85, nu=6.0,
                    converged=True)
    v_bar = g.omega / (1.0 - (g.alpha + g.gamma / 2.0 + g.beta))
    model = CalibratedModel(garch=g, ushape=u, sigma0=float(np.sqrt(v_bar)),
                            smile=DEFAULT_SMILE, vix0=15.0, source="test")
    rng = np.random.default_rng(1)
    spots = np.full((60, 78), 6000.0) + rng.normal(0, 2.0, (60, 78)).cumsum(1) * 0.1
    paths = SimpleNamespace(spots=spots, sigmas=np.full((60, 78), 1.5 * np.sqrt(v_bar)))
    ladder = np.arange(5100.0, 6900.0 + 2.5, 5.0)
    res = {}
    for tag, kw in [("legacy", {}), ("budget", {"atm_budget": True})]:
        cfg = SimRunConfig(strategy_name="T", bar_size="5m", skew_beta=1.0, **kw)
        es = run_entry(model, cfg, _strategy_sim(), paths, ladder,
                       dyn=build_dynamics(model, cfg))
        res[tag] = es
    for tag in res:
        late = (res[tag].entry_minute >= 47) & res[tag].entered
        print(f"{tag}: late-window mean fill credit "
              f"{res[tag].fill[late].mean() if late.any() else float('nan'):.4f}")
    late_l = (res["legacy"].entry_minute >= 47) & res["legacy"].entered
    late_b = (res["budget"].entry_minute >= 47) & res["budget"].entered
    assert late_l.any() and late_b.any()
    assert res["budget"].fill[late_b].mean() > res["legacy"].fill[late_l].mean()
```

Before this test, extend `tests/test_sim_pricing.py`'s import (line 5) to
`from sim_calibrate import (DEFAULT_SMILE, CalibratedModel, GarchParams, SmileDynamics)`
and delete the now-redundant local imports inside `_budget_model` (Task 11).

- [ ] **Step 3: Run the new tests, then capture the full-stack baseline**

Run: `python -m pytest tests/test_sim_regression.py tests/test_sim_pricing.py -v -s`
Expected: `test_phase_C_budget_off_matches_phase_B` PASS (atm_budget=False is
bit-identical already); `test_phase_C_full_stack_baseline_unchanged` FAIL (no ABC npz
yet); the bias report test PASSes with its printed numbers.

Then capture:

Run: `python tests/fixtures/generate_sim_baseline.py --tag ABC --skew-beta 1.0 --t-gamma 0.4 --budget`
Expected: NN/60 entered (same NN as legacy/A/AB).

Re-run: `python -m pytest tests/test_sim_regression.py -v -s`
Expected: all PASS; note the printed numbers (they go into the commit message).

- [ ] **Step 4: Export the dials** — in `sim_jobs.py`, replace the
  `skew_beta=cfg.skew_beta),` line added in Task 6 with:

```python
                                                    skew_beta=cfg.skew_beta,
                                                    skew_t_gamma=cfg.skew_t_gamma,
                                                    atm_budget=cfg.atm_budget,
                                                    budget_beta=cfg.budget_beta),
```

- [ ] **Step 5: README** — extend the smile-dynamics section:

```markdown
`atm_budget = true` replaces the flat ATM level with a variance-budget anchor:
ATM IV is re-anchored each bar to the model's annualized remaining expected variance
(closed-form GJR conditional expectation weighted by the intraday U-shape), normalized
so the first bar matches the captured snapshot exactly. Quiet paths now show the
model's intraday IV profile — early burn-off and progressive firm-up into the close
(the trough's depth/timing follows the close-bucket weight) — instead of a flat level.
Note the anchor's state sensitivity is materially stronger than the legacy linear
`vol_beta` link (it is the theory value: remaining variance scales with the persistent
GARCH state); treat `budget_beta` as the A/B dial for that channel. The variance-risk-
premium burn-off that makes real quiet-day late IV lower than the model's expectation
is an accepted residual (spec §7). `skew_t_gamma` (0..1, literature anchor ~0.4)
scales the skew tilt by `(T0/T)^gamma`, steepening wings toward expiry.
```

- [ ] **Step 6: Full suite + commit**

Run: `python -m pytest tests/ -v`
Expected: all PASS (this closes **Gate C'**).

```bash
git add tests/test_sim_regression.py sim_jobs.py README.md tests/fixtures/sim_baseline_ABC.npz
git commit -m "feat(sim): Gate C' closed — variance-budget ATM anchor, full stack pinned (late-window bias legacy -> budget: -X.X%)"
```

### Task 13: Final sweep

**Files:**
- Modify: none expected (read-only verification); fix anything the sweep surfaces.

- [ ] **Step 1: Verify the complete chain in one run**

Run: `python -m pytest tests/test_sim_regression.py tests/test_sim_config.py tests/test_sim_pricing.py tests/test_sim_calibrate.py tests/test_sim_engine.py -v`
Expected: all PASS — four npz links (legacy, A, AB, ABC) plus all unit/property tests.

- [ ] **Step 2: Full repo suite**

Run: `python -m pytest tests/ -v`
Expected: all PASS, no skips introduced by this work.

- [ ] **Step 3: Docs audit** — confirm in `README.md`: the three dials documented with defaults, the formula, the validation-gate methodology (fan-vs-market per spec §3), all in English. Confirm `sim_jobs` export lists all four dials.

- [ ] **Step 4: Commit (only if the sweep changed something) + summarize**

```bash
git status --short   # must be clean if nothing surfaced
```

Report: chain links verified, bias numbers from Tasks 8/12, and the dial defaults.
