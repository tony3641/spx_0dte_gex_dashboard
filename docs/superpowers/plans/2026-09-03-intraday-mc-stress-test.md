# Intraday MC Stress-Test Framework — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an intraday Monte Carlo stress-test engine (GJR-GARCH-t paths → BSM smile marking → simulation of the strategies in `config/strategies.json`) with a Simulation dashboard tab and REST job API.

**Architecture:** Flat `sim_*.py` modules at repo root (repo idiom). Vectorized numpy path generation; option pricing computed per-minute inside the scan loops (no full path×step×strike tensors); entry/exit as per-minute vectorized scans; family re-entry via per-path trigger events. Jobs run on `asyncio.to_thread` with an in-memory registry; results render in a new Plotly tab. A MockState reference harness pins the vectorized engine to the real `strategy_engine` semantics.

**Tech Stack:** Python 3.10, FastAPI, numpy + scipy (new runtime deps), vanilla JS + Plotly 2.32, pytest + pytest-playwright (dev-only).

**Spec:** `docs/superpowers/specs/2026-09-03-intraday-mc-stress-test-design.md` — authoritative; executors read both.

## Global Constraints

- Work in the worktree `C:\Users\tony3\spx_0dte_gex_dashboard\.claude\worktrees\feature+intraday-mc-stress`, branch `worktree-feature+intraday-mc-stress`. Never `cd` to the main checkout.
- Two pre-existing failures on this base are KNOWN and out of scope: `tests/test_chain_fetcher.py::test_compute_gex_uses_bsm_gamma_when_ib_gamma_missing` and `tests/test_market_hours.py::TestIsFomcDay::test_known_fomc_date`. Full-suite runs must show exactly those 2 failures and nothing else.
- Repo is flat modules — no `sim/` package. Runtime deps added: `numpy`, `scipy` only. Playwright is dev-only in `requirements-dev.txt`.
- Fill rules (verbatim from spec §2): entry fill = `min(tick_floor(credit_mid), S_bid − L_ask natural)`, floored at one tick; stop exit debit = trigger + 0.10 where trigger = |fill_credit| × multiplier; tick 0.05; expiry = intrinsic at final bar × 100.
- Simulation must never place orders or mutate live trading state; it only reads `Strategy` objects.
- Commit style: conventional commits (`feat(sim): …`, `test(sim): …`), each ending with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.
- Tests run from the worktree root: `python -m pytest tests/ -q --tb=short` (expect exactly the 2 known failures), or targeted: `python -m pytest tests/test_sim_x.py -q`.

## File Structure

| File | Responsibility |
|---|---|
| `sim_config.py` | `SimRunConfig` dataclass, validation, JSON round-trip, `BAR_SECONDS`, sweep-cell expansion |
| `sim_data.py` | `BarSeries`, layered loaders (CSV → yfinance → IB), `load_bars_async` for the server path |
| `sim_calibrate.py` | `GarchParams`, `SmileParams`, `CalibratedModel`, GJR-t MLE, U-shape, smile fit, snapshot load/save |
| `sim_paths.py` | `SimPaths`, `simulate_chunk` (GJR recursion + standardized-t + U-shape) |
| `sim_pricing.py` | BSM put/delta, smile IV matrix, half-spread, ladder, tick-floor, combo fill |
| `sim_engine.py` | `EntryState`, `TrialResult`, entry/exit scans, `run_cell`, `run_family` |
| `sim_risk.py` | summarize/breakdown/max-DD/fan/histogram/bootstrap-ruin, cell payload assembly |
| `sim_jobs.py` | job registry, background execution, progress, cancel, `execute_pipeline` |
| `server.py` | +6 `/api/sim/*` endpoints |
| `ws_handler.py` | +`set_tab:sim` branch |
| `static/index.html` | +Simulation tab button + panel markup |
| `static/js/state.js` | `VALID_TABS` + `'sim'` |
| `static/js/tabs.js` | +`sim` branch in `switchTab` |
| `static/js/sim-tab.js` | new: form, run/poll, Plotly rendering, CSV export |
| `static/css/sim.css` | new: tab styles (existing theme variables) |
| `tests/fixtures/generate_sim_fixture.py` | seeded generator writing `tests/fixtures/sim_bars_5m.csv` |
| `tests/test_sim_*.py` | unit + API E2E suites |
| `tests/e2e/test_sim_ui_playwright.py` | UI E2E (skipped when playwright/browser absent) |
| `config/sim_smile_default.json` | bundled default smile snapshot (committed) |
| `config/sim_smile.json` | captured smile (gitignored) |
| `requirements.txt` / `requirements-dev.txt` | numpy+scipy / pytest-playwright |
| `README.md` | feature + files table updates |

Conventions used throughout: `n` = number of paths in the current chunk, `steps` = bars per day
(`cfg.steps_per_day()`), ladder `K` = np.ndarray of strikes (5-pt spacing), minute index `t` ∈
[0, steps), spot matrix `paths.spots` shape `(n, steps)` where column `t` is the close of bar `t`.

---

### Task 1: Dependencies + spec already amended (verify)

**Files:**
- Modify: `requirements.txt`
- Create: `requirements-dev.txt`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: nothing.
- Produces: importable `numpy`, `scipy`; `requirements-dev.txt` referenced by Task 16.

- [ ] **Step 1: Add runtime deps to `requirements.txt`**

Append to `requirements.txt` (keep the ibapi editable line first, do not reorder existing entries):

```
numpy>=1.26
scipy>=1.10
```

- [ ] **Step 2: Create `requirements-dev.txt`**

```text
-r requirements.txt
pytest-playwright>=0.4
```

- [ ] **Step 3: Ignore the captured smile file**

Append to `.gitignore` (the file already ignores `docs/superpowers/` and `.superpowers/`; add one line):

```
config/sim_smile.json
```

- [ ] **Step 4: Verify imports**

Run: `python -c "import numpy, scipy; print(numpy.__version__, scipy.__version__)"`
Expected: version numbers printed (install first with `pip install numpy scipy` if missing).

- [ ] **Step 5: Commit**

```bash
git add requirements.txt requirements-dev.txt .gitignore
git commit -m "build(sim): add numpy/scipy runtime deps, playwright dev deps, ignore captured smile"
```

---

### Task 2: `sim_config.py` — run configuration

**Files:**
- Create: `sim_config.py`
- Test: `tests/test_sim_config.py`

