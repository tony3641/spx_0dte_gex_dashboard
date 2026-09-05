# Intraday Smile Dynamics — Design Spec

- **Date:** 2026-09-05
- **Branch:** `feature/intraday-mc-stress`
- **Status:** Design approved in chat; ready for implementation planning
- **Scope:** `sim_*` modules only (simulator). No live-chain, strategy-engine, or order-path changes.
- **Language:** this and all repo documents are in English.

## 1. Summary

The Monte Carlo simulator prices options along each simulated intraday path with one SVI
smile fitted or snapshotted once at calibration time. Today the smile's five parameters
(`a, b, rho, m0, sigma`) are static for the entire simulated day; the only intraday
dynamic is a parallel IV level shift driven by the path's own GARCH volatility state,
`vol_beta * (sigma_t - sigma0)`.

This spec adds three orthogonal dynamics, delivered as three separately-verified phases:

| Phase | Config dial(s) | Effect | What moves |
|---|---|---|---|
| **A** | `skew_beta` | sigma-driven skew tilt | slope only (exactly zero at ATM) |
| **B** | `skew_t_gamma` | expiry amplification `(T0/T)^gamma` | rescales Phase A's tilt in time |
| **C'** | `atm_budget`, `budget_beta` | variance-budget ATM anchor | level only (m-independent) |

Each phase must pass its own independent validation gate **before** the next phase is
stacked on top. Neutral dial values reproduce current behavior bit-for-bit, so the
regression baseline never drifts and every change is attributable to exactly one phase.

## 2. Current state (verified in code)

- `calibrate()` (`sim_calibrate.py:335`) loads the smile once via `load_smile_snapshot()`
  (`sim_calibrate.py:298`; captured `config/sim_smile.json` -> default file -> built-in
  `DEFAULT_SMILE`) and freezes it into `CalibratedModel.smile`. Nothing re-touches it.
- `smile_iv()` (`sim_pricing.py:41`) is the single IV evaluation path:

  ```python
  base   = smile.iv(m)                       # static SVI in log-moneyness
  linked = base + vol_beta * (sigma_col - sigma0)
  return np.clip(linked, 0.01, 5.0)
  ```

  `vol_beta = 0.75` default (`sim_config.py:33`). The SVI shape never changes; the shift
  is a uniform parallel translation (equivalent to moving `a` only).
- No time dependence: `smile_iv` takes no `T`. `T_left[t]` (`sim_engine.py:140`) enters
  only `bsm_put` / `bsm_put_delta` — prices decay with theta, but the IV *input* never
  does. Consequence: a 15:30 entry is priced at the 09:35 IV level; late-window credits
  are systematically overstated on quiet paths.
- Moneyness `m = log(K / S_t)` is recomputed against current spot each bar — the smile is
  sticky-moneyness and follows spot with no shape response after a crash.
- Four call sites, all in `sim_engine.py`: `:170` (dynamic-k entry), `:200` (engine entry
  scan), `:285` (mark-to-market), `:325` (per-path MTM).
- VIX is not simulated: `vix_map` (`sim_engine.py:121`) derives `VIX_t = clip(vix0 *
  sigma_t / sigma0, 5, 100)` purely from the path's sigma state and uses it only for
  entry gating. "Bind the smile to VIX" and "bind it to the sigma state" are therefore
  the same information source in this simulator.
- The path generator already contains the leverage effect: GJR asymmetry term
  (`sim_paths.py:47-48`, `gamma * eps^2 * neg`), per-bar U-shape multiplier
  (`sim_paths.py:42`), and per-path state initialized at long-run variance
  `v_bar = omega / (1 - p)` (`sim_paths.py:40`).

## 3. Design rationale (condensed from the design discussion)

