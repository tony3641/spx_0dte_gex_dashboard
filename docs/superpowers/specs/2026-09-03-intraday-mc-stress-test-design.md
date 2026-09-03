# Intraday Monte Carlo Stress-Test Framework — Design

**Date:** 2026-09-03
**Status:** Approved design, pending implementation plan
**Branch:** `worktree-feature+intraday-mc-stress` (harness-managed worktree branch; renamed to a clean
`feature/intraday-mc-stress` when the PR is opened)

---

## 1. Purpose

A daily Monte Carlo is meaningless for 0DTE SPX bull put spreads: the strategy's fate is decided by
intraday structure (U-shaped volatility, crash clustering) and the theta-vs-gamma race. This feature
adds an **intraday high-frequency Monte Carlo engine** plus a dashboard **Simulation tab** that:

1. Generates N simulated trading days of SPX price paths from a GJR-GARCH(1,1) + Student-t model
   calibrated on real intraday bars, with the intraday U-shape volatility profile.
2. Marks a spread to market every simulated bar via BSM under a vol-linked IV smile.
3. Runs the *existing* strategy definitions from `config/strategies.json` — entry conditions, tick-rule
   fills, stop-loss / take-profit / expiry settlement, and parent→child re-entry triggers — against the
   synthetic market.
4. Reports the day-PnL distribution and tail risk (win rate, CVaR, max drawdown, ruin probability), and
   supports three core experiments: stop-loss multiplier sweeps, fixed-vs-dynamic short-strike distance,
   and stress-dial scenarios (fatter tails, stronger leverage effect, IV pop).

### Goals

- Interactive: ~10k paths per config in seconds (chunked, vectorized), sweep grids in one job.
- Honest: adverse tick rounding on entry, market-order stop fills (+0.10), documented approximations.
- Reuse: strategy semantics come from `config/strategies.json` via the existing `Strategy` dataclasses;
  a reference harness asserts the vectorized engine agrees with the real `strategy_engine` logic.
- Safe: simulation never touches live trading state or places orders.

### Non-goals (v1)

- Heston / stochastic-vol-with-jumps models (GJR-GARCH-t is the v1 engine; the path-generator interface
  leaves room for later models).
- Multi-day *sequential* simulation with overnight gaps (equity curves come from bootstrapping day PnLs).
- Persisting run results to disk (in-memory job registry + client-side CSV export).
- Bear-call / iron-condor simulation (direction field exists; v1 validates bull_put).

---

## 2. Decisions (agreed during brainstorming)

| # | Question | Decision |
|---|---|---|
| 1 | Calibration data source | **Layered:** user CSV (any resolution) → yfinance → IB live client. UI exposes a bar-length selector (1m / 5m / 5s…); 5s is only satisfiable from CSV. Result header always states source + resolution actually used. |
| 2 | Simulated scope | **Both, phased:** single-strategy runs first; family mode (parent + children re-entry) second, same feature. |
| 3 | Intraday spread marking | **Vol-linked smile:** quadratic IV(m) fit from a real chain snapshot (sticky-moneyness), level shifted by λ·(σ_path − σ₀). λ=0 degenerates to static smile; flat-IV mode available as a sanity checkbox. |
| 4 | Entry fill | Theoretical credit mid **rounded down to the 0.05 tick grid** (adverse): 0.27→0.25, 0.23→0.20. Candidate credit uses the conservative combo side (S_bid − L_ask of the synthetic chain). |
| 5 | Stop-loss fill | Market order: exit debit = **trigger price + 0.10** credit points, exactly. Take-profit (if configured): fill at the TP limit when the mark crosses it. Expiry: intrinsic at the 16:00 SPXW PM settle × 100. |
| 6 | Engine style | **Hybrid, chunked:** numpy for paths + pricing tensors; tensor scans for entry/stop; per-path Python loop only for family-trigger events. Approach-1 MockState loop kept as a *test* to pin semantics. |
| 7 | Testing | Three tiers: unit, API-level end-to-end, UI-level end-to-end (Playwright). Hermetic via a committed fixture CSV. |

---

## 3. Architecture

Flat modules at repo root (`sim_*` prefix), matching the existing idiom. No subpackage.