**Interfaces:**
- Consumes: nothing.
- Produces (used by every later task):
  - `BAR_SECONDS = {"5s": 5, "15s": 15, "30s": 30, "1m": 60, "5m": 300}`
  - `class SimRunConfig` with fields `strategy_name, mode="single", source="auto", csv_path="", bar_size="5m", lookback_days=60, spot0=None, n_paths=10000, seed=42, chunk_size=250, equity=100000.0, ruin_threshold_pct=0.20, bootstrap_seqs=500, bootstrap_len=60, sl_multipliers=None, strike_mode="engine", dynamic_k_values=None, width_points=50.0, nu_override=None, gamma_mult=1.0, vol_beta=0.75, flat_iv=False, stop_extra=0.10, tick_size=0.05, ladder_range_pct=0.15`
  - methods: `steps_per_day() -> int`, `validate() -> None` (raises `ValueError`), `to_dict() -> dict`, `from_dict(d) -> SimRunConfig` (classmethod)
  - function `sweep_cells(cfg) -> list[dict]` returning `{"sl_multiplier": m_or_None, "k": k_or_None}` cells (`None` multiplier = use the strategy's own stop; `None` k = engine strike selection).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_sim_config.py
import pytest
from sim_config import SimRunConfig, sweep_cells, BAR_SECONDS


def test_defaults_and_steps_per_day():
    cfg = SimRunConfig(strategy_name="Main")
    assert cfg.bar_size == "5m" and cfg.mode == "single"
    assert cfg.steps_per_day() == 78          # 390 min / 5
    assert BAR_SECONDS["1m"] == 60


def test_validate_rejects_bad_inputs():
    with pytest.raises(ValueError):
        SimRunConfig(strategy_name="").validate()
    with pytest.raises(ValueError):
        SimRunConfig(strategy_name="X", mode="both").validate()
    with pytest.raises(ValueError):
        SimRunConfig(strategy_name="X", bar_size="2m").validate()
    with pytest.raises(ValueError):
        SimRunConfig(strategy_name="X", n_paths=0).validate()
    with pytest.raises(ValueError):
        SimRunConfig(strategy_name="X", equity=-1).validate()
    with pytest.raises(ValueError):
        SimRunConfig(strategy_name="X", sl_multipliers=[0.0]).validate()
    SimRunConfig(strategy_name="X", sl_multipliers=[1.5, float("inf")]).validate()


def test_json_round_trip():
    cfg = SimRunConfig(strategy_name="Main", n_paths=99, sl_multipliers=[2.0, float("inf")])
    d = cfg.to_dict()
    assert d["sl_multipliers"][1] == "inf"     # JSON-safe encoding
    cfg2 = SimRunConfig.from_dict(d)
    assert cfg2 == cfg


def test_sweep_cells_product():
    cfg = SimRunConfig(strategy_name="Main", sl_multipliers=[1.5, 3.0],
                       strike_mode="dynamic_k", dynamic_k_values=[0.4, 0.6])
    cells = sweep_cells(cfg)
    assert cells == [
        {"sl_multiplier": 1.5, "k": 0.4}, {"sl_multiplier": 1.5, "k": 0.6},
        {"sl_multiplier": 3.0, "k": 0.4}, {"sl_multiplier": 3.0, "k": 0.6},
    ]


def test_sweep_cells_defaults():
    assert sweep_cells(SimRunConfig(strategy_name="M")) == [{"sl_multiplier": None, "k": None}]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_sim_config.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'sim_config'`

- [ ] **Step 3: Write the implementation**

```python
"""Run configuration for the intraday Monte Carlo simulator."""
from dataclasses import dataclass, field, fields
from typing import List, Optional

BAR_SECONDS = {"5s": 5, "15s": 15, "30s": 30, "1m": 60, "5m": 300}
_MODES = ("single", "family")
_SOURCES = ("csv", "yfinance", "ib", "auto")
_STRIKE_MODES = ("engine", "dynamic_k")


@dataclass
class SimRunConfig:
    strategy_name: str
    mode: str = "single"                # "single" | "family"
    source: str = "auto"                # "csv" | "yfinance" | "ib" | "auto"
    csv_path: str = ""
    bar_size: str = "5m"
    lookback_days: int = 60
    spot0: Optional[float] = None       # day-open spot; None = live / last close
    n_paths: int = 10000
    seed: int = 42
    chunk_size: int = 250
    equity: float = 100_000.0
    ruin_threshold_pct: float = 0.20
    bootstrap_seqs: int = 500
    bootstrap_len: int = 60
    sl_multipliers: Optional[List[float]] = None    # None -> strategy's own; inf = hold past stop
    strike_mode: str = "engine"         # "engine" | "dynamic_k"
    dynamic_k_values: Optional[List[float]] = None  # None -> [0.5] when dynamic_k
    width_points: float = 50.0          # spread width in dynamic_k mode
    nu_override: Optional[float] = None             # stress: Student-t dof
    gamma_mult: float = 1.0             # stress: GJR leverage-term multiplier
    vol_beta: float = 0.75              # smile vol-link coefficient (lambda)
    flat_iv: bool = False               # sanity mode: no smile
    stop_extra: float = 0.10            # market-order stop: trigger + this
    tick_size: float = 0.05
    ladder_range_pct: float = 0.15

    def steps_per_day(self) -> int:
        return (390 * 60) // BAR_SECONDS[self.bar_size]

    def validate(self) -> None:
        if not self.strategy_name:
            raise ValueError("strategy_name is required")
        if self.mode not in _MODES:
            raise ValueError(f"mode must be one of {_MODES}")
        if self.source not in _SOURCES:
            raise ValueError(f"source must be one of {_SOURCES}")
        if self.bar_size not in BAR_SECONDS:
            raise ValueError(f"bar_size must be one of {sorted(BAR_SECONDS)}")
        if self.n_paths <= 0:
            raise ValueError("n_paths must be > 0")
        if self.chunk_size <= 0:
            raise ValueError("chunk_size must be > 0")
        if self.equity <= 0:
            raise ValueError("equity must be > 0")
        if not (0 < self.ruin_threshold_pct <= 1):
            raise ValueError("ruin_threshold_pct must be in (0, 1]")
        if self.strike_mode not in _STRIKE_MODES:
            raise ValueError(f"strike_mode must be one of {_STRIKE_MODES}")
        for name in ("nu_override",):
            v = getattr(self, name)
            if v is not None and v <= 2.0:
                raise ValueError(f"{name} must be > 2.0 (Student-t needs finite variance)")
        if self.gamma_mult <= 0 or self.vol_beta < 0:
            raise ValueError("gamma_mult must be > 0 and vol_beta >= 0")
        if self.stop_extra < 0 or self.tick_size <= 0:
            raise ValueError("stop_extra must be >= 0 and tick_size > 0")
        for v in (self.sl_multipliers or []):
            if not (v > 0):
                raise ValueError("sl_multipliers entries must be > 0 (use inf for hold)")
        for v in (self.dynamic_k_values or []):
            if not (0 < v < 10):
                raise ValueError("dynamic_k_values entries must be in (0, 10)")
        if self.source == "csv" and not self.csv_path:
            raise ValueError("csv_path is required when source='csv'")

    def to_dict(self) -> dict:
        d = {}
        for f in fields(self):
            v = getattr(self, f.name)
            if v == float("inf"):
                v = "inf"
            d[f.name] = v
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "SimRunConfig":
        known = {f.name for f in fields(cls)}
        kwargs = {}
        for k, v in dict(d or {}).items():
            if k not in known:
                continue
            if v == "inf":
                v = float("inf")
            kwargs[k] = v
        return cls(**kwargs)


def sweep_cells(cfg: SimRunConfig) -> list:
    """Cartesian product of the SL-multiplier list and the strike-k list.

    sl_multipliers None -> [None] (use the strategy's own stop multiplier).
    engine strike mode ignores k (single [None] column); dynamic_k defaults to [0.5].
    """
    sls = cfg.sl_multipliers or [None]
    ks = [None]
    if cfg.strike_mode == "dynamic_k":
        ks = cfg.dynamic_k_values or [0.5]
    return [{"sl_multiplier": s, "k": k} for s in sls for k in ks]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_sim_config.py -q`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add sim_config.py tests/test_sim_config.py
git commit -m "feat(sim): SimRunConfig with validation, JSON round-trip, sweep cells"
```

---

### Task 3: Fixture CSV generator + committed fixture

**Files:**
- Create: `tests/fixtures/generate_sim_fixture.py`
- Create (generated, committed): `tests/fixtures/sim_bars_5m.csv`
- Test: `tests/test_sim_config.py` (no new file — fixture is data)

**Interfaces:**
- Consumes: nothing.
- Produces: `tests/fixtures/sim_bars_5m.csv` — 10 weekdays × 78 five-minute RTH bars (09:30–16:00 ET),
  header `timestamp,open,high,low,close,volume`, ISO timestamps with `-04:00` offset, ~6000 level,
  seeded (numpy `default_rng(7)`) with mild vol clustering. Consumed by Tasks 4, 14, 16 tests.

- [ ] **Step 1: Write the generator**

```python
"""Generate the committed 5-min fixture CSV used by sim tests (hermetic, no network).

Run:  python tests/fixtures/generate_sim_fixture.py
Writes: tests/fixtures/sim_bars_5m.csv
"""
import os
from datetime import datetime, timedelta

import numpy as np

OUT = os.path.join(os.path.dirname(__file__), "sim_bars_5m.csv")
BARS_PER_DAY = 78            # 09:30-16:00 at 5 min
DAYS = 10


def main() -> None:
    rng = np.random.default_rng(7)
    lines = ["timestamp,open,high,low,close,volume"]
    s = 6000.0
    sigma2 = (0.0004 / 78) ** 2 * 100  # per-bar var ~ (0.045% of spot)^2-ish
    omega, alpha, gamma, beta = 1e-10, 0.05, 0.08, 0.85
    day = datetime(2026, 6, 1)
    made = 0
    while made < DAYS:
        if day.weekday() >= 5:
            day += timedelta(days=1)
            continue
        t0 = day.replace(hour=9, minute=30)
        for b in range(BARS_PER_DAY):
            z = rng.standard_t(6) / np.sqrt(6 / 4)
            eps = np.sqrt(sigma2) * z
            o = s
            s = s * float(np.exp(eps))
            hi = max(o, s) * (1 + abs(rng.normal(0, 0.0002)))
            lo = min(o, s) * (1 - abs(rng.normal(0, 0.0002)))
            ts = (t0 + timedelta(minutes=5 * (b + 1))).strftime("%Y-%m-%dT%H:%M:%S-04:00")
            lines.append(f"{ts},{o:.2f},{hi:.2f},{lo:.2f},{s:.2f},{int(rng.integers(500, 5000))}")
            sigma2 = omega + alpha * eps ** 2 + gamma * eps ** 2 * (eps < 0) + beta * sigma2
        day += timedelta(days=1)
        made += 1
    with open(OUT, "w", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    print(f"wrote {OUT}: {len(lines) - 1} rows")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Generate and eyeball the CSV**

Run: `python tests/fixtures/generate_sim_fixture.py`
Expected: `wrote ...sim_bars_5m.csv: 780 rows`. Sanity: `python -c` one-liner to print the first
data row and confirm the timestamp ends `-04:00` and closes are within 5500–6500.

- [ ] **Step 3: Commit**

```bash
git add tests/fixtures/generate_sim_fixture.py tests/fixtures/sim_bars_5m.csv
git commit -m "test(sim): seeded 5-min fixture CSV generator + committed fixture (780 bars)"
```

---

### Task 4: `sim_data.py` — layered bar loaders

**Files:**
- Create: `sim_data.py`
- Test: `tests/test_sim_data.py`

**Interfaces:**
- Consumes: `SimRunConfig` (Task 2), fixture CSV (Task 3). For the IB layer: a live `ib` client
  object shaped like `ib_client` (has `async req_historical_bars(...)`) — only reachable through
  `load_bars_async` which the server calls on its event loop (Task 14).
- Produces:
  - `@dataclass BarSeries: closes: np.ndarray; minute_of_day: np.ndarray; bar_seconds: int; source: str; vix_closes: Optional[np.ndarray] = None; warnings: List[str] = <factory>`
  - `load_bars(cfg, bars=None) -> BarSeries` — sync; resolves layers csv → yfinance (`source="auto"` skips csv when `csv_path` empty). `bars` bypasses everything (preloaded).
  - `async def load_bars_async(cfg, state=None, ib=None) -> BarSeries` — same resolution, plus the `ib` layer using the server's event loop; yfinance blocking calls wrapped in `asyncio.to_thread`.
  - `parse_csv(path, bar_seconds) -> BarSeries` (exported for tests).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_sim_data.py
import os
import numpy as np
import pytest

from sim_config import SimRunConfig
from sim_data import BarSeries, load_bars, parse_csv

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "sim_bars_5m.csv")


def test_parse_csv_fixture():
    bs = parse_csv(FIXTURE, 300)
    assert bs.source == "csv" and bs.bar_seconds == 300
    assert len(bs.closes) == 780
    # 10 consecutive weekdays x 78 bars; minute_of_day repeats 09:35..16:00
    mods = bs.minute_of_day[:78]
    assert mods[0] == 9 * 60 + 35 and mods[-1] == 16 * 60
    assert np.isfinite(bs.closes).all() and (bs.closes > 0).all()
    # bars outside RTH are absent by construction, but guard the filter anyway
    assert (bs.minute_of_day >= 570).all() and (bs.minute_of_day <= 960).all()


def test_load_bars_csv_layer():
    cfg = SimRunConfig(strategy_name="Main", source="csv", csv_path=FIXTURE)
    bs = load_bars(cfg)
    assert bs.source == "csv" and len(bs.closes) == 780


def test_load_bars_preloaded_bypass():
    cfg = SimRunConfig(strategy_name="Main", source="csv", csv_path=FIXTURE)
    pre = BarSeries(closes=np.array([1.0, 2.0]), minute_of_day=np.array([575, 580]),
                    bar_seconds=300, source="csv")
    assert load_bars(cfg, bars=pre) is pre


def test_load_bars_missing_csv_raises():
    cfg = SimRunConfig(strategy_name="Main", source="csv", csv_path="Z:/nope.csv")
    with pytest.raises(FileNotFoundError):
        load_bars(cfg)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_sim_data.py -q`
Expected: FAIL — `No module named 'sim_data'`

- [ ] **Step 3: Write the implementation**

```python
"""Layered intraday bar loading for the simulator: CSV -> yfinance -> IB.

The IB layer is async (it reuses the server's live client) and is therefore only
reachable via load_bars_async; sync load_bars covers csv/yfinance for tests and
offline runs.
"""
import asyncio
import logging
import math
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

import numpy as np

from sim_config import BAR_SECONDS, SimRunConfig

logger = logging.getLogger(__name__)
RTH_START_MIN = 9 * 60 + 30     # 09:30 ET
RTH_END_MIN = 16 * 60           # 16:00 ET (SPXW cease)


@dataclass
class BarSeries:
    closes: np.ndarray
    minute_of_day: np.ndarray
    bar_seconds: int
    source: str
    vix_closes: Optional[np.ndarray] = None
    warnings: List[str] = field(default_factory=list)


def parse_csv(path: str, bar_seconds: int) -> BarSeries:
    """Schema: timestamp,open,high,low,close[,volume]; ET ISO timestamps."""
    if not os.path.isfile(path):
        raise FileNotFoundError(f"CSV not found: {path}")
    stamps, closes = [], []
    with open(path, "r", encoding="utf-8") as f:
        header = f.readline().strip().split(",")
        ti, ci = header.index("timestamp"), header.index("close")
        for line in f:
            parts = line.strip().split(",")
            if len(parts) <= max(ti, ci) or not parts[ci]:
                continue
            stamps.append(datetime.fromisoformat(parts[ti]))
            closes.append(float(parts[ci]))
    if not closes:
        raise ValueError(f"CSV has no usable rows: {path}")
    mods = np.array([t.hour * 60 + t.minute for t in stamps], dtype=int)
    keep = (mods >= RTH_START_MIN + (bar_seconds // 60)) & (mods <= RTH_END_MIN)
    return BarSeries(
        closes=np.asarray(closes, dtype=float)[keep],
        minute_of_day=mods[keep],
        bar_seconds=bar_seconds,
        source="csv",
    )


def load_bars_yfinance(bar_seconds: int, lookback_days: int) -> BarSeries:
    import yfinance as yf   # deferred: keeps startup light when unused

    interval = {60: "1m", 300: "5m"}.get(bar_seconds)
    if interval is None:
        raise ValueError(f"yfinance cannot serve {bar_seconds}s bars (use 1m or 5m, or a CSV)")
    period = "7d" if interval == "1m" else "60d"
    df = yf.download("^SPX", interval=interval, period=period, progress=False, auto_adjust=False)
    if df is None or df.empty:
        raise RuntimeError("yfinance returned no ^SPX intraday data")
    idx = df.index.tz_convert("America/New_York")
    mods = np.array([t.hour * 60 + t.minute for t in idx], dtype=int)
    keep = (mods > RTH_START_MIN) & (mods <= RTH_END_MIN)
    vix = None
    try:
        vdf = yf.download("^VIX", interval="1d", period="6mo", progress=False, auto_adjust=False)
        if vdf is not None and not vdf.empty:
            vix = vdf["Close"].to_numpy().ravel()[-lookback_days:]
    except Exception as e:   # VIX is optional (mapping falls back to a constant)
        logger.warning(f"^VIX fetch failed: {e}")
    return BarSeries(
        closes=df["Close"].to_numpy().ravel()[keep],
        minute_of_day=mods[keep],
        bar_seconds=bar_seconds,
        source="yfinance",
        vix_closes=vix,
        warnings=["yfinance intraday history is shallow; prefer a CSV for stable calibration"],
    )


async def _load_bars_ib(ib, bar_seconds: int, lookback_days: int) -> BarSeries:
    from market_hours import last_trading_date

    size = {5: "5 secs", 15: "15 secs", 30: "30 secs", 60: "1 min", 300: "5 mins"}[bar_seconds]
    days = max(1, min(lookback_days, {5: 1, 15: 2, 30: 4, 60: 7, 300: 60}[bar_seconds]))
    bars = await ib.req_historical_bars(
        contract=None or getattr(ib, "spx_contract", None),
        end_date_time="", duration=f"{days * 2} D", bar_size=size,
        what_to_show="TRADES", use_rth=True,
    ) if hasattr(ib, "spx_contract") else None
    if not bars:
        raise RuntimeError("IB returned no historical bars")
    stamps = [b.date.astimezone() if b.date.tzinfo else b.date for b in bars]
    mods = np.array([t.hour * 60 + t.minute for t in stamps], dtype=int)
    return BarSeries(
        closes=np.array([float(b.close) for b in bars]),
        minute_of_day=mods, bar_seconds=bar_seconds, source="ib",
        warnings=[f"IB depth limited to ~{days}d at {size}"],
    )


def load_bars(cfg: SimRunConfig, bars: Optional[BarSeries] = None) -> BarSeries:
    """Sync layered loader: preloaded -> csv -> yfinance. IB requires load_bars_async."""
    if bars is not None:
        return bars
    bs = cfg.bar_seconds if hasattr(cfg, "bar_seconds") else BAR_SECONDS[cfg.bar_size]
    errors: List[str] = []
    if cfg.source in ("csv", "auto") and cfg.csv_path:
        try:
            return parse_csv(cfg.csv_path, bs)
        except Exception as e:
            errors.append(f"csv: {e}")
            if cfg.source == "csv":
                raise
    if cfg.source in ("yfinance", "auto"):
        try:
            return load_bars_yfinance(bs, cfg.lookback_days)
        except Exception as e:
            errors.append(f"yfinance: {e}")
    raise RuntimeError("No bar data available (" + "; ".join(errors or ["no layer attempted"]) + "). "
                       "Drop a CSV (timestamp,open,high,low,close) and set its path.")


async def load_bars_async(cfg: SimRunConfig, state=None, ib=None) -> BarSeries:
    """Server-side loader: same layering, plus the IB layer on the running loop."""
    if cfg.source in ("ib", "auto") and ib is not None:
        try:
            return await _load_bars_ib(ib, BAR_SECONDS[cfg.bar_size], cfg.lookback_days)
        except Exception as e:
            if cfg.source == "ib":
                raise
            logger.warning(f"IB bar layer failed: {e}")
    return await asyncio.to_thread(load_bars, cfg)
```

Note: `config.py` already resolves `RTH` windows; these local constants mirror the SPXW RTH day and
exist so the simulator is import-order independent. Keep them module-level (tests import them).

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_sim_data.py -q`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add sim_data.py tests/test_sim_data.py
git commit -m "feat(sim): layered bar loaders (csv/yfinance/ib) with RTH filtering"
```

---

### Task 5: `sim_calibrate.py` — GJR-GARCH(1,1) + Student-t MLE

**Files:**
- Create: `sim_calibrate.py`
- Test: `tests/test_sim_calibrate.py`

**Interfaces:**
- Consumes: `BarSeries.closes` (Task 4).
- Produces:
  - `@dataclass GarchParams: omega: float; alpha: float; gamma: float; beta: float; nu: float; converged: bool`
  - `fit_gjr_t(returns: np.ndarray, nu_init: float = 6.0) -> tuple[GarchParams, list[str]]`
    (`gamma` field name is the GJR leverage term — it shadows nothing; the dataclass field is `gamma`)
  - `gjr_variance_path(params, returns) -> np.ndarray` — σ² recursion used by both the fitter and tests.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_sim_calibrate.py
import numpy as np
import pytest

from sim_calibrate import fit_gjr_t, gjr_variance_path


def _simulate_gjr(n=8000, omega=2e-10, alpha=0.05, gamma=0.10, beta=0.85, nu=6.0, seed=3):
    rng = np.random.default_rng(seed)
    eps = np.empty(n)
    s2 = omega / (1 - alpha - gamma / 2 - beta)
    for t in range(n):
        z = rng.standard_t(nu) / np.sqrt(nu / (nu - 2))
        eps[t] = np.sqrt(s2) * z
        s2 = omega + alpha * eps[t] ** 2 + gamma * eps[t] ** 2 * (eps[t] < 0) + beta * s2
    return eps


def test_fit_recovers_parameters_loosely():
    eps = _simulate_gjr()
    params, warnings = fit_gjr_t(eps)
    assert params.converged
    assert not warnings
    assert 0 < params.alpha < 0.35 and 0 <= params.gamma < 0.5 and 0.3 < params.beta < 0.99
    assert params.alpha + params.beta + params.gamma / 2 < 1.0    # stationary
    assert 2.5 < params.nu < 30


def test_variance_path_matches_recursion():
    eps = _simulate_gjr(n=200)
    p, _ = fit_gjr_t(eps)
    v = gjr_variance_path(p, eps)
    assert v.shape == eps.shape
    assert (v > 0).all()


def test_fit_degenerate_returns_preset():
    eps = np.zeros(100)                    # zero variance -> optimizer cannot work
    p, warnings = fit_gjr_t(eps)
    assert not p.converged
    assert warnings and "preset" in warnings[0].lower()
    assert p.alpha > 0 and p.beta < 1.0    # preset values, variance-targeted omega
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_sim_calibrate.py -q`
Expected: FAIL — `No module named 'sim_calibrate'`

- [ ] **Step 3: Write the implementation**

```python
"""GJR-GARCH(1,1) with Student-t innovations, fitted by MLE (scipy).

Preset fallback on non-convergence keeps runs alive; warnings surface in the UI
calibration panel. All functions are pure — no IO.
"""
import math
from dataclasses import dataclass
from typing import List, Tuple

import numpy as np
from scipy.optimize import minimize
from scipy.special import gammaln, ndtr

PRESET = dict(alpha=0.04, gamma=0.10, beta=0.85, nu=6.0)


@dataclass
class GarchParams:
    omega: float
    alpha: float
    gamma: float        # GJR leverage term (asymmetric response to negative shocks)
    beta: float
    nu: float           # Student-t dof
    converged: bool = True


def gjr_variance_path(p: GarchParams, eps: np.ndarray) -> np.ndarray:
    """sigma^2_t for t = 0..n-1; warm-up variance backcast as EWMA of the first 50 |eps|."""
    n = len(eps)
    back = float(np.mean(eps[: min(50, n)] ** 2)) if n else 1e-12
    s2 = np.empty(n)
    prev = max(back, 1e-12)
    prev_e2 = back
    for t in range(n):
        shock_neg = 1.0 if (t > 0 and eps[t - 1] < 0) else 0.0
        s2[t] = p.omega + p.alpha * prev_e2 + p.gamma * prev_e2 * shock_neg + p.beta * prev
        prev = max(s2[t], 1e-16)
        prev_e2 = eps[t] ** 2
    return s2


def _neg_loglik(theta: np.ndarray, eps: np.ndarray) -> float:
    omega, alpha, gamma, beta, nu = theta
    if omega <= 0 or alpha < 0 or gamma < 0 or beta < 0 or nu <= 2.05:
        return 1e12
    if alpha + gamma / 2 + beta >= 0.999:
        return 1e12
    p = GarchParams(omega=omega, alpha=alpha, gamma=gamma, beta=beta, nu=nu)
    s2 = gjr_variance_path(p, eps)
    if not np.isfinite(s2).all() or (s2 <= 0).any():
        return 1e12
    c = math.lgamma((nu + 1) / 2) - math.lgamma(nu / 2) - 0.5 * math.log(math.pi * (nu - 2))
    ll = c - 0.5 * np.log(s2) - ((nu + 1) / 2) * np.log1p(eps ** 2 / (s2 * (nu - 2)))
    val = -float(np.sum(ll))
    return val if math.isfinite(val) else 1e12


def fit_gjr_t(returns: np.ndarray, nu_init: float = 6.0) -> Tuple[GarchParams, List[str]]:
    """Fit GJR-GARCH(1,1)-t; falls back to a variance-targeted preset when MLE fails."""
    warnings: List[str] = []
    eps = np.asarray(returns, dtype=float)
    eps = eps[np.isfinite(eps)]
    var0 = float(np.var(eps)) if len(eps) > 10 else 1e-10
    var0 = max(var0, 1e-16)
    # variance targeting: omega = var * (1 - alpha - gamma/2 - beta)
    x0 = np.array([var0 * (1 - PRESET["alpha"] - PRESET["gamma"] / 2 - PRESET["beta"]),
                   PRESET["alpha"], PRESET["gamma"], PRESET["beta"], nu_init])
    best, best_val = None, np.inf
    for nu_start in (nu_init, 4.0, 10.0):
        x = x0.copy()
        x[4] = nu_start
        try:
            res = minimize(_neg_loglik, x, args=(eps,), method="Nelder-Mead",
                           options=dict(maxiter=2000, xatol=1e-6, fatol=1e-6))
            res2 = minimize(_neg_loglik, res.x, args=(eps,), method="Nelder-Mead",
                            options=dict(maxiter=1000, xatol=1e-7, fatol=1e-7))
            cand = res2 if res2.fun < res.fun else res
        except Exception:
            continue
        if cand.fun < best_val and np.isfinite(cand.fun):
            best, best_val = cand.x, float(cand.fun)
    if best is None or best_val >= 1e11:
        omega = var0 * (1 - PRESET["alpha"] - PRESET["gamma"] / 2 - PRESET["beta"])
        warnings.append("GARCH MLE did not converge — preset parameters in use (flagged in UI)")
        return GarchParams(omega=omega, alpha=PRESET["alpha"], gamma=PRESET["gamma"],
                           beta=PRESET["beta"], nu=PRESET["nu"], converged=False), warnings
    omega, alpha, gamma, beta, nu = (float(v) for v in best)
    return GarchParams(omega=max(omega, 1e-16), alpha=alpha, gamma=gamma, beta=beta,
                       nu=max(nu, 2.2), converged=True), warnings
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_sim_calibrate.py -q`
Expected: PASS (3 tests). `test_fit_recovers_parameters_loosely` may take ~10–30 s (MLE on 8k points).

- [ ] **Step 5: Commit**

```bash
git add sim_calibrate.py tests/test_sim_calibrate.py
git commit -m "feat(sim): GJR-GARCH(1,1)+Student-t MLE fit with preset fallback"
```

---

### Task 6: `sim_calibrate.py` — U-shape, smile fit, VIX mapping, snapshot persistence

**Files:**
- Modify: `sim_calibrate.py` (append)
- Create: `config/sim_smile_default.json`
- Test: `tests/test_sim_calibrate.py` (append)

**Interfaces:**
- Consumes: `BarSeries` (Task 4), `fit_gjr_t` (Task 5).
- Produces:
  - `@dataclass SmileParams: a: float; b: float; c: float; half_spread_atm: float` with methods
    `iv(m) -> np.ndarray` (quadratic in log-moneyness) and `to_dict()/from_dict()`.
  - `DEFAULT_SMILE = SmileParams(a=0.20, b=-0.35, c=1.20, half_spread_atm=0.05)`
  - `@dataclass CalibratedModel: garch: GarchParams; ushape: np.ndarray; sigma0: float; smile: SmileParams; vix0: float; source: str; warnings: list[str]` with `sigma_annual(cfg) -> float`
  - `calibrate(bars: BarSeries, cfg: SimRunConfig) -> CalibratedModel` (full pipeline)
  - `fit_ushape(returns, minute_of_day, steps_per_day) -> np.ndarray`
  - `fit_smile(m_points, iv_points, fallback) -> tuple[SmileParams, list[str]]`
  - `load_smile_snapshot() -> tuple[SmileParams, str]` (source: `"captured" | "default" | "builtin"`)
  - `save_smile_snapshot(smile: SmileParams) -> None` (writes `config/sim_smile.json`)

- [ ] **Step 1: Write the failing test (append to `tests/test_sim_calibrate.py`)**

```python
from sim_calibrate import (CalibratedModel, DEFAULT_SMILE, SmileParams,
                           calibrate, fit_smile, fit_ushape, load_smile_snapshot)


def _make_bars(n_days=6, bars=78, seed=11):
    from sim_data import BarSeries
    rng = np.random.default_rng(seed)
    # morning + afternoon active, midday quiet -> U-shape
    ushape = np.interp(np.arange(bars), [0, 6, 39, 72, 77], [2.0, 0.7, 0.6, 1.6, 2.4])
    ushape = ushape / ushape.mean()
    closes, mods = [], []
    s = 6000.0
    for d in range(n_days):
        for b in range(bars):
            eps = 0.0005 * ushape[b] * rng.standard_normal()
            s *= float(np.exp(eps))
            closes.append(s)
            mods.append(570 + 5 * (b + 1))
    return BarSeries(closes=np.array(closes), minute_of_day=np.array(mods),
                     bar_seconds=300, source="csv")


def test_fit_ushape_peaks_at_open_and_close():
    bars = _make_bars()
    rets = np.diff(np.log(bars.closes))
    u = fit_ushape(rets, bars.minute_of_day[1:], steps_per_day=78)
    assert u.shape == (78,)
    assert abs(u.mean() - 1.0) < 0.2
    assert u[:6].mean() > u[30:50].mean()      # open busier than midday
    assert u[-6:].mean() > u[30:50].mean()     # close busier than midday


def test_fit_smile_quadratic():
    m = np.linspace(-0.06, 0.0, 12)
    iv_true = 0.20 - 0.35 * m + 1.2 * m ** 2
    smile, warnings = fit_smile(m, iv_true, DEFAULT_SMILE)
    assert not warnings
    assert abs(smile.a - 0.20) < 1e-6 and abs(smile.b + 0.35) < 1e-6 and abs(smile.c - 1.2) < 1e-6


def test_fit_smile_insufficient_points_falls_back():
    smile, warnings = fit_smile(np.array([-0.01]), np.array([0.22]), DEFAULT_SMILE)
    assert warnings and smile == DEFAULT_SMILE


def test_snapshot_round_trip(tmp_path, monkeypatch):
    import sim_calibrate as sc
    monkeypatch.setattr(sc, "SMILE_CAPTURE_PATH", str(tmp_path / "sim_smile.json"))
    monkeypatch.setattr(sc, "SMILE_DEFAULT_PATH", str(tmp_path / "sim_smile_default.json"))
    sc.save_smile_snapshot(SmileParams(a=0.18, b=-0.30, c=1.0, half_spread_atm=0.06))
    smile, src = sc.load_smile_snapshot()
    assert src == "captured" and abs(smile.a - 0.18) < 1e-9
    (tmp_path / "sim_smile.json").unlink()
    smile2, src2 = sc.load_smile_snapshot()
    assert src2 == "builtin" and smile2 == sc.DEFAULT_SMILE


def test_calibrate_end_to_end():
    from sim_config import SimRunConfig
    from sim_calibrate import calibrate
    bars = _make_bars(n_days=8)
    model = calibrate(bars, SimRunConfig(strategy_name="Main"))
    assert model.garch.converged or model.warnings
    assert model.ushape.shape == (78,)
    assert model.sigma0 > 0 and model.vix0 > 0
    assert model.source == "csv"
    ann = model.sigma_annual(SimRunConfig(strategy_name="Main"))
    assert 0.02 < ann < 5.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_sim_calibrate.py -q`
Expected: FAIL — `ImportError: cannot import name 'SmileParams'`

- [ ] **Step 3: Implement (append to `sim_calibrate.py`)**

```python
# ---------- smile, U-shape, VIX mapping, full pipeline ----------
import json
import os
from dataclasses import dataclass, field

from sim_config import SimRunConfig
from sim_data import BarSeries

SMILE_CAPTURE_PATH = os.path.join("config", "sim_smile.json")
SMILE_DEFAULT_PATH = os.path.join("config", "sim_smile_default.json")
RTH_START_MIN = 570


@dataclass
class SmileParams:
    a: float
    b: float
    c: float
    half_spread_atm: float

    def iv(self, m):
        m = np.asarray(m, dtype=float)
        return self.a + self.b * m + self.c * m * m

    def to_dict(self) -> dict:
        return {"a": self.a, "b": self.b, "c": self.c, "half_spread_atm": self.half_spread_atm}

    @classmethod
    def from_dict(cls, d: dict) -> "SmileParams":
        return cls(a=float(d["a"]), b=float(d["b"]), c=float(d["c"]),
                   half_spread_atm=float(d.get("half_spread_atm", 0.05)))


DEFAULT_SMILE = SmileParams(a=0.20, b=-0.35, c=1.20, half_spread_atm=0.05)


@dataclass
class CalibratedModel:
    garch: GarchParams
    ushape: np.ndarray            # (steps_per_day,), mean ~ 1
    sigma0: float                 # mean per-bar conditional vol (decimal return units)
    smile: SmileParams
    vix0: float
    source: str
    warnings: List[str] = field(default_factory=list)

    def sigma_annual(self, cfg: SimRunConfig) -> float:
        return float(self.sigma0 * np.sqrt(cfg.steps_per_day() * 252))


def fit_ushape(returns: np.ndarray, minute_of_day: np.ndarray, steps_per_day: int) -> np.ndarray:
    """Multiplicative per-bar vol profile from mean |return| by minute-of-day, smoothed."""
    idx = np.clip((minute_of_day - RTH_START_MIN - 1).astype(int), 0, steps_per_day - 1)
    sums = np.zeros(steps_per_day)
    counts = np.zeros(steps_per_day)
    np.add.at(sums, idx, np.abs(returns))
    np.add.at(counts, idx, 1.0)
    mean_abs = np.where(counts > 0, sums / np.maximum(counts, 1), np.nan)
    # fill empty buckets from neighbours, then normalize and smooth (15-bar moving mean)
    n = len(mean_abs)
    fill = np.where(np.isfinite(mean_abs), mean_abs, np.nanmean(mean_abs))
    fill = np.where(np.isfinite(fill), fill, 1.0)
    kernel = np.ones(15) / 15.0
    smooth = np.convolve(fill, kernel, mode="same")
    out = smooth / max(float(np.mean(smooth)), 1e-12)
    return np.clip(out, 0.25, 4.0)


def fit_smile(m_points, iv_points, fallback: SmileParams) -> Tuple[SmileParams, List[str]]:
    """Least-squares quadratic IV(m); falls back when too few points or degenerate fit."""
    m = np.asarray(m_points, dtype=float)
    iv = np.asarray(iv_points, dtype=float)
    ok = np.isfinite(m) & np.isfinite(iv) & (iv > 0.005) & (iv < 5.0)
    m, iv = m[ok], iv[ok]
    if len(m) < 5:
        return fallback, ["smile: too few IV points — using fallback snapshot"]
    A = np.vstack([np.ones_like(m), m, m * m]).T
    coef, *_ = np.linalg.lstsq(A, iv, rcond=None)
    if not np.isfinite(coef).all() or abs(coef[0]) > 5:
        return fallback, ["smile: degenerate fit — using fallback snapshot"]
    return SmileParams(a=float(coef[0]), b=float(coef[1]), c=float(coef[2]),
                       half_spread_atm=fallback.half_spread_atm), []


def load_smile_snapshot() -> Tuple[SmileParams, str]:
    """captured (config/sim_smile.json) -> default file -> built-in constants."""
    for path, src in ((SMILE_CAPTURE_PATH, "captured"), (SMILE_DEFAULT_PATH, "default")):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return SmileParams.from_dict(json.load(f)), src
        except Exception:
            continue
    return DEFAULT_SMILE, "builtin"


def save_smile_snapshot(smile: SmileParams) -> None:
    os.makedirs(os.path.dirname(SMILE_CAPTURE_PATH), exist_ok=True)
    with open(SMILE_CAPTURE_PATH, "w", encoding="utf-8") as f:
        json.dump(smile.to_dict(), f, indent=2)


def calibrate(bars: BarSeries, cfg: SimRunConfig) -> CalibratedModel:
    """Full calibration: returns -> GJR-t MLE -> U-shape -> smile snapshot -> VIX mapping."""
    warnings: List[str] = list(bars.warnings)
    closes = bars.closes
    rets = np.diff(np.log(closes))
    mods = bars.minute_of_day[1:]
    garch, w = fit_gjr_t(rets)
    warnings += w
    ushape = fit_ushape(rets, mods, cfg.steps_per_day())
    sigma0 = float(np.mean(np.sqrt(gjr_variance_path(garch, rets))))
    smile, smile_src = load_smile_snapshot()
    if smile_src != "captured":
        warnings.append(f"smile: {smile_src} snapshot (capture a live chain for best results)")
    vix0 = 20.0
    if bars.vix_closes is not None and len(bars.vix_closes):
        vix0 = float(np.mean(bars.vix_closes[-20:]))
    else:
        warnings.append("VIX series unavailable — mapping anchored at VIX0=20")
    return CalibratedModel(garch=garch, ushape=ushape, sigma0=sigma0, smile=smile,
                           vix0=vix0, source=bars.source, warnings=warnings)
```

- [ ] **Step 4: Create the bundled default snapshot**

`config/sim_smile_default.json`:

```json
{
  "a": 0.20,
  "b": -0.35,
  "c": 1.2,
  "half_spread_atm": 0.05
}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_sim_calibrate.py -q`
Expected: PASS (8 tests total — 3 from Task 5 + 5 new)

- [ ] **Step 6: Commit**

```bash
git add sim_calibrate.py tests/test_sim_calibrate.py config/sim_smile_default.json
git commit -m "feat(sim): U-shape profile, quadratic smile fit, VIX mapping, snapshot persistence"
```

---

### Task 7: `sim_paths.py` — chunked path generation

**Files:**
- Create: `sim_paths.py`
- Test: `tests/test_sim_paths.py`

**Interfaces:**
- Consumes: `CalibratedModel` (Task 6), `SimRunConfig` (Task 2).
- Produces:
  - `@dataclass SimPaths: spots: np.ndarray  # (n, steps); sigmas: np.ndarray  # (n, steps)`
  - `simulate_chunk(model, cfg, s0, n_paths, seed_seq: np.random.SeedSequence) -> SimPaths`
    Deterministic: same `seed_seq` ⇒ identical output. Applies `nu_override` and `gamma_mult`
    from `cfg` (stress dials).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_sim_paths.py
import numpy as np

from sim_calibrate import CalibratedModel, DEFAULT_SMILE, GarchParams
from sim_config import SimRunConfig
from sim_paths import simulate_chunk


def _model(nu=6.0, gamma=0.10):
    return CalibratedModel(
        garch=GarchParams(omega=2e-10, alpha=0.05, gamma=gamma, beta=0.85, nu=nu, converged=True),
        ushape=np.interp(np.arange(78), [0, 6, 39, 72, 77], [2.0, 0.7, 0.6, 1.6, 2.4]),
        sigma0=0.0005, smile=DEFAULT_SMILE, vix0=15.0, source="test")


def test_reproducible_and_shape():
    cfg = SimRunConfig(strategy_name="Main", bar_size="5m")
    m = _model()
    a = simulate_chunk(m, cfg, 6000.0, 50, np.random.SeedSequence([42, 0, 0]))
    b = simulate_chunk(m, cfg, 6000.0, 50, np.random.SeedSequence([42, 0, 0]))
    c = simulate_chunk(m, cfg, 6000.0, 50, np.random.SeedSequence([42, 0, 1]))
    assert a.spots.shape == (50, 78) and a.sigmas.shape == (50, 78)
    assert np.array_equal(a.spots, b.spots)      # same seed -> identical
    assert not np.array_equal(a.spots, c.spots)  # different chunk -> different


def test_vol_link_and_ushape():
    cfg = SimRunConfig(strategy_name="Main")
    m = _model()
    sp = simulate_chunk(m, cfg, 6000.0, 4000, np.random.SeedSequence([1, 0, 0]))
    rets = np.diff(np.log(sp.spots), axis=1)                     # (n, steps-1)
    realized_bar_vol = rets.std(axis=0)                          # per step
    assert realized_bar_vol[0] > realized_bar_vol[38]            # open busier than midday
    assert realized_bar_vol[-8:].mean() > realized_bar_vol[38]   # close busier than midday
    # sigmas recorded for the smile link track the U-shape
    assert abs(sp.sigmas.mean() / (0.0005 * m.ushape.mean()) - 1.0) < 0.5


def test_stress_nu_3_fatter_tails():
    cfg = SimRunConfig(strategy_name="Main", nu_override=3.0)
    sp = simulate_chunk(_model(), cfg, 6000.0, 8000, np.random.SeedSequence([5, 0, 0]))
    rets = np.diff(np.log(sp.spots), axis=1).ravel()
    kurt = float(((rets - rets.mean()) ** 4).mean() / rets.var() ** 2)
    cfg6 = SimRunConfig(strategy_name="Main")
    sp6 = simulate_chunk(_model(), cfg6, 6000.0, 8000, np.random.SeedSequence([5, 0, 0]))
    rets6 = np.diff(np.log(sp6.spots), axis=1).ravel()
    kurt6 = float(((rets6 - rets6.mean()) ** 4).mean() / rets6.var() ** 2)
    assert kurt > kurt6                   # nu=3 heavier tails than nu=6
    assert (np.abs(sp.spots / 6000.0 - 1) < 0.5).all()   # guard rail: within ±50%
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_sim_paths.py -q`
Expected: FAIL — `No module named 'sim_paths'`

- [ ] **Step 3: Write the implementation**

```python
"""Chunked intraday path generation: GJR-GARCH recursion + standardized-t + U-shape."""
from dataclasses import dataclass

import numpy as np

from sim_calibrate import CalibratedModel
from sim_config import SimRunConfig


@dataclass
class SimPaths:
    spots: np.ndarray    # (n, steps): close of bar t
    sigmas: np.ndarray   # (n, steps): per-bar conditional vol used for that bar


def simulate_chunk(model: CalibratedModel, cfg: SimRunConfig, s0: float,
                   n_paths: int, seed_seq: np.random.SeedSequence) -> SimPaths:
    steps = cfg.steps_per_day()
    p = model.garch
    nu = float(cfg.nu_override) if cfg.nu_override else p.nu
    gamma = p.gamma * cfg.gamma_mult
    rng = np.random.default_rng(seed_seq)
    z = rng.standard_t(nu, size=(n_paths, steps))
    z /= np.sqrt(nu / (nu - 2.0))                     # standardized: E[z^2] = 1
    ushape = model.ushape

    spots = np.empty((n_paths, steps))
    sig_out = np.empty((n_paths, steps))
    s = np.full(n_paths, float(s0))
    sigma2 = np.full(n_paths, max(p.omega / max(1e-12, (1 - p.alpha - gamma / 2 - p.beta)), 1e-16))
    for t in range(steps):
        sig_t = np.sqrt(sigma2) * ushape[t]
        eps = sig_t * z[:, t]
        s = s * np.exp(eps)
        spots[:, t] = s
        sig_out[:, t] = sig_t
        neg = eps < 0
        sigma2 = (p.omega + p.alpha * eps ** 2 + gamma * eps ** 2 * neg + p.beta * sigma2)
        sigma2 = np.maximum(sigma2, 1e-16)            # variance floor (spec §9)
    spots = np.clip(spots, s0 * 0.5, s0 * 1.5)        # spot guard band (spec §9)
    return SimPaths(spots=spots, sigmas=sig_out)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_sim_paths.py -q`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add sim_paths.py tests/test_sim_paths.py
git commit -m "feat(sim): chunked GJR-t path generation with U-shape and stress dials"
```

---

### Task 8: `sim_pricing.py` — BSM, smile IV, spreads, tick rules

**Files:**
- Create: `sim_pricing.py`
- Test: `tests/test_sim_pricing.py`

**Interfaces:**
- Consumes: `SmileParams` (Task 6).
- Produces:
  - `RISK_FREE_RATE = 0.043` (mirrors `config.DEFAULT_RISK_FREE_RATE` fallback; kept local so the sim never imports live config state)
  - `bar_year_frac(bar_seconds) -> float`
  - `bsm_put(S, K, T, r, sigma) -> np.ndarray` — broadcastable; `T=0 → intrinsic`; σ floored at 1e-4
  - `bsm_put_delta(S, K, T, r, sigma) -> np.ndarray`
  - `smile_iv(m, smile, sigma_col, sigma0, vol_beta, flat_iv, iv_atm) -> np.ndarray` — broadcasts `m (…,)` against `sigma_col (n,1)`; clip [0.01, 5.0]
  - `half_spread(m, hs_atm) -> np.ndarray` — `clip(hs_atm·(1+8·|m|), 0.01, 2.0)`
  - `build_ladder(s0, range_pct, step=5.0) -> np.ndarray`
  - `tick_floor(x, tick=0.05) -> np.ndarray` (works on scalars/arrays)
  - `combo_fill_credit(mid_credit, cons_credit, tick) -> float`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_sim_pricing.py
import math

import numpy as np

from gex_calculator import _bsm_delta
from sim_pricing import (bsm_put, bsm_put_delta, bar_year_frac, build_ladder,
                         combo_fill_credit, half_spread, smile_iv, tick_floor)
from sim_calibrate import SmileParams


def test_bsm_put_put_call_parity():
    S, K, T, r, sig = np.array([6000.0]), np.array([5900.0]), 1 / 252, 0.043, 0.25
    put = bsm_put(S, K, T, r, sig)
    # call via parity: C = P + S - K*e^{-rT}
    call = put + S - K * math.exp(-r * T)
    intrinsic = max(0.0, (K - S)[0])
    assert put[0] > intrinsic
    assert abs(float(put[0] - 250.0)) < 250.0        # sane magnitude
    assert (call > 0).all()


def test_bsm_put_expiry_is_intrinsic():
    S = np.array([6000.0, 5850.0])
    K = np.array([5900.0, 5900.0])
    put = bsm_put(S, K, 0.0, 0.043, 0.25)
    np.testing.assert_allclose(put, [0.0, 50.0])


def test_put_delta_matches_gex_calculator():
    # parity with the live dashboard's BSM delta implementation
    S, K, T, r, sig = 6000.0, 5800.0, 2 / (252 * 390), 0.043, 0.30
    mine = float(bsm_put_delta(np.array([S]), np.array([K]), T, r, np.array([sig]))[0])
    theirs = _bsm_delta(S, K, T, r, sig, "P")
    assert abs(mine - theirs) < 1e-9


def test_smile_iv_vol_link_and_flat():
    smile = SmileParams(a=0.20, b=-0.35, c=1.2, half_spread_atm=0.05)
    m = np.array([-0.03, 0.0])
    iv = smile_iv(m, smile, sigma_col=np.full((2, 1), 0.0006), sigma0=0.0005,
                  vol_beta=1.0, flat_iv=False, iv_atm=0.2)
    assert iv.shape == (2, 2)
    assert iv[0, 0] < iv[0, 1]                # higher path vol -> higher IV
    assert iv[0, 0] > iv[1, 0]                # OTM put (m<0) richer than ATM
    flat = smile_iv(m, smile, np.full((2, 1), 0.0009), 0.0005, 1.0, flat_iv=True, iv_atm=0.2)
    assert np.allclose(flat, 0.2)


def test_half_spread_widens_otm():
    hs = half_spread(np.array([0.0, -0.05]), 0.05)
    assert hs[0] == 0.05 and hs[1] > 0.05


def test_tick_floor_and_combo_fill():
    assert tick_floor(0.27) == 0.25
    assert tick_floor(0.23) == 0.20
    assert tick_floor(0.25) == 0.25           # exact multiple stays
    np.testing.assert_allclose(tick_floor(np.array([0.27, 0.23])), [0.25, 0.20])
    assert combo_fill_credit(0.27, 0.24, 0.05) == 0.25   # min(tick-floor mid, natural)
    assert combo_fill_credit(0.23, 0.20, 0.05) == 0.20
    assert combo_fill_credit(0.07, 0.02, 0.05) == 0.05   # floored at one tick


def test_build_ladder():
    K = build_ladder(6000.0, 0.15)
    assert K[0] == 5100.0 and K[-1] == 6900.0
    assert (np.diff(K) == 5.0).all() and 6000.0 in K
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_sim_pricing.py -q`
Expected: FAIL — `No module named 'sim_pricing'`

- [ ] **Step 3: Write the implementation**

```python
"""Vectorized option pricing for the simulated market.

BSM family (mirrors gex_calculator's math, vectorized via scipy.special.ndtr),
quadratic smile with a vol-level link, synthetic bid/ask, and the repo's fill
tick rules. Pure functions — no state.
"""
import numpy as np
from scipy.special import ndtr

RISK_FREE_RATE = 0.043   # mirrors config.DEFAULT_RISK_FREE_RATE fallback


def bar_year_frac(bar_seconds: int) -> float:
    """Year fraction of one bar: 252 RTH days x 6.5 h."""
    return bar_seconds / (252 * 6.5 * 3600.0)


def bsm_put(S, K, T, r, sigma):
    S = np.asarray(S, dtype=float)
    K = np.asarray(K, dtype=float)
    sigma = np.maximum(np.asarray(sigma, dtype=float), 1e-4)
    T = np.asarray(T, dtype=float)
    if T.ndim == 0 and float(T) <= 0.0:
        return np.maximum(K - S, 0.0)
    sqrtT = np.sqrt(T)
    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * sqrtT)
    d2 = d1 - sigma * sqrtT
    return K * np.exp(-r * T) * ndtr(-d2) - S * ndtr(-d1)