| Question raised | Decision | Why |
|---|---|---|
| Are the SVI params really static? | Confirmed for `a, b, rho, m0, sigma` — but the `vol_beta` parallel shift already moves the level | The gap is precisely skew/curvature dynamics and T->0 response, not the ATM level |
| Runtime cost of dynamics? | Closed-form parametric responses only; **never refit SVI at runtime** | BSM special functions (`ndtr`, `log`, `exp`) dominate the hot loop; a tilt adds <1%; the budget tables are O(steps) precompute. A per-(path, bar) L-BFGS refit would be 10^4-10^6x more expensive |
| Bind to VIX or to returns? | Bind to the path's sigma state `sigma_t`; no VIX path | `vix_map` makes VIX a deterministic function of `sigma_t`; GJR already encodes the asymmetric (down-move-sensitive) response; the smile and the VIX entry gate stay coherent by construction |
| Which literature? | Borrow functional forms, not engines | Heston/rBergomi engines would rewrite the path generator (rejected). Ported conclusions: implied skew slope responds to the vol state (correlated stochastic vol); short-dated skew scales like `tau^(H-1/2)` with `H ~= 0.1` ("Volatility is rough", Gatheral-Jaisson-Rosenbaum 2018) -> gamma ~= 0.4; remaining expected variance defines the fair ATM anchor |
| Can we tune a simple power-law decay `eta` from live tape? | **Rejected.** Replaced by the variance-budget anchor (zero free time parameters) | On a single intraday path, time and vol state are collinear (the U-shape correlates time-of-day with vol; directional days are dominated by the stress term) — `eta` is not identifiable from live IV paths. Worse, the true intraday IV decay is non-monotone (fast early burn-off, midday trough, late firm-up — the mirror of the U-shape), which a monotone `(1-f)^eta` cannot reproduce. The U-shape is already estimated from hundreds of days of bars in `fit_ushape()` (`sim_calibrate.py:170`), so the decay *shape* is estimated, not tuned |
| Production tuning discipline | All dials neutral-default to legacy (bit-identical); tuning = explicit A/B runs | Same methodology as the existing `vol_cap_mult` work (terminal fan kept consistent with the IV-implied distribution). The SVI clips (`SVI_WING_CAP`, floor) bound the worst case of a mis-set coefficient. The existing smile-capture infrastructure (`save_smile_snapshot`) becomes the future panel-calibration data source |

## 4. Unified IV model

Final form after all three phases, for log-moneyness `m = log(K / S_t)` at bar `t`:

```
t_scale = T_ref / max(T_t, T_floor)                          # shared per-bar time factor
        T_ref = T_left[0],  T_floor = 0.5 * bar_year_frac(bar_secs)
ratio   = clamp(sigma_t / sigma0 - 1,  -1.0, +3.0)          # tail guard, tilt input only
tilt    = -skew_beta * t_scale ** skew_t_gamma * ratio * m   # Phase A x Phase B
level   = atm_budget ? iv0 * ( sqrt( (A(t) + B(t) * sigma_tilde_t^2) / V0 * t_scale ) - 1 )
                     : vol_beta * (sigma_t - sigma0)         # Phase C' replaces legacy shift
        sigma_tilde_t^2 = v_bar + budget_beta * (sigma_t^2 - v_bar)
IV_t(m) = clip( smile.iv(m) + level + tilt,  0.01, 5.0 )
```

Properties:

- **Annualization (budget branch).** The `t_scale` factor makes `L(t)` an *annualized*
  IV ratio (remaining expected variance per unit of remaining time), not a total-variance
  ratio — without it the anchor would double-count the theta BSM already applies via
  `T_left`. With a flat U-shape, `S(t)/S(0) = T_t/T_ref` cancels `t_scale` exactly and
  the quiet-path ATM IV is flat (the variance-budget baseline).

- **Orthogonality.** `tilt` is exactly zero at `m = 0` — Phases A/B never move the ATM
  IV. `level` is independent of `m` — Phase C' never changes the shape. Each phase's
  effect on any output is attributable without confounding.
- **Neutral defaults are legacy.** With `skew_beta = 0`, `skew_t_gamma = 0`,
  `atm_budget = False`: `tilt` is identically zero and `level` reduces to
  `vol_beta * (sigma_t - sigma0)` computed in the same operation order as today, so
  results are bit-identical to master (enforced by a fixture test, not assumed).
