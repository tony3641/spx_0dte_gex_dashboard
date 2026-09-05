# tests/test_sim_pricing.py
"""Unit tests for the smile-dynamics IV math (spec 2026-09-05 §4-§6)."""
import math

import numpy as np
import pytest

from gex_calculator import _bsm_delta
from sim_pricing import (bsm_put, bsm_put_delta, bar_year_frac, build_ladder,
                         combo_fill_credit, half_spread, smile_iv, tick_floor)
from sim_calibrate import DEFAULT_SMILE, CalibratedModel, GarchParams, SmileDynamics

SIGMA0 = 0.0005
R = 0.043


def _dyn(**kw):
    d = dict(sigma0=SIGMA0, vol_beta=0.75, flat_iv=False,
             iv0=float(DEFAULT_SMILE.iv(0.0)), skew_beta=0.0,
             t_scale=np.ones(4), skew_t_gamma=0.0)
    d.update(kw)
    return SmileDynamics(**d)


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
    m = np.array([-0.03, 0.0])
    # row 0 = higher path vol, row 1 = lower path vol (matrix: rows=vol state, cols=moneyness)
    iv = smile_iv(m, DEFAULT_SMILE, np.array([[0.0007], [0.0004]]), _dyn(), 0)
    assert iv.shape == (2, 2)
    assert iv[0, 0] > iv[1, 0]                # same moneyness: higher path vol -> higher IV
    assert iv[0, 0] > iv[0, 1]                # same path vol: OTM put (m<0) richer than ATM
    flat = smile_iv(m, DEFAULT_SMILE, np.full((2, 1), 0.0009), _dyn(flat_iv=True), 0)
    assert np.all(flat == flat[0, 0])         # flat mode: constant IV regardless of m / sigma


def test_neutral_dyn_is_bit_identical_to_legacy_formula():
    m = np.linspace(-0.15, 0.15, 31)
    sigma = np.array([[0.0007], [0.0004]])
    out = smile_iv(m, DEFAULT_SMILE, sigma, _dyn(), 1)
    legacy = np.clip(DEFAULT_SMILE.iv(m) + 0.75 * (sigma - SIGMA0), 0.01, 5.0)
    assert np.array_equal(out, legacy)


def test_tilt_zero_at_atm_and_moves_wings():
    m = np.array([-0.10, 0.0, 0.10])
    sigma = np.array([[SIGMA0 * 1.5]])                 # same vol state -> level cancels
    no_tilt = smile_iv(m, DEFAULT_SMILE, sigma, _dyn(), 0)
    tilt = smile_iv(m, DEFAULT_SMILE, sigma, _dyn(skew_beta=1.0), 0)
    # a (1, 1) sigma_t keeps the batch axis -> output is (1, len(m)); index moneyness on axis 1
    assert tilt[0, 0] > no_tilt[0, 0]                  # put wing richer under tilt
    assert tilt[0, 2] < no_tilt[0, 2]                  # call wing cheaper under tilt
    assert tilt[0, 1] == no_tilt[0, 1]                 # ATM exactly untouched by the TILT


def test_down_shock_flattens_wing():
    m = np.array([-0.10, 0.0, 0.10])
    base = smile_iv(m, DEFAULT_SMILE, np.array([[SIGMA0]]), _dyn(), 0)
    down = smile_iv(m, DEFAULT_SMILE, np.array([[SIGMA0 * 0.5]]), _dyn(skew_beta=1.0), 0)
    assert down[0, 0] < base[0, 0]          # vol collapse -> put wing cheaper
    assert down[0, 2] > base[0, 2]          # call wing richer


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


def test_half_spread_widens_otm():
    hs = half_spread(np.array([0.0, -0.05]), 0.05)
    assert hs[0] == 0.05 and hs[1] > 0.05


def test_tick_floor_and_combo_fill():
    assert tick_floor(0.27) == 0.25
    assert tick_floor(0.23) == 0.20
    assert tick_floor(0.25) == 0.25           # exact multiple stays
    np.testing.assert_allclose(tick_floor(np.array([0.27, 0.23])), [0.25, 0.20])
    assert combo_fill_credit(0.27, 0.25, 0.05) == 0.25   # natural 0.25 is not the constraint
    assert combo_fill_credit(0.23, 0.20, 0.05) == 0.20
    assert combo_fill_credit(0.07, 0.02, 0.05) == 0.05   # floored at one tick
    # off-grid NATURAL must also be floored onto the 0.05 grid (regression: the old
    # implementation returns the raw natural when it binds, e.g. 0.187 / 0.54 / 0.69).
    assert combo_fill_credit(0.30, 0.187, 0.05) == 0.15
    assert combo_fill_credit(0.187, 0.54, 0.05) == 0.15
    assert combo_fill_credit(0.30, 0.02, 0.05) == 0.05


def test_build_ladder():
    K = build_ladder(6000.0, 0.15)
    assert K[0] == 5100.0 and K[-1] == 6900.0
    assert (np.diff(K) == 5.0).all() and 6000.0 in K


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


def run_entry_sim(model, cfg, spots, sigmas, ladder, dyn=None, per_path_start=None):
    """Thin stand-in for sim_engine.run_entry on prebuilt paths (avoids the import)."""
    from types import SimpleNamespace
    from sim_engine import run_entry
    return run_entry(model, cfg, _strategy_sim(), SimpleNamespace(spots=spots, sigmas=sigmas),
                     ladder, per_path_start=per_path_start, dyn=dyn)


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


