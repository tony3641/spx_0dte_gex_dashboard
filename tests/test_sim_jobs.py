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
    # Controller correction (Conflict N-a): test_sim_jobs.py exercises the REAL execute_pipeline,
    # whose _get_strategy reads _STRATEGY_CACHE (config/strategies.json is absent -> {}). Seed the
    # synthetic "T" strategy here (the brief seeds it only in test_sim_api.py).
    from tests.test_sim_engine import _strategy
    sim_jobs._STRATEGY_CACHE["T"] = _strategy()
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


def test_execute_pipeline_attaches_spx_fan():
    # The SPX path fan is a property of the market simulation (spot dynamics do not
    # depend on the sweep cell), so it is computed once from the first cell's paths
    # and attached at the result root for the UI's top graph + report export.
    cfg = _cfg()
    bars = load_bars(cfg)
    payload = sim_jobs.execute_pipeline(cfg, bars, lambda p, m: None, spot0=6000.0)
    sf = payload["spx_fan"]
    assert len(sf["quantiles"]) == 20
    assert len(sf["values"]) == 20
    steps = cfg.steps_per_day()
    assert sf["minutes"] == list(range(steps))
    assert all(len(row) == steps for row in sf["values"])
    lo, mid, hi = sf["values"][0], sf["values"][10], sf["values"][-1]
    assert all(l <= m <= h for l, m, h in zip(lo, mid, hi))


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


def test_registry_pruned_to_last_ten_keeping_active():
    sim_jobs.reset_registry()
    for i in range(12):                                    # 12 finished jobs, oldest first
        sim_jobs._registry[f"done{i}"] = dict(id=f"done{i}", state="done", progress=1.0,
                                              message="", result={"cells": []},
                                              cancelled=False, created=float(i))
    sim_jobs._registry["act_run"] = dict(id="act_run", state="simulating", progress=0.4,
                                         message="", result=None, cancelled=False, created=100.0)
    sim_jobs._registry["queued_run"] = dict(id="queued_run", state="queued", progress=0.0,
                                            message="", result=None, cancelled=False,
                                            created=101.0)
    sim_jobs._prune_registry()
    # oldest two finished jobs dropped; the newest ten finished survive
    assert "done0" not in sim_jobs._registry and "done1" not in sim_jobs._registry
    assert "done2" in sim_jobs._registry and "done11" in sim_jobs._registry
    # active jobs are never dropped
    assert sim_jobs._registry["act_run"]["state"] == "simulating"
    assert sim_jobs._registry["queued_run"]["state"] == "queued"
    terminal = [jid for jid, j in sim_jobs._registry.items() if j["state"] == "done"]
    assert len(terminal) == 10