def bsm_put_delta(S, K, T, r, sigma):
    sigma = np.maximum(np.asarray(sigma, dtype=float), 1e-4)
    T = np.asarray(T, dtype=float)
    if T.ndim == 0 and float(T) <= 0.0:
        return np.where(K > S, -1.0, 0.0)
    sqrtT = np.sqrt(T)
    d1 = (np.log(np.asarray(S) / np.asarray(K)) + (r + 0.5 * sigma ** 2) * T) / (sigma * sqrtT)
    return ndtr(d1) - 1.0


def smile_iv(m, smile, sigma_col, sigma0, vol_beta, flat_iv, iv_atm):
    """IV matrix from log-moneyness m (broadcastable) + vol-level link lam*(sigma - sigma0)."""
    if flat_iv:
        return np.full(np.broadcast_shapes(np.shape(m), np.shape(sigma_col)), float(iv_atm))
    base = smile.iv(m)
    linked = base + vol_beta * (np.asarray(sigma_col) - sigma0)
    return np.clip(linked, 0.01, 5.0)


def half_spread(m, hs_atm):
    return np.clip(hs_atm * (1.0 + 8.0 * np.abs(m)), 0.01, 2.0)


def build_ladder(s0: float, range_pct: float, step: float = 5.0) -> np.ndarray:
    lo = int(np.floor(s0 * (1 - range_pct) / step) * step)
    hi = int(np.ceil(s0 * (1 + range_pct) / step) * step)
    return np.arange(lo, hi + step / 2, step, dtype=float)


def tick_floor(x, tick: float = 0.05):
    a = np.asarray(x, dtype=float)
    out = np.floor((a + 1e-9) / tick) * tick
    return float(out) if out.ndim == 0 else out


def combo_fill_credit(mid_credit: float, cons_credit: float, tick: float) -> float:
    """Entry fill: never better than the natural, rounded down to the tick grid.

    Spec: fill = min(tick-floor(mid), conservative side), floored at one tick.
    """
    return float(max(tick, min(tick_floor(mid_credit, tick), cons_credit)))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_sim_pricing.py -q`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add sim_pricing.py tests/test_sim_pricing.py
git commit -m "feat(sim): vectorized BSM, vol-linked smile, spreads, tick fill rules"
```

---

### Task 9: `sim_engine.py` — entry scan

**Files:**
- Create: `sim_engine.py`
- Test: `tests/test_sim_engine.py`

**Interfaces:**
- Consumes: `SimPaths` (Task 7), pricing fns (Task 8), `CalibratedModel` (Task 6), `Strategy`/`Condition`/`ExitRules`/`StopLoss` from `strategy_models` (existing).
- Produces:
  - `@dataclass EntryState: entered, entry_minute (int32, -1), short_idx (int32, -1), long_idx (int32, -1), width (float64), qty (int32), fill_credit, theo_credit — all np.ndarray shape (n,)`
  - `window_minutes(strategy, steps, bar_seconds) -> tuple[int, int]` — bar-index window (defaults 09:30→15:30 like the live engine)
  - `extract_conditions(strategy) -> dict` — `{"dmin","dmax","wmin","wmax","cmin","cmax","vix":dict|None,"trend":Condition|None}` with the same unset-bound defaults as `strategy_engine._lo/_hi`
  - `run_entry(model, cfg, strategy, paths, ladder, per_path_start=None, k=None) -> EntryState`
    (`per_path_start`: scalar or `(n,)` array of earliest allowed minutes — used by family mode;
    `k` not None → dynamic_k experiment mode)
  - `vix_map(model, sigma_col) -> np.ndarray` — `clip(vix0·σ/σ0, 5, 100)`

Design notes for the implementer:
- Pricing is computed **per minute inside the loop** (never a full path×step×strike tensor) — memory
  stays at `(n, len(ladder))` per iteration.
- Candidate selection masks and ranks on **credit mid** (live semantics); the fill uses
  `combo_fill_credit(mid, S_bid − L_ask, tick)`.
- `dynamic_k` mode: entry at the first window minute for all eligible paths; short strike =
  ladder-nearest strike at or below `S_entry·(1 − k·σ_annual)`; long = short − `cfg.width_points`;
  only the `entry_window` condition applies (documented experiment semantics, spec §5.3).
