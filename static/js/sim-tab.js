// Simulation tab: run form, job polling, Plotly rendering, JSON report export.
(function () {
    'use strict';
    let currentResult = null;
    let currentConfig = null;   // exact form config the current result came from (export)
    let selectedCell = 0;
    let pollTimer = null;
    let pollFailures = 0;   // consecutive status-fetch failures (bounded retry in poll())
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
        clearReport();          // every run starts from a blank report — no stale charts
        currentConfig = cfg;
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
        pollFailures = 0;
        pollTimer = setInterval(async () => {
            let state = null;
            let terminal = false;
            try {
                const resp = await fetch(`/api/sim/status/${jobId}`);
                if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
                state = await resp.json();
                $('simProgress').style.width = `${Math.round((state.progress || 0) * 100)}%`;
                $('simStatus').textContent = `${state.state} — ${state.message || ''}`;
                if (state.state === 'done') {
                    const r = await fetch(`/api/sim/result/${jobId}`);
                    if (!r.ok) throw new Error(`result HTTP ${r.status}`);
                    currentResult = await r.json();
                    selectedCell = 0;
                    render();
                }
                // Reset the failure counter only after a full tick (status AND, when done,
                // the result fetch) succeeds — else a persistent result-fetch failure on a
                // terminal job would reset to 0 each tick and never trip the >=3 bound.
                pollFailures = 0;
                terminal = ['done', 'error', 'cancelled'].includes(state.state);
            } catch (err) {
                pollFailures += 1;
                // Bounded retry: one transient status-fetch failure must not permanently stop
                // the timer while the job keeps running. Only give up after a few consecutive
                // failures (the fetch failures are transient, not a terminal job state).
                terminal = pollFailures >= 3;
                if (terminal) $('simStatus').textContent = `poll failed: ${err}`;
            }
            if (terminal) {
                clearInterval(pollTimer);
                pollTimer = null;
                $('simRun').disabled = false;
                $('simCancel').disabled = true;
                if (state && state.state === 'error') $('simStatus').className = 'sim-status error';
            }
        }, 500);
    }

    async function cancel() {
        clearInterval(pollTimer);
        $('simRun').disabled = false;
        $('simCancel').disabled = true;
        if (activeJobId) await fetch(`/api/sim/cancel/${activeJobId}`, { method: 'POST' });
    }

    const CHART_IDS = ['simSpotFan', 'simHist', 'simFan', 'simDD'];

    function purgeCharts() {
        CHART_IDS.forEach(id => {
            const el = $(id);
            if (!el) return;
            if (window.Plotly && el._fullData) Plotly.purge(el);
            el.innerHTML = '';
        });
    }

    // Force-reset the report to its initial blank state. Form inputs are kept; a run
    // already in flight keeps running server-side (the next Run re-reports busy).
    function clearReport() {
        clearInterval(pollTimer);
        pollTimer = null;
        pollFailures = 0;
        activeJobId = null;
        currentResult = null;
        currentConfig = null;
        selectedCell = 0;
        purgeCharts();
        $('simTiles').innerHTML = '';
        $('simSweep').innerHTML = '';
        $('simDataInfo').textContent = '';
        $('simDDSub').textContent = '';
        $('simProgress').style.width = '0%';
        $('simStatus').className = 'sim-status';
        $('simStatus').textContent = '';
        $('simExport').disabled = true;
        $('simRun').disabled = false;
        $('simCancel').disabled = true;
    }

    // -------- rendering --------
    const fmt = (v, d = 0) => (v == null || isNaN(v)) ? '—' :
        v.toLocaleString(undefined, { maximumFractionDigits: d, minimumFractionDigits: d });

    function tiles(cell) {
        const s = cell.stats;
        const items = [
            ['Exp PnL / day', `$${fmt(s.mean, 0)}`,
                'Average simulated profit or loss per trading day across all paths that entered. Positive is good; a small negative number means this configuration is expected to lose money on average.'],
            ['Win rate', `${fmt(s.win_rate * 100, 1)}%`,
                'Share of entered paths that finished with a positive PnL after the exit fills. Tells you how often the strategy is right, not how big the wins are.'],
            ['CVaR 1%', `$${fmt(s.cvar1, 0)}`,
                'Conditional Value-at-Risk: the average of the worst 1% of days. A big negative number here means the worst tail days are severe — a good companion to the mean.'],
            ['Worst day', `$${fmt(s.worst_day, 0)}`,
                'The single worst simulated day PnL across all paths. One extreme outlier — compare it to CVaR to see whether it is a one-off or a persistent tail.'],
            ['Max DD p95', `$${fmt(cell.dd.p95, 0)}`,
                '95th percentile of the biggest peak-to-trough equity drawdown seen within a single day. 95% of days draw down less than this; a large number means a rough intraday ride even when the day closes green.'],
            ['Ruin prob', `${fmt(cell.ruin_prob * 100, 2)}%`,
                'Probability that a 60-day sequence of these days ever draws down past your equity × ruin threshold. It is the chance the account hits the floor — the number that most defines survival.'],
            ['Never entered', `${fmt(s.never_entered_pct * 100, 1)}%`,
                'Share of paths where the entry conditions never fired, so no trade was taken. High means the conditions are too strict — few days qualify.'],
            ['Paths', fmt(s.n),
                'Total number of simulated paths this cell ran. Larger is more statistically stable; the confidence in every other tile scales with this.'],
        ];
        $('simTiles').innerHTML = items.map(([k, v, tip]) =>
            `<div class="sim-tile"><div class="v">${v}</div>` +
            `<div class="k">${k}<span class="help" tabindex="0" aria-label="${k}">?` +
            `<span class="tooltip" role="tooltip">${tip}</span></span></div></div>`).join('');
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

    const RTH_START_MIN = 570;   // 09:30 ET — mirrors sim_engine / sim_data

    function _bar_seconds(bar_size) {
        const n = parseFloat(bar_size.replace(/[a-z]+$/i, ''));
        return /m$/i.test(bar_size) ? n * 60 : n;
    }

    function _hm(min) {
        const h = String(Math.floor(min / 60)).padStart(2, '0');
        const m = String(min % 60).padStart(2, '0');
        return `${h}:${m}`;
    }

    // X data (bar index) -> minute-of-day, so "bar 40" reads as a clock time on the fan.
    function _minute_of_day(idx, bar_secs) {
        return RTH_START_MIN + Math.round(idx * bar_secs / 60);
    }

    function _clock_ticks(x) {
        const lo = Math.min(...x), hi = Math.max(...x);
        const ticks = [];
        // Hourly on-the-hour marks (10:00..16:00), then anchor the session start (09:30).
        for (let m = Math.ceil(RTH_START_MIN / 60) * 60; m <= 960; m += 60) {
            if (m >= lo - 1 && m <= hi + 1) ticks.push({ val: m, text: _hm(m) });
        }
        if (RTH_START_MIN >= lo - 1 && RTH_START_MIN <= hi + 1
                && !ticks.some(t => t.val === RTH_START_MIN)) {
            ticks.unshift({ val: RTH_START_MIN, text: _hm(RTH_START_MIN) });
        }
        if (ticks.length < 2) {                            // degenerate span: space evenly
            const step = Math.max(60, Math.round((hi - lo) / 5 / 60) * 60);
            for (let m = Math.ceil(lo / step) * step; m <= hi; m += step)
                ticks.push({ val: m, text: _hm(m) });
        }
        return ticks;
    }

    // Shared Plotly styling for every sim chart (was chart-local; the SPX fan needs it too).
    const SIM_DARK = { paper_bgcolor: 'rgba(0,0,0,0)', plot_bgcolor: 'rgba(0,0,0,0)',
                       font: { color: '#c9cdd4' } };
    const SIM_MARGIN = { l: 64, r: 20, t: 8, b: 46 };
    const simAxis = (label) => ({ title: { text: label, font: { size: 11, color: '#9aa0aa' } },
                                  gridcolor: 'rgba(255,255,255,0.06)' });

    function charts(cell, meta) {
        const stats = cell.stats || {};
        const bar_secs = _bar_seconds((meta && meta.bar_size) || '5m');

        // Day-PnL histogram — mirror the fan/max-DD guards: a cell with zero entered paths
        // has a degenerate/empty binning, so skip the chart instead of plotting nothing.
        if (stats.entered > 0 && cell.hist && cell.hist.edges && cell.hist.edges.length > 1) {
            Plotly.newPlot('simHist', [{
                type: 'bar', x: cell.hist.edges.slice(1).map((e, i) => (e + cell.hist.edges[i]) / 2),
                y: cell.hist.counts, marker: { color: '#4a89dc' }, name: 'Paths',
            }], Object.assign({ bargap: 0.02, showlegend: false,
                    xaxis: simAxis('Day PnL ($)'), yaxis: simAxis('Number of paths (binned)'),
                    margin: SIM_MARGIN }, SIM_DARK),
                { responsive: true, displayModeBar: false });
        }

        // MTM quantile fan — X mapped to clock time; percentiles named and legended.
        const f = cell.fan;
        if (f && f.minutes && f.minutes.length) {
            const x = f.minutes.map(i => _minute_of_day(i, bar_secs));
            const ticks = _clock_ticks(x);
            Plotly.newPlot('simFan', [
                { x, y: f.q95, mode: 'lines', line: { width: 1, color: '#3f8f5f' }, name: '95th pct' },
                { x, y: f.q75, mode: 'lines', line: { width: 1, color: '#3f8f5f' }, name: '75th pct' },
                { x, y: f.q50, mode: 'lines', line: { width: 2, color: '#e8c15a' }, name: 'Median' },
                { x, y: f.q25, mode: 'lines', line: { width: 1, color: '#e06c60' }, name: '25th pct' },
                { x, y: f.q05, mode: 'lines', line: { width: 1, color: '#e06c60' },
                  fill: 'tonexty', fillcolor: 'rgba(224,108,96,0.12)', name: '5th pct' },
            ], Object.assign({
                    showlegend: true,
                    legend: { orientation: 'h', y: -0.16, x: 0.5, xanchor: 'center',
                              font: { size: 10, color: '#c9cdd4' } },
                    xaxis: Object.assign(simAxis('Time of day (ET)'), {
                        tickvals: ticks.map(t => t.val), ticktext: ticks.map(t => t.text),
                        range: [Math.min(...x), Math.max(...x)] }),
                    yaxis: Object.assign(simAxis('Spread PnL ($)'), {
                        zeroline: true, zerolinecolor: 'rgba(255,255,255,0.18)', zerolinewidth: 1 }),
                    margin: SIM_MARGIN },
                SIM_DARK), { responsive: true, displayModeBar: false });
        }

        // Bootstrap max-DD distribution
        const ddh = cell.dd_hist || {};
        if (ddh.max_dd && ddh.max_dd.length) {
            const length = (meta && meta.bootstrap_len) || 60;
            $('simDDSub').textContent =
                `${length}-d curves · ruin prob ${fmt(cell.ruin_prob * 100, 2)}%`;
            Plotly.newPlot('simDD', [{
                type: 'histogram', x: ddh.max_dd, marker: { color: '#8a6fc8' }, nbinsx: 40,
                name: 'Paths',
            }], Object.assign({ showlegend: false,
                    xaxis: simAxis(`Max drawdown ($ over ${length}d curves)`),
                    yaxis: simAxis('Count of bootstrap runs'), margin: SIM_MARGIN },
                SIM_DARK), { responsive: true, displayModeBar: false });
        }
    }

    // Percentile -> line color: red shades below the median, gold at 50, green above —
    // the same cold/hot semantics as the MTM fan's edge lines.
    function _fanColor(p) {
        const lerp = (a, b, t) => [0, 1, 2].map(i => Math.round(a[i] + (b[i] - a[i]) * t));
        const red = [224, 108, 96], gold = [232, 193, 90], green = [63, 143, 95];
        const rgb = p === 50 ? gold : p < 50 ? lerp(red, gold, p / 50)
                                             : lerp(gold, green, (p - 50) / 45);
        return `rgb(${rgb[0]},${rgb[1]},${rgb[2]})`;
    }

    // Top-of-report fan: the simulated SPX index itself at every 5th percentile (p0..p95).
    // A property of the market simulation — spot dynamics ignore the sweep cell — so it is
    // rendered once per run from the result root, not per selected cell.
    function spotFanChart(meta) {
        const sf = currentResult && currentResult.spx_fan;
        if (!sf || !sf.minutes || !sf.minutes.length || !sf.values || !sf.values.length) return;
        const bar_secs = _bar_seconds((meta && meta.bar_size) || '5m');
        const x = sf.minutes.map(i => _minute_of_day(i, bar_secs));
        const ticks = _clock_ticks(x);
        Plotly.newPlot('simSpotFan', sf.quantiles.map((q, i) => ({
            x, y: sf.values[i], mode: 'lines',
            line: { width: q === 50 ? 2 : 1, color: _fanColor(q) }, name: `p${q}`,
        })), Object.assign({
                showlegend: true,
                legend: { orientation: 'h', y: -0.18, x: 0.5, xanchor: 'center',
                          font: { size: 9, color: '#c9cdd4' } },
                xaxis: Object.assign(simAxis('Time of day (ET)'), {
                    tickvals: ticks.map(t => t.val), ticktext: ticks.map(t => t.text),
                    range: [Math.min(...x), Math.max(...x)] }),
                yaxis: simAxis('SPX level'),
                margin: SIM_MARGIN },
            SIM_DARK), { responsive: true, displayModeBar: false });
    }

    function render() {
        if (!currentResult || !currentResult.cells.length) return;
        const cell = currentResult.cells[selectedCell];
        tiles(cell);
        sweepTable();
        charts(cell, currentResult.meta);
        spotFanChart(currentResult.meta);
        const m = currentResult.meta;
        $('simDataInfo').textContent =
            `source: ${m.source} · ${m.bar_size} · ${m.steps_per_day} bars/day · ` +
            `garch ${m.garch.converged ? 'fitted' : 'PRESET'}\n` +
            [...(m.garch_warnings || []), ...(m.data_warnings || [])].join('\n');
        $('simExport').disabled = false;
    }

    function glossary() {
        // Reuse the live page's help text so the exported JSON is self-explanatory to an
        // AI agent (or human) that never sees the UI. Read at export time so the notes
        // can never drift from what the page actually says.
        const g = { tiles: {}, charts: {}, fields: {} };
        document.querySelectorAll('.sim-tile').forEach(t => {
            const label = t.querySelector('.k'), tip = t.querySelector('.tooltip');
            if (label && tip) g.tiles[label.childNodes[0].textContent.trim()] =
                tip.textContent.trim();
        });
        document.querySelectorAll('.sim-chart-block').forEach(b => {
            const head = b.querySelector('.sim-chart-head'), tip = b.querySelector('.tooltip');
            if (head && tip) g.charts[head.childNodes[0].textContent.trim()] =
                tip.textContent.trim();
        });
        g.fields = {
            run_config: 'The exact run-form values used for this simulation (captured at run time).',
            meta: 'Run context: strategy, data source, bar size, GARCH fit (with warnings), smile, dials, seed.',
            spx_fan: 'Simulated SPX price quantiles per bar: minutes are minute-of-day offsets from RTH open, quantiles are percentiles p0..p95, values[i] is the price curve for quantiles[i].',
            cells: 'One entry per (SL ×, k) sweep configuration; the webpage charts/tiles reflect the selected cell.',
            sl_multiplier: 'Stop-loss multiplier applied to the spread credit; "inf" means hold to expiry.',
            k: 'Short-strike distance in standard deviations of the simulated day (dynamic-k mode).',
            stats: 'Per-cell day-PnL distribution: mean/median/std, win rate, CVaR 5%/1%, worst day, path counts.',
            breakdown: 'Exit-reason counts: expired, stop, take_profit, never (entry never fired).',
            hist: 'Day-PnL histogram exactly as plotted: bin edges ($) and path counts.',
            dd: 'Intraday max-drawdown stats across entered paths (mean / p95 / worst).',
            dd_hist: 'Bootstrap resample: per-sequence max drawdowns, their mean/p95 and the ruin probability.',
            fan: 'MTM quantile fan of the open spread through the day (entered paths).',
        };
        return g;
    }

    // Full-page report as one self-contained JSON: everything the page visualizes plus
    // the run config and a glossary, sized for an AI agent to analyze later.
    function exportReport() {
        if (!currentResult) return;
        const report = {
            schema_version: 1,
            report_kind: 'spx_0dte_sim',
            exported_at: new Date().toISOString(),
            run_config: currentConfig || {},
            meta: currentResult.meta || {},
            spx_fan: currentResult.spx_fan || null,
            cells: currentResult.cells || [],
            glossary: glossary(),
        };
        const a = document.createElement('a');
        a.href = URL.createObjectURL(
            new Blob([JSON.stringify(report, null, 2)], { type: 'application/json' }));
        a.download = 'sim_report.json';
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
        try {
            const r = await fetch('/api/sim/smile/capture', { method: 'POST' });
            const body = await r.json().catch(() => ({}));
            $('simSmileInfo').textContent = r.ok
                ? `smile: captured (${body.points} pts)` : `capture failed: ${body.detail || r.status}`;
        } catch (err) {
            // A network error must show inline, not surface as an unhandled rejection.
            $('simSmileInfo').textContent = `capture failed: ${err.message || err}`;
        }
    }

    function init() {
        $('simRun').addEventListener('click', run);
        $('simCancel').addEventListener('click', cancel);
        $('simClear').addEventListener('click', clearReport);
        $('simExport').addEventListener('click', exportReport);
        $('simCaptureSmile').addEventListener('click', captureSmile);
    }

    document.addEventListener('DOMContentLoaded', init);
    window.SimTab = { init, onShow };
})();
