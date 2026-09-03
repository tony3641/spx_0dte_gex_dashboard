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


def test_bootstrap_ruin_counts_first_bar_loss():
    # an equity path that OPENS with a loss larger than the threshold must show a drawdown
    out = bootstrap_ruin(np.array([-50_000.0]), equity=100_000.0, threshold_pct=0.20,
                         n_seqs=5, seq_len=1, seed=0)
    assert out["max_dd"] == [50_000.0] * 5          # max_dd is a LIST of per-seq values (5 seqs)
    assert out["ruin_prob"] == 1.0
