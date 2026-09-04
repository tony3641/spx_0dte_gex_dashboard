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
from tests.test_sim_engine import _model, _strategy


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
        m = np.log(ladder / spot)
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
        assert abs(best.credit_mid - float(es.theo_credit[p])) < 1e-3
        checked += 1
    assert checked >= 5          # the fixture must produce enough entries to be a real test
