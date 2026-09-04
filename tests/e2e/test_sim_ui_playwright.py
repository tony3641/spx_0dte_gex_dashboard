# tests/e2e/test_sim_ui_playwright.py
"""UI end-to-end: real server + real browser + real engine (small hermetic run)."""
import json
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
STRAT_PATH = os.path.join(ROOT, "config", "strategies.json")


def _plot_info(page, chart_id):
    """Read Plotly's full-data/layout for a chart div via the page's own Plotly object."""
    return page.evaluate(
        """(cid) => {
            const gd = document.getElementById(cid);
            if (!gd || !gd._fullData) return null;
            const xt = gd.layout.xaxis, yt = gd.layout.yaxis;
            return {
                n: gd._fullData.length,
                showlegend: !!(gd.layout.showlegend === true
                               || (gd.layout.showlegend === undefined && gd._fullData.length > 1)),
                names: gd._fullData.map(d => d.name),
                xaxis_title: xt && xt.title ? (xt.title.text || null) : null,
                yaxis_title: yt && yt.title ? (yt.title.text || null) : null,
                ticktext: xt && xt.ticktext ? xt.ticktext : null,
            };
        }""",
        chart_id,
    )


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _ensure_hermetic_strategies() -> bool:
    """Write a minimal strategies.json so the server loads >=1 strategy.

    config/strategies.json is git-ignored and absent in a clean checkout; without it the
    #simStrategy dropdown is empty and select_option(index=0) fails. This is the ONLY thing
    that makes the run possible (the run config is otherwise strategy-agnostic). Returns True
    if we wrote it (so the caller removes it in teardown).
    """
    if os.path.exists(STRAT_PATH):
        return False
    os.makedirs(os.path.dirname(STRAT_PATH), exist_ok=True)
    strategies = {
        "Main": {
            "name": "Main", "direction": "bull_put",
            "conditions": [
                {"kind": "short_delta", "enabled": True, "params": {"min": 0.3, "max": 0.45}},
                {"kind": "spread_width", "enabled": True, "params": {"min": 40, "max": 65}},
                {"kind": "credit", "enabled": True, "params": {"min": 3.0}},
                {"kind": "entry_window", "enabled": True, "params": {"start": "09:35", "end": "10:00"}},
            ],
            "exit_rules": {"take_profit": None, "stop_loss": {"multiplier": 6.0}, "hold_to_expire": False},
            "auto_execute": False, "armed": False, "target_expiry": "", "budget": 40000.0,
            "run_days": [0, 1, 2, 3, 4], "short_day_enabled": True,
            "run_on_fomc": True, "run_on_nfp": True, "parent_name": "",
            "subsequent_triggers": [], "trigger_logic": "any",
        }
    }
    with open(STRAT_PATH, "w") as f:
        json.dump(strategies, f, indent=2)
    return True


@pytest.fixture(scope="module")
def server_url():
    wrote_strat = _ensure_hermetic_strategies()
    proc = None
    try:
        port = _free_port()
        env = dict(os.environ, SERVER_PORT=str(port), SERVER_HOST="127.0.0.1")
        # Strip Discord secrets: an ambient DISCORD_TOKEN makes the child server spend
        # ~15s on a Discord login (on top of the IB-down connect timeout), which can push
        # boot past the 30s deadline and fail the test spuriously.
        for k in list(env):
            if k.startswith("DISCORD_"):
                env.pop(k, None)
        # Keep the boot hermetic against a live IB gateway too: server.py gates its HTTP
        # listener behind the IB setup (connect_ib + a full chain prefetch), and in an
        # environment where IB is actually reachable that prefetch can blow the 30s deadline.
        # Point IB at a closed port so the connect fails fast (ECONNREFUSED) and the server
        # comes up exactly as it does in CI.
        env["IB_HOST"] = "127.0.0.1"
        env["IB_PORT"] = str(port + 1)
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
            pytest.fail("server did not start within 30s")
        yield url
    finally:
        if proc is not None:
            try:
                proc.send_signal(signal.SIGINT)
            except (ValueError, OSError):
                # Windows: Popen.send_signal does not support SIGINT (signal 2).
                proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
        # Unconditional hermetic cleanup: always run even when boot failed before yield,
        # so a stray config/strategies.json never leaks into later runs.
        if wrote_strat and os.path.exists(STRAT_PATH):
            os.remove(STRAT_PATH)


def test_simulation_tab_runs_and_renders(server_url):
    # Earlier modules in the suite import server.py, which switches the asyncio
    # event-loop policy to Selector on Windows (needed by the IB client). Playwright's
    # sync API spawns a Node subprocess, which requires a Proactor loop on Windows
    # (a Selector loop raises NotImplementedError on subprocess transports) — so flip
    # the policy for the Playwright session and restore it afterwards.
    import asyncio
    prev_policy = None
    if os.name == "nt":
        prev_policy = asyncio.get_event_loop_policy()
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    try:
        with pw.sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.goto(f"{server_url}/#sim")
            page.click('button[data-tab="sim"]')
            # strategy_list is pushed on WS connect; wait for it to populate the dropdown
            # (otherwise select_option(index=0) raises on an empty list)
            page.wait_for_function("document.querySelectorAll('#simStrategy option').length > 0", timeout=30_000)
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
            # Plotly emits more than one .main-svg per chart in the bundled version, so
            # just assert the histogram actually drew (>=1) rather than an exact count.
            assert page.locator("#simHist .main-svg").count() >= 1

            # --- report readability: per-metric help chips, chart headers, chart anatomy ---
            assert page.locator(".sim-tile .help").count() >= 6     # "?" chip on each tile
            assert page.locator(".sim-chart-head").count() == 3     # header above each chart
            hist = _plot_info(page, "simHist")
            fan = _plot_info(page, "simFan")
            dd = _plot_info(page, "simDD")
            # axis titles on every chart
            assert hist and hist["xaxis_title"] and hist["yaxis_title"]
            assert dd and dd["xaxis_title"] and dd["yaxis_title"]
            assert fan and fan["xaxis_title"] and fan["yaxis_title"]
            # fan: named percentile traces + a legend + clock-tick labels (time of day)
            assert len(fan["names"]) == 5
            assert fan["showlegend"] is True
            assert fan["ticktext"] and any(":" in t for t in fan["ticktext"])

            page.screenshot(path=os.path.join(ROOT, "docs", "screenshot_sim.png"), full_page=False)
            browser.close()
    finally:
        if prev_policy is not None:
            asyncio.set_event_loop_policy(prev_policy)