- **Tail guards.** The ratio clamp `[0, 4]` bounds the tilt on uncapped GARCH ratchet
  paths (sigma up to ~10x sigma0 when `atm_iv` is unset); the final `clip` bounds the IV
  itself. The tilt clamp applies only to the tilt, never to the budget anchor.
- **Last bar.** `T_left[steps-1] = 0` -> prices are intrinsic regardless of IV; the
  anchor value there is irrelevant and `T_floor` prevents division by zero in `g(t)`.

## 5. Phase A — sigma-driven skew tilt

**Model.** `tilt = -skew_beta * (sigma_t/sigma0 - 1) * m`. Sign convention: `skew_beta >= 0`
means a vol spike raises put-wing IV (m < 0) more than call-wing IV — the leverage-effect
signature. ATM IV is untouched.

**Magnitude anchor.** With the cap active (`atm_iv` set, `vol_cap_mult = 2.0`) sigma stays
within ~2.3x sigma0, so the max tilt at the ladder edge (|m| = 0.15) with
`skew_beta = 1` is ~+0.20 (20 vol points) — sane. The clamp covers uncapped tails.

**Implementation.**

- `sim_config.py`: `skew_beta: float = 0.0`, validated `>= 0` in `__post_init__`
  (next to the existing `vol_beta` check).
- `sim_pricing.py`: tilt term in `smile_iv`; module constants
  `_VOL_RATIO_MIN = -1.0`, `_VOL_RATIO_MAX = 3.0`.

## 6. Phase B — expiry amplification

**Model.** `g(t) = (T_ref / max(T_t, T_floor)) ** skew_t_gamma` multiplies the tilt.
Rough-vol literature anchor: ATM skew ~ `tau^(H-1/2)`, `H ~= 0.1` -> `gamma ~= 0.4`
(cross-asset consensus constant; not tuned from local tape — nobody re-estimates H daily).

**Magnitude honesty.** At `gamma = 0.4`: ~2.9x by 30 minutes to close, ~14x at the last
half-bar. Deep OTM wings will price very steep into the close on stress paths. That is
the intended effect and exactly what this phase's gate quantifies; `gamma` is a dial,
not an article of faith.

**Implementation.**

- `sim_config.py`: `skew_t_gamma: float = 0.0`, validated `0 <= gamma <= 1`.
- `g` precomputed once per run as a length-`steps` vector (`T_left` already exists);
  call sites pass the scalar `g[t]`.

## 7. Phase C' — variance-budget ATM anchor

**Math (all closed form, O(steps) precompute).**

```
p_eff  = alpha + gamma * gamma_mult / 2 + beta        # must apply gamma_mult,
                                                       # same as sim_paths.py:21
v_bar  = omega / (1 - p_eff)                           # same reference as sim_paths.py:40
E[sigma^2_{t+k} | sigma_t] = v_bar + p_eff^k * (sigma_t^2 - v_bar)   # z symmetric

S(t) = sum_{k=t+1}^{steps-1} u_k^2 * dt                # backward: S(t) = S(t+1) + u_{t+1}^2 * dt
P(t) = sum_{k=t+1}^{steps-1} p_eff^(k-t) * u_k^2 * dt  # backward: P(t) = p_eff * (u_{t+1}^2 * dt + P(t+1))
A(t) = v_bar * (S(t) - P(t));   B(t) = P(t)
V(t, sigma_t) = A(t) + B(t) * sigma_tilde_t^2
sigma_tilde_t^2 = v_bar + budget_beta * (sigma_t^2 - v_bar)
V0 = v_bar * S(0)                                      # = V(0, initial state)
t_scale_t = T_ref / max(T_t, T_floor)                  # same table the tilt uses
L(t) = iv0 * sqrt(V(t) / V0 * t_scale_t)               # annualized by remaining time
```

- `iv0 = smile.iv(0.0)`. `V0` uses the path generator's own initial state
  (`sigma^2 = v_bar`), so `L(0) = iv0` holds **exactly** — the anchor reattaches to the
  market snapshot at t=0 with no level jump.
