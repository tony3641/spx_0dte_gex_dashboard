    // Chart setup
    // ======================================================================
    const CHART_BG = '#111827';
    const GRID_COLOR = '#1e293b';
    const TEXT_COLOR = '#94a3b8';

    // ======================================================================
    // Responsive helpers
    // ======================================================================
    function isMobile() { return window.innerWidth < 600; }

    function mobilePriceMargin() {
        return isMobile() ? { t: 28, r: 50, b: 32, l: 40 } : { t: 36, r: 80, b: 40, l: 60 };
    }
    function mobileGexMargin() {
        return isMobile() ? { t: 28, r: 30, b: 32, l: 40 } : { t: 36, r: 60, b: 40, l: 60 };
    }
    function mobileSmileMargin() {
        return isMobile()
            ? { t: 20, r: 40, b: 20, l: 40 }
            : { t: 28, r: 60, b: 30, l: 60 };
    }
    function mobileAxisFontSize() { return isMobile() ? 9 : 11; }

    const priceLayout = {
        paper_bgcolor: CHART_BG,
        plot_bgcolor: CHART_BG,
        margin: { t: 36, r: 80, b: 40, l: 60 },
        xaxis: {
            color: TEXT_COLOR,
            gridcolor: GRID_COLOR,
            tickformat: '%H:%M',
            type: 'date',
            rangeslider: { visible: false },
        },
        yaxis: {
            color: TEXT_COLOR,
            gridcolor: GRID_COLOR,
            side: 'right',
            tickformat: ',.2f',
        },
        showlegend: false,
        shapes: [],
        annotations: [],
    };

    const gexLayout = {
        paper_bgcolor: CHART_BG,
        plot_bgcolor: CHART_BG,
        margin: { t: 36, r: 60, b: 40, l: 60 },
        barmode: 'relative',
        xaxis: {
            color: TEXT_COLOR,
            gridcolor: GRID_COLOR,
            title: { text: 'Strike', font: { color: TEXT_COLOR, size: 11 } },
        },
        yaxis: {
            color: TEXT_COLOR,
            gridcolor: GRID_COLOR,
            title: { text: 'GEX ($)', font: { color: TEXT_COLOR, size: 11 } },
        },
        showlegend: true,
        legend: {
            x: 0.01, y: 0.99,
            font: { color: TEXT_COLOR, size: 11 },
            bgcolor: 'rgba(0,0,0,0)',
        },
        shapes: [],
        annotations: [],
    };

    function initPriceChart() {
        Plotly.newPlot('priceChart', [{
            x: [],
            open: [],
            high: [],
            low: [],
            close: [],
            type: 'candlestick',
            name: 'SPX',
            increasing: { line: { color: '#22c55e' }, fillcolor: '#22c55e' },
            decreasing: { line: { color: '#ef4444' }, fillcolor: '#ef4444' },
        }], { ...priceLayout, margin: mobilePriceMargin() }, {
            responsive: true,
            displayModeBar: false,
        });
        state.priceChartReady = true;
    }

    function initGexChart() {
        const fs = mobileAxisFontSize();
        const layout = {
            ...gexLayout,
            margin: mobileGexMargin(),
            xaxis: { ...gexLayout.xaxis, title: { text: 'Strike', font: { color: TEXT_COLOR, size: fs } } },
            yaxis: { ...gexLayout.yaxis, title: { text: 'GEX ($)', font: { color: TEXT_COLOR, size: fs } } },
            legend: { ...gexLayout.legend, font: { color: TEXT_COLOR, size: fs } },
        };
        Plotly.newPlot('gexChart', [
            {
                x: [], y: [],
                type: 'bar',
                name: 'Call GEX',
                marker: { color: '#22c55e80' },
            },
            {
                x: [], y: [],
                type: 'bar',
                name: 'Put GEX',
                marker: { color: '#ef444480' },
            },
            {
                x: [], y: [],
                type: 'scatter',
                mode: 'lines+markers',
                name: 'Net GEX',
                line: { color: '#facc15', width: 1.5 },
                marker: { color: '#facc15', size: 3 },
            },
        ], layout, {
            responsive: true,
            displayModeBar: false,
        });
        state.gexChartReady = true;
    }

    function initSmileChart() {
        const fs = mobileAxisFontSize();
        const m = mobileSmileMargin();
        const layout = {
            paper_bgcolor: CHART_BG,
            plot_bgcolor: CHART_BG,
            margin: m,
            showlegend: true,
            legend: {
                x: 0.01, y: 1.0,
                font: { color: TEXT_COLOR, size: fs },
                bgcolor: 'rgba(0,0,0,0)',
                orientation: 'h',
            },
            grid: { rows: 2, columns: 1, subplots: [['xy'], ['x2y3']], roworder: 'top to bottom', ygap: 0.12 },
            // Top subplot: Calls
            xaxis:  { color: TEXT_COLOR, gridcolor: GRID_COLOR, showticklabels: false, matches: 'x2' },
            yaxis:  { color: TEXT_COLOR, gridcolor: GRID_COLOR, title: { text: 'Call IV %', font: { color: '#4ade80', size: fs } }, side: 'left' },
            yaxis2: { color: TEXT_COLOR, gridcolor: 'rgba(0,0,0,0)', title: { text: 'Efficiency', font: { color: '#facc15', size: fs } }, side: 'right', overlaying: 'y', showgrid: false },
            // Bottom subplot: Puts
            xaxis2: { color: TEXT_COLOR, gridcolor: GRID_COLOR, title: { text: 'Strike', font: { color: TEXT_COLOR, size: fs } } },
            yaxis3: { color: TEXT_COLOR, gridcolor: GRID_COLOR, title: { text: 'Put IV %', font: { color: '#f87171', size: fs } }, side: 'left' },
            yaxis4: { color: TEXT_COLOR, gridcolor: 'rgba(0,0,0,0)', title: { text: 'Efficiency', font: { color: '#facc15', size: fs } }, side: 'right', overlaying: 'y3', showgrid: false },
            annotations: [
                { text: 'CALLS', xref: 'paper', yref: 'paper', x: 0.5, y: 1.01, showarrow: false, font: { color: '#4ade80', size: 11, weight: 'bold' } },
                { text: 'PUTS',  xref: 'paper', yref: 'paper', x: 0.5, y: 0.46, showarrow: false, font: { color: '#f87171', size: 11, weight: 'bold' } },
            ],
            shapes: [],
        };
        // 4 traces:  call IV, call eff, put IV, put eff
        Plotly.newPlot('smileChart', [
            { x: [], y: [], type: 'scatter', mode: 'lines', name: 'Call IV', line: { color: '#4ade80', width: 2 }, xaxis: 'x', yaxis: 'y' },
            { x: [], y: [], type: 'scatter', mode: 'lines', name: 'Call Eff', line: { color: '#facc15', width: 1.5, dash: 'dot' }, xaxis: 'x', yaxis: 'y2' },
            { x: [], y: [], type: 'scatter', mode: 'lines', name: 'Put IV', line: { color: '#f87171', width: 2 }, xaxis: 'x2', yaxis: 'y3' },
            { x: [], y: [], type: 'scatter', mode: 'lines', name: 'Put Eff', line: { color: '#facc15', width: 1.5, dash: 'dot' }, xaxis: 'x2', yaxis: 'y4' },
        ], layout, { responsive: true, displayModeBar: false });
        state.smileChartReady = true;
    }

    // ======================================================================
    // Chart update functions
    // ======================================================================
    function updatePriceChart() {
        if (!state.priceChartReady || state.bars.length === 0) return;

        const times  = state.bars.map(b => b.time);
        const opens  = state.bars.map(b => b.open);
        const highs  = state.bars.map(b => b.high);
        const lows   = state.bars.map(b => b.low);
        const closes = state.bars.map(b => b.close);

        // Detect session boundary: first bar of the most-recent calendar date
        let sessionStartTime = null;
        if (state.bars.length > 1) {
            const lastDate = state.bars[state.bars.length - 1].time.substring(0, 10);
            for (let i = 0; i < state.bars.length; i++) {
                if (state.bars[i].time.substring(0, 10) === lastDate) {
                    // Only draw the line when there are prev-day bars
                    if (i > 0) sessionStartTime = state.bars[i].time;
                    break;
                }
            }
        }

        // Build horizontal level lines
        const shapes = [];
        const annotations = [];

        // Session-boundary vertical line
        if (sessionStartTime) {
            const sessionDate = sessionStartTime.substring(0, 10);
            shapes.push({
                type: 'line',
                xref: 'x', x0: sessionStartTime, x1: sessionStartTime,
                yref: 'paper', y0: 0, y1: 1,
                line: { color: '#475569', width: 1.5, dash: 'dashdot' },
            });
            annotations.push({
                xref: 'x', x: sessionStartTime,
                yref: 'paper', y: 0.99,
                text: `- ${sessionDate}`,
                showarrow: false,
                font: { color: '#94a3b8', size: 10 },
                xanchor: 'left',
                bgcolor: 'rgba(17,24,39,0.85)',
                borderpad: 3,
            });
        }

        if (state.gex) {
            const levels = [
                { val: state.gex.call_wall, color: '#22c55e', label: 'Call Wall', dash: 'solid' },
                { val: state.gex.put_wall, color: '#ef4444', label: 'Put Wall', dash: 'solid' },
                { val: state.gex.gamma_flip, color: '#eab308', label: 'Gamma Flip', dash: 'dash' },
                { val: state.gex.max_pain, color: '#3b82f6', label: 'Max Pain', dash: 'dash' },
            ];

            for (const lv of levels) {
                if (lv.val == null) continue;
                shapes.push({
                    type: 'line',
                    xref: 'paper', x0: 0, x1: 1,
                    yref: 'y', y0: lv.val, y1: lv.val,
                    line: { color: lv.color, width: 1.5, dash: lv.dash },
                });
                annotations.push({
                    xref: 'paper', x: 1.01,
                    yref: 'y', y: lv.val,
                    text: `${lv.label} ${lv.val}`,
                    showarrow: false,
                    font: { color: lv.color, size: 10 },
                    xanchor: 'left',
                });
            }
        }

        Plotly.react('priceChart', [{
            x: times,
            open: opens,
            high: highs,
            low: lows,
            close: closes,
            type: 'candlestick',
            name: 'SPX',
            increasing: { line: { color: '#22c55e' }, fillcolor: '#22c55e' },
            decreasing: { line: { color: '#ef4444' }, fillcolor: '#ef4444' },
        }], {
            ...priceLayout,
            shapes,
            annotations,
        });
    }

    function appendBar(bar) {
        state.bars.push(bar);
        if (state.bars.length > 7200) {
            state.bars = state.bars.slice(-7200);
        }
        updatePriceChart();
    }

    function updateLastBar(bar) {
        if (state.bars.length === 0) {
            state.bars.push(bar);
        } else {
            // Replace the last bar (same minute)
            const last = state.bars[state.bars.length - 1];
            if (last.time === bar.time) {
                state.bars[state.bars.length - 1] = bar;
            } else {
                state.bars.push(bar);
            }
        }
        updatePriceChart();
    }

    function handleChainProgress(data) {
        const gexOverlay  = document.getElementById('gexLoading');
        const gexTextEl   = document.getElementById('gexLoadingText');
        const gexSubEl    = document.getElementById('gexLoadingSub');
        const smileOverlay = document.getElementById('smileLoading');
        const smileTextEl  = document.getElementById('smileLoadingText');
        const smileSubEl   = document.getElementById('smileLoadingSub');

        if (data.phase === 'done') {
            gexOverlay.classList.add('hidden');
            smileOverlay.classList.add('hidden');
            return;
        }

        // Only show during the very first fetch - suppress spinner for recurring updates
        if (state.gex) return;

        gexOverlay.classList.remove('hidden');
        smileOverlay.classList.remove('hidden');

        const phaseText = {
            starting:   'Fetching option chain-',
            qualifying: 'Qualifying contracts-',
            fetching:   'Streaming market data-',
            computing:  'Computing GEX-',
        };
        const text = phaseText[data.phase] || 'Fetching option chain-';
        gexTextEl.textContent = text;
        smileTextEl.textContent = text;

        if (data.phase === 'fetching') {
            const subText = `Batch ${data.batch} of ${data.total_batches}`;
            gexSubEl.textContent = subText;
            smileSubEl.textContent = subText;
        } else {
            gexSubEl.textContent = '';
            smileSubEl.textContent = '';
        }
    }

    function updateGexChart() {
        if (!state.gexChartReady) return;

        // Pick data source based on gex mode
        const gexData = state.gexMode === 'monthly' ? state.monthlyGex : state.gex;
        if (!gexData || !gexData.gex_bars) return;

        const bars = gexData.gex_bars;
        const strikes = bars.map(b => b.strike);
        const callGex = bars.map(b => b.call_gex);
        const putGex = bars.map(b => b.put_gex);
        const netGexPerBar = bars.map(b => b.net_gex);

        // Vertical line at current spot
        const shapes = [];
        const annotations = [];

        // Use latest currentSpot (updated via status msgs) so the line moves in
        // real-time as ES moves, rather than waiting for the next chain fetch.
        const spotForLine = state.currentSpot > 0 ? state.currentSpot : (gexData.spot_price || 0);
        const spotLabel = state.esDerived
            ? `ES derived SPX: ${spotForLine}`
            : `SPX: ${spotForLine}`;

        if (spotForLine > 0) {
            shapes.push({
                type: 'line',
                xref: 'x', x0: spotForLine, x1: spotForLine,
                yref: 'paper', y0: 0, y1: 1,
                line: { color: '#f8fafc', width: 2, dash: 'dot' },
            });
            annotations.push({
                x: spotForLine,
                yref: 'paper', y: 1.02,
                text: spotLabel,
                showarrow: false,
                font: { color: state.esDerived ? '#facc15' : '#f8fafc', size: 10 },
            });
        }

        // Mark key strikes on GEX chart
        const keyLevels = [
            { val: gexData.call_wall, color: '#22c55e', label: 'CW' },
            { val: gexData.put_wall, color: '#ef4444', label: 'PW' },
            { val: gexData.gamma_flip, color: '#eab308', label: 'GF' },
            { val: gexData.max_pain, color: '#3b82f6', label: 'MP' },
        ];

        for (const lv of keyLevels) {
            if (lv.val == null) continue;
            shapes.push({
                type: 'line',
                xref: 'x', x0: lv.val, x1: lv.val,
                yref: 'paper', y0: 0, y1: 1,
                line: { color: lv.color, width: 1.5, dash: 'dash' },
            });
            annotations.push({
                x: lv.val,
                yref: 'paper', y: -0.06,
                text: lv.label,
                showarrow: false,
                font: { color: lv.color, size: 10, },
            });
        }

        // Net GEX + regime annotation in top-right of chart
        const netGex = gexData.total_net_gex;
        if (netGex != null) {
            const isPositive = netGex >= 0;
            const regimeLabel = isPositive ? '- CONVERGING' : '- DIVERGING';
            const regimeColor = isPositive ? '#4ade80' : '#f87171';
            const callOI = gexData.total_call_oi;
            const putOI = gexData.total_put_oi;
            const callOIStr = (callOI != null && callOI > 0) ? callOI.toLocaleString() : '-';
            const putOIStr = (putOI != null && putOI > 0) ? putOI.toLocaleString() : '-';
            
            // P/C OI Ratio
            let pcRatioStr = '-';
            let pcRatioColor = '#94a3b8';
            if (callOI > 0) {
                const pcRatio = putOI / callOI;
                pcRatioStr = pcRatio.toFixed(2);
                pcRatioColor = pcRatio <= 1 ? '#4ade80' : '#f87171';
            }
            
            // GEX Skew %
            let gexSkewStr = '-';
            let gexSkewColor = '#94a3b8';
            const totalCallG = gexData.total_call_gex || 0;
            const totalPutG = Math.abs(gexData.total_put_gex || 0);
            const totalGross = totalCallG + totalPutG;
            if (totalGross > 0) {
                const pct = (totalCallG / totalGross * 100);
                gexSkewStr = pct.toFixed(0) + '%';
                gexSkewColor = pct >= 50 ? '#4ade80' : '#f87171';
            }
            
            annotations.push({
                xref: 'paper', x: 0.98,
                yref: 'paper', y: 0.99,
                text: `Net GEX: <b>${fmtGex(netGex)}</b>   <span style="color:${regimeColor}">${regimeLabel}</span><br>Call OI/Put OI: <span style="color:#4ade80"><b>${callOIStr}</b></span>/<span style="color:#f87171"><b>${putOIStr}</b></span><br>P/C OI: <span style="color:${pcRatioColor}"><b>${pcRatioStr}</b></span>   Call GEX%: <span style="color:${gexSkewColor}"><b>${gexSkewStr}</b></span>`,
                showarrow: false,
                font: { color: '#e2e8f0', size: 12 },
                xanchor: 'right',
                yanchor: 'top',
                bgcolor: '#1a202c',
                bordercolor: isPositive ? '#16a34a' : '#dc2626',
                borderwidth: 2,
                borderpad: 8,
                borderradius: 4,
            });
        }

        // Calculate common range from all smile data if available
        let commonRange = null;
        if (gexData && gexData.smile_data && gexData.smile_data.length > 0) {
            const smileStrikes = gexData.smile_data.map(d => d.strike).filter(s => s != null);
            if (smileStrikes.length > 0) {
                const minSmile = Math.min(...smileStrikes);
                const maxSmile = Math.max(...smileStrikes);
                // Add 1% padding
                const range = maxSmile - minSmile;
                commonRange = [minSmile - range * 0.01, maxSmile + range * 0.01];
            }
        }

        Plotly.react('gexChart', [
            {
                x: strikes,
                y: callGex,
                type: 'bar',
                name: 'Call GEX',
                marker: { color: strikes.map(() => '#22c55e80') },
                customdata: bars.map(b => [fmtGex(b.call_gex), (b.call_oi ?? 0).toLocaleString(), (b.call_vol ?? 0).toLocaleString()]),
                hovertemplate: '<b>Strike: %{x}</b><br>Call GEX: %{customdata[0]}<br>Call OI: %{customdata[1]}<br>Call Vol: %{customdata[2]}<extra></extra>',
            },
            {
                x: strikes,
                y: putGex,
                type: 'bar',
                name: 'Put GEX',
                marker: { color: strikes.map(() => '#ef444480') },
                customdata: bars.map(b => [fmtGex(b.put_gex), (b.put_oi ?? 0).toLocaleString(), (b.put_vol ?? 0).toLocaleString()]),
                hovertemplate: '<b>Strike: %{x}</b><br>Put GEX: %{customdata[0]}<br>Put OI: %{customdata[1]}<br>Put Vol: %{customdata[2]}<extra></extra>',
            },
            {
                x: strikes,
                y: netGexPerBar,
                type: 'scatter',
                mode: 'lines+markers',
                name: 'Net GEX',
                line: { color: '#facc15', width: 1.5 },
                marker: { color: '#facc15', size: 3 },
                customdata: bars.map(b => [fmtGex(b.net_gex)]),
                hovertemplate: '<b>Strike: %{x}</b><br>Net GEX: %{customdata[0]}<extra></extra>',
            },
        ], {
            ...gexLayout,
            shapes,
            annotations,
            xaxis: { ...gexLayout.xaxis, range: commonRange },
        });
    }

    function updateSmileChart() {
        if (!state.smileChartReady) return;

        // Pick data source based on gex mode
        const gexData = state.gexMode === 'monthly' ? state.monthlyGex : state.gex;
        if (!gexData || !gexData.smile_data) return;

        const sd = gexData.smile_data;
        if (sd.length === 0) return;

        // Separate call and put data (filter nulls)
        const callStrikes = [], callIV = [], callEff = [], callCustom = [];
        const putStrikes  = [], putIV  = [], putEff  = [], putCustom = [];

        for (const d of sd) {
            if (d.call_iv != null) {
                callStrikes.push(d.strike);
                callIV.push(d.call_iv);
                callEff.push(d.call_efficiency);
                callCustom.push([
                    d.call_delta != null ? d.call_delta.toFixed(4) : '-',
                    d.call_charm != null ? d.call_charm.toFixed(6) : '-',
                    d.call_efficiency != null ? d.call_efficiency.toFixed(4) : '-',
                ]);
            }
            if (d.put_iv != null) {
                putStrikes.push(d.strike);
                putIV.push(d.put_iv);
                putEff.push(d.put_efficiency);
                putCustom.push([
                    d.put_delta != null ? d.put_delta.toFixed(4) : '-',
                    d.put_charm != null ? d.put_charm.toFixed(6) : '-',
                    d.put_efficiency != null ? d.put_efficiency.toFixed(4) : '-',
                ]);
            }
        }

        // Calculate common range from all smile data
        let commonRange = null;
        const smileStrikes = sd.map(d => d.strike).filter(s => s != null);
        if (smileStrikes.length > 0) {
            const minSmile = Math.min(...smileStrikes);
            const maxSmile = Math.max(...smileStrikes);
            // Add 1% padding
            const range = maxSmile - minSmile;
            commonRange = [minSmile - range * 0.01, maxSmile + range * 0.01];
        }

        // Spot + key level vertical lines for both subplots
        const shapes = [];
        const spotForLine = state.currentSpot > 0 ? state.currentSpot : (gexData.spot_price || 0);
        const addVertical = (xref, val, color, dash) => {
            if (val == null || val <= 0) return;
            shapes.push({
                type: 'line', xref: xref, x0: val, x1: val,
                yref: 'paper', y0: 0, y1: 1,
                line: { color: color, width: 1, dash: dash },
            });
        };
        // Draw on both x-axes
        for (const xr of ['x', 'x2']) {
            addVertical(xr, spotForLine, '#f8fafc', 'dot');
            addVertical(xr, gexData.call_wall, '#22c55e', 'dash');
            addVertical(xr, gexData.put_wall, '#ef4444', 'dash');
            addVertical(xr, gexData.gamma_flip, '#eab308', 'dash');
        }

        // Preserve the CALLS / PUTS subtitle annotations
        const annotations = [
            { text: 'CALLS', xref: 'paper', yref: 'paper', x: 0.5, y: 1.01, showarrow: false, font: { color: '#4ade80', size: 11 } },
            { text: 'PUTS',  xref: 'paper', yref: 'paper', x: 0.5, y: 0.46, showarrow: false, font: { color: '#f87171', size: 11 } },
        ];

        const hoverCall = '<b>Strike: %{x}</b><br>Call IV: %{y:.1f}%<br>Delta: %{customdata[0]}<br>Charm: %{customdata[1]}<br>Efficiency: %{customdata[2]}<extra></extra>';
        const hoverCallEff = '<b>Strike: %{x}</b><br>Efficiency: %{y:.4f}<extra></extra>';
        const hoverPut = '<b>Strike: %{x}</b><br>Put IV: %{y:.1f}%<br>Delta: %{customdata[0]}<br>Charm: %{customdata[1]}<br>Efficiency: %{customdata[2]}<extra></extra>';
        const hoverPutEff = '<b>Strike: %{x}</b><br>Efficiency: %{y:.4f}<extra></extra>';

        Plotly.react('smileChart', [
            { x: callStrikes, y: callIV, type: 'scatter', mode: 'lines', name: 'Call IV',
              line: { color: '#4ade80', width: 2 }, xaxis: 'x', yaxis: 'y',
              customdata: callCustom, hovertemplate: hoverCall },
            { x: callStrikes, y: callEff, type: 'scatter', mode: 'lines', name: 'Call Eff',
              line: { color: '#facc15', width: 1.5, dash: 'dot' }, xaxis: 'x', yaxis: 'y2',
              hovertemplate: hoverCallEff },
            { x: putStrikes, y: putIV, type: 'scatter', mode: 'lines', name: 'Put IV',
              line: { color: '#f87171', width: 2 }, xaxis: 'x2', yaxis: 'y3',
              customdata: putCustom, hovertemplate: hoverPut },
            { x: putStrikes, y: putEff, type: 'scatter', mode: 'lines', name: 'Put Eff',
              line: { color: '#facc15', width: 1.5, dash: 'dot' }, xaxis: 'x2', yaxis: 'y4',
              hovertemplate: hoverPutEff },
        ], {
            paper_bgcolor: CHART_BG,
            plot_bgcolor: CHART_BG,
            margin: mobileSmileMargin(),
            showlegend: true,
            legend: {
                x: 0.01, y: 1.0,
                font: { color: TEXT_COLOR, size: mobileAxisFontSize() },
                bgcolor: 'rgba(0,0,0,0)',
                orientation: 'h',
            },
            grid: { rows: 2, columns: 1, subplots: [['xy'], ['x2y3']], roworder: 'top to bottom', ygap: 0.12 },
            xaxis:  { color: TEXT_COLOR, gridcolor: GRID_COLOR, showticklabels: false, matches: 'x2', range: commonRange },
            yaxis:  { color: TEXT_COLOR, gridcolor: GRID_COLOR, title: { text: 'Call IV %', font: { color: '#4ade80', size: mobileAxisFontSize() } }, side: 'left' },
            yaxis2: { color: TEXT_COLOR, gridcolor: 'rgba(0,0,0,0)', title: { text: 'Efficiency', font: { color: '#facc15', size: mobileAxisFontSize() } }, side: 'right', overlaying: 'y', showgrid: false },
            xaxis2: { color: TEXT_COLOR, gridcolor: GRID_COLOR, title: { text: 'Strike', font: { color: TEXT_COLOR, size: mobileAxisFontSize() } }, range: commonRange },
            yaxis3: { color: TEXT_COLOR, gridcolor: GRID_COLOR, title: { text: 'Put IV %', font: { color: '#f87171', size: mobileAxisFontSize() } }, side: 'left' },
            yaxis4: { color: TEXT_COLOR, gridcolor: 'rgba(0,0,0,0)', title: { text: 'Efficiency', font: { color: '#facc15', size: mobileAxisFontSize() } }, side: 'right', overlaying: 'y3', showgrid: false },
            shapes,
            annotations,
        });
    }

    // ======================================================================
    // GEX mode toggle (0DTE ↔ Monthly)
    // ======================================================================
    function setGexMode(mode) {
        if (mode !== '0dte' && mode !== 'monthly') return;
        if (mode === state.gexMode) return;

        state.gexMode = mode;
        updateGexModeToggle();

        // Notify server
        if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(`set_gex_mode:${mode}`);
        }

        // Re-render charts with the active data source
        updateGexChart();
        updateSmileChart();
    }

    function updateGexModeToggle() {
        const btns = document.querySelectorAll('#gexModeToggle .gex-mode-btn');
        btns.forEach(btn => {
            btn.classList.toggle('active', btn.dataset.mode === state.gexMode);
        });

        // Update chart labels
        const gexLabel = document.getElementById('gexChartLabel');
        const smileLabel = document.getElementById('smileChartLabel');
        if (state.gexMode === 'monthly') {
            const expStr = state.monthlyExpiration || '';
            gexLabel.textContent = `Gamma Exposure (GEX) — SPX Monthly${expStr ? ' ' + expStr : ''}`;
            smileLabel.textContent = `IV Smile — SPX Monthly${expStr ? ' ' + expStr : ''}`;
        } else {
            gexLabel.textContent = 'Gamma Exposure (GEX) by Strike';
            smileLabel.textContent = 'IV Smile & Delta-Decay Efficiency';
        }
    }

    function handleMonthlyGexProgress(data) {
        const gexOverlay = document.getElementById('gexLoading');
        const gexTextEl = document.getElementById('gexLoadingText');
        const gexSubEl = document.getElementById('gexLoadingSub');
        const smileOverlay = document.getElementById('smileLoading');
        const smileTextEl = document.getElementById('smileLoadingText');
        const smileSubEl = document.getElementById('smileLoadingSub');

        if (data.phase === 'done') {
            gexOverlay.classList.add('hidden');
            smileOverlay.classList.add('hidden');
            return;
        }

        // Only show spinner if we're in monthly mode and don't have data yet
        if (state.gexMode !== 'monthly') return;
        if (state.monthlyGex) return;

        gexOverlay.classList.remove('hidden');
        smileOverlay.classList.remove('hidden');
        gexTextEl.textContent = 'Fetching monthly option chain\u2026';
        gexSubEl.textContent = '';
        smileTextEl.textContent = 'Fetching monthly option chain\u2026';
        smileSubEl.textContent = '';
    }

    // ======================================================================