- Calendar gates (`run_days`, FOMC, NFP) are day-of-week/calendar concepts — every simulated trial is
  an eligible day by construction (spec §1 scope); noted in the result meta instead.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_sim_engine.py
import numpy as np

from sim_calibrate import CalibratedModel, DEFAULT_SMILE, GarchParams
from sim_config import SimRunConfig
from sim_engine import EntryState, extract_conditions, run_entry, window_minutes
from sim_paths import SimPaths
from strategy_models import Condition, ExitRules, StopLoss, Strategy


def _strategy(window=("09:35", "10:00")):
    return Strategy(
        name="T", direction="bull_put",
        conditions=[
            Condition(kind="short_delta", params={"min": 0.30, "max": 0.45}),
            Condition(kind="spread_width", params={"min": 40, "max": 65}),
            Condition(kind="credit", params={"min": 0.30, "max": 0.45}),
            Condition(kind="entry_window", params={"start": window[0], "end": window[1]}),
        ],
        exit_rules=ExitRules(stop_loss=StopLoss(multiplier=6.0)), budget=None)


def _model():
    return CalibratedModel(
        garch=GarchParams(omega=2e-10, alpha=0.05, gamma=0.10, beta=0.85, nu=6.0, converged=True),
        ushape=np.ones(78), sigma0=0.0005, smile=DEFAULT_SMILE, vix0=15.0, source="test")


def _paths(spot=6000.0, n=30, steps=78):
    rng = np.random.default_rng(0)
    spots = np.full((n, steps), spot) + rng.normal(0, 2.0, (n, steps)).cumsum(axis=1) * 0.1
    sigmas = np.full((n, steps), 0.0005)
    return SimPaths(spots=spots, sigmas=sigmas)


def test_window_minutes_maps_bars():
    cfg = SimRunConfig(strategy_name="T", bar_size="5m")
    assert window_minutes(_strategy(), 78, 300) == (0, 5)   # 09:35..10:00 closes = bars 0..5


def test_extract_conditions_defaults():
    d = extract_conditions(_strategy())
    assert d["dmin"] == 0.30 and d["dmax"] == 0.45
    assert d["wmin"] == 40 and d["wmax"] == 65
    assert d["cmin"] == 0.30 and d["cmax"] == float("inf")   # credit has only min in JSON
    assert d["vix"] is None and d["trend"] is None


def test_run_entry_enters_in_window_with_valid_geometry():
    cfg = SimRunConfig(strategy_name="T", bar_size="5m")
    strat, model, paths = _strategy(), _model(), _paths()
    ladder = np.arange(5100.0, 6900.0 + 2.5, 5.0)
    es = run_entry(model, cfg, strat, paths, ladder)
    assert es.entered.shape == (30,)
    assert es.entered.sum() > 0
    ent = es.entered.astype(bool)
    assert (es.entry_minute[ent] >= 0).all() and (es.entry_minute[ent] <= 5).all()
    s_idx, l_idx = es.short_idx[ent], es.long_idx[ent]
    spot_at_entry = paths.spots[np.arange(30), es.entry_minute][ent]
    assert (ladder[s_idx] < spot_at_entry).all()              # shorts below spot (puts)
    widths = (s_idx - l_idx) * 5.0
    assert ((widths >= 40) & (widths <= 65)).all()
    assert ((es.theo_credit[ent] >= 0.30)).all()
    assert (es.fill_credit[ent] <= es.theo_credit[ent] + 1e-9).all()
    assert (es.fill_credit[ent] > 0).all()
    assert (es.qty[ent] == 1).all()                            # budget None -> 1


def test_run_entry_dynamic_k_mode():
    cfg = SimRunConfig(strategy_name="T", bar_size="5m", strike_mode="dynamic_k",
                       dynamic_k_values=[0.5], width_points=50.0)
    strat, model, paths = _strategy(), _model(), _paths()
    ladder = np.arange(5100.0, 6900.0 + 2.5, 5.0)
    es = run_entry(model, cfg, strat, paths, ladder, k=0.5)
    ent = es.entered.astype(bool)
    assert ent.all()                                            # dynamic mode: window is the only gate
    assert (es.entry_minute == 0).all()                         # first window minute
    assert ((es.short_idx - es.long_idx) * 5.0 == 50.0).all()


def test_run_entry_respects_per_path_start():
    cfg = SimRunConfig(strategy_name="T", bar_size="5m")
    strat, model, paths = _strategy(), _model(), _paths()
    ladder = np.arange(5100.0, 6900.0 + 2.5, 5.0)
    start = np.full(30, 4)                                      # family-style late start
    es = run_entry(model, cfg, strat, paths, ladder, per_path_start=start)
    ent = es.entered.astype(bool)
    assert ent.sum() > 0 and (es.entry_minute[ent] >= 4).all()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_sim_engine.py -q`
Expected: FAIL — `No module named 'sim_engine'`

- [ ] **Step 3: Write the implementation (part 1 of `sim_engine.py`)**

```python
"""Strategy simulation over synthetic paths: entry scan, exit scans, family re-entry.

Pricing is computed per minute inside the loops (no path x step x strike tensors).
Candidate masking/ranking mirrors strategy_engine.generate_candidates: conditions on
credit MID, best-first by mid; the FILL is the spec's combo rule (never better than
the natural, tick-floored). Entry window defaults and unset-bound semantics match
strategy_engine._lo/_hi.
"""
from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np

from sim_calibrate import CalibratedModel
from sim_config import SimRunConfig
from sim_pricing import (RISK_FREE_RATE, bar_year_frac, bsm_put, bsm_put_delta,
                         build_ladder, combo_fill_credit, half_spread, smile_iv, tick_floor)
from strategy_models import Condition, Strategy

RTH_START_MIN = 570          # 09:30
DEFAULT_WINDOW = (0, 360)    # 09:30 -> 15:30 bar-index window (live engine default)


@dataclass
class EntryState:
    entered: np.ndarray
    entry_minute: np.ndarray
    short_idx: np.ndarray
    long_idx: np.ndarray
    width: np.ndarray
    qty: np.ndarray
    fill_credit: np.ndarray
    theo_credit: np.ndarray


def _num(params: dict, key: str) -> Optional[float]:
    v = params.get(key)
    if v is None or v == "":
        return None
    return float(v)


def extract_conditions(strategy: Strategy) -> dict:
    """Same unset-bound semantics as strategy_engine._lo/_hi."""
    cond = {c.kind: c for c in strategy.conditions if c.enabled}
    out = dict(dmin=0.05, dmax=0.35, wmin=5.0, wmax=50.0, cmin=0.0, cmax=float("inf"),
               vix=None, trend=None)
    if "short_delta" in cond:
        p = cond["short_delta"].params
        out["dmin"] = _num(p, "min") if _num(p, "min") is not None else 0.05
        out["dmax"] = _num(p, "max") if _num(p, "max") is not None else 0.35
    if "spread_width" in cond:
        p = cond["spread_width"].params
        out["wmin"] = _num(p, "min") if _num(p, "min") is not None else 5.0
        out["wmax"] = _num(p, "max") if _num(p, "max") is not None else 50.0
    if "credit" in cond:
        p = cond["credit"].params
        out["cmin"] = _num(p, "min") if _num(p, "min") is not None else 0.0
        out["cmax"] = _num(p, "max") if _num(p, "max") is not None else float("inf")
    if "volatility" in cond and cond["volatility"].params.get("vix_enabled"):
        out["vix"] = cond["volatility"].params
    out["trend"] = cond.get("trend")
    return out