- Quiet-path behavior (`sigma_t ~ sqrt(v_bar)`): `L(t) = iv0 * sqrt(S(t)/S(0) * t_scale_t)`
  — a pure
  U-shape ratio: fast early burn-off, then progressive firm-up into the close (the
  trough's depth/timing follows the close-bucket weight). The intraday decay
  shape is *estimated from bar history*, with zero free parameters.
- **State sensitivity vs legacy.** The anchor's sigma-sensitivity is materially
  stronger than the legacy linear `vol_beta` link: with `p_eff ~= 0.95`, a 4x vol-state
  shock raises remaining expected variance ~1.7x (annualized IV ~+30%), where the
  legacy shift moves IV by well under 1%. This is the theory value — `budget_beta` is
  the A/B dial for it (and for the implied-overreacts-vs-realized channel).
- Stress behavior: through `B(t) * budget_beta * (sigma_t^2 - v_bar)`, elevated path
  sigma raises remaining expected variance and the IV level — the stress channel the
  legacy `vol_beta` shift provided, now with time-consistent persistence (`p_eff^k`).
- `budget_beta` semantics: 1.0 = theory; 0.0 = pure time shape; >1 amplifies the
  state sensitivity (headroom for the implied-overreacts-vs-realized residual channel).

**Known residual (accepted, documented).** The anchor models expected *remaining*
variance; it does not model variance-risk-premium burn-off. On quiet days it will still
sit somewhat above the real market's late-day IV — bounded and far closer than the
constant IV it replaces. Not modeled (see Non-goals). Additionally, the final few bars
annualize a shrinking variance sample, so the model's ATM IV rises steeply in the last
minutes (bounded by `T_floor`; prices there sit near intrinsic, so the impact is
immaterial) — verify against panel snapshots in future work.

**Implementation.**

- `sim_calibrate.py`: new frozen dataclass `SmileDynamics` (fields: `skew_beta`,
  `g_vec`, `atm_budget`, `budget_beta`, `A`/`B`/`V0` when enabled, `sigma0`, `vol_beta`,
  `flat_iv`, `iv0`) + `build_dynamics(model, cfg)` pure function, placed next to
  `CalibratedModel`.
- `sim_config.py`: `atm_budget: bool = False`, `budget_beta: float = 1.0` (>= 0).
- `smile_iv` evolves to `smile_iv(m, smile, sigma_t, dyn, t)` — one signature solves all
  four call sites; legacy behavior is expressed as neutral `dyn` values, not branches.
- **Do not cache `dyn` in `_CALIB_CACHE`** (`sim_jobs.py:146`): the cache key covers the
  data source only, while `dyn` depends on run-level dials. Build in `execute_pipeline`
  (`sim_jobs.py:137`) after calibration, once per run (O(steps), negligible), pass into
  `run_cell` / `run_family`; engine functions accept `dyn=None` and lazily build, so
  existing tests and direct callers keep working.

## 8. Staging and independent validation gates

Hard rule (user requirement): **each phase is implemented, independently verified, and
confirmed correct before the next phase is stacked.** A failed gate blocks the stack.

| Gate | Runs against | Must pass |
|---|---|---|
| **Gate A** | Phase A only (`skew_t_gamma=0`, `atm_budget=False`) | (1) Unit: tilt direction (sigma up -> m<0 IV up, m>0 IV down, m=0 unchanged); clamp bounds; `flat_iv` short-circuit unaffected; put price monotone in strike on a grid (no butterfly flips). (2) **Bit-identical regression**: `skew_beta=0` full runs on 3 fixed seeds are `array_equal` vs master (fixture infrastructure in `tests/fixtures/generate_sim_fixture.py`). (3) Behavioral signature: `skew_beta=1` richens the put wing (m<0 IV up) and NARROWS bull-put credits on a vol spike / widens them on a collapse, verifiable on down-drift paths vs legacy; deltas visible in the export report |
| **Gate B** | Phase A + B | (1) `skew_t_gamma=0` bit-identical to the Gate-A state. (2) At `gamma=0.4`, put prices monotone in strike at **every** bar. (3) Quantified late-window (last hour) credit/delta shift on stress paths — the report that justifies the phase |
| **Gate C'** | Phase A + B + C' | (1) Table identities: `V = A + B*sigma^2` vs brute-force per-bar summation, and closed-form `E[sigma^2]` vs an explicit GJR recursion simulation (tol 1e-12). (2) `L(0) = iv0` exact. (3) Quiet-path IV shows the U-shape signature (early decline > midday; late firm-up — directional assertions). (4) Stress-path IV rises into the close. (5) `atm_budget=False` bit-identical to the Gate-B state. (6) Full-stack report: early-vs-late window entry credit bias, C' vs legacy — the original motivation, must show a number |
| **Final** | Full stack | Full test suite; README and export docs updated in the same change |

Bit-identical chains: master -> (A, dial off) -> (A+B, gamma off) -> (A+B+C', budget off).
Any link breaking `array_equal` is a release blocker.

## 9. Implementation map

| File | Change |
|---|---|
| `sim_config.py` | 4 new fields (`skew_beta`, `skew_t_gamma`, `atm_budget`, `budget_beta`) + validation |
| `sim_calibrate.py` | `SmileDynamics` dataclass + `build_dynamics(model, cfg)` (backward recursions, shared `t_scale` table) |
| `sim_pricing.py` | `smile_iv(m, smile, sigma_t, dyn, t)`; ratio clamp constants |
| `sim_engine.py` | accept/thread `dyn`; update call sites `:170`, `:200`, `:285`, `:325` |
| `sim_jobs.py` | build `dyn` per run in `execute_pipeline`; add new dials to the export meta (`:195`) |
| `README.md` | formula, parameter meanings, tuning guidance (same change as the code) |
| `tests/test_sim_pricing.py` | new — tilt/g/clamp/flat-iv/bit-identical units |
| `tests/test_sim_calibrate.py` | extend — `build_dynamics` recursions, identities, `L(0)=iv0` |
| `tests/fixtures/generate_sim_fixture.py` | regression fixtures for the bit-identical chains |

## 10. Config, export, docs

- New `SimRunConfig` fields with neutral defaults; `validate()` raises on negative
  `skew_beta` / `budget_beta` and on `skew_t_gamma` outside `[0, 1]` (repo pattern:
  explicit `validate()`, not `__post_init__`).
- Export meta `dials` (`sim_jobs.py:195`) gains the four new fields so every report is
  self-describing.
- README documents the model, the formula, each dial's meaning and its theory/legacy
  default, and the validation-gate methodology (English).

## 11. Testing matrix

| Layer | Tests |
|---|---|
| Pure math | `smile_iv` tilt direction/clamp/flat-iv; `g_vec` values incl. `T_floor`; `A`/`B` recursion vs brute force; `E[sigma^2]` closed form vs explicit recursion; `L(0)=iv0` |
| Properties | put monotonicity in strike per bar (A, B); ATM invariance under A/B; shape invariance under C' |
| Regression | bit-identical chains (3 seeds each) via fixtures |
| Behavioral | credits on down-drift vs driftless paths (A); late-window shift (B); quiet-path U-shape IV path + early/late entry bias (C') |

## 12. Non-goals (YAGNI)

- Heston / rough-bergomi stochastic-vol engine (would rewrite the path generator).
- Runtime SVI refitting (forbidden by the cost rule).
- A simulated stochastic VIX path (redundant with `vix_map`).
- Dynamics for SVI curvature parameters `m0`, `sigma` (second-order; revisit only with
  panel snapshot evidence).
- Explicit variance-risk-premium decay modeling (accepted residual, see §7).

## 13. Future work

- Accumulate multi-timestamp captured smiles (`save_smile_snapshot`) into a panel and
  empirically validate the budget anchor's shape against real intraday IV.
- A/B tune `budget_beta` (implied-vs-realized overreaction channel) and `skew_beta` using
  the fan-vs-market methodology once panel data exists.
- Revisit VRP burn-off only if the quiet-day residual proves material for late-window
  strategy statistics.