```
config/strategies.json ──► Strategy dataclasses (strategy_models)      [existing, reused as-is]
        │
sim_data.py        Layered bar loaders: CSV → yfinance → IB; RTH filtering; metadata
sim_calibrate.py   GJR-GARCH(1,1)+Student-t MLE (scipy), U-shape profile, smile-snapshot
                   fit, VIX mapping → CalibratedModel (memoized)
sim_paths.py       Vectorized path generation → spot matrix + realized-vol-state matrix
sim_pricing.py     Vectorized BSM puts/deltas on strike ladders; quadratic smile;
                   synthetic bid/ask from the captured half-spread profile
sim_engine.py      Strategy simulation: entry scan, fills, stop/TP scans, expiry settle;
                   single-strategy and family orchestration
sim_risk.py        PnL distribution, CVaR, exit-reason breakdown, intraday DD,
                   bootstrap equity curves → max-DD distribution + ruin probability
sim_config.py      SimRunConfig dataclass: validation + JSON (de)serialization
sim_jobs.py        Job registry: background thread (asyncio.to_thread), one job at a
                   time, cooperative cancel, progress → WS broadcast

server.py          + POST /api/sim/run · GET /api/sim/status/{id}
                   + GET /api/sim/result/{id} · POST /api/sim/cancel/{id}
                   + GET /api/sim/smile · POST /api/sim/smile/capture
ws_handler.py      + `set_tab:sim` branch; `sim_progress` push messages
static/            + js/sim-tab.js, css/sim.css; index.html tab button + panel;
                   state.js VALID_TABS; tabs.js switchTab branch
tests/             + unit, API end-to-end, and Playwright UI end-to-end suites
                   + reference harness (MockState loop over real strategy_engine)
```

**Data flow:** run panel → `POST /api/sim/run` (SimRunConfig JSON) → job thread:
load bars → calibrate (memoized on source + resolution + lookback) → simulate in chunks of
250–500 paths (progress after each chunk) → risk metrics → result stored in an in-memory registry
(last 10 runs) → tab polls status / fetches result and renders. Strategies are read from the same
`state.strategies` objects the live engine uses; family mode finds children via `parent_name`.

**Two honesty rules:**

1. **No strategy semantics are invented in sim code.** Entry conditions are evaluated from the same
   `Condition` params in the JSON with the same defaults as `strategy_engine._lo/_hi`; candidate pick =
   max credit among valid (short, long) pairs — the same ordering the live engine applies. Where the
   live engine reads `state.vix`, the sim feeds a mapped VIX (§5.4).
2. **The reference harness is a test, not a mode.** A small-N Python loop drives the real
   `generate_candidates` / `evaluate_conditions` on synthetic chains through a mock state; CI asserts
   agreement with the vectorized engine within tolerance.

---

## 4. Market data & calibration

### 4.1 Data sources (`sim_data.py`)

| Layer | Resolution | Behavior |
|---|---|---|
| CSV | any (1s – 1d) | Documented schema: `timestamp,open,high,low,close[,volume]`, ET timestamps. RTH-filtered on load. Path configured in the UI. |
| yfinance | 1m (7d) / 5m (60d) | Zero-setup default; `^SPX` intraday + `^VIX` daily for the VIX mapping. |
| IB | 1m – 1s (shallow) | Only when the dashboard is connected; reuses `req_historical_bars`. |

- The resolution selector states which layer satisfied it; **simulation step = bar size actually loaded**
  (390 steps/day at 1m, 78 at 5m, 4,680 at 5s). No resampling: if 5s is requested but unavailable, the
  run proceeds at the coarsest available resolution and the result header says so.
- Loader returns bars + metadata (source, resolution, coverage, warnings).

### 4.2 Calibration (`sim_calibrate.py`) — memoized on (source, resolution, lookback)

1. **Returns:** log returns of loaded bars, de-meaned.
2. **GJR-GARCH(1,1), Student-t innovations**, MLE via `scipy.optimize`:
   `σ²ₜ = ω + α·ε²ₜ₋₁ + γ·ε²ₜ₋₁·1[εₜ₋₁<0] + β·σ²ₜ₋₁`. The fitted γ is the leverage-effect lever
   (stress dial scales it).
3. **Intraday U-shape:** multiplicative profile u(minute-of-day) from normalized mean |ε| per
   minute-of-day bucket, smoothed; nonparametric (no assumed functional form). Applied as σₜ·u(t).
4. **Smile snapshot:** quadratic IV(m), m = log(K/S), fitted to captured 0DTE put IVs; plus a
   half-spread profile by moneyness for synthetic bid/ask. Sources: live-chain capture (UI button,
   reads `state.chain_quotes_cache`) or a bundled default snapshot JSON.
5. **VIX mapping:** VIX₀ and scale from `^VIX` closes vs realized vol when available; constant fallback.

Graceful degradation: non-converged GARCH → preset parameters, flagged in the UI; no smile capture →
bundled default snapshot. The calibration panel always shows fitted vs defaulted.

---

## 5. Path generation, pricing, execution

### 5.1 Paths (`sim_paths.py`)

Per chunk, vectorized over (paths × steps):

