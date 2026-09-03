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
    # (brief Step 5 note) the synthetic "T" strategy lives only in tests; config/strategies.json
    # is absent, so _get_strategy would raise unknown-strategy. Seed the cache like test_sim_jobs.
    from tests.test_sim_engine import _strategy
    sim_jobs._STRATEGY_CACHE["T"] = _strategy()
    return TestClient(server.app)


def test_full_run_lifecycle_over_http(client):
    # "inf" as a STRING: JSON has no Infinity literal and httpx's request encoder forbids
    # float('inf') (allow_nan=False); SimRunConfig.from_dict decodes "inf" -> float('inf')
    # (hold-past-stop), so the string form is the wire-correct spelling of the same value.
    body = {"strategy_name": "T", "source": "csv", "csv_path": FIXTURE,
            "n_paths": 30, "chunk_size": 15, "bar_size": "5m",
            "sl_multipliers": [2.0, "inf"], "equity": 100000}
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


def test_result_returns_409_when_job_not_finished(client, monkeypatch):
    """GET /api/sim/result/{id} must 409 (not 200/500) while the job is still running."""
    import threading

    import sim_jobs

    release, started = threading.Event(), threading.Event()

    def slow_pipeline(cfg, bars, cb, spot0, cancel_check=None, state=None):
        started.set()
        release.wait(10)
        return {"meta": {"strategy": cfg.strategy_name}, "cells": []}

    monkeypatch.setattr(sim_jobs, "execute_pipeline", slow_pipeline)
    body = {"strategy_name": "T", "source": "csv", "csv_path": FIXTURE, "n_paths": 10}
    r = client.post("/api/sim/run", json=body)
    assert r.status_code == 200
    job_id = r.json()["job_id"]
    assert started.wait(5), "background job never started"
    rr = client.get(f"/api/sim/result/{job_id}")
    assert rr.status_code == 409
    assert rr.json()["detail"] == "job not finished"
    assert client.post(f"/api/sim/cancel/{job_id}").json()["cancelled"] is True
    release.set()
    deadline = time.time() + 15
    while time.time() < deadline:
        if client.get(f"/api/sim/status/{job_id}").json()["state"] in ("done", "cancelled"):
            break
        time.sleep(0.05)


def test_smile_capture_succeeds_when_chain_seeded(client, monkeypatch, tmp_path):
    """POST /api/sim/smile/capture fits + saves a smile when a live chain is present."""
    import sim_calibrate
    old_cache = server.state.chain_quotes_cache
    old_spot = server.state.spx_price
    monkeypatch.setattr(sim_calibrate, "SMILE_CAPTURE_PATH", str(tmp_path / "sim_smile.json"))
    # chain rows mirror chain_manager.build_chain_quotes: strike + put_iv in percent.
    strikes = [{"strike": float(s), "put_iv": 20.0} for s in (5700, 5800, 5900, 5950,
                                                               6000, 6050, 6100)]
    server.state.chain_quotes_cache = {"strikes": strikes}
    server.state.spx_price = 6000.0
    try:
        r = client.post("/api/sim/smile/capture")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["source"] == "captured"
        assert body["points"] >= 5
        assert {"a", "b", "c", "half_spread_atm"} <= set(body["smile"])
    finally:
        server.state.chain_quotes_cache = old_cache
        server.state.spx_price = old_spot