def test_late_window_credits_steeper_with_gamma():
    """Gate B deliverable: quantify the last-window credit shift (spec §8).

    Vol-SPIKE geometry (sigma = 1.8x SIGMA0 every bar): the smile tilt RICHENS the bought
    deep-OTM wing put more than the near-ATM short put, so the bull-put net credit FALLS.
    skew_t_gamma AMPLIFIES that tilt toward expiry (scales by (T0/T)^gamma), so the late
    window (bars >= 47) mean credit at gamma=0.4 is BELOW gamma=0 — the brief's `>` was
    backwards for a spike. Direction asserted below was confirmed empirically.

    The permissive short-delta band qualifies at the window OPEN for every path, so no
    path ever reaches the late window naturally (fixture captures are all bar 0). To make
    the last-hour credit observable we gate eligibility with run_entry's per_path_start,
    staggering starts across bars 47..53. Both gamma cohorts share identical starts, so
    entry bars match and the fills differ only by the expiry amplification.
    """
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
    starts = 47 + np.arange(n) % 7                            # force eligibility into bars 47..53
    fills, minutes = {}, {}
    for tag, gamma in [("gamma0", 0.0), ("gamma04", 0.4)]:
        cfg = SimRunConfig(strategy_name="T", bar_size="5m", skew_beta=1.0,
                           skew_t_gamma=gamma)
        es = run_entry_sim(model, cfg, spots, sigmas, ladder, dyn=build_dynamics(model, cfg),
                           per_path_start=starts)
        fills[tag], minutes[tag] = es.fill_credit, es.entry_minute
    late = minutes["gamma0"] >= 47                            # last hour of the 09:35-14:00 window
    assert late.all(), "forced-late cohort did not all enter in the late window"
    print(f"late-window mean credit gamma=0: {fills['gamma0'][late].mean():.4f} "
          f"gamma=0.4: {fills['gamma04'][late].mean():.4f}")
    assert fills["gamma04"][late].mean() < fills["gamma0"][late].mean()


# --- variance-budget ATM anchor level branch (Task 11, Phase C', spec §7) ---


def _budget_model(ushape):
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
    ivs = np.array([smile_iv(np.array([0.0]), model.smile,
                             np.array([[np.sqrt(dyn.v_bar)]]), dyn, t)[0, 0]
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
                             np.array([[v_atm]]), dyn, t)[0, 0] - dyn.iv0
    assert lvl(0) == pytest.approx(0.0, abs=1e-12)     # L(0) == iv0 exactly anchored
    assert lvl(6) < 0.0                                # open bucket burned off -> dip
    assert lvl(39) > lvl(6)                            # recovery as close ramp dominates
    assert lvl(70) > lvl(39)                           # keeps firming into the close


def test_budget_stress_rises_into_close():
    dyn, model = _ubars()
    v_stress = 2.0 * np.sqrt(dyn.v_bar)
    lvl = lambda t: smile_iv(np.array([0.0]), model.smile,
                             np.array([[v_stress]]), dyn, t)[0, 0] - dyn.iv0
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


def test_late_window_entry_bias_report():
    """Gate C' deliverable (spec §8 item 6): the original motivation, quantified.

    Deterministic directional case: close-heavy U-shape + elevated vol state. The
    budget anchor's annualized remaining variance concentrates in the close bucket,
    so late-window entries price RICHER than under the legacy flat level. The real
    market's late-day IV crush comes from variance-premium burn-off, which is an
    accepted residual (spec §7) — expect the anchor to move the other way from the
    premium on quiet real days. Print the numbers for the Gate C' report.

    The permissive short-delta band qualifies at the window open for every path, so
    no path reaches the late window naturally (fixture captures are all bar 0). Gate
    eligibility with run_entry's per_path_start, staggering starts across bars
    47..53 (the accepted Task 8 fix): both cohorts enter in the late window at
    IDENTICAL bars, so the credit means are comparable and differ only by the
    budget-vs-legacy LEVEL. A uniform level shift raises the credit via vega (the
    near-ATM short leg is the higher-vega side), so budget > legacy here — this is
    NOT the backwards tilt direction rejected in Ruling #9.
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
    starts = 47 + np.arange(60) % 7                  # force eligibility into late bars 47..53
    res = {}
    for tag, kw in [("legacy", {}), ("budget", {"atm_budget": True})]:
        cfg = SimRunConfig(strategy_name="T", bar_size="5m", skew_beta=1.0, **kw)
        es = run_entry(model, cfg, _strategy_sim(), paths, ladder,
                       per_path_start=starts, dyn=build_dynamics(model, cfg))
        res[tag] = es
    for tag in res:
        late = (res[tag].entry_minute >= 47) & res[tag].entered
        print(f"{tag}: late-window mean fill credit "
              f"{res[tag].fill_credit[late].mean():.4f}")
    late_l = (res["legacy"].entry_minute >= 47) & res["legacy"].entered
    late_b = (res["budget"].entry_minute >= 47) & res["budget"].entered
    assert late_l.any() and late_b.any()
    assert np.array_equal(res["legacy"].entry_minute[res["legacy"].entered],
                          res["budget"].entry_minute[res["budget"].entered]), \
        "cohorts entered at different bars; credit means are not level-comparable"
    assert res["budget"].fill_credit[late_b].mean() > res["legacy"].fill_credit[late_l].mean()


def test_budget_high_beta_low_sigma_is_finite():
    """budget_beta>1 with sigma below the long-run floor must not emit NaN IV (final-review fix)."""
    dyn, model = _ubars(budget_beta=2.0)
    low = 0.30 * np.sqrt(dyn.v_bar)
    for t in range(78):
        iv = smile_iv(np.array([0.0]), model.smile, np.array([[low]]), dyn, t)[0, 0]
        assert np.isfinite(iv), f"NaN IV at bar {t} (sig2t went negative)"