- Draw z ~ standardized-t(ν) → εₜ = σₜ·u(t)·zₜ → GJR recursion updates σ²ₜ₊₁; `Sₜ₊₁ = Sₜ·exp(εₜ)`
  (zero intraday drift — defensible at 0DTE horizons and implied by the chosen model).
- **Stress dials:** ν override (5→3 fattens tails), γ ×factor (1.5 amplifies crash clustering).
- Guards: σ² floor (no variance collapse), spot clip to a sane band, per-chunk RNG streams seeded from
  the run seed (same seed ⇒ identical results).
- Outputs: spot matrix (paths × steps) + realized-vol-state matrix (drives §5.2 smile link).

**Stated limitation:** stop checks happen on bar closes; an intra-bar spike-and-revert through the
trigger is invisible at 5m. Finer bars shrink this blind spot (the reason 5s CSV matters).

### 5.2 Pricing (`sim_pricing.py`)

- Fixed SPXW-style ladder: 5-pt strikes, ±15% around entry spot (configurable).
- Vectorized BSM puts/deltas per (path, minute, strike) — same math family as `gex_calculator`'s helpers.
- Smile: `IV(K,t) = smile(mₜ) + λ·(σ_path,t − σ₀)`, clipped to [0.01, 5.0]. λ default 0.5–1.0, UI-settable;
  λ=0 = static sticky-moneyness smile; a "flat IV" checkbox replaces the smile with the ATM level.
- Synthetic bid/ask = theo ± half the captured half-spread profile.

### 5.3 Execution (`sim_engine.py`)

