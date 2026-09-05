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
    cfg.skew_beta = a.skew_beta    # apply the dial or the npz is silently the neutral capture
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
             fill=entry.fill_credit, pnl=np.array([t.pnl for t in trials]), mtm0=mtm0,
             skew_beta=a.skew_beta, t_gamma=a.t_gamma, budget=np.array(int(a.budget)))
    print(f"wrote {out}: {int(entry.entered.sum())}/{cfg.n_paths} paths entered")


if __name__ == "__main__":
    main()