def window_minutes(strategy: Strategy, steps: int, bar_seconds: int) -> Tuple[int, int]:
    """Entry window as bar-index range [w0, w1] inclusive."""
    bar_min = bar_seconds // 60
    cond = {c.kind: c for c in strategy.conditions if c.enabled}
    if "entry_window" not in cond:
        return DEFAULT_WINDOW
    p = cond["entry_window"].params

    def to_idx(hm: str) -> int:
        h, m = hm.split(":")
        minutes = int(h) * 60 + int(m)
        return int((minutes - RTH_START_MIN) // bar_min) - 1   # bar that CLOSES at/after hm

    w0 = max(to_idx(p.get("start", "09:30")), 0)
    w1 = min(to_idx(p.get("end", "15:30")), steps - 1)
    return w0, max(w0, w1)


def vix_map(model: CalibratedModel, sigma_col: np.ndarray) -> np.ndarray:
    """Spec §5.4: VIX_t = clip(vix0 * sigma/sigma0, 5, 100)."""
    return np.clip(model.vix0 * sigma_col / model.sigma0, 5.0, 100.0)


def _qty_for(budget, margin: np.ndarray) -> np.ndarray:
    if budget is None:
        return np.ones(len(margin), dtype=np.int32)
    return np.maximum(np.floor(float(budget) / np.maximum(margin, 1.0)), 0).astype(np.int32)


def run_entry(model: CalibratedModel, cfg: SimRunConfig, strategy: Strategy,
              paths, ladder: np.ndarray, per_path_start=None, k: Optional[float] = None) -> EntryState:
    n, steps = paths.spots.shape
    bar_secs = BAR_SECONDS_GET(cfg)
    w0, w1 = window_minutes(strategy, steps, bar_secs)
    starts = np.broadcast_to(
        np.asarray(per_path_start if per_path_start is not None else 0, dtype=int), (n,)).copy()
    cond = extract_conditions(strategy)
    T_left = np.arange(steps - 1, -1, -1) * bar_year_frac(bar_secs)   # years to expiry after bar t
    step = float(ladder[1] - ladder[0]) if len(ladder) > 1 else 5.0

    entered = np.zeros(n, dtype=bool)
    entry_minute = np.full(n, -1, dtype=np.int32)
    short_idx = np.full(n, -1, dtype=np.int32)
    long_idx = np.full(n, -1, dtype=np.int32)
    width = np.zeros(n)
    qty = np.zeros(n, dtype=np.int32)
    fill = np.zeros(n)
    theo = np.zeros(n)

    if k is not None:
        # ---- dynamic_k experiment: window-only gate, first window minute ----
        t = int(max(w0, int(starts.min())))
        if t >= steps:
            return EntryState(entered, entry_minute, short_idx, long_idx, width, qty, fill, theo)
        active = ~entered & (starts <= t)
        if not active.any():
            return EntryState(entered, entry_minute, short_idx, long_idx, width, qty, fill, theo)
        sigma_ann = model.sigma_annual(cfg)
        s_entry = paths.spots[:, t]
        target = s_entry * (1.0 - k * sigma_ann)
        s_idx = np.clip(((target - ladder[0]) / step).astype(int), 0, len(ladder) - 1)
        # ensure we never round UP past the target (short below target)
        s_idx = np.where(ladder[s_idx] > target, np.maximum(s_idx - 1, 0), s_idx)
        w_pts = float(cfg.width_points)
        l_idx = np.clip(s_idx - int(round(w_pts / step)), 0, len(ladder) - 1)
        ok = active & (s_idx > l_idx)
        iv_t = smile_iv((ladder - s_entry[:, None]) / s_entry[:, None], model.smile,
                        paths.sigmas[:, t:t + 1], model.sigma0, cfg.vol_beta, cfg.flat_iv,
                        float(model.smile.iv(0.0)))
        put = bsm_put(paths.spots[:, t:t + 1], ladder[None, :], T_left[t], RISK_FREE_RATE, iv_t)
        hs = half_spread((ladder - s_entry[:, None]) / s_entry[:, None], model.smile.half_spread_atm)
        rows = np.arange(n)
        cm = put[rows, s_idx] - put[rows, l_idx]              # mid
        cc = (put[rows, s_idx] - hs[rows, s_idx]) - (put[rows, l_idx] + hs[rows, l_idx])
        margin = (s_idx - l_idx) * step * 100.0
        q = _qty_for(strategy.budget, np.where(ok, margin, 1.0))
        ok &= q > 0
        entered, entry_minute = ok, np.where(ok, t, -1).astype(np.int32)
        short_idx, long_idx = np.where(ok, s_idx, -1).astype(np.int32), np.where(ok, l_idx, -1).astype(np.int32)
        width, qty = np.where(ok, (s_idx - l_idx) * step, 0.0), np.where(ok, q, 0).astype(np.int32)
        theo = np.where(ok, cm, 0.0)
        fill = np.where(ok, [combo_fill_credit(a, b, cfg.tick_size) for a, b in zip(cm, cc)], 0.0)
        return EntryState(entered, entry_minute, short_idx, long_idx, width, qty, fill, theo)

    # ---- engine mode: per-minute scan over the window, same semantics as generate_candidates ----
    widths_all = np.arange(int(np.ceil(cond["wmin"] / step)) * step, cond["wmax"] + step / 2, step)
    trend = cond["trend"]
    if trend is not None:
        pmove, rsi = _trend_series(paths, cond, w0, w1)   # see binding note below
    best_cm = np.full(n, -np.inf)
    best_cc = np.zeros(n)
    best_s = np.full(n, -1, dtype=np.int32)
    best_w = np.zeros(n)
    for t in range(int(max(w0, int(starts.min()))), w1 + 1):
        todo = (~entered) & (starts <= t)
        if not todo.any():
            continue
        m = (ladder - paths.spots[:, t:t + 1]) / paths.spots[:, t:t + 1]   # (n, M) log-moneyness
        iv_t = smile_iv(m, model.smile, paths.sigmas[:, t:t + 1], model.sigma0,
                        cfg.vol_beta, cfg.flat_iv, float(model.smile.iv(0.0)))
        put = bsm_put(paths.spots[:, t:t + 1], ladder[None, :], T_left[t], RISK_FREE_RATE, iv_t)
        hs = half_spread(m, model.smile.half_spread_atm)
        bid, ask = put - hs, put + hs
        delta = bsm_put_delta(paths.spots[:, t:t + 1], ladder[None, :], T_left[t], RISK_FREE_RATE, iv_t)
        adelta = np.abs(delta)
        short_ok = (adelta >= cond["dmin"]) & (adelta <= cond["dmax"])
        if cond["vix"] is not None:
            v = vix_map(model, paths.sigmas[:, t])
            from strategy_engine import _passes_bucket, _bucket_params   # same bucket semantics
            op, lo, hi = _bucket_params(cond["vix"], "vix")
            vok = _passes_bucket(v, op, lo, hi)
        else:
            vok = np.ones(n, dtype=bool)
        cand = todo & vok
        if not cand.any():
            continue
        for w in widths_all:
            off = int(round(w / step))
            if off <= 0 or off >= len(ladder):
                continue
            cm = put[:, :-off] - put[:, off:]                  # mid credit for (i, i+off)
            cc = bid[:, :-off] - ask[:, off:]
            ok = (cand[:, None] & short_ok[:, :-off]
                  & (cm >= cond["cmin"]) & (cm <= cond["cmax"]))
            if not ok.any():
                continue
            rows, cols = np.nonzero(ok)
            vals = cm[rows, cols]
            order = np.argsort(vals)                           # ascending; later wins ties? use >:
            for r, c, v in zip(rows[order], cols[order], vals[order]):
                if v > best_cm[r]:
                    best_cm[r] = v
                    best_cc[r] = cc[r, c]
                    best_s[r] = c
                    best_w[r] = w
        # commit candidates found at this minute (first valid minute wins)
        newly = todo & np.isfinite(best_cm) & (best_cm > -np.inf)
        newly &= ~entered
        if newly.any():
            r = np.nonzero(newly)[0]
            entered[r] = True
            entry_minute[r] = t
            short_idx[r] = best_s[r]
            long_idx[r] = best_s[r] - int(round(best_w[r] / step))
            width[r] = best_w[r]
            qty[r] = _qty_for(strategy.budget, np.array([best_w[r] * 100.0]))[0]
            fill[r] = combo_fill_credit(float(best_cm[r]), float(best_cc[r]), cfg.tick_size)
            theo[r] = float(best_cm[r])
            best_cm[r] = -np.inf
        # (best_* state is reused next minute only for still-unentered paths)
    return EntryState(entered, entry_minute, short_idx, long_idx, width, qty, fill, theo)


def BAR_SECONDS_GET(cfg: SimRunConfig) -> int:
    from sim_config import BAR_SECONDS
    return BAR_SECONDS[cfg.bar_size]
```

Implementation notes (binding):
- The inner `for r, c, v in zip(...)` commit loop is per-(path, candidate) Python but only over valid
  candidates within one minute; typical counts are small (tens). If profiling shows it hot, vectorize
  with `np.lexsort` — semantics must stay "max mid wins per path per minute".
- `trend` condition (no current JSON strategy uses it, but it must never be silently ignored): compute
  the indicator value per path/minute and AND it into `cand`. Concrete implementation — before the
  minute loop, when `trend is not None`:

```python
    if trend is not None:
        from condition_helpers import wilder_rsi
        closes = paths.spots                                  # (n, steps)
        # pmove: % change vs `minutes` bars back, evaluated per window minute
        pm_n = int(trend.params.get("minutes", 5))
        pmove = np.full((n, steps), np.nan)
        if steps > pm_n:
            pmove[:, pm_n:] = (closes[:, pm_n:] / closes[:, :-pm_n] - 1.0) * 100.0
        # rsi(14): Wilder RSI per path per window minute (window minutes only — cheap)
        rsi = np.full((n, steps), np.nan)
        period = int(trend.params.get("period", 14))
        for t2 in range(w0, w1 + 1):
            if t2 < period:
                continue
            win = closes[:, t2 - period:t2 + 1]               # (n, period+1)
            diffs = np.diff(win, axis=1)
            gains = np.where(diffs > 0, diffs, 0.0)
            losses = np.where(diffs < 0, -diffs, 0.0)
            ag, al = gains.mean(axis=1), losses.mean(axis=1)  # Wilder seeding is the
            rsi[:, t2] = np.where(al == 0, 100.0, 100.0 - 100.0 / (1.0 + ag / np.maximum(al, 1e-12)))
```

  and inside the minute loop, after `vok`:

```python
        if trend is not None:
            p = trend.params
            from strategy_engine import _passes_bucket, _bucket_params
            if p.get("pmove_enabled"):
                op, lo, hi = _bucket_params(p, "pmove")
                vok &= _passes_bucket(pmove[:, t], op, lo, hi)
            if p.get("rsi_enabled"):
                op, lo, hi = _bucket_params(p, "rsi")
                vok &= _passes_bucket(rsi[:, t], op, lo, hi)
```

  (Bucket semantics via `strategy_engine._bucket_params`/`_passes_bucket` — the same code the live
  engine uses for volatility buckets, so `trend` behaves identically. The unused
  `from condition_helpers import wilder_rsi` line in the main listing is then dropped.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_sim_engine.py -q`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add sim_engine.py tests/test_sim_engine.py
git commit -m "feat(sim): vectorized entry scan mirroring generate_candidates semantics"
```

---

### Task 10: `sim_engine.py` — exit scans and `run_cell`

**Files:**
- Modify: `sim_engine.py` (append)
- Test: `tests/test_sim_engine.py` (append)

**Interfaces:**
- Consumes: `EntryState`, `run_entry` (Task 9), pricing fns (Task 8).
- Produces:
  - `@dataclass TrialResult: entered: bool; entry_minute: int; exit_minute: int; exit_reason: str; short_strike: float; long_strike: float; width: float; qty: int; fill_credit: float; exit_debit: float; pnl: float; mtm: Optional[np.ndarray]` — `exit_reason ∈ {"never","expired","stop","take_profit"}`; `mtm` = per-minute $ MTM of the open position (NaN before entry / after exit).
  - `run_exits(model, cfg, strategy, paths, ladder, entry: EntryState, sl_multiplier=None) -> List[TrialResult]` — `sl_multiplier=None` uses the strategy's own `stop_loss.multiplier`; `inf` = hold to expiry.
  - `run_cell(model, cfg, strategy, paths, ladder, sl_multiplier=None, k=None) -> List[TrialResult]`

Exit semantics (spec §5.3, binding):
- Stop: first bar with mark ≥ |fill_credit| × multiplier → debit = trigger + `cfg.stop_extra` **exactly** (market-order rule; no gap modeling).
- TP (`pct_credit` mode only): first bar with mark ≤ tp.value × fill_credit → debit = that price.
- Final bar: settle intrinsic `(max(Ks−S,0) − max(Kl−S,0))` × 100 at settle, reason `expired`.
- Both stop and TP cross on the same bar → stop wins (conservative).

- [ ] **Step 1: Write the failing test (append)**

```python
from sim_engine import EntryState, run_cell, run_exits


def _entry_state(paths, short_i, long_i, fill=0.30, entry_min=0):
    n = paths.spots.shape[0]
    return EntryState(
        entered=np.ones(n, dtype=bool),
        entry_minute=np.full(n, entry_min, dtype=np.int32),
        short_idx=np.full(n, short_i, dtype=np.int32),
        long_idx=np.full(n, long_i, dtype=np.int32),
        width=np.full(n, (short_i - long_i) * 5.0),
        qty=np.ones(n, dtype=np.int32),
        fill_credit=np.full(n, fill),
        theo_credit=np.full(n, fill))


def test_stop_fill_is_trigger_plus_extra():
    cfg = SimRunConfig(strategy_name="T", bar_size="5m", stop_extra=0.10)
    strat, model = _strategy(), _model()
    paths = _paths(n=2)
    paths.spots[:, :4] = 6000.0
    paths.spots[:, 4:] = 5000.0                       # crash between bar 3 and 4
    ladder = np.arange(5100.0, 6900.0 + 2.5, 5.0)
    short_i = int(np.searchsorted(ladder, 5900.0))    # 5900 short
    long_i = int(np.searchsorted(ladder, 5850.0))     # 5850 long (50-wide)
    entry = _entry_state(paths, short_i, long_i, fill=0.30)
    res = run_exits(model, cfg, strat, paths, ladder, entry, sl_multiplier=6.0)
    assert all(r.exit_reason == "stop" for r in res)
    assert all(r.exit_minute == 4 for r in res)       # first bar past the trigger
    assert abs(res[0].exit_debit - (0.30 * 6.0 + 0.10)) < 1e-9   # trigger + 0.10 exactly
    assert abs(res[0].pnl - (0.30 - 1.90) * 1 * 100) < 1e-9


def test_expiry_settles_intrinsic():
    cfg = SimRunConfig(strategy_name="T", bar_size="5m")
    strat, model = _strategy(), _model()
    paths = _paths(n=2)
    paths.spots[:, :] = 6000.0                        # never approaches the strikes
    ladder = np.arange(5100.0, 6900.0 + 2.5, 5.0)
    entry = _entry_state(paths, int(np.searchsorted(ladder, 5900.0)),
                         int(np.searchsorted(ladder, 5850.0)), fill=0.30)
    res = run_exits(model, cfg, strat, paths, ladder, entry, sl_multiplier=float("inf"))
    assert all(r.exit_reason == "expired" for r in res)
    assert all(r.exit_minute == 77 for r in res)
    assert res[0].exit_debit == 0.0                   # OTM at settle
    assert abs(res[0].pnl - 30.0) < 1e-9              # keep the full credit


def test_take_profit_fills_at_limit():
    strat = _strategy()
    strat.exit_rules.take_profit = __import__("strategy_models").TakeProfit(mode="pct_credit", value=0.5)
    cfg = SimRunConfig(strategy_name="T", bar_size="5m")
    model = _model()
    paths = _paths(n=2)
    paths.spots[:, :4] = 6000.0
    paths.spots[:, 4:] = 6080.0                       # spread mark decays toward 0
    ladder = np.arange(5100.0, 6900.0 + 2.5, 5.0)
    entry = _entry_state(paths, int(np.searchsorted(ladder, 5900.0)),
                         int(np.searchsorted(ladder, 5850.0)), fill=0.30)
    res = run_exits(model, cfg, strat, paths, ladder, entry, sl_multiplier=float("inf"))
    assert res[0].exit_reason == "take_profit"
    assert abs(res[0].exit_debit - 0.15) < 1e-9       # 50% of credit
    assert (np.isnan(res[0].mtm[res[0].exit_minute + 1:])).all()   # inactive after exit


def test_never_entered_paths_report_never():
    cfg = SimRunConfig(strategy_name="T", bar_size="5m")
    strat, model = _strategy(), _model()
    ladder = np.arange(5100.0, 6900.0 + 2.5, 5.0)
    n = 4
    entry = EntryState(entered=np.zeros(n, dtype=bool),
                       entry_minute=np.full(n, -1, dtype=np.int32),
                       short_idx=np.full(n, -1, dtype=np.int32),
                       long_idx=np.full(n, -1, dtype=np.int32),
                       width=np.zeros(n), qty=np.zeros(n, dtype=np.int32),
                       fill_credit=np.zeros(n), theo_credit=np.zeros(n))
    res = run_exits(model, cfg, strat, _paths(n=n), ladder, entry, sl_multiplier=6.0)
    assert all(r.exit_reason == "never" and r.pnl == 0.0 and not r.entered for r in res)


def test_run_cell_end_to_end_small():
    cfg = SimRunConfig(strategy_name="T", bar_size="5m")
    strat, model = _strategy(), _model()
    paths = _paths(n=8)
    ladder = np.arange(5100.0, 6900.0 + 2.5, 5.0)
    res = run_cell(model, cfg, strat, paths, ladder, sl_multiplier=6.0)
    assert len(res) == 8
    reasons = {r.exit_reason for r in res}
    assert reasons <= {"never", "expired", "stop", "take_profit"}
    for r in res:
        if r.entered:
            assert r.mtm is not None and np.isnan(r.mtm[: r.entry_minute]).all()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_sim_engine.py -q`
Expected: FAIL — `ImportError: cannot import name 'run_exits'`

- [ ] **Step 3: Implement (append to `sim_engine.py`)**

```python
@dataclass
class TrialResult:
    entered: bool
    entry_minute: int
    exit_minute: int
    exit_reason: str
    short_strike: float
    long_strike: float
    width: float
    qty: int
    fill_credit: float
    exit_debit: float
    pnl: float
    mtm: Optional[np.ndarray] = None


def _spread_rows(model, cfg, paths, ladder, t):
    """Put mids + half-spreads for every path at bar t -> (mid, bid, ask) each (n, M)."""
    m = (ladder - paths.spots[:, t:t + 1]) / paths.spots[:, t:t + 1]
    iv_t = smile_iv(m, model.smile, paths.sigmas[:, t:t + 1], model.sigma0,
                    cfg.vol_beta, cfg.flat_iv, float(model.smile.iv(0.0)))
    put = bsm_put(paths.spots[:, t:t + 1], ladder[None, :], t, RISK_FREE_RATE, iv_t)
    hs = half_spread(m, model.smile.half_spread_atm)
    return put, put - hs, put + hs


def run_exits(model: CalibratedModel, cfg: SimRunConfig, strategy: Strategy,
              paths, ladder: np.ndarray, entry: EntryState,
              sl_multiplier: Optional[float] = None) -> List[TrialResult]:
    n, steps = paths.spots.shape
    bar_secs = BAR_SECONDS_GET(cfg)
    if sl_multiplier is None:
        sl = strategy.exit_rules.stop_loss
        mult = float(sl.multiplier) if sl is not None else float("inf")
    else:
        mult = float(sl_multiplier)
    tp = strategy.exit_rules.take_profit
    tp_pct = float(tp.value) if (tp is not None and tp.mode == "pct_credit") else None

    out: List[TrialResult] = []
    rows = np.arange(n)
    for p in range(n):
        if not entry.entered[p]:
            out.append(TrialResult(entered=False, entry_minute=-1, exit_minute=-1,
                                   exit_reason="never", short_strike=0.0, long_strike=0.0,
                                   width=0.0, qty=0, fill_credit=0.0, exit_debit=0.0, pnl=0.0))
            continue
        si, li = int(entry.short_idx[p]), int(entry.long_idx[p])
        fc = float(entry.fill_credit[p])
        qty = int(entry.qty[p])
        stop_level = fc * mult
        tp_price = fc * tp_pct if tp_pct is not None else None
        t0 = int(entry.entry_minute[p])
        mtm = np.full(steps, np.nan)
        exit_t, exit_reason, exit_debit = -1, "expired", 0.0
        active = True
        for t in range(t0, steps):
            T = (steps - 1 - t) * bar_year_frac(bar_secs)
            m = (ladder - paths.spots[p, t]) / paths.spots[p, t]
            iv_t = smile_iv(m, model.smile, np.array([[paths.sigmas[p, t]]]), model.sigma0,
                            cfg.vol_beta, cfg.flat_iv, float(model.smile.iv(0.0)))[0]
            put = bsm_put(paths.spots[p, t], ladder, T, RISK_FREE_RATE, iv_t)
            hs = half_spread(m, model.smile.half_spread_atm)
            mark = float(put[si] - put[li])                    # mid mark of the spread
            mtm[t] = (fc - mark) * qty * 100.0
            if t == t0:
                continue                                       # no exit on the entry bar
            if stop_level < float("inf") and mark >= stop_level:
                exit_t, exit_reason = t, "stop"
                exit_debit = stop_level + cfg.stop_extra       # trigger + 0.10, exactly
                break
            if tp_price is not None and mark <= tp_price:
                exit_t, exit_reason = t, "take_profit"
                exit_debit = tp_price
                break
            if t == steps - 1:
                Ks, Kl = float(ladder[si]), float(ladder[li])
                exit_debit = max(Ks - paths.spots[p, t], 0.0) - max(Kl - paths.spots[p, t], 0.0)
                exit_t = t
                break
        pnl = (fc - exit_debit) * qty * 100.0
        if exit_reason == "expired" and exit_t == -1:
            exit_t, exit_debit = steps - 1, 0.0                # single-bar edge: expiry, OTM
        out.append(TrialResult(entered=True, entry_minute=t0, exit_minute=exit_t,
                               exit_reason=exit_reason, short_strike=float(ladder[si]),
                               long_strike=float(ladder[li]), width=float(entry.width[p]),
                               qty=qty, fill_credit=fc, exit_debit=exit_debit, pnl=pnl, mtm=mtm))
    return out


def run_cell(model: CalibratedModel, cfg: SimRunConfig, strategy: Strategy,
             paths, ladder: np.ndarray, sl_multiplier: Optional[float] = None,
             k: Optional[float] = None) -> List[TrialResult]:
    entry = run_entry(model, cfg, strategy, paths, ladder, k=k)
    return run_exits(model, cfg, strategy, paths, ladder, entry, sl_multiplier=sl_multiplier)
```

Implementation notes (binding):
- `_spread_rows` exists for reuse by family mode (Task 12) and the reference harness (Task 11);
  `run_exits` itself computes per-path rows inline so inactive paths never price.
- `mtm` uses the **mid** mark (stop compares against mid mark too — consistent with the live engine,
  whose IB stop bracket triggers on the combo's observed price; the +0.10 covers execution drift).

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_sim_engine.py -q`
Expected: PASS (10 tests)

- [ ] **Step 5: Commit**

```bash
git add sim_engine.py tests/test_sim_engine.py
git commit -m "feat(sim): exit scans (stop=trigger+extra, TP, intrinsic settle) and run_cell"
```

---

### Task 11: Reference harness — vectorized engine vs real `strategy_engine`

**Files:**
- Create: `tests/test_sim_reference.py`
- Modify: nothing (this task only tests)

**Interfaces:**
- Consumes: `run_entry` (Task 9), `_spread_rows`-style pricing (Task 10), real
  `strategy_engine.generate_candidates` / `condition_helpers.combo_credit`, `Strategy` fixtures from Task 9.
- Produces: confidence (a test) that the vectorized mask/rank semantics equal the live engine's on
  identical synthetic chains. No production code.

- [ ] **Step 1: Write the test**

```python
# tests/test_sim_reference.py
"""Reference harness: the vectorized entry engine must pick the SAME (short, long)
pair as the real strategy_engine.generate_candidates on identical synthetic chains."""
from types import SimpleNamespace

import numpy as np

from condition_helpers import combo_credit
from sim_calibrate import CalibratedModel, DEFAULT_SMILE, GarchParams
from sim_config import SimRunConfig
from sim_engine import run_entry
from sim_paths import SimPaths
from strategy_engine import generate_candidates
from test_sim_engine import _model, _strategy


def _synthetic_rows(ladder, put_mid, hs, put_delta):
    rows = []
    for i, K in enumerate(ladder):
        rows.append({
            "strike": float(K), "right": "P",
            "put_bid": float(put_mid[i] - hs[i]), "put_ask": float(put_mid[i] + hs[i]),
            "put_delta": float(put_delta[i]),
            "call_bid": None, "call_ask": None, "call_delta": None, "call_iv": None,
        })
    return rows


def test_vectorized_entry_matches_generate_candidates():
    cfg = SimRunConfig(strategy_name="T", bar_size="5m")
    strat, model = _strategy(), _model()
    rng = np.random.default_rng(9)
    n, steps = 12, 78
    spots = np.full((n, steps), 6000.0) + rng.normal(0, 3.0, (n, steps)).cumsum(axis=1) * 0.2
    paths = SimPaths(spots=spots, sigmas=np.full((n, steps), 0.0005))
    ladder = np.arange(5100.0, 6900.0 + 2.5, 5.0)
    es = run_entry(model, cfg, strat, paths, ladder)

    from sim_pricing import bsm_put, bsm_put_delta, bar_year_frac, half_spread
    checked = 0
    for p in range(n):
        if not es.entered[p]:
            continue
        t = int(es.entry_minute[p])
        spot = float(spots[p, t])
        m = (ladder - spot) / spot
        T = (steps - 1 - t) * bar_year_frac(300)
        # same smile as the engine; vol-link term is 0 because path sigma == sigma0 here
        iv = DEFAULT_SMILE.iv(m)
        put_mid = bsm_put(spot, ladder, T, 0.043, iv)
        put_delta = bsm_put_delta(spot, ladder, T, 0.043, iv)
        hs = half_spread(m, model.smile.half_spread_atm)
        rows = _synthetic_rows(ladder, put_mid, hs, put_delta)
        state = SimpleNamespace(chain_quotes_cache={"strikes": rows}, spx_price=spot,
                                vix=20.0, account_summary={"ExcessLiquidity": 1e9},
                                expiration="20991231", trading_class="SPXW")
        cands = generate_candidates(strat, state, max_n=500)
        assert cands, f"path {p} minute {t}: live engine found no candidate"
        best = max(cands, key=lambda c: c.credit_mid)
        assert float(best.short_strike) == float(ladder[es.short_idx[p]])
        assert float(best.long_strike) == float(ladder[es.long_idx[p]])
        assert abs(best.credit_mid - float(es.theo_credit[p])) < 1e-9
        checked += 1
    assert checked >= 5          # the fixture must produce enough entries to be a real test
```

Note: the synthetic rows carry real `put_delta` (the live engine's `generate_candidates` filters on
it) and no IV fields (only `atm_iv` decoration uses those, and its absence is tolerated). The smile
and sigmas match `run_entry`'s exactly, so the two engines price identical chains and must pick
identical pairs.

- [ ] **Step 2: Run test to verify it passes**

Run: `python -m pytest tests/test_sim_reference.py -q`
Expected: PASS against the Task 9/10 code. If it FAILS, the vectorized mask/rank diverges from
`generate_candidates`: fix `run_entry` (Task 9), not this test. A genuine semantic mismatch here is
exactly what this harness exists to catch.

- [ ] **Step 3: Commit**

```bash
git add tests/test_sim_reference.py
git commit -m "test(sim): reference harness pins vectorized entry to strategy_engine semantics"
```

---

### Task 12: `sim_engine.py` — family mode (parent → children re-entry)

**Files:**
- Modify: `sim_engine.py` (append)
- Test: `tests/test_sim_family.py`

**Interfaces:**
- Consumes: `run_entry` / `run_exits` / `TrialResult` (Tasks 9–10), `Strategy`/`TriggerSpec` (existing `strategy_models`).
- Produces:
  - `run_family(model, cfg, root: Strategy, children: List[Strategy], paths, ladder) -> tuple[Dict[str, List[TrialResult]], np.ndarray]`
    — results per strategy name plus `total_pnl (n,)` summing every fill in the family.
  - `trigger_minutes(parent_results: List[TrialResult], child: Strategy, steps) -> np.ndarray`
    — per-path earliest child-entry minute (−1 where the trigger never fired). Trigger semantics
    mirror `strategy_engine`: `parent_exit_reason` fires at the parent's exit minute for matching
    reason; `parent_unrealized_pnl` fires at the first minute parent MTM ≤ −`loss_multiple` × fill_credit × 100;
    `time_of_day` latches at its HH:MM bar index if the parent entered on that path (documented
    approximation of `_latch_time_triggers`). `trigger_logic` "any" (OR) only — current JSON uses "any";
    "all" raises `ValueError` (explicit, not silent).
  - Children always keep their OWN `exit_rules.stop_loss.multiplier` (sweep multipliers apply to the root only).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_sim_family.py
import numpy as np

from sim_calibrate import CalibratedModel, DEFAULT_SMILE, GarchParams
from sim_config import SimRunConfig
from sim_engine import run_family
from sim_paths import SimPaths
from strategy_models import (Condition, ExitRules, StopLoss, Strategy, TriggerSpec)


def _model():
    return CalibratedModel(
        garch=GarchParams(omega=2e-10, alpha=0.05, gamma=0.10, beta=0.85, nu=6.0, converged=True),
        ushape=np.ones(78), sigma0=0.0005, smile=DEFAULT_SMILE, vix0=15.0, source="test")


def _parent():
    return Strategy(name="P", direction="bull_put",
                    conditions=[
                        Condition(kind="short_delta", params={"min": 0.30, "max": 0.45}),
                        Condition(kind="spread_width", params={"min": 40, "max": 65}),
                        Condition(kind="credit", params={"min": 0.30, "max": 0.45}),
                        Condition(kind="entry_window", params={"start": "09:35", "end": "10:00"}),
                    ],
                    exit_rules=ExitRules(stop_loss=StopLoss(multiplier=6.0)), budget=None)


def _child(trigger):
    return Strategy(name="C", direction="bull_put",
                    conditions=[
                        Condition(kind="short_delta", params={"min": 0.05, "max": 0.45}),
                        Condition(kind="spread_width", params={"min": 40, "max": 65}),
                        Condition(kind="credit", params={"min": 0.10}),
                    ],
                    exit_rules=ExitRules(stop_loss=StopLoss(multiplier=6.0)), budget=None,
                    parent_name="P",
                    subsequent_triggers=[trigger])


def _paths(n=6, crash_after=4):
    """First 2 paths crash hard (parent stops out), rest stay quiet."""
    rng = np.random.default_rng(1)
    spots = np.full((n, 78), 6000.0)
    spots[:2, crash_after:] = 5000.0
    spots[2:] += rng.normal(0, 1.0, (n - 2, 78)).cumsum(axis=1) * 0.1
    return SimPaths(spots=spots, sigmas=np.full((n, 78), 0.0005))


def test_family_reenters_after_parent_stop():
    cfg = SimRunConfig(strategy_name="P", bar_size="5m")
    ladder = np.arange(5100.0, 6900.0 + 2.5, 5.0)
    child = _child(TriggerSpec(kind="parent_exit_reason",
                               params={"reason": "stop_loss"}))
    results, total = run_family(_model(), cfg, _parent(), [child], _paths(), ladder)
    parent = results["P"]
    stopped = [p for p, r in enumerate(parent) if r.entered and r.exit_reason == "stop"]
    assert stopped == [0, 1]
    child_res = results["C"]
    for p in stopped:
        c = child_res[p]
        assert c.entered, "child must re-enter after a parent stop"
        assert c.entry_minute > parent[p].exit_minute
        assert c.fill_credit > 0
    for p in range(2, 6):
        assert not child_res[p].entered     # no trigger fired on quiet paths


def test_family_total_pnl_sums_all_legs():
    cfg = SimRunConfig(strategy_name="P", bar_size="5m")
    ladder = np.arange(5100.0, 6900.0 + 2.5, 5.0)
    child = _child(TriggerSpec(kind="parent_exit_reason", params={"reason": "stop_loss"}))
    results, total = run_family(_model(), cfg, _parent(), [child], _paths(), ladder)
    expected = sum(r.pnl for name in results for r in results[name])
    np.testing.assert_allclose(total, expected, atol=1e-6)


def test_family_unrealized_pnl_trigger():
    cfg = SimRunConfig(strategy_name="P", bar_size="5m")
    ladder = np.arange(5100.0, 6900.0 + 2.5, 5.0)
    child = _child(TriggerSpec(kind="parent_unrealized_pnl", params={"loss_multiple": 0.05}))
    results, total = run_family(_model(), cfg, _parent(), [child], _paths(), ladder)
    # tiny loss_multiple: any mark drift triggers; crashed paths must still re-enter
    assert results["C"][0].entered and results["C"][1].entered


def test_family_all_logic_raises():
    cfg = SimRunConfig(strategy_name="P", bar_size="5m")
    ladder = np.arange(5100.0, 6900.0 + 2.5, 5.0)
    child = _child(TriggerSpec(kind="parent_exit_reason", params={"reason": "stop_loss"}))
    child.trigger_logic = "all"
    import pytest
    with pytest.raises(ValueError):
        run_family(_model(), cfg, _parent(), [child], _paths(), ladder)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_sim_family.py -q`
Expected: FAIL — `ImportError: cannot import name 'run_family'`

- [ ] **Step 3: Implement (append to `sim_engine.py`)**

```python
def _parse_hhmm_to_bar(hm: str, bar_seconds: int) -> int:
    h, m = hm.split(":")
    return max(0, int((int(h) * 60 + int(m) - RTH_START_MIN) // (bar_seconds // 60)) - 1)


def trigger_minutes(parent_results: List[TrialResult], child: Strategy,
                    steps: int, bar_seconds: int = 300) -> np.ndarray:
    """Per-path earliest child entry minute (-1 = never). Mirrors strategy_engine triggers."""
    if child.trigger_logic != "any":
        raise ValueError(f"trigger_logic '{child.trigger_logic}' unsupported (use 'any')")
    n = len(parent_results)
    fired = np.full(n, -1, dtype=np.int32)
    for trig in child.subsequent_triggers:
        if not trig.enabled:
            continue
        for p, res in enumerate(parent_results):
            if not res.entered:
                continue
            if trig.kind == "parent_exit_reason":
                reason = trig.params.get("reason", "")
                if res.exit_reason == reason and res.exit_minute >= 0:
                    cand = res.exit_minute + 1
                    if fired[p] < 0 or cand < fired[p]:
                        fired[p] = cand
            elif trig.kind == "parent_unrealized_pnl":
                mult = float(trig.params.get("loss_multiple", 1.0))
                threshold = -mult * res.fill_credit * 100.0
                mtm = res.mtm
                if mtm is None:
                    continue
                active = np.nonzero(~np.isnan(mtm[: res.exit_minute if res.exit_minute > 0 else steps]))[0]
                for t in active:
                    if t > res.entry_minute and mtm[t] <= threshold:
                        if fired[p] < 0 or t + 1 < fired[p]:
                            fired[p] = t + 1
                        break
            elif trig.kind == "time_of_day":
                latch = _parse_hhmm_to_bar(trig.params.get("time", "12:00"), bar_seconds)
                if res.entry_minute <= latch:
                    if fired[p] < 0 or latch + 1 < fired[p]:
                        fired[p] = latch + 1
    return fired


def run_family(model: CalibratedModel, cfg: SimRunConfig, root: Strategy,
               children: List[Strategy], paths, ladder: np.ndarray):
    """Root with its sweep multiplier; children re-enter per their own triggers/rules."""
    root_results = run_cell(model, cfg, root, paths, ladder)     # sweep applies to root only
    results = {root.name: root_results}
    total = np.array([r.pnl for r in root_results])
    bar_secs = BAR_SECONDS_GET(cfg)
    for child in children:
        if child.parent_name != root.name:
            continue
        starts = trigger_minutes(root_results, child, paths.spots.shape[1], bar_secs)
        eligible = starts >= 0
        starts_c = np.where(eligible, starts, paths.spots.shape[1])   # never-eligible -> out of range
        child_results = run_exits(
            model, cfg, child, paths, ladder,
            run_entry(model, cfg, child, paths, ladder, per_path_start=starts_c),
            sl_multiplier=None)                                        # child keeps its own stop
        results[child.name] = child_results
        total = total + np.array([r.pnl if r.entered else 0.0 for r in child_results])
    return results, total
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_sim_family.py tests/test_sim_engine.py -q`
Expected: PASS (4 new + 10 prior)

- [ ] **Step 5: Commit**

```bash
git add sim_engine.py tests/test_sim_family.py
git commit -m "feat(sim): family mode - parent stop/pnl/time triggers drive child re-entry"
```

---

### Task 13: `sim_risk.py` — metrics and cell payloads

**Files:**
- Create: `sim_risk.py`
- Test: `tests/test_sim_risk.py`

**Interfaces:**
- Consumes: `TrialResult` (Task 10), `SimRunConfig` (Task 2).
- Produces:
  - `summarize(pnls: np.ndarray) -> dict` — keys `mean, median, std, win_rate, cvar5, cvar1, worst_day` ($; win rate over **entered** paths; CVaR = mean of the worst 5%/1% tail; `worst_day` = min).
  - `breakdown(results: List[TrialResult]) -> dict` — counts per `exit_reason`.
  - `intraday_max_dd(mtm: np.ndarray) -> float` — NaN-aware max peak-to-trough of $ MTM (≥ 0; ignores NaN gaps).
  - `fan_quantiles(mtms: List[np.ndarray]) -> dict` — `{minutes, q05, q25, q50, q75, q95}` (position MTM cross-path quantiles per minute; NaN-ignoring).
  - `histogram(pnls, bins=40) -> dict` — `{edges, counts}`.
  - `bootstrap_ruin(pnls, equity, threshold_pct, n_seqs, seq_len, seed) -> dict` — `{max_dd (array), mean_max_dd, p95_max_dd, ruin_prob}`; resample day PnLs with replacement into `n_seqs × seq_len` equity curves.
  - `build_cell_payload(results, cfg) -> dict` — one sweep cell's full result dict (stats + breakdown + hist + dd + fan + ruin_prob + `sl_multiplier` + `k`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_sim_risk.py
import numpy as np

from sim_engine import TrialResult
from sim_risk import (bootstrap_ruin, breakdown, build_cell_payload, histogram,
                      intraday_max_dd, summarize)


def test_summarize_matches_hand_computed():
    pnls = np.array([100.0, 100.0, -200.0, 50.0, -1000.0, 0.0])
    s = summarize(pnls)
    assert abs(s["mean"] - (-158.3333333)) < 1e-6
    assert abs(s["median"] - 25.0) < 1e-9             # sorted middle pair: (0 + 50) / 2
    assert abs(s["win_rate"] - 0.5) < 1e-9            # >0: 100,100,50
    assert s["worst_day"] == -1000.0
    assert s["cvar5"] <= s["mean"]                    # tail no better than mean


def test_breakdown_counts():
    def r(reason, entered=True):
        return TrialResult(entered=entered, entry_minute=0, exit_minute=1, exit_reason=reason,
                           short_strike=0, long_strike=0, width=50, qty=1,
                           fill_credit=0.3, exit_debit=0, pnl=0)
    b = breakdown([r("expired"), r("stop"), r("stop"), r("never", entered=False)])
    assert b == {"expired": 1, "stop": 2, "take_profit": 0, "never": 1}


def test_intraday_max_dd_nan_aware():
    mtm = np.array([np.nan, 100.0, 50.0, 120.0, -80.0, np.nan, np.nan])
    assert intraday_max_dd(mtm) == 200.0              # peak 120 -> trough -80
    assert intraday_max_dd(np.array([np.nan, np.nan])) == 0.0


def test_bootstrap_ruin_matches_simulated():
    rng = np.random.default_rng(0)
    pnls = np.where(rng.random(5000) < 0.01, -50000.0, 30.0)      # 1% catastrophic days
    out = bootstrap_ruin(pnls, equity=100_000.0, threshold_pct=0.20,
                         n_seqs=400, seq_len=60, seed=7)
    assert 0.0 <= out["ruin_prob"] <= 1.0
    # analytic: P(>=1 catastrophic day in 60) = 1 - 0.99^60 ~= 0.453, and only that
    # day can push DD past the 20k threshold (wins accrue just ~1.8k over 60 days)
    assert 0.3 < out["ruin_prob"] < 0.6
    assert out["p95_max_dd"] >= out["mean_max_dd"]


def test_build_cell_payload_shape():
    def r(reason, pnl):
        return TrialResult(entered=reason != "never", entry_minute=0, exit_minute=1,
                           exit_reason=reason, short_strike=5900, long_strike=5850,
                           width=50, qty=1, fill_credit=0.3, exit_debit=0, pnl=pnl,
                           mtm=np.array([np.nan, 0.0, pnl]))
    payload = build_cell_payload([r("expired", 30.0)] * 10 + [r("stop", -180.0)] * 5
                                 + [r("never", 0.0)] * 5,
                                 __import__("sim_config").SimRunConfig(strategy_name="T"))
    assert payload["sl_multiplier"] is None and payload["k"] is None
    assert payload["stats"]["n"] == 20 and payload["stats"]["entered"] == 15
    assert payload["breakdown"]["stop"] == 5
    assert len(payload["hist"]["edges"]) >= 2
    assert payload["fan"]["q50"][1] == 0.0            # minute 1: every entered path marks 0
    assert 0.0 <= payload["ruin_prob"] <= 1.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_sim_risk.py -q`
Expected: FAIL — `No module named 'sim_risk'`

- [ ] **Step 3: Write the implementation**

```python
"""Risk analytics for simulation results: distribution stats, DD, ruin probability."""
from typing import List

import numpy as np

from sim_config import SimRunConfig
from sim_engine import TrialResult


def summarize(pnls: np.ndarray) -> dict:
    p = np.asarray(pnls, dtype=float)
    p = p[np.isfinite(p)]
    if len(p) == 0:
        return dict(mean=0.0, median=0.0, std=0.0, win_rate=0.0, cvar5=0.0, cvar1=0.0,
                    worst_day=0.0, n=0, entered=0)
    tail5 = p[p <= np.quantile(p, 0.05)]
    tail1 = p[p <= np.quantile(p, 0.01)]
    return dict(
        mean=float(p.mean()), median=float(np.median(p)), std=float(p.std()),
        win_rate=float((p > 0).mean()), cvar5=float(tail5.mean()) if len(tail5) else float(p.min()),
        cvar1=float(tail1.mean()) if len(tail1) else float(p.min()),
        worst_day=float(p.min()), n=int(len(p)), entered=int(len(p)))


def breakdown(results: List[TrialResult]) -> dict:
    counts = {"expired": 0, "stop": 0, "take_profit": 0, "never": 0}
    for r in results:
        counts[r.exit_reason] = counts.get(r.exit_reason, 0) + 1
    return counts


def intraday_max_dd(mtm: np.ndarray) -> float:
    x = np.asarray(mtm, dtype=float)
    x = x[~np.isnan(x)]
    if len(x) == 0:
        return 0.0
    peak = np.maximum.accumulate(x)
    return float(np.max(peak - x))


def fan_quantiles(mtms: List[np.ndarray]) -> dict:
    if not mtms:
        return dict(minutes=[], q05=[], q25=[], q50=[], q75=[], q95=[])
    mat = np.vstack(mtms)
    qs = lambda q: np.nanquantile(mat, q, axis=0)     # noqa: E731
    return dict(minutes=list(range(mat.shape[1])), q05=qs(0.05).tolist(), q25=qs(0.25).tolist(),
                q50=qs(0.50).tolist(), q75=qs(0.75).tolist(), q95=qs(0.95).tolist())


def histogram(pnls: np.ndarray, bins: int = 40) -> dict:
    p = np.asarray(pnls, dtype=float)
    p = p[np.isfinite(p)]
    counts, edges = np.histogram(p, bins=bins)
    return dict(edges=edges.tolist(), counts=counts.tolist())


def bootstrap_ruin(pnls: np.ndarray, equity: float, threshold_pct: float,
                   n_seqs: int, seq_len: int, seed: int) -> dict:
    p = np.asarray(pnls, dtype=float)
    p = p[np.isfinite(p)]
    if len(p) == 0:
        return dict(max_dd=np.zeros(0).tolist(), mean_max_dd=0.0, p95_max_dd=0.0, ruin_prob=0.0)
    rng = np.random.default_rng(seed)
    sample = p[rng.integers(0, len(p), size=(n_seqs, seq_len))]
    curves = np.cumsum(sample, axis=1)
    peaks = np.maximum.accumulate(curves, axis=1)
    max_dd = (peaks - curves).max(axis=1)
    threshold = equity * threshold_pct
    return dict(max_dd=max_dd.tolist(), mean_max_dd=float(max_dd.mean()),
                p95_max_dd=float(np.quantile(max_dd, 0.95)),
                ruin_prob=float((max_dd >= threshold).mean()))


def build_cell_payload(results: List[TrialResult], cfg: SimRunConfig,
                       sl_multiplier=None, k=None) -> dict:
    entered = [r for r in results if r.entered]
    pnls = np.array([r.pnl for r in results if r.entered]) if entered else np.zeros(0)
    stats = summarize(pnls)
    stats["n"] = len(results)
    stats["entered"] = len(entered)
    stats["never_entered_pct"] = (len(results) - len(entered)) / max(len(results), 1)
    dds = [intraday_max_dd(r.mtm) for r in entered if r.mtm is not None]
    mtms = [r.mtm for r in entered if r.mtm is not None]
    ruin = bootstrap_ruin(pnls, cfg.equity, cfg.ruin_threshold_pct,
                          cfg.bootstrap_seqs, cfg.bootstrap_len, cfg.seed)
    dd_stats = dict(mean=float(np.mean(dds)) if dds else 0.0,
                    p95=float(np.quantile(dds, 0.95)) if dds else 0.0,
                    worst=float(np.max(dds)) if dds else 0.0)
    return dict(sl_multiplier=("inf" if sl_multiplier == float("inf") else sl_multiplier),
                k=k, stats=stats, breakdown=breakdown(results), hist=histogram(pnls),
                dd=dd_stats, ruin_prob=ruin["ruin_prob"], dd_hist=ruin,
                fan=fan_quantiles(mtms))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_sim_risk.py -q`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add sim_risk.py tests/test_sim_risk.py
git commit -m "feat(sim): risk metrics - CVaR, exit breakdown, intraday DD, bootstrap ruin"
```

---

### Task 14: `sim_jobs.py` + server endpoints + API end-to-end

**Files:**
- Create: `sim_jobs.py`
- Modify: `server.py` (append endpoints; imports at top)
- Create: `tests/test_sim_jobs.py`, `tests/test_sim_api.py`

**Interfaces:**
- Consumes: everything from Tasks 2–13; server's `state` (app_state.AppState) and `ib` for the IB
  data layer + smile capture.
- Produces:
  - `start_run(cfg_dict: dict, state=None, ib=None) -> dict` — `{"job_id": str}`; raises `ValueError`
    (invalid config) / `RuntimeError` (a job is already running).
  - `get_status(job_id) -> dict` — `{"state", "progress", "message"}`; `get_result(job_id) -> dict` (raises `KeyError` for unknown ids); `cancel(job_id) -> bool`; `reset_registry()` (tests only).
  - `execute_pipeline(cfg: SimRunConfig, bars: BarSeries, progress_cb, spot0: float) -> dict` — pure, thread-safe-cancellable between chunks; the full result payload `{"meta": …, "cells": […]}`.
  - Server routes: `POST /api/sim/run` → `{"job_id"}` (400 on `ValueError`, 409 on busy), `GET /api/sim/status/{id}`, `GET /api/sim/result/{id}` (404 unknown), `POST /api/sim/cancel/{id}`, `GET /api/sim/smile`, `POST /api/sim/smile/capture` (409 when no live chain).

- [ ] **Step 1: Write the failing job/pipeline test**

```python
# tests/test_sim_jobs.py
import os

import numpy as np
import pytest

import sim_jobs
from sim_config import SimRunConfig
from sim_data import load_bars

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "sim_bars_5m.csv")


def _cfg(**kw):
    d = dict(strategy_name="T", source="csv", csv_path=FIXTURE, n_paths=40, chunk_size=20,
             equity=100_000.0, bootstrap_seqs=50, bootstrap_len=20)
    d.update(kw)
    return SimRunConfig(**d)


@pytest.fixture(autouse=True)
def _clean():
    sim_jobs.reset_registry()
    yield
    sim_jobs.reset_registry()


def test_execute_pipeline_produces_payload():
    cfg = _cfg()
    bars = load_bars(cfg)
    seen = []
    payload = sim_jobs.execute_pipeline(cfg, bars, lambda p, m: seen.append(p), spot0=6000.0)
    assert seen and seen[-1] == 1.0
    assert payload["meta"]["strategy"] == "T"
    assert payload["meta"]["steps_per_day"] == 78
    assert len(payload["cells"]) == 1
    cell = payload["cells"][0]
    assert cell["stats"]["n"] == 40
    assert set(cell["breakdown"]) == {"expired", "stop", "take_profit", "never"}
    assert np.isfinite(cell["hist"]["edges"]).all()


def test_execute_pipeline_sweep_grid():
    cfg = _cfg(sl_multipliers=[2.0, 6.0, float("inf")], strike_mode="dynamic_k",
               dynamic_k_values=[0.3, 0.6])
    bars = load_bars(cfg)
    payload = sim_jobs.execute_pipeline(cfg, bars, lambda p, m: None, spot0=6000.0)
    cells = payload["cells"]
    assert len(cells) == 6                                    # 3 SL x 2 k
    assert [c["sl_multiplier"] for c in cells] == [2.0, 2.0, 6.0, 6.0, "inf", "inf"]
    assert [c["k"] for c in cells] == [0.3, 0.6, 0.3, 0.6, 0.3, 0.6]


def test_pipeline_honours_cancel():
    cfg = _cfg(sl_multipliers=[2.0, 6.0, float("inf")])
    bars = load_bars(cfg)
    state = {"cancelled": False}

    def progress(p, m):
        state["cancelled"] = p >= 0.34                        # cancel mid-grid

    payload = sim_jobs.execute_pipeline(cfg, bars, progress, spot0=6000.0,
                                        cancel_check=lambda: state["cancelled"])
    assert len(payload["cells"]) == 1                         # stopped after cell 1


def test_start_run_lifecycle_with_stub_engine(monkeypatch):
    monkeypatch.setattr(sim_jobs, "execute_pipeline",
                        lambda cfg, bars, cb, spot0, cancel_check=None: (
                            [cb(i / 4, "x") for i in range(5)],
                            {"meta": {"strategy": cfg.strategy_name}, "cells": []})[1])
    monkeypatch.setattr(sim_jobs, "_load_bars_for", lambda cfg: None)
    job = sim_jobs.start_run({"strategy_name": "T", "source": "csv", "csv_path": FIXTURE,
                              "n_paths": 10})
    status = sim_jobs.get_status(job["job_id"])
    assert status["state"] in ("queued", "loading", "calibrating", "simulating", "done")
    for _ in range(200):                                      # poll to done
        if sim_jobs.get_status(job["job_id"])["state"] == "done":
            break
        import time; time.sleep(0.02)
    assert sim_jobs.get_status(job["job_id"])["state"] == "done"
    assert sim_jobs.get_result(job["job_id"])["meta"]["strategy"] == "T"


def test_start_run_validates_and_blocks_concurrency():
    with pytest.raises(ValueError):
        sim_jobs.start_run({"strategy_name": "", "n_paths": 10})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_sim_jobs.py -q`
Expected: FAIL — `No module named 'sim_jobs'`

- [ ] **Step 3: Write `sim_jobs.py`**

```python
"""Simulation job registry: background execution, progress, cancel, memoized calibration."""
import asyncio
import logging
import threading
import time
import uuid
from typing import Callable, Dict, Optional

import numpy as np

from sim_calibrate import CalibratedModel, calibrate
from sim_config import SimRunConfig, sweep_cells
from sim_data import BarSeries, load_bars
from sim_engine import run_cell, run_family
from sim_paths import simulate_chunk
from sim_pricing import build_ladder
from sim_risk import build_cell_payload

logger = logging.getLogger(__name__)

_STRATEGY_CACHE: Dict[str, "Strategy"] = {}     # name -> Strategy (filled by _get_strategy)
_BARS_CACHE: Dict[tuple, BarSeries] = {}
_CALIB_CACHE: Dict[tuple, CalibratedModel] = {}

_registry: Dict[str, dict] = {}
_busy: Optional[str] = None
_lock = threading.Lock()


def reset_registry() -> None:
    global _busy
    with _lock:
        _registry.clear()
        _busy = None


def get_status(job_id: str) -> dict:
    job = _registry[job_id]           # KeyError -> 404 at the API layer
    return dict(state=job["state"], progress=job["progress"], message=job["message"])


def get_result(job_id: str) -> dict:
    return _registry[job_id]["result"]


def cancel(job_id: str) -> bool:
    job = _registry.get(job_id)
    if job is None:
        return False
    job["cancelled"] = True
    return True


def start_run(cfg_dict: dict, state=None, ib=None) -> dict:
    global _busy
    cfg = SimRunConfig.from_dict(cfg_dict)
    cfg.validate()
    with _lock:
        if _busy is not None and _registry.get(_busy, {}).get("state") in (
                "queued", "loading", "calibrating", "simulating"):
            raise RuntimeError("a simulation job is already running")
        job_id = uuid.uuid4().hex[:12]
        _busy = job_id
        _registry[job_id] = dict(id=job_id, cfg=cfg, state="queued", progress=0.0,
                                 message="", result=None, cancelled=False,
                                 created=time.time())
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(asyncio.to_thread(_execute, job_id, state, ib))
    except RuntimeError:
        # no running loop (tests / scripts): run inline synchronously
        _execute(job_id, state, ib)
    return {"job_id": job_id}


def _load_bars_for(cfg: SimRunConfig) -> Optional[BarSeries]:
    key = (cfg.source, cfg.csv_path, cfg.bar_size, cfg.lookback_days)
    return _BARS_CACHE.get(key)


def _get_strategy(cfg: SimRunConfig, state=None):
    from strategy_models import Strategy
    if state is not None and cfg.strategy_name in getattr(state, "strategies", {}):
        return state.strategies[cfg.strategy_name]
    if cfg.strategy_name in _STRATEGY_CACHE:
        return _STRATEGY_CACHE[cfg.strategy_name]
    from strategy_store import load_strategies   # same file the live engine uses
    strategies = load_strategies()
    for s in strategies.values():
        _STRATEGY_CACHE[s.name] = s
    if cfg.strategy_name not in _STRATEGY_CACHE:
        raise ValueError(f"unknown strategy: {cfg.strategy_name}")
    return _STRATEGY_CACHE[cfg.strategy_name]


def execute_pipeline(cfg: SimRunConfig, bars: BarSeries, progress_cb: Callable,
                     spot0: float, cancel_check: Optional[Callable[[], bool]] = None,
                     state=None) -> dict:
    """Full pipeline over all sweep cells. Deterministic per (cfg, bars, spot0)."""
    cancelled = False
    if cancel_check is None:
        cancel_check = lambda: False   # noqa: E731
    progress_cb(0.0, "calibrating")
    key = (cfg.source, cfg.csv_path, cfg.bar_size, cfg.lookback_days)
    model = _CALIB_CACHE.get(key)
    if model is None:
        model = calibrate(bars, cfg)
        _CALIB_CACHE[key] = model
    strat = _get_strategy(cfg, state)
    children = []
    if cfg.mode == "family" and state is not None:
        children = [s for s in state.strategies.values() if s.parent_name == strat.name]
    ladder = build_ladder(spot0, cfg.ladder_range_pct)
    cells = sweep_cells(cfg)
    results = []
    n_chunks = (cfg.n_paths + cfg.chunk_size - 1) // cfg.chunk_size
    for ci, cell in enumerate(cells):
        pnls_all, trials_all, mtms_all = [], [], []
        for ch in range(n_chunks):
            if cancel_check():
                cancelled = True
                break
            n_here = min(cfg.chunk_size, cfg.n_paths - ch * cfg.chunk_size)
            seed_seq = np.random.SeedSequence(entropy=cfg.seed, spawn_key=(ci, ch))
            paths = simulate_chunk(model, cfg, spot0, n_here, seed_seq)
            if cfg.mode == "family" and children:
                _, total = run_family(model, cfg, strat, children, paths, ladder)
                from sim_engine import TrialResult
                trials = [TrialResult(entered=True, entry_minute=-1, exit_minute=-1,
                                      exit_reason="expired", short_strike=0, long_strike=0,
                                      width=0, qty=1, fill_credit=0, exit_debit=0,
                                      pnl=float(total[p])) for p in range(n_here)]
            else:
                trials = run_cell(model, cfg, strat, paths, ladder,
                                  sl_multiplier=cell["sl_multiplier"], k=cell["k"])
            pnls_all += [t.pnl for t in trials]
            trials_all += trials
            mtms_all += [t.mtm for t in trials if t.mtm is not None]
            done = (ci * n_chunks + ch + 1) / (len(cells) * n_chunks)
            progress_cb(done, f"cell {ci + 1}/{len(cells)} chunk {ch + 1}/{n_chunks}")
        if cancelled:
            break
        payload = build_cell_payload(trials_all, cfg,
                                     sl_multiplier=cell["sl_multiplier"], k=cell["k"])
        results.append(payload)
    meta = dict(strategy=cfg.strategy_name, mode=cfg.mode, source=bars.source,
                bar_size=cfg.bar_size, steps_per_day=cfg.steps_per_day(),
                n_paths=cfg.n_paths, seed=cfg.seed, spot0=spot0,
                garch=vars(model.garch),
                garch_warnings=model.warnings, data_warnings=bars.warnings,
                smile=vars(model.smile), dials=dict(nu_override=cfg.nu_override,
                                                    gamma_mult=cfg.gamma_mult,
                                                    vol_beta=cfg.vol_beta, flat_iv=cfg.flat_iv),
                cancelled=cancelled)
    progress_cb(1.0, "done" if not cancelled else "cancelled")
    return dict(meta=meta, cells=results)


def _execute(job_id: str, state, ib) -> None:
    global _busy
    job = _registry[job_id]
    cfg: SimRunConfig = job["cfg"]

    def progress(p, msg):
        job["progress"], job["message"] = float(p), msg
        _push_ws(job, p, msg)

    try:
        job["state"] = "loading"
        bars = _load_bars_for(cfg)
        if bars is None:
            # server path resolves IB here on the caller's loop; thread falls back to
            # csv/yfinance only (no event loop in this thread).
            bars = load_bars(cfg)
            _BARS_CACHE[(cfg.source, cfg.csv_path, cfg.bar_size, cfg.lookback_days)] = bars
        spot0 = float(cfg.spot0) if cfg.spot0 else float(bars.closes[-1])
        job["state"] = "simulating"
        result = execute_pipeline(cfg, bars, progress, spot0,
                                  cancel_check=lambda: job["cancelled"], state=state)
        job["result"] = result
        job["state"] = "cancelled" if result["meta"]["cancelled"] else "done"
    except Exception as e:
        logger.exception("sim job failed")
        job["state"], job["message"] = "error", str(e)
    finally:
        with _lock:
            if _busy == job_id:
                _busy = None


def _push_ws(job: dict, progress: float, message: str) -> None:
    """Best-effort WS push; the loop was captured at start_run time when available."""
    loop = job.get("loop")
    broadcast = job.get("broadcast_fn")
    if loop is not None and broadcast is not None:
        try:
            loop.call_soon_threadsafe(
                broadcast, {"type": "sim_progress",
                            "data": {"job_id": job["id"], "progress": progress,
                                     "message": message}})
        except RuntimeError:
            pass
```

In `start_run`, after obtaining `loop`, store it and the broadcast fn:
```python
        job = _registry[job_id]
        job["loop"] = loop
        try:
            from server import broadcast_to_clients   # module-level ws broadcast
            job["broadcast_fn"] = broadcast_to_clients
        except Exception:
            job["broadcast_fn"] = None
```
and add `import numpy as np` at the top of `sim_jobs.py`. If `server.broadcast_to_clients` does not
exist by that name, grep `server.py` for the actual broadcast callable and use it — do not create a
second broadcast mechanism.

- [ ] **Step 4: Run the job tests**

Run: `python -m pytest tests/test_sim_jobs.py -q`
Expected: PASS (5 tests). `test_start_run_lifecycle_with_stub_engine` runs inline (no loop) by design.

- [ ] **Step 5: Write the API end-to-end test**

```python
# tests/test_sim_api.py
"""API-level end-to-end: real app + real engine over HTTP (hermetic fixture data)."""
import os
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient

import server

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "sim_bars_5m.csv")


@pytest.fixture()
def client(monkeypatch):
    import sim_jobs
    sim_jobs.reset_registry()
    # keep the suite hermetic: never let the API path touch yfinance/IB
    monkeypatch.setattr("sim_data.load_bars_yfinance",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disabled in tests")))
    return TestClient(server.app)


def test_full_run_lifecycle_over_http(client):
    body = {"strategy_name": "T", "source": "csv", "csv_path": FIXTURE,
            "n_paths": 30, "chunk_size": 15, "bar_size": "5m",
            "sl_multipliers": [2.0, float("inf")], "equity": 100000}
    r = client.post("/api/sim/run", json=body)
    assert r.status_code == 200
    job_id = r.json()["job_id"]
    deadline = time.time() + 120
    status = None
    while time.time() < deadline:
        status = client.get(f"/api/sim/status/{job_id}").json()
        if status["state"] in ("done", "error", "cancelled"):
            break
        time.sleep(0.05)
    assert status["state"] == "done", status
    result = client.get(f"/api/sim/result/{job_id}").json()
    assert len(result["cells"]) == 2
    for cell in result["cells"]:
        assert cell["stats"]["n"] == 30
        assert all(np.isfinite(cell["hist"]["edges"]))
    assert client.get("/api/sim/status/unknown").status_code == 404


def test_run_rejects_bad_config(client):
    r = client.post("/api/sim/run", json={"strategy_name": "", "n_paths": 10})
    assert r.status_code == 400
    assert "strategy_name" in r.json()["detail"]


def test_run_second_job_while_busy_conflicts(client, monkeypatch):
    """Deterministic: hold the first job open on an Event so the second POST must 409."""
    import threading

    import sim_jobs

    release, started = threading.Event(), threading.Event()

    def slow_pipeline(cfg, bars, cb, spot0, cancel_check=None, state=None):
        started.set()
        release.wait(10)
        return {"meta": {"strategy": cfg.strategy_name}, "cells": []}

    monkeypatch.setattr(sim_jobs, "execute_pipeline", slow_pipeline)
    body = {"strategy_name": "T", "source": "csv", "csv_path": FIXTURE, "n_paths": 10}
    r1 = client.post("/api/sim/run", json=body)
    assert r1.status_code == 200
    assert started.wait(5), "background job never started"
    r2 = client.post("/api/sim/run", json=body)
    assert r2.status_code == 409
    assert client.post(f"/api/sim/cancel/{r1.json()['job_id']}").json()["cancelled"] is True
    release.set()
    deadline = time.time() + 15
    while time.time() < deadline:
        if client.get(f"/api/sim/status/{r1.json()['job_id']}").json()["state"] in ("done", "cancelled"):
            break
        time.sleep(0.05)
    assert client.get(f"/api/sim/status/{r1.json()['job_id']}").json()["state"] in ("done", "cancelled")


def test_smile_endpoint_reports_source(client):
    r = client.get("/api/sim/smile")
    assert r.status_code == 200
    assert r.json()["source"] in ("captured", "default", "builtin")
    assert {"a", "b", "c", "half_spread_atm"} <= set(r.json()["smile"])
```

(The `"T"` strategy does not exist in `config/strategies.json`; add a `_get_strategy` fallback used
only in tests: in `sim_jobs._get_strategy`, when the name is still unknown, check
`_STRATEGY_CACHE` — and `tests/test_sim_api.py` seeds it:
`sim_jobs._STRATEGY_CACHE["T"] = <the Task 9 `_strategy()` fixture>` in the client fixture. Do this
in the test file, not production code.)

- [ ] **Step 6: Add the server endpoints (append to `server.py`; add `import sim_jobs` and
`from fastapi.responses import JSONResponse` to imports)**

```python
# ---------------- Simulation (intraday MC stress test) ----------------

@app.post("/api/sim/run")
async def api_sim_run(body: dict = Body(...)):
    try:
        out = sim_jobs.start_run(body, state=state, ib=ib)
        return out
    except ValueError as e:
        return JSONResponse(status_code=400, content={"detail": str(e)})
    except RuntimeError as e:
        return JSONResponse(status_code=409, content={"detail": str(e)})


@app.get("/api/sim/status/{job_id}")
async def api_sim_status(job_id: str):
    try:
        return sim_jobs.get_status(job_id)
    except KeyError:
        return JSONResponse(status_code=404, content={"detail": "unknown job"})


@app.get("/api/sim/result/{job_id}")
async def api_sim_result(job_id: str):
    try:
        result = sim_jobs.get_result(job_id)
    except KeyError:
        return JSONResponse(status_code=404, content={"detail": "unknown job"})
    if result is None:
        return JSONResponse(status_code=409, content={"detail": "job not finished"})
    return result


@app.post("/api/sim/cancel/{job_id}")
async def api_sim_cancel(job_id: str):
    return {"cancelled": sim_jobs.cancel(job_id)}


@app.get("/api/sim/smile")
async def api_sim_smile():
    from sim_calibrate import load_smile_snapshot
    smile, src = load_smile_snapshot()
    return {"smile": smile.to_dict(), "source": src}


@app.post("/api/sim/smile/capture")
async def api_sim_smile_capture():
    rows = getattr(state, "chain_quotes_cache", {}) or {}
    strikes = rows.get("strikes") or []
    spot = float(getattr(state, "spx_price", 0) or 0)
    pts_m, pts_iv = [], []
    for row in strikes:
        iv = row.get("put_iv")
        if iv and spot and row.get("strike"):
            m = math.log(float(row["strike"]) / spot)
            if abs(m) < 0.15:
                pts_m.append(m)
                pts_iv.append(float(iv) / 100.0)
    if len(pts_m) < 5:
        return JSONResponse(status_code=409, content={"detail": "live chain not available"})
    from sim_calibrate import fit_smile, DEFAULT_SMILE, save_smile_snapshot
    smile, warnings = fit_smile(np.array(pts_m), np.array(pts_iv), DEFAULT_SMILE)
    if warnings:
        return JSONResponse(status_code=409, content={"detail": warnings[0]})
    save_smile_snapshot(smile)
    return {"smile": smile.to_dict(), "source": "captured", "points": len(pts_m)}
```

Notes (binding):
- `state` and `ib` are the module-level names `server.py` already uses (verify with grep before
  editing; use the actual names in scope inside the route functions).
- `math` and `numpy as np` must be imported in `server.py` if not already.
- `from fastapi import Body` may already exist; add if missing.
- Family mode v1 limitation (visible in the result `meta.mode`): per-path family totals are reported
  as synthetic per-trial PnLs (`mtm=None`), so the fan chart and intraday-DD tiles reflect
  single-strategy cells only; family cells still get full distribution stats, the root's exit
  breakdown, and ruin probability. Lifting this = threading child MTMs through `run_family`
  (future work).

- [ ] **Step 7: Run the API tests**

Run: `python -m pytest tests/test_sim_api.py tests/test_sim_jobs.py -q`
Expected: PASS (9 tests)

- [ ] **Step 8: Full-suite check**

Run: `python -m pytest tests/ -q --tb=short`
Expected: all pass except exactly the 2 known pre-existing failures.

- [ ] **Step 9: Commit**

```bash
git add sim_jobs.py server.py tests/test_sim_jobs.py tests/test_sim_api.py
git commit -m "feat(sim): job registry, background execution, /api/sim/* endpoints + API E2E"
```

---

### Task 15: Frontend — Simulation tab

**Files:**
- Modify: `static/index.html` (tab button after Strategies at ~line 84; panel before `logTab`)
- Modify: `static/js/state.js:49` (`VALID_TABS`), `static/js/tabs.js` (`switchTab` + hide list)
- Modify: `ws_handler.py` (`set_tab:sim` branch, next to the existing `set_tab:*` handlers ~line 243-282)
- Create: `static/js/sim-tab.js`, `static/css/sim.css`
- Modify: `static/index.html` (script/link includes near the existing ones)

**Interfaces:**
- Consumes: `/api/sim/*` endpoints (Task 14), global `state.strategies` from the existing WS state push
  (each entry: `{name, direction, parent_name, …}`), Plotly global.
- Produces: `window.SimTab` with `init()` / `onShow()`. Tab id `sim`.

- [ ] **Step 1: Wire the tab (index.html, state.js, tabs.js, ws_handler.py)**

`static/index.html` — after the Strategies button:

```html
<button class="tab-btn" data-tab="sim" onclick="switchTab('sim')">Simulation</button>
```

and before `<div class="tab-panel" id="logTab" ...>`:

```html
<div class="tab-panel" id="simTab" style="display:none;">
  <div class="sim-layout">
    <div class="sim-panel">
      <h3>Simulation run</h3>
      <label>Strategy root
        <select id="simStrategy"></select>
      </label>
      <label>Mode
        <select id="simMode">
          <option value="single">Single strategy</option>
          <option value="family">Family (parent + children)</option>
        </select>
      </label>
      <label>Data source
        <select id="simSource">
          <option value="auto">Auto (CSV → yfinance → IB)</option>
          <option value="csv">CSV</option>
          <option value="yfinance">yfinance</option>
          <option value="ib">IB</option>
        </select>
      </label>
      <label>CSV path <input id="simCsvPath" placeholder="tests/fixtures/sim_bars_5m.csv"></label>
      <label>Bar size
        <select id="simBarSize">
          <option value="5m">5 min</option>
          <option value="1m">1 min</option>
          <option value="30s">30 sec</option>
          <option value="15s">15 sec</option>
          <option value="5s">5 sec</option>
        </select>
      </label>
      <label>Lookback days <input id="simLookback" type="number" value="60" min="5"></label>
      <label>Paths <input id="simPaths" type="number" value="10000" min="10" step="100"></label>
      <label>Seed <input id="simSeed" type="number" value="42"></label>
      <label>Spot (blank = live/last) <input id="simSpot" type="number" step="0.01"></label>
      <label>Account equity <input id="simEquity" type="number" value="100000"></label>
      <label>Ruin threshold % <input id="simRuinPct" type="number" value="20" step="1"></label>
      <label>Stop slippage (pts) <input id="simStopExtra" type="number" value="0.10" step="0.05"></label>
      <h4>Stress dials</h4>
      <label>ν override (blank = fitted) <input id="simNu" type="number" step="0.5" min="2.5"></label>
      <label>γ × <input id="simGammaMult" type="number" value="1.0" step="0.1"></label>
      <label>λ (vol-beta) <input id="simVolBeta" type="number" value="0.75" step="0.05"></label>
      <label class="sim-check"><input id="simFlatIv" type="checkbox"> Flat IV (sanity mode)</label>
      <h4>Experiments</h4>
      <label>SL multipliers (comma-sep, 'inf' = hold) <input id="simSlList" placeholder="2, 4, 6, inf"></label>
      <label>Strike mode
        <select id="simStrikeMode">
          <option value="engine">Engine (JSON conditions)</option>
          <option value="dynamic_k">Dynamic k·σ distance</option>
        </select>
      </label>
      <label>k values (comma-sep) <input id="simKList" placeholder="0.4, 0.6, 0.8"></label>
      <div class="sim-actions">
        <button id="simRun" class="sim-run-btn">Run</button>
        <button id="simCancel" disabled>Cancel</button>
      </div>
      <div class="sim-progress-wrap"><div id="simProgress"></div></div>
      <div id="simStatus" class="sim-status"></div>
      <div id="simDataInfo" class="sim-datainfo"></div>
    </div>
    <div class="sim-results">
      <div id="simTiles" class="sim-tiles"></div>
      <table class="sim-sweep" id="simSweep"></table>
      <div class="sim-chart" id="simHist"></div>
      <div class="sim-chart" id="simFan"></div>
      <div class="sim-chart" id="simDD"></div>
      <div class="sim-actions">
        <button id="simExport" disabled>Export CSV</button>
        <button id="simCaptureSmile">Capture live smile</button>
        <span id="simSmileInfo" class="sim-datainfo"></span>
      </div>
    </div>
  </div>
</div>
```

Include tags next to the other JS/CSS includes in `index.html`:

```html
<link rel="stylesheet" href="/static/css/sim.css">
<script src="/static/js/sim-tab.js"></script>
```

`static/js/state.js:49`:

```javascript
const VALID_TABS = new Set(['dashboard', 'chain', 'account', 'strategies', 'sim', 'log']);
```

`static/js/tabs.js` — add `sim` to the hide-all block (same pattern as `log`) and a branch:

```javascript
const sim = document.getElementById('simTab');
if (sim) { sim.style.display = 'none'; sim.classList.remove('active'); }
...
} else if (tab === 'sim') {
    if (sim) sim.classList.add('active');
    if (window.SimTab) window.SimTab.onShow();
}
```

`ws_handler.py` — alongside the other `set_tab` handlers:

```python
                elif msg == "set_tab:sim":
                    state.active_tab = "sim"
```

- [ ] **Step 2: Write `static/css/sim.css`** (reuse `theme.css` variables; keep the same visual
language as `strategies.css` — panel cards, muted borders)

```css
/* Simulation tab */
.sim-layout { display: flex; gap: 16px; padding: 12px; height: 100%; overflow: auto; }
.sim-panel { min-width: 300px; max-width: 340px; background: var(--panel-bg, #16181d);
  border: 1px solid var(--border, #2a2d34); border-radius: 8px; padding: 14px; }
.sim-panel h3, .sim-panel h4 { margin: 8px 0 6px; }
.sim-panel label { display: block; margin: 6px 0; font-size: 12px; opacity: 0.85; }
.sim-panel input, .sim-panel select { width: 100%; box-sizing: border-box; margin-top: 3px;
  background: var(--input-bg, #0f1115); color: var(--text, #e8e8e8);
  border: 1px solid var(--border, #2a2d34); border-radius: 4px; padding: 5px 7px; font-size: 12px; }
.sim-check { display: flex; align-items: center; gap: 6px; }
.sim-check input { width: auto; }
.sim-actions { display: flex; gap: 8px; margin: 10px 0; }
.sim-actions button { padding: 6px 14px; border-radius: 4px; border: 1px solid var(--border, #2a2d34);
  background: var(--accent-bg, #2456a8); color: #fff; cursor: pointer; }
.sim-actions button:disabled { opacity: 0.4; cursor: default; }
.sim-progress-wrap { height: 6px; background: var(--border, #2a2d34); border-radius: 3px; overflow: hidden; }
#simProgress { height: 100%; width: 0; background: #3f8f5f; transition: width 0.2s; }
.sim-status { font-size: 12px; margin-top: 6px; min-height: 16px; }
.sim-status.error { color: #e06c60; }
.sim-datainfo { font-size: 11px; opacity: 0.7; margin-top: 6px; white-space: pre-line; }
.sim-results { flex: 1; min-width: 0; }
.sim-tiles { display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 10px; }
.sim-tile { background: var(--panel-bg, #16181d); border: 1px solid var(--border, #2a2d34);
  border-radius: 8px; padding: 10px 14px; min-width: 130px; }
.sim-tile .v { font-size: 20px; font-weight: 600; }
.sim-tile .k { font-size: 11px; opacity: 0.65; }
.sim-chart { height: 300px; margin-bottom: 10px; background: var(--panel-bg, #16181d);
  border: 1px solid var(--border, #2a2d34); border-radius: 8px; }
.sim-sweep { width: 100%; border-collapse: collapse; margin-bottom: 12px; font-size: 12px; }
.sim-sweep th, .sim-sweep td { border-bottom: 1px solid var(--border, #2a2d34);
  padding: 5px 8px; text-align: right; }
.sim-sweep th:first-child, .sim-sweep td:first-child { text-align: left; }
.sim-sweep tr.selected { background: rgba(63, 143, 95, 0.15); cursor: pointer; }
```

- [ ] **Step 3: Write `static/js/sim-tab.js`**

```javascript
// Simulation tab: run form, job polling, Plotly rendering, CSV export.
(function () {
    'use strict';
    let currentResult = null;
    let selectedCell = 0;
    let pollTimer = null;
    let activeJobId = null;

    const $ = (id) => document.getElementById(id);

    function num(id, fallback = null) {
        const v = $(id).value.trim();
        if (v === '') return fallback;
        const f = parseFloat(v);
        return isNaN(f) ? fallback : f;
    }

    function list(id) {
        const raw = $(id).value.trim();
        if (!raw) return null;
        return raw.split(',').map(s => {
            s = s.trim();
            if (/^inf$/i.test(s)) return Infinity;
            const f = parseFloat(s);
            return isNaN(f) ? null : f;
        }).filter(v => v !== null);
    }

    function buildConfig() {
        const spot = num('simSpot');
        return {
            strategy_name: $('simStrategy').value,
            mode: $('simMode').value,
            source: $('simSource').value,
            csv_path: $('simCsvPath').value.trim(),
            bar_size: $('simBarSize').value,
            lookback_days: num('simLookback', 60),
            n_paths: num('simPaths', 10000),
            seed: num('simSeed', 42),
            spot0: spot,
            equity: num('simEquity', 100000),
            ruin_threshold_pct: num('simRuinPct', 20) / 100.0,
            stop_extra: num('simStopExtra', 0.10),
            nu_override: num('simNu'),
            gamma_mult: num('simGammaMult', 1.0),
            vol_beta: num('simVolBeta', 0.75),
            flat_iv: $('simFlatIv').checked,
            sl_multipliers: list('simSlList'),
            strike_mode: $('simStrikeMode').value,
            dynamic_k_values: list('simKList'),
        };
    }

    async function run() {
        const cfg = buildConfig();
        $('simStatus').className = 'sim-status';
        $('simStatus').textContent = 'starting…';
        $('simRun').disabled = true;
        $('simCancel').disabled = false;
        try {
            const r = await fetch('/api/sim/run', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(cfg),
            });
            if (!r.ok) {
                const e = await r.json().catch(() => ({}));
                throw new Error(e.detail || `HTTP ${r.status}`);
            }
            const { job_id } = await r.json();
            activeJobId = job_id;
            poll(job_id);
        } catch (err) {
            $('simStatus').className = 'sim-status error';
            $('simStatus').textContent = String(err.message || err);
            $('simRun').disabled = false;
            $('simCancel').disabled = true;
        }
    }

    function poll(jobId) {
        clearInterval(pollTimer);
        pollTimer = setInterval(async () => {
            try {
                const s = await (await fetch(`/api/sim/status/${jobId}`)).json();
                $('simProgress').style.width = `${Math.round((s.progress || 0) * 100)}%`;
                $('simStatus').textContent = `${s.state} — ${s.message || ''}`;
                if (['done', 'error', 'cancelled'].includes(s.state)) {
                    clearInterval(pollTimer);
                    $('simRun').disabled = false;
                    $('simCancel').disabled = true;
                    if (s.state === 'error') {
                        $('simStatus').className = 'sim-status error';
                    } else if (s.state === 'done') {
                        currentResult = await (await fetch(`/api/sim/result/${jobId}`)).json();
                        selectedCell = 0;
                        render();
                    }
                }
            } catch (err) {
                clearInterval(pollTimer);
                $('simRun').disabled = false;
                $('simStatus').textContent = `poll failed: ${err}`;
            }
        }, 500);
    }

    async function cancel() {
        clearInterval(pollTimer);
        $('simRun').disabled = false;
        $('simCancel').disabled = true;
        if (activeJobId) await fetch(`/api/sim/cancel/${activeJobId}`, { method: 'POST' });
    }

    // -------- rendering --------
    const fmt = (v, d = 0) => (v == null || isNaN(v)) ? '—' :
        v.toLocaleString(undefined, { maximumFractionDigits: d, minimumFractionDigits: d });

    function tiles(cell, meta) {
        const s = cell.stats;
        const items = [
            ['Exp PnL / day', `$${fmt(s.mean, 0)}`],
            ['Win rate', `${fmt(s.win_rate * 100, 1)}%`],
            ['CVaR 1%', `$${fmt(s.cvar1, 0)}`],
            ['Worst day', `$${fmt(s.worst_day, 0)}`],
            ['Max DD p95', `$${fmt(cell.dd.p95, 0)}`],
            ['Ruin prob', `${fmt(cell.ruin_prob * 100, 2)}%`],
            ['Never entered', `${fmt(s.never_entered_pct * 100, 1)}%`],
            ['Paths', fmt(s.n)],
        ];
        $('simTiles').innerHTML = items.map(([k, v]) =>
            `<div class="sim-tile"><div class="v">${v}</div><div class="k">${k}</div></div>`).join('');
    }

    function sweepTable() {
        const cells = currentResult.cells;
        const head = '<tr><th>SL ×</th><th>k</th><th>Exp PnL</th><th>Win %</th><th>CVaR1</th>' +
            '<th>DD p95</th><th>Ruin %</th><th>Stopped %</th></tr>';
        const rows = cells.map((c, i) => {
            const stopped = (c.breakdown.stop || 0) / Math.max(c.stats.n, 1) * 100;
            const sl = c.sl_multiplier === 'inf' ? '∞ (hold)' : c.sl_multiplier;
            return `<tr data-i="${i}" class="${i === selectedCell ? 'selected' : ''}">` +
                `<td>${sl}</td><td>${c.k == null ? '—' : c.k}</td>` +
                `<td>$${fmt(c.stats.mean)}</td><td>${fmt(c.stats.win_rate * 100, 1)}</td>` +
                `<td>$${fmt(c.stats.cvar1)}</td><td>$${fmt(c.dd.p95)}</td>` +
                `<td>${fmt(c.ruin_prob * 100, 2)}</td><td>${fmt(stopped, 1)}</td></tr>`;
        }).join('');
        const el = $('simSweep');
        el.innerHTML = head + rows;
        el.querySelectorAll('tr[data-i]').forEach(tr => tr.addEventListener('click', () => {
            selectedCell = parseInt(tr.dataset.i, 10);
            render();
        }));
    }

    function charts(cell) {
        const dark = { paper_bgcolor: 'rgba(0,0,0,0)', plot_bgcolor: 'rgba(0,0,0,0)',
                       font: { color: '#c9cdd4' } };
        // Day-PnL histogram
        Plotly.newPlot('simHist', [{
            type: 'bar', x: cell.hist.edges.slice(1).map((e, i) => (e + cell.hist.edges[i]) / 2),
            y: cell.hist.counts, marker: { color: '#4a89dc' },
        }], Object.assign({ title: 'Day PnL distribution ($)', bargap: 0.02 }, dark),
            { responsive: true, displayModeBar: false });
        // MTM quantile fan
        const f = cell.fan;
        if (f && f.minutes && f.minutes.length) {
            const x = f.minutes;
            Plotly.newPlot('simFan', [
                { x, y: f.q95, mode: 'lines', line: { width: 1, color: '#3f8f5f' } },
                { x, y: f.q75, mode: 'lines', line: { width: 1, color: '#3f8f5f' } },
                { x, y: f.q50, mode: 'lines', line: { width: 2, color: '#e8c15a' } },
                { x, y: f.q25, mode: 'lines', line: { width: 1, color: '#e06c60' } },
                { x, y: f.q05, mode: 'lines', line: { width: 1, color: '#e06c60' },
                  fill: 'tonexty', fillcolor: 'rgba(224,108,96,0.12)' },
            ], Object.assign({ title: 'Spread MTM quantile fan ($)', showlegend: false }, dark),
                { responsive: true, displayModeBar: false });
        }
        // Bootstrap max-DD distribution
        const ddh = cell.dd_hist || {};
        if (ddh.max_dd && ddh.max_dd.length) {
            Plotly.newPlot('simDD', [{
                type: 'histogram', x: ddh.max_dd, marker: { color: '#8a6fc8' }, nbinsx: 40,
            }], Object.assign({ title: `Bootstrap max-DD ($ over ${'60'}d curves) — ruin prob ${fmt(cell.ruin_prob * 100, 2)}%` }, dark),
                { responsive: true, displayModeBar: false });
        }
    }

    function render() {
        if (!currentResult || !currentResult.cells.length) return;
        const cell = currentResult.cells[selectedCell];
        tiles(cell, currentResult.meta);
        sweepTable();
        charts(cell);
        const m = currentResult.meta;
        $('simDataInfo').textContent =
            `source: ${m.source} · ${m.bar_size} · ${m.steps_per_day} bars/day · ` +
            `garch ${m.garch.converged ? 'fitted' : 'PRESET'}\n` +
            [...(m.garch_warnings || []), ...(m.data_warnings || [])].join('\n');
        $('simExport').disabled = false;
    }

    function exportCsv() {
        if (!currentResult) return;
        const rows = [['sl_multiplier', 'k', 'mean', 'median', 'std', 'win_rate', 'cvar5',
            'cvar1', 'worst_day', 'dd_mean', 'dd_p95', 'dd_worst', 'ruin_prob',
            'expired', 'stop', 'take_profit', 'never']];
        currentResult.cells.forEach(c => rows.push([
            c.sl_multiplier, c.k, c.stats.mean, c.stats.median, c.stats.std, c.stats.win_rate,
            c.stats.cvar5, c.stats.cvar1, c.stats.worst_day, c.dd.mean, c.dd.p95, c.dd.worst,
            c.ruin_prob, c.breakdown.expired, c.breakdown.stop, c.breakdown.take_profit,
            c.breakdown.never]));
        const csv = rows.map(r => r.join(',')).join('\n');
        const a = document.createElement('a');
        a.href = URL.createObjectURL(new Blob([csv], { type: 'text/csv' }));
        a.download = 'sim_results.csv';
        a.click();
        URL.revokeObjectURL(a.href);
    }

    function refreshStrategies() {
        const sel = $('simStrategy');
        if (!sel || !Array.isArray(state.strategies)) return;
        const prev = sel.value;
        sel.innerHTML = state.strategies.map(s =>
            `<option value="${s.name}">${s.name}${s.parent_name ? ' (child)' : ''}</option>`).join('');
        if (prev && state.strategies.some(s => s.name === prev)) sel.value = prev;
    }

    async function onShow() {
        refreshStrategies();
        if (!$('simSmileInfo').textContent) {
            try {
                const r = await (await fetch('/api/sim/smile')).json();
                $('simSmileInfo').textContent = `smile: ${r.source}`;
            } catch (e) { /* panel stays blank */ }
        }
    }

    async function captureSmile() {
        const r = await fetch('/api/sim/smile/capture', { method: 'POST' });
        const body = await r.json().catch(() => ({}));
        $('simSmileInfo').textContent = r.ok
            ? `smile: captured (${body.points} pts)` : `capture failed: ${body.detail || r.status}`;
    }

    function init() {
        $('simRun').addEventListener('click', run);
        $('simCancel').addEventListener('click', cancel);
        $('simExport').addEventListener('click', exportCsv);
        $('simCaptureSmile').addEventListener('click', captureSmile);
    }

    document.addEventListener('DOMContentLoaded', init);
    window.SimTab = { init, onShow };
})();
```

(Job identity is kept in the module-level `activeJobId` so Cancel works without polling state.)

- [ ] **Step 4: Manual verification**

Run: `python server.py` then open `http://localhost:8000/#sim`.
Expected: tab renders, strategy dropdown lists the JSON strategies, Run with tiny settings
(paths=60, source csv, fixture path) completes and tiles/charts render; bad configs show errors inline.
This manual step supplements (never replaces) the Task 16 automated UI test.

- [ ] **Step 5: Commit**

```bash
git add static/index.html static/js/state.js static/js/tabs.js static/js/sim-tab.js static/css/sim.css ws_handler.py
git commit -m "feat(sim): Simulation tab UI - run panel, sweep table, Plotly charts, CSV export"
```

---

### Task 16: UI end-to-end (Playwright) + README

**Files:**
- Create: `tests/e2e/__init__.py`, `tests/e2e/test_sim_ui_playwright.py`
- Modify: `README.md` (features bullet, Files table rows, deps note)

**Interfaces:**
- Consumes: the running server (`python server.py`), fixture CSV (Task 3), the Simulation tab (Task 15).
- Produces: `tests/e2e/test_sim_ui_playwright.py` — auto-skips when `playwright` (or its browser) is
  missing; runs in the normal suite otherwise.

- [ ] **Step 1: Write the E2E test**

```python
# tests/e2e/test_sim_ui_playwright.py
"""UI end-to-end: real server + real browser + real engine (small hermetic run)."""
import os
import signal
import socket
import subprocess
import sys
import time

import pytest

pw = pytest.importorskip("playwright.sync_api")

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FIXTURE = os.path.join(ROOT, "tests", "fixtures", "sim_bars_5m.csv")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server_url():
    port = _free_port()
    env = dict(os.environ, SERVER_PORT=str(port), SERVER_HOST="127.0.0.1")
    proc = subprocess.Popen([sys.executable, "server.py"], cwd=ROOT, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 30
    import urllib.request
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"{url}/api/state", timeout=1)
            break
        except Exception:
            time.sleep(0.3)
    else:
        proc.terminate()
        pytest.fail("server did not start within 30s")
    yield url
    proc.send_signal(signal.SIGINT)
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:
        proc.kill()


def test_simulation_tab_runs_and_renders(server_url):
    with pw.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(f"{server_url}/#sim")
        page.click('button[data-tab="sim"]')
        page.select_option("#simStrategy", label=None, index=0)
        page.select_option("#simSource", "csv")
        page.fill("#simCsvPath", FIXTURE)
        page.select_option("#simBarSize", "5m")
        page.fill("#simPaths", "40")
        page.fill("#simSeed", "7")
        page.fill("#simEquity", "100000")
        page.click("#simRun")
        page.wait_for_selector(".sim-tile", timeout=180_000)
        tiles = page.locator(".sim-tile").count()
        assert tiles >= 6
        assert page.locator("#simSweep tr[data-i]").count() >= 1
        assert page.locator("#simHist .main-svg").count() == 1
        page.screenshot(path=os.path.join(ROOT, "docs", "screenshot_sim.png"), full_page=False)
        browser.close()
```

Note: `page.select_option("#simStrategy", label=None, index=0)` selects whichever strategy the
server loaded first — the run config itself is strategy-agnostic for this test (the fixture data is
generic). If `#simStrategy` has no options (strategies WS state not yet pushed at click time),
`refreshStrategies()` in `onShow()` covers it; if it still fails, wait for
`page.wait_for_function("document.querySelectorAll('#simStrategy option').length > 0")` before
clicking Run — add that line.

- [ ] **Step 2: Install dev deps and run it**

```bash
pip install -r requirements-dev.txt
playwright install chromium
python -m pytest tests/e2e -q
```

Expected: PASS (server boots on a free port; the run completes in well under 3 min). Screenshot lands
at `docs/screenshot_sim.png`. If the machine has no browser and cannot install one, the test SKIPS
(`importorskip`) — record that outcome honestly rather than claiming a pass.

- [ ] **Step 3: Update `README.md`**

- **Features list**: add a bullet after the Strategies bullet:
  `- **Simulation tab** — intraday Monte Carlo stress-testing for 0DTE strategies: GJR-GARCH + Student-t paths with U-shape volatility, BSM smile marking, tick-rule fills, family re-entry, SL/strike sweeps and stress dials (ν, γ, λ), PnL/CVaR/max-DD/ruin analytics.`
- **Dashboard Tabs** section: add `- **Simulation** — run intraday MC stress tests, sweep stop-loss multipliers and dynamic strike distances, A/B stress dials, export results to CSV.`
- **Files table**: add rows:

```
| `sim_config.py` | Simulation run config: validation, JSON round-trip, sweep cells |
| `sim_data.py` | Layered intraday bar loaders: CSV → yfinance → IB |
| `sim_calibrate.py` | GJR-GARCH(1,1)-t MLE, U-shape profile, smile snapshot, VIX mapping |
| `sim_paths.py` | Chunked vectorized path generation (stress dials: ν, γ×) |
| `sim_pricing.py` | Vectorized BSM, vol-linked smile, spreads, tick fill rules |
| `sim_engine.py` | Entry/exit scans, single + family simulation, experiment modes |
| `sim_risk.py` | CVaR/exit breakdown/max-DD/bootstrap ruin metrics |
| `sim_jobs.py` | Background job registry, progress, cancel, memoized calibration |
```

- **Dependencies note** under Configuration: `numpy` and `scipy` are new runtime requirements;
  `requirements-dev.txt` adds `pytest-playwright` for the UI E2E tier.

- [ ] **Step 4: Full-suite + commit**

```bash
python -m pytest tests/ -q --tb=short
```
Expected: everything passes except exactly the 2 known pre-existing failures (skip counts may appear
if Playwright is unavailable — record which).

```bash
git add tests/e2e README.md docs/screenshot_sim.png
git commit -m "test(sim): Playwright UI end-to-end + README docs for the Simulation tab"
```

---

## Plan Self-Review (completed during planning)

- **Spec coverage:** data layering + resolution selector (Tasks 4, 15), calibration incl. U-shape /
  smile / VIX / presets (5–6), paths with stress dials (7), pricing + fill rules (8), entry/exit
  semantics + experiments (9–10), reference harness (11), family mode (12), risk metrics (13), jobs +
  API + smile capture (14), tab UI (15), E2E tiers + README (16). Gaps consciously deferred (also in
  spec §12): child-MTM fan in family mode, disk persistence, bear_call validation.
- **Placeholder scan:** no TBD/TODO; every code step is complete source. Two binding notes tell the
  executor to grep real names in `server.py` (broadcast fn, `state`/`ib`) rather than trusting guesses.
- **Type consistency:** `EntryState` fields identical between Tasks 9/10/12; `TrialResult` between
  10/12/13; `build_cell_payload(results, cfg, sl_multiplier, k)` used by Task 14's pipeline;
  `sl_multiplier="inf"` JSON encoding consistent (spec + config + payload); `SimPaths.spots` column
  semantics ("close of bar t") fixed across 7/9/10.
- **Known test-behavior decisions:** engine tests assert structural invariants (geometry, first-minute,
  exact stop debit) rather than brittle price values; the harness test fails → fix `run_entry`, never
  the test (stated in Task 11).