- **Entry scan** (vectorized over the strategy's entry window, e.g. minutes 120–210 for `Main`):
  - Conditions evaluated verbatim from the JSON strategy: `entry_window`, `short_delta` band (ladder
    deltas), `spread_width` band (ladder geometry), `credit` band (synthetic combo credit,
    conservative side S_bid − L_ask), `volatility` (mapped VIX), `trend` (RSI / pmove on the synthetic
    price matrix).
  - Valid (short, long) pairs per (path, minute) → mask → argmax credit = "best candidate first", the
    live engine's ordering. Size = `floor(budget / (width × 100))` — same as `_position_size`.
  - First valid minute wins; one entry per strategy per simulated day (mirrors the one-shot cycle).
- **Fill rules:** entry credit = tick-floor of the candidate's theo mid (0.05 grid, adverse). Stop:
  exit debit = trigger + 0.10. TP: limit fill at the TP price on crossing. Expiry: intrinsic × 100.
- **Exit scan:** mark of the held spread per bar; stop when mark ≥ |fill_credit| × multiplier;
  per-trial record: entry/exit minutes, exit reason, filled prices, per-minute MTM series.
- **Family mode:** parent runs vectorized; children re-scan only on paths where their trigger fired —
  `parent_exit_reason` (e.g. `stop_loss`) re-runs the child's entry scan from the trigger minute forward;
  `parent_unrealized_pnl` (e.g. 1.5× loss) uses the parent's MTM series; `time_of_day` triggers latch
  when their time passes during the parent's trade (mirrors `_latch_time_triggers`). Same fill rules,
  same one-entry-per-day per strategy, day PnL = Σ all legs. Trigger semantics mirror `strategy_engine`'s
  latch behavior; the reference harness covers a family case.
- **Experiment modes:**
  1. **SL sweep:** list of multipliers (e.g. 1.5 … 6.0, plus ∞ = hold past stop) in one job.
  2. **Strike mode:** `engine` (JSON conditions) vs `dynamic_k` — short strike = S_entry × (1 − k·σ̂_GJR),
     tick-rounded; long = short − width; k sweepable.
  3. **Stress dials:** ν, γ×, λ — each run tags its dials for A/B against the baseline.

### 5.4 VIX mapping

`VIX_t = clip(VIX₀ × (σ_path,t / σ₀), 5, 100)` with σ₀ the calibration-period mean vol and VIX₀ its
matching VIX mean. Documented approximation: the `volatility` condition tests this proxy, not a
simulated VIX process.

---

## 6. Risk metrics (`sim_risk.py`)

All monetary figures in $ (contracts × 100):

- **Day-PnL distribution:** mean, median, σ, win rate, CVaR₅ / CVaR₁, worst day.
- **Exit-reason breakdown:** expired / stopped / take-profit / never-entered (the never-entered share
  matters given VIX and entry-window gates).
- **Intraday max drawdown** per trial from the MTM series.
- **Bootstrap equity curves:** sample day PnLs into M sequences of L trading days → max-DD distribution;
  **ruin prob = P(max DD ≥ threshold)**, threshold as % of a user-entered account equity (default 20%).
- Experiment comparisons: sweep table per cell + overlaid histograms vs baseline.

---

## 7. API & jobs

| Endpoint | Purpose |
|---|---|
| `POST /api/sim/run` | Validate SimRunConfig (400 + reason on bad input); returns `{job_id}`. |
| `GET /api/sim/status/{id}` | `{state: queued\|loading\|calibrating\|simulating\|done\|error\|cancelled, progress: 0–1, message}` |
| `GET /api/sim/result/{id}` | Full payload: stats, histogram arrays, sweep table, run metadata. |
| `POST /api/sim/cancel/{id}` | Cooperative cancel between chunks. |
| `GET /api/sim/smile` | Stored/bundled default smile snapshot. |
| `POST /api/sim/smile/capture` | Snapshot live put-IVs from `state.chain_quotes_cache` (409 when IB is down). |

- Execution via `asyncio.to_thread`; one job at a time; registry keeps the last 10 results in memory.
- `sim_progress` WS pushes while the tab is active (progress bar animates without aggressive polling).
- **Isolation guarantee:** the engine only *reads* strategy structures. No orders, no kill-switch
  interaction; nothing on the sim tab can place a real trade.

---

## 8. UI: Simulation tab

Same wiring pattern as existing tabs (button + panel in `index.html`, `VALID_TABS`, `switchTab`,
`set_tab:sim`, Plotly charts, `theme.css` conventions; `dataviz` skill consulted at implementation time).

- **Run panel (left):** strategy / family-root select (populated from live strategies), mode toggle
  (single / family), resolution + source + lookback, N paths, seed, account equity, ruin threshold,
  fill constants (stop +0.10 editable; tick rounding fixed), stress dials (ν, γ×, λ), experiment
  builders (SL list, strike mode + k list), Run / Cancel.
- **Data & calibration panel:** which layer loaded, bar count / coverage, fitted vs defaulted flags,
  smile capture button.
- **Results area:** stat tiles (exp PnL, win rate, CVaR₁, ruin prob) → day-PnL histogram → quantile-fan
  MTM chart → equity max-DD chart → exit-reason breakdown → sweep table (cells clickable to swap the
  charts). Re-runs with a changed dial overlay onto the previous histogram for A/B.

---

## 9. Error handling

Every failure lands in the job's `error` state with a human-readable message surfaced in the tab:

- No data from any layer → suggest dropping a CSV (path shown in the message).
- Calibration non-convergence → fall back to preset parameters, **warn visibly**, never fail silently.
- IB absent on smile capture → 409 + tab hint.
- Cancel → `cancelled` state, partial results discarded.
- Numeric guards (σ² floor, spot clip, degenerate ladder) prevent NaN cascades from extreme paths.

---

## 10. Testing (three tiers)

1. **Unit** (pytest, existing conventions, `tests/run_tests.py` compatible):
   - `sim_paths`: t-kurtosis matches ν; U-shape variance peaks at open/close; seeded reproducibility.
   - `sim_pricing`: parity vs `gex_calculator` BSM helpers; tick rule (0.27→0.25, 0.23→0.20).
   - `sim_engine`: hand-computed small scenarios (known path → stop fill = trigger + 0.10; expiry
     intrinsic; never-entered path) + **reference-harness agreement** (MockState loop through the real
     `generate_candidates` / `evaluate_conditions` vs vectorized decisions on the same synthetic chain,
     tolerance-checked, including one family case).
   - `sim_risk`: metrics vs hand-computed fixtures.
2. **API end-to-end:** real FastAPI app + real engine over the HTTP lifecycle: `POST /api/sim/run` with
   a small seeded config → status transitions → result invariants (sweep cells complete, no NaNs, stats
   self-consistent). Hermetic via a committed **fixture CSV** (synthetic 5-min bars, generated offline).
3. **UI end-to-end** (Playwright, dev-only dependency): against a running server — open the Simulation
   tab, submit a small run through the real form, wait for completion, assert tiles/charts/sweep table
   render.

Known pre-existing failures on `origin/master` (unrelated, not to be fixed here unless trivial):
`test_chain_fetcher.py::test_compute_gex_uses_bsm_gamma_when_ib_gamma_missing` (tolerance drift),
`test_market_hours.py::TestIsFomcDay::test_known_fomc_date` (params.yaml lists only 2026-01-28).

---

## 11. Dependencies

| Package | Scope | Reason |
|---|---|---|
| numpy | runtime | Vectorized paths/pricing tensors. |
| scipy | runtime | GJR-GARCH MLE fit (offline, per calibration). |
| pytest-playwright (+ browser) | dev-only | UI end-to-end tier. |

## 12. Future work

- Heston/jump-diffusion path engine behind the same interface.
- Disk persistence + run history browser.
- Bear-call direction validation; multi-day sequential simulation with overnight dynamics.
- Estimated IV feedback (refit smile per path state) if the λ-link proves too coarse.
