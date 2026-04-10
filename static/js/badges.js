    // Badge updates
    // ======================================================================
    function fmtGex(val) {
        // Format GEX in billions (B) or millions (M) for readability
        if (val == null) return '-';
        const abs = Math.abs(val);
        const sign = val < 0 ? '-' : '+';
        if (abs >= 1e9)  return sign + (abs / 1e9).toFixed(2) + 'B';
        if (abs >= 1e6)  return sign + (abs / 1e6).toFixed(1) + 'M';
        if (abs >= 1e3)  return sign + (abs / 1e3).toFixed(0) + 'K';
        return sign + abs.toFixed(0);
    }

    function updateBadges() {
        const spot = state.bars.length > 0 ? state.bars[state.bars.length - 1].close : null;
        document.getElementById('spotBadge').textContent = spot ? spot.toFixed(2) : '-';

        if (state.gex) {
            document.getElementById('callWallBadge').textContent =
                state.gex.call_wall != null ? state.gex.call_wall.toFixed(0) : '-';
            document.getElementById('putWallBadge').textContent =
                state.gex.put_wall != null ? state.gex.put_wall.toFixed(0) : '-';
            document.getElementById('gammaFlipBadge').textContent =
                state.gex.gamma_flip != null ? state.gex.gamma_flip.toFixed(1) : '-';
            document.getElementById('maxPainBadge').textContent =
                state.gex.max_pain != null ? state.gex.max_pain.toFixed(0) : '-';

            // Net GEX value
            const netGex = state.gex.total_net_gex;
            document.getElementById('netGexBadge').textContent = fmtGex(netGex);

            // MM Hedging regime
            const regimeBadge = document.getElementById('mmRegimeBadge');
            const regimeText  = document.getElementById('mmRegimeText');
            if (netGex != null) {
                regimeBadge.style.display = '';
                if (netGex >= 0) {
                    regimeBadge.className = 'level-badge badge-converging';
                    regimeText.innerHTML  = '- MM: CONVERGING';
                    regimeText.title      = 'Positive GEX: dealers are net long gamma. They sell into rallies and buy dips - market-stabilising.';
                } else {
                    regimeBadge.className = 'level-badge badge-diverging';
                    regimeText.innerHTML  = '- MM: DIVERGING';
                    regimeText.title      = 'Negative GEX: dealers are net short gamma. They chase moves in the same direction - market-amplifying.';
                }
            } else {
                regimeBadge.style.display = 'none';
            }
        }
    }

    function updateStatus(data) {
        // Connection dot
        const dot = document.getElementById('connDot');
        const connText = document.getElementById('connStatus');
        if (data.connected) {
            dot.className = 'dot dot-green';
            connText.textContent = 'IB Connected';
            document.getElementById('loadingOverlay').classList.add('hidden');
        } else {
            dot.className = 'dot dot-red';
            connText.textContent = 'IB Disconnected';
        }

        // Market status
        const ms = document.getElementById('marketStatus');
        const msVal = data.market_status || '-';
        ms.textContent = msVal;
        if (msVal === 'RTH') ms.style.color = '#22c55e';
        else if (msVal === 'GTH' || msVal === 'CURB') ms.style.color = '#eab308';
        else ms.style.color = '#94a3b8';

        // Show/hide Extended Hours badge in order entry row
        const rthBadge = document.getElementById('outsideRthBadge');
        if (rthBadge) rthBadge.style.display = (msVal === 'GTH' || msVal === 'CURB') ? '' : 'none';

        // Expiration
        document.getElementById('expDisplay').textContent = data.expiration || '-';

        // Last GEX update
        if (data.last_chain_update) {
            document.getElementById('lastGexUpdate').textContent = data.last_chain_update;
        }

        // Data mode badge & chart title
        if (data.data_mode) {
            state.dataMode = data.data_mode;
            state.historicalDate = data.historical_date || '';
            updateModeBadge();
        }

        // Update ES-derived spot in real-time (between chain fetches)
        if (data.spot_price > 0) {
            const prevSpot = state.currentSpot;
            const prevDerived = state.esDerived;
            state.currentSpot = data.spot_price;
            state.esDerived = data.es_derived || false;
            // Re-render GEX spot line if spot or regime changed
            if (state.gexChartReady && state.gex &&
                (state.currentSpot !== prevSpot || state.esDerived !== prevDerived)) {
                updateGexChart();
                updateSmileChart();
            }
        }

        // Sync loading overlay with chain_fetching flag
        // (mainly for reconnects where the client missed the starting event)
        if (data.chain_fetching === true && !state.gex) {
            const gexOverlay = document.getElementById('gexLoading');
            const smileOverlay = document.getElementById('smileLoading');
            document.getElementById('gexLoadingText').textContent = 'Fetching option chain-';
            document.getElementById('gexLoadingSub').textContent = '';
            document.getElementById('smileLoadingText').textContent = 'Fetching option chain-';
            document.getElementById('smileLoadingSub').textContent = '';
            gexOverlay.classList.remove('hidden');
            smileOverlay.classList.remove('hidden');
        }
    }

    function promptReconnectIb() {
        const portText = prompt('Enter IB API port number:', '7497');
        if (portText === null) return;
        const port = parseInt(portText.trim(), 10);
        if (!Number.isInteger(port) || port <= 0 || port > 65535) {
            alert('Please enter a valid port number between 1 and 65535.');
            return;
        }
        reconnectIb(port);
    }

    async function reconnectIb(port) {
        try {
            const response = await fetch('/api/reconnect_ib', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ port }),
            });
            if (!response.ok) {
                const errorData = await response.json().catch(() => null);
                const message = errorData?.detail || response.statusText || 'Unknown error';
                throw new Error(message);
            }
            const result = await response.json();
            document.getElementById('overlayMsg').textContent = `Reconnecting IB on port ${result.port}...`;
            document.getElementById('loadingOverlay').classList.remove('hidden');
            alert(`Reconnect request sent for port ${result.port}.`);
        } catch (e) {
            console.error('IB reconnect failed', e);
            alert('IB reconnect failed: ' + e.message);
        }
    }

    function updateModeBadge() {
        const badge = document.getElementById('modeBadge');
        const text  = document.getElementById('modeBadgeText');
        const title = document.getElementById('priceChartTitle');

        if (state.dataMode === 'live') {
            badge.style.display = '';
            badge.className = 'level-badge badge-mode-live';
            text.textContent = '\u25CF LIVE';
            title.textContent = 'SPX Intraday \u2014 Live';
        } else if (state.dataMode === 'historical') {
            badge.style.display = '';
            badge.className = 'level-badge badge-mode-hist';
            text.textContent = '\u25CB HISTORICAL ' + state.historicalDate;
            title.textContent = 'SPX Intraday \u2014 ' + state.historicalDate + ' (last session)';
        } else {
            badge.style.display = 'none';
            title.textContent = 'SPX Intraday';
        }
    }

    // ======================================================================
