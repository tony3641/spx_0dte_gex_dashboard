// Simulation tab: run form, job polling, Plotly rendering, CSV export.
(function () {
    'use strict';
    let currentResult = null;
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
                pollFailures = 0;
                $('simProgress').style.width = `${Math.round((state.progress || 0) * 100)}%`;
                $('simStatus').textContent = `${state.state} — ${state.message || ''}`;
                if (state.state === 'done') {
                    const r = await fetch(`/api/sim/result/${jobId}`);
                    if (!r.ok) throw new Error(`result HTTP ${r.status}`);
                    currentResult = await r.json();
                    selectedCell = 0;
                    render();
                }
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

    // -------- rendering --------
    const fmt = (v, d = 0) => (v == null || isNaN(v)) ? '—' :
        v.toLocaleString(undefined, { maximumFractionDigits: d, minimumFractionDigits: d });

    function tiles(cell, meta) {
        const s = cell.stats;
        const items = [
            ['Exp PnL / day', `$${fmt(s.mean, 0)}`],
            ['Win rate', `${fmt(s.win_rate * 100, 1)}%`],
            ['CVaR 1%', `$${fmt(s.cvar1, 0)}`],
            ['Worst day', `$${fmt(s.worst_day, 0)}`],
            ['Max DD p95', `$${fmt(cell.dd.p95, 0)}`],
            ['Ruin prob', `${fmt(cell.ruin_prob * 100, 2)}%`],
            ['Never entered', `${fmt(s.never_entered_pct * 100, 1)}%`],
            ['Paths', fmt(s.n)],
        ];
        $('simTiles').innerHTML = items.map(([k, v]) =>
            `<div class="sim-tile"><div class="v">${v}</div><div class="k">${k}</div></div>`).join('');
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

    function charts(cell) {
        const dark = { paper_bgcolor: 'rgba(0,0,0,0)', plot_bgcolor: 'rgba(0,0,0,0)',
                       font: { color: '#c9cdd4' } };
        const stats = cell.stats || {};
        // Day-PnL histogram — mirror the fan/max-DD guards: a cell with zero entered paths
        // has a degenerate/empty binning, so skip the chart instead of plotting nothing.
        if (stats.entered > 0 && cell.hist && cell.hist.edges && cell.hist.edges.length > 1) {
            Plotly.newPlot('simHist', [{
                type: 'bar', x: cell.hist.edges.slice(1).map((e, i) => (e + cell.hist.edges[i]) / 2),
                y: cell.hist.counts, marker: { color: '#4a89dc' },
            }], Object.assign({ title: 'Day PnL distribution ($)', bargap: 0.02 }, dark),
                { responsive: true, displayModeBar: false });
        }
        // MTM quantile fan
        const f = cell.fan;
        if (f && f.minutes && f.minutes.length) {
            const x = f.minutes;
            Plotly.newPlot('simFan', [
                { x, y: f.q95, mode: 'lines', line: { width: 1, color: '#3f8f5f' } },
                { x, y: f.q75, mode: 'lines', line: { width: 1, color: '#3f8f5f' } },
                { x, y: f.q50, mode: 'lines', line: { width: 2, color: '#e8c15a' } },
                { x, y: f.q25, mode: 'lines', line: { width: 1, color: '#e06c60' } },
                { x, y: f.q05, mode: 'lines', line: { width: 1, color: '#e06c60' },
                  fill: 'tonexty', fillcolor: 'rgba(224,108,96,0.12)' },
            ], Object.assign({ title: 'Spread MTM quantile fan ($)', showlegend: false }, dark),
                { responsive: true, displayModeBar: false });
        }
        // Bootstrap max-DD distribution
        const ddh = cell.dd_hist || {};
        if (ddh.max_dd && ddh.max_dd.length) {
            Plotly.newPlot('simDD', [{
                type: 'histogram', x: ddh.max_dd, marker: { color: '#8a6fc8' }, nbinsx: 40,
            }], Object.assign({ title: `Bootstrap max-DD ($ over ${'60'}d curves) — ruin prob ${fmt(cell.ruin_prob * 100, 2)}%` }, dark),
                { responsive: true, displayModeBar: false });
        }
    }

    function render() {
        if (!currentResult || !currentResult.cells.length) return;
        const cell = currentResult.cells[selectedCell];
        tiles(cell, currentResult.meta);
        sweepTable();
        charts(cell);
        const m = currentResult.meta;
        $('simDataInfo').textContent =
            `source: ${m.source} · ${m.bar_size} · ${m.steps_per_day} bars/day · ` +
            `garch ${m.garch.converged ? 'fitted' : 'PRESET'}\n` +
            [...(m.garch_warnings || []), ...(m.data_warnings || [])].join('\n');
        $('simExport').disabled = false;
    }

    function exportCsv() {
        if (!currentResult) return;
        const rows = [['sl_multiplier', 'k', 'mean', 'median', 'std', 'win_rate', 'cvar5',
            'cvar1', 'worst_day', 'dd_mean', 'dd_p95', 'dd_worst', 'ruin_prob',
            'expired', 'stop', 'take_profit', 'never']];
        currentResult.cells.forEach(c => rows.push([
            c.sl_multiplier, c.k, c.stats.mean, c.stats.median, c.stats.std, c.stats.win_rate,
            c.stats.cvar5, c.stats.cvar1, c.stats.worst_day, c.dd.mean, c.dd.p95, c.dd.worst,
            c.ruin_prob, c.breakdown.expired, c.breakdown.stop, c.breakdown.take_profit,
            c.breakdown.never]));
        const csv = rows.map(r => r.join(',')).join('\n');
        const a = document.createElement('a');
        a.href = URL.createObjectURL(new Blob([csv], { type: 'text/csv' }));
        a.download = 'sim_results.csv';
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
        $('simExport').addEventListener('click', exportCsv);
        $('simCaptureSmile').addEventListener('click', captureSmile);
    }

    document.addEventListener('DOMContentLoaded', init);
    window.SimTab = { init, onShow };
})();
