    // Option Chain Table
    // ======================================================================
    function handleChainQuotes(data) {
        if (!data || !data.strikes) return;
        const scope = data.scope || 'full';

        if (scope === 'full' && data.strikes.length === 0 && Object.keys(state.chainData).length > 0) {
            console.warn('Ignoring empty full chain_quotes payload; preserving existing chain table data');
            return;
        }

        state.chainMeta = {
            spot_price: data.spot_price,
            annual_vol: data.annual_vol,
            expiration_raw: data.expiration_raw,
            tte_years: data.tte_years,
            sigma_move: data.sigma_move,
            call_wall: data.call_wall,
            put_wall: data.put_wall,
            gamma_flip: data.gamma_flip,
        };
        state.chainLastUpdateMs = data.timestamp_iso ? Date.parse(data.timestamp_iso) : Date.now();
        updateChainUpdateAge();

        // Full payload replaces all rows; stream payload merges to avoid table wipe.
        if (scope === 'full' || Object.keys(state.chainData).length === 0) {
            state.chainData = {};
            for (const row of data.strikes) {
                state.chainData[row.strike] = row;
            }
        } else {
            for (const row of data.strikes) {
                const existing = state.chainData[row.strike] || { strike: row.strike };
                state.chainData[row.strike] = { ...existing, ...row };
            }
        }

        console.debug(`chain_quotes scope=${scope} rows=${data.strikes.length} mergedRows=${Object.keys(state.chainData).length}`);
        renderChainTable();
        if (state.activeTab === 'chain') {
            reportChainViewportCenter(scope === 'full');
        }
        updateStrategyPrices();
    }

    function handleChainTick(data) {
        if (!data || !data.ticks) return;
        state.chainLastUpdateMs = data.timestamp_iso ? Date.parse(data.timestamp_iso) : Date.now();
        updateChainUpdateAge();
        for (const t of data.ticks) {
            const row = state.chainData[t.strike];
            if (!row) continue;
            const side = t.right === 'C' ? 'call' : 'put';
            const fields = {bid: 'bid', ask: 'ask', bid_size: 'bid_size', ask_size: 'ask_size',
                            volume: 'volume', last: 'last', delta: 'delta', gamma: 'gamma', iv: 'iv'};
            for (const [src, dst] of Object.entries(fields)) {
                if (t[src] !== undefined) {
                    const key = `${side}_${dst}`;
                    const fullKey = `${t.strike}_${t.right}_${dst}`;
                    const oldVal = row[key];
                    row[key] = t[src];
                    // Flash animation on cell update
                    const cellId = `chain_${t.strike}_${side}_${dst}`;
                    const cell = document.getElementById(cellId);
                    if (cell && t[src] !== null && oldVal !== t[src]) {
                        cell.textContent = formatChainVal(dst, t[src]);
                        cell.classList.remove('flash-up', 'flash-down');
                        void cell.offsetWidth; // force reflow
                        if (oldVal !== null && oldVal !== undefined) {
                            cell.classList.add(t[src] > oldVal ? 'flash-up' : 'flash-down');
                        }
                    }
                }
            }
        }
        updateStrategyPrices();
    }

    function formatChainVal(field, val) {
        if (val === null || val === undefined) return '-';
        if (field === 'bid' || field === 'ask' || field === 'last') return val.toFixed(2);
        if (field === 'delta') return val.toFixed(3);
        if (field === 'gamma') return val.toFixed(4);
        if (field === 'iv') return val.toFixed(1) + '%';
        if (field === 'bid_size' || field === 'ask_size' || field === 'volume' || field === 'oi')
            return val.toLocaleString();
        return String(val);
    }

    function updateChainUpdateAge() {
        const el = document.getElementById('chainLastUpdateInfo');
        if (!el) return;
        if (!state.chainLastUpdateMs) {
            el.textContent = 'Last update: -';
            return;
        }
        const sec = Math.max(0, Math.floor((Date.now() - state.chainLastUpdateMs) / 1000));
        el.textContent = `Last update: +${sec}s`;
    }

    function hasSelectedLeg(strike, right, action) {
        return state.strategyLegs.some(l => l.strike === strike && l.right === right && l.action === action);
    }

    function refreshSelectionHighlights() {
        document.querySelectorAll('.cell-selected-bid').forEach(el => el.classList.remove('cell-selected-bid'));
        document.querySelectorAll('.cell-selected-ask').forEach(el => el.classList.remove('cell-selected-ask'));
        for (const leg of state.strategyLegs) {
            const side = leg.right === 'C' ? 'call' : 'put';
            const actionSide = leg.action === 'BUY' ? 'ask' : 'bid';
            const cell = document.getElementById(`chain_${leg.strike}_${side}_${actionSide}`);
            if (cell) {
                cell.classList.add(leg.action === 'BUY' ? 'cell-selected-ask' : 'cell-selected-bid');
            }
        }
    }

    function renderChainTable() {
        const tbody = document.getElementById('chainBody');
        const allStrikes = Object.keys(state.chainData).map(Number).sort((a, b) => a - b);
        const spot = state.chainMeta ? state.chainMeta.spot_price : state.currentSpot;
        const annualVol = (state.chainMeta && state.chainMeta.annual_vol) ? state.chainMeta.annual_vol : 0.20;
        const dailyStd = (spot > 0) ? (spot * annualVol / Math.sqrt(252)) : 0;
        const lowerBound = spot > 0 ? (spot - 5 * dailyStd - 60) : -Infinity;
        const upperBound = spot > 0 ? (spot + 5 * dailyStd + 60) : Infinity;
        const strikes = allStrikes.filter(s => s >= lowerBound && s <= upperBound);
        if (strikes.length === 0) {
            tbody.innerHTML = '<tr><td colspan="19" style="text-align:center;color:#475569;padding:40px;">Waiting for option chain data...</td></tr>';
            document.getElementById('chainRangeInfo').textContent = 'Visible range: -';
            return;
        }

        const cw = state.chainMeta ? state.chainMeta.call_wall : null;
        const pw = state.chainMeta ? state.chainMeta.put_wall : null;
        const gf = state.chainMeta ? state.chainMeta.gamma_flip : null;
        const atmStrike = spot > 0 ? strikes.reduce((a, b) => Math.abs(a - spot) < Math.abs(b - spot) ? a : b) : null;
        document.getElementById('chainRangeInfo').textContent =
            `Visible range: ${Math.round(lowerBound)} to ${Math.round(upperBound)} (5sigma +/- 60)`;

        // Build rows
        let html = '';
        for (const strike of strikes) {
            const r = state.chainData[strike];
            const rowClasses = [];
            let tags = '';
            const callItmClass = (spot > 0 && strike < spot) ? 'itm-call' : '';
            const putItmClass = (spot > 0 && strike > spot) ? 'itm-put' : '';
            const sigmaAbs = r.sigma_distance_abs;
            const sigmaSigned = r.sigma_distance_signed;
            const strikeSigmaClass = getStrikeSigmaClass(sigmaAbs);
            const strikeSigmaTitle = buildStrikeSigmaTitle(strike, sigmaSigned, sigmaAbs, spot);
            if (strike === atmStrike) rowClasses.push('row-atm');
            if (cw !== null && strike === cw) { rowClasses.push('row-call-wall'); tags += '<span class="strike-tag strike-tag-cw">CW</span>'; }
            if (pw !== null && strike === pw) { rowClasses.push('row-put-wall'); tags += '<span class="strike-tag strike-tag-pw">PW</span>'; }
            if (gf !== null && Math.abs(strike - gf) < 2.5) { rowClasses.push('row-gamma-flip'); tags += '<span class="strike-tag strike-tag-gf">GF</span>'; }

            html += `<tr class="${rowClasses.join(' ')}" data-strike="${strike}">`;
            // === Call side (right-to-left: IV, Vol, OI, Gamma, Delta, AskSz, Ask, Bid, BidSz) ===
            html += td('call', 'iv', strike, r.call_iv, callItmClass);
            html += td('call', 'volume', strike, r.call_volume, callItmClass);
            html += td('call', 'oi', strike, r.call_oi, callItmClass);
            html += td('call', 'gamma', strike, r.call_gamma, callItmClass);
            html += td('call', 'delta', strike, r.call_delta, callItmClass);
            html += td('call', 'ask_size', strike, r.call_ask_size, callItmClass);
            html += `<td class="cell-ask ${callItmClass} ${hasSelectedLeg(strike, 'C', 'BUY') ? 'cell-selected-ask' : ''}" id="chain_${strike}_call_ask" onclick="addLeg(${strike},'C','BUY')" title="Buy ${strike}C">${fv('ask', r.call_ask)}</td>`;
            html += `<td class="cell-bid ${callItmClass} ${hasSelectedLeg(strike, 'C', 'SELL') ? 'cell-selected-bid' : ''}" id="chain_${strike}_call_bid" onclick="addLeg(${strike},'C','SELL')" title="Sell ${strike}C">${fv('bid', r.call_bid)}</td>`;
            html += td('call', 'bid_size', strike, r.call_bid_size, callItmClass);
            // === Strike center ===
            html += `<td class="strike-col ${strikeSigmaClass}" title="${strikeSigmaTitle}">${strike}${tags}</td>`;
            // === Put side (left-to-right: BidSz, Bid, Ask, AskSz, Delta, Gamma, OI, Vol, IV) ===
            html += td('put', 'bid_size', strike, r.put_bid_size, putItmClass);
            html += `<td class="cell-bid ${putItmClass} ${hasSelectedLeg(strike, 'P', 'SELL') ? 'cell-selected-bid' : ''}" id="chain_${strike}_put_bid" onclick="addLeg(${strike},'P','SELL')" title="Sell ${strike}P">${fv('bid', r.put_bid)}</td>`;
            html += `<td class="cell-ask ${putItmClass} ${hasSelectedLeg(strike, 'P', 'BUY') ? 'cell-selected-ask' : ''}" id="chain_${strike}_put_ask" onclick="addLeg(${strike},'P','BUY')" title="Buy ${strike}P">${fv('ask', r.put_ask)}</td>`;
            html += td('put', 'ask_size', strike, r.put_ask_size, putItmClass);
            html += td('put', 'delta', strike, r.put_delta, putItmClass);
            html += td('put', 'gamma', strike, r.put_gamma, putItmClass);
            html += td('put', 'oi', strike, r.put_oi, putItmClass);
            html += td('put', 'volume', strike, r.put_volume, putItmClass);
            html += td('put', 'iv', strike, r.put_iv, putItmClass);
            html += '</tr>';
        }
        tbody.innerHTML = html;
    }

    function getStrikeSigmaClass(sigmaAbs) {
        if (sigmaAbs === null || sigmaAbs === undefined || !isFinite(sigmaAbs)) return '';
        if (sigmaAbs <= 1) return 'strike-sigma-1';
        if (sigmaAbs <= 2) return 'strike-sigma-2';
        if (sigmaAbs <= 3) return 'strike-sigma-3';
        return '';
    }

    function buildStrikeSigmaTitle(strike, sigmaSigned, sigmaAbs, spot) {
        let signed = sigmaSigned;
        let abs = sigmaAbs;
        if ((signed === null || signed === undefined || !isFinite(signed)) &&
            state.chainMeta && state.chainMeta.sigma_move && state.chainMeta.sigma_move > 0 && spot > 0) {
            signed = (strike - spot) / state.chainMeta.sigma_move;
            abs = Math.abs(signed);
        }
        if (abs === null || abs === undefined || !isFinite(abs)) {
            return `Strike ${strike} (sigma distance unavailable)`;
        }
        const signPrefix = signed >= 0 ? '+' : '';
        const direction = signed >= 0 ? 'above' : 'below';
        return `Strike ${strike}: ${signPrefix}${signed.toFixed(2)} sigma (${abs.toFixed(2)} sigma ${direction} spot ${spot.toFixed(2)})`;
    }

    function td(side, field, strike, val, extraClass = '') {
        return `<td class="${extraClass}" id="chain_${strike}_${side}_${field}">${fv(field, val)}</td>`;
    }
    function fv(field, val) { return formatChainVal(field, val); }

    function scrollToATM() {
        const spot = state.chainMeta ? state.chainMeta.spot_price : state.currentSpot;
        if (spot <= 0) return;
        const wrap = document.getElementById('chainTableWrap');
        const row = wrap.querySelector('tr.row-atm');
        if (row) {
            // Scroll so ATM is roughly centered
            const rowTop = row.offsetTop;
            const wrapH = wrap.clientHeight;
            wrap.scrollTop = rowTop - wrapH / 2 + row.clientHeight / 2;
            reportChainViewportCenter(true);
        }
    }

    function getChainViewportCenterStrike() {
        const wrap = document.getElementById('chainTableWrap');
        if (!wrap) return null;

        const rows = wrap.querySelectorAll('tbody tr[data-strike]');
        if (!rows || rows.length === 0) return null;

        const centerY = wrap.scrollTop + (wrap.clientHeight / 2);
        let bestStrike = null;
        let bestDist = Number.POSITIVE_INFINITY;

        for (const row of rows) {
            const strike = Number(row.getAttribute('data-strike'));
            if (!Number.isFinite(strike)) continue;
            const rowCenterY = row.offsetTop + (row.offsetHeight / 2);
            const dist = Math.abs(rowCenterY - centerY);
            if (dist < bestDist) {
                bestDist = dist;
                bestStrike = strike;
            }
        }

        return Number.isFinite(bestStrike) ? bestStrike : null;
    }

    function flushViewportCenterSend(force = false) {
        if (state.chainViewportPendingStrike === null) return;
        if (!(ws && ws.readyState === WebSocket.OPEN)) return;
        if (state.activeTab !== 'chain') return;

        const strike = state.chainViewportPendingStrike;
        if (!Number.isFinite(strike) || strike <= 0) return;
        if (!force && state.chainViewportCenterStrike === strike) return;

        if (!force && Number.isFinite(state.chainViewportCenterStrike) &&
            strike < state.chainViewportCenterStrike &&
            (state.chainViewportCenterStrike - strike) < CHAIN_VIEWPORT_CENTER_THRESHOLD) {
            return;
        }

        state.chainViewportCenterStrike = strike;
        state.chainViewportLastSentMs = Date.now();
        ws.send(`viewport_center:${strike.toFixed(1)}`);
    }

    function reportChainViewportCenter(force = false) {
        if (state.activeTab !== 'chain') return;

        const strike = getChainViewportCenterStrike();
        if (!Number.isFinite(strike) || strike <= 0) return;

        state.chainViewportPendingStrike = strike;

        const now = Date.now();
        const elapsed = now - state.chainViewportLastSentMs;
        if (force || elapsed >= CHAIN_VIEWPORT_SEND_THROTTLE_MS) {
            if (state.chainViewportSendTimer) {
                clearTimeout(state.chainViewportSendTimer);
                state.chainViewportSendTimer = null;
            }
            flushViewportCenterSend(force);
            return;
        }

        if (state.chainViewportSendTimer) return;
        state.chainViewportSendTimer = setTimeout(() => {
            state.chainViewportSendTimer = null;
            flushViewportCenterSend(false);
        }, CHAIN_VIEWPORT_SEND_THROTTLE_MS - elapsed);
    }

    // ======================================================================
