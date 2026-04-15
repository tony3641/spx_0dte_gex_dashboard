    // Strategy Builder
    // ======================================================================
    function addLeg(strike, right, action) {
        // Check if same strike+right already exists
        const existing = state.strategyLegs.find(l => l.strike === strike && l.right === right);
        if (existing) {
            if (existing.action === action) {
                existing.qty++;
            } else {
                // Opposite action: reduce qty or flip
                existing.qty--;
                if (existing.qty <= 0) {
                    existing.action = action;
                    existing.qty = 1;
                }
            }
        } else {
            state.strategyLegs.push({
                id: state.nextLegId++,
                strike, right, action,
                qty: 1,
            });
        }
        renderStrategy();
    }

    function removeLeg(legId) {
        state.strategyLegs = state.strategyLegs.filter(l => l.id !== legId);
        renderStrategy();
    }

    function updateLegQty(legId, newQty) {
        const leg = state.strategyLegs.find(l => l.id === legId);
        if (!leg) return;
        const q = parseInt(newQty);
        if (q > 0) {
            leg.qty = q;
        } else {
            removeLeg(legId);
            return;
        }
        renderStrategy();
    }

    function clearStrategy() {
        state.strategyLegs = [];
        const lmtInput = document.getElementById('stratLmtPrice');
        if (lmtInput) {
            lmtInput.value = '';
            lmtInput.dataset.autofilled = 'true';
        }
        renderStrategy();
    }

    function bumpAllQty(delta) {
        if (state.strategyLegs.length === 0) return;
        for (const leg of state.strategyLegs) {
            leg.qty = Math.max(1, leg.qty + delta);
        }
        renderStrategy();
    }

    function manualRefreshChain() {
        if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send('refresh_chain');
        }
    }

    function getLegQuote(leg) {
        const r = state.chainData[leg.strike];
        if (!r) return { bid: null, ask: null, last: null };
        const side = leg.right === 'C' ? 'call' : 'put';
        return {
            bid: r[side + '_bid'] ?? null,
            ask: r[side + '_ask'] ?? null,
            last: r[side + '_last'] ?? null,
        };
    }

    function getLegDelta(leg) {
        const r = state.chainData[leg.strike];
        if (!r) return null;
        const side = leg.right === 'C' ? 'call' : 'put';
        const rawDelta = r[side + '_delta'];
        if (rawDelta === null || rawDelta === undefined || !isFinite(rawDelta)) return null;
        const actionSign = leg.action === 'BUY' ? 1 : -1;
        return rawDelta * leg.qty * 100 * actionSign;
    }

    function formatSignedNumber(val, decimals = 1) {
        const sign = val > 0 ? '+' : '';
        return sign + val.toFixed(decimals);
    }

    function _gcd(a, b) {
        let x = Math.abs(parseInt(a, 10) || 0);
        let y = Math.abs(parseInt(b, 10) || 0);
        while (y !== 0) {
            const t = y;
            y = x % y;
            x = t;
        }
        return x || 1;
    }

    function _spxTickForPrice(price) {
        const p = Math.abs(parseFloat(price) || 0);
        return p > 2 ? 0.10 : 0.05;
    }

    function _roundSpxPrice(value) {
        const p = parseFloat(value);
        if (Number.isNaN(p)) return 0.0;
        const tick = _spxTickForPrice(p);
        const rounded = Math.round(p / tick) * tick;
        return rounded;
    }

    function updatePriceInputStep(input) {
        if (!input) return;
        const val = parseFloat(input.value);
        input.step = _spxTickForPrice(val).toFixed(2);
    }

    function normalizeComboLegQty(legs) {
        if (!legs || legs.length === 0) {
            return { comboQty: 1, ratios: [] };
        }
        const qtys = legs.map(l => Math.max(1, parseInt(l.qty, 10) || 1));
        let comboQty = qtys[0];
        for (let i = 1; i < qtys.length; i++) {
            comboQty = _gcd(comboQty, qtys[i]);
        }
        comboQty = Math.max(1, comboQty);
        const ratios = qtys.map(q => Math.max(1, Math.floor(q / comboQty)));
        return { comboQty, ratios };
    }

    function renderStrategy() {
        const content = document.getElementById('strategyContent');
        const summary = document.getElementById('strategySummary');
        const countEl = document.getElementById('strategyLegCount');
        const emptyEl = document.getElementById('strategyEmpty');

        if (state.strategyLegs.length === 0) {
            content.innerHTML = '<div class="strategy-empty" id="strategyEmpty">Click <span style="color:#4ade80">Ask</span> to buy or <span style="color:#f87171">Bid</span> to sell an option</div>';
            summary.style.display = 'none';
            countEl.textContent = '';
            const orderRow = document.getElementById('orderEntryRow');
            if (orderRow) orderRow.style.display = 'none';
            refreshSelectionHighlights();
            return;
        }

        countEl.textContent = `(${state.strategyLegs.length} leg${state.strategyLegs.length > 1 ? 's' : ''})`;

        let html = '<table class="strategy-legs"><thead><tr>';
        html += '<th>Action</th><th>Strike</th><th>Type</th><th>Qty</th>';
        html += '<th>Bid</th><th>Ask</th><th>Mid</th><th>Delta</th><th></th>';
        html += '</tr></thead><tbody>';

        for (const leg of state.strategyLegs) {
            const q = getLegQuote(leg);
            const mid = (q.bid !== null && q.ask !== null) ? ((q.bid + q.ask) / 2).toFixed(2) : '-';
            const legDelta = getLegDelta(leg);
            const legDeltaClass = legDelta === null ? '' : (legDelta > 0 ? 'summary-credit' : (legDelta < 0 ? 'summary-debit' : ''));
            const actionClass = leg.action === 'BUY' ? 'leg-buy' : 'leg-sell';
            html += '<tr>';
            html += `<td class="${actionClass}">${leg.action}</td>`;
            html += `<td>${leg.strike}</td>`;
            html += `<td>${leg.right === 'C' ? 'Call' : 'Put'}</td>`;
            html += `<td><span class="qty-group"><button class="qty-btn" onclick="bumpAllQty(-1)" title="Decrease all legs">-</button><input type="number" class="leg-qty" value="${leg.qty}" min="1" onchange="updateLegQty(${leg.id}, this.value)"><button class="qty-btn" onclick="bumpAllQty(1)" title="Increase all legs">+</button></span></td>`;
            html += `<td>${q.bid !== null ? q.bid.toFixed(2) : '-'}</td>`;
            html += `<td>${q.ask !== null ? q.ask.toFixed(2) : '-'}</td>`;
            html += `<td>${mid}</td>`;
            html += `<td class="${legDeltaClass}">${legDelta !== null ? formatSignedNumber(legDelta, 1) : '-'}</td>`;
            html += `<td><span class="leg-remove" onclick="removeLeg(${leg.id})">x</span></td>`;
            html += '</tr>';
        }
        html += '</tbody></table>';
        content.innerHTML = html;
        refreshSelectionHighlights();

        // Compute combo pricing
        computeCombo();
        summary.style.display = '';
        // Show order entry row
        const orderRow = document.getElementById('orderEntryRow');
        if (orderRow) orderRow.style.display = '';
    }

    function updateStrategyPrices() {
        if (state.strategyLegs.length === 0) return;
        // Re-render leg prices and recompute combo
        renderStrategy();
    }

    function computeCombo() {
        const legs = state.strategyLegs;
        if (legs.length === 0) return;

        const norm = normalizeComboLegQty(legs);

        // Combo Bid = worst fill if you want to buy the combo:
        //   BUY legs - pay ask, SELL legs - receive bid
        // Combo Ask = worst fill if you want to sell the combo:
        //   BUY legs - pay bid (to close), SELL legs - receive ask
        // We define from the BUYER's perspective of the combo:
        //   Combo natural debit (what you'd pay at market) = sum of (BUY_ask - SELL_bid) per leg
        //   Combo credit (what you'd receive) = sum of (SELL_ask - BUY_bid) per leg

        let comboBid = 0, comboAsk = 0;
        let allValid = true;
        let combinedDelta = 0;
        let allDeltaValid = true;

        for (let i = 0; i < legs.length; i++) {
            const leg = legs[i];
            const ratioQty = Math.max(1, norm.ratios[i] || 1);
            const q = getLegQuote(leg);
            const legDelta = getLegDelta(leg);
            if (legDelta === null) {
                allDeltaValid = false;
            } else {
                combinedDelta += legDelta;
            }
            if (q.bid === null || q.ask === null) {
                allValid = false;
                continue;
            }
            if (leg.action === 'BUY') {
                // Buying: you pay ask, combo bid contribution is negative (cost)
                comboBid += ratioQty * q.bid;   // If YOU want to sell the combo, close buys at bid
                comboAsk += ratioQty * q.ask;   // If YOU want to buy the combo, buys cost ask
            } else {
                // Selling: you receive bid
                comboBid -= ratioQty * q.ask;   // If selling combo, close shorts at ask (cost)
                comboAsk -= ratioQty * q.bid;   // If buying combo, shorts give you bid (credit)
            }
        }

        // comboBid = what you'd receive selling the combo
        // comboAsk = what you'd pay buying the combo
        // Flip sign convention: positive = credit to you

        const comboBidEl = document.getElementById('comboBid');
        const comboAskEl = document.getElementById('comboAsk');
        const comboMidEl = document.getElementById('comboMid');
        const comboNetEl = document.getElementById('comboNet');
        const comboDeltaEl = document.getElementById('comboDelta');

        if (allDeltaValid) {
            comboDeltaEl.textContent = formatSignedNumber(combinedDelta, 1);
            comboDeltaEl.className = 'summary-value ' + (combinedDelta > 0 ? 'summary-credit' : (combinedDelta < 0 ? 'summary-debit' : ''));
        } else {
            comboDeltaEl.textContent = '-';
            comboDeltaEl.className = 'summary-value';
        }

        if (!allValid) {
            comboBidEl.textContent = '-';
            comboAskEl.textContent = '-';
            comboMidEl.textContent = '-';
            comboNetEl.textContent = '-';
        } else {
            // Convention: positive value = debit (you pay), negative = credit (you receive)
            // comboAsk is the cost to enter the position
            // Net: negative comboAsk = credit, positive = debit
            const netCost = comboAsk;  // cost to enter: positive = debit
            const bid = comboBid;
            const ask = comboAsk;
            const mid = (bid + ask) / 2;

            comboBidEl.textContent = bid.toFixed(2);
            comboAskEl.textContent = ask.toFixed(2);
            comboMidEl.textContent = mid.toFixed(2);

            // Auto-fill limit price input with signed combo mid (only if user hasn't typed yet).
            // Credit spreads auto-fill a negative value; debit spreads positive.
            const lmtInput = document.getElementById('stratLmtPrice');
            if (lmtInput && lmtInput.dataset.autofilled !== 'false') {
                const tick = _spxTickForPrice(mid);
                const signedMid = netCost <= 0
                    ? -(Math.round(Math.abs(mid) / tick) * tick)
                    :   (Math.round(mid / tick) * tick);
                lmtInput.value = signedMid.toFixed(2);
                lmtInput.dataset.autofilled = 'true';
                updatePriceInputStep(lmtInput);
            }

            if (netCost > 0) {
                comboNetEl.textContent = 'Debit ' + netCost.toFixed(2);
                comboNetEl.className = 'summary-value summary-debit';
            } else {
                comboNetEl.textContent = 'Credit ' + netCost.toFixed(2);
                comboNetEl.className = 'summary-value summary-credit';
            }

            // Max profit / loss / breakeven
            computePayoff(mid);
        }
    }

    function computePayoff(premium) {
        const legs = state.strategyLegs;
        const maxProfitEl = document.getElementById('comboMaxProfit');
        const maxLossEl = document.getElementById('comboMaxLoss');
        const breakevenEl = document.getElementById('comboBreakeven');

        if (legs.length === 0) return;

        // Iterate spot prices from min_strike - 50 to max_strike + 50
        const allStrikes = legs.map(l => l.strike);
        const lo = Math.min(...allStrikes) - 100;
        const hi = Math.max(...allStrikes) + 100;
        const step = 0.5;

        let maxProfit = -Infinity;
        let maxLoss = Infinity;
        const breakevens = [];
        let prevPnl = null;

        for (let S = lo; S <= hi; S += step) {
            let pnl = -premium * 100; // cost of entry (premium per contract - 100 multiplier)
            for (const leg of legs) {
                let intrinsic = 0;
                if (leg.right === 'C') {
                    intrinsic = Math.max(0, S - leg.strike);
                } else {
                    intrinsic = Math.max(0, leg.strike - S);
                }
                const sign = leg.action === 'BUY' ? 1 : -1;
                pnl += sign * leg.qty * intrinsic * 100; // - 100 multiplier
            }

            if (pnl > maxProfit) maxProfit = pnl;
            if (pnl < maxLoss) maxLoss = pnl;

            // Detect zero-crossing for breakeven
            if (prevPnl !== null && ((prevPnl < 0 && pnl >= 0) || (prevPnl >= 0 && pnl < 0))) {
                // Linear interpolation
                const be = S - step + step * Math.abs(prevPnl) / (Math.abs(prevPnl) + Math.abs(pnl));
                breakevens.push(be.toFixed(1));
            }
            prevPnl = pnl;
        }

        // Check for unlimited risk/reward
        const hasNakedShort = legs.some(l => l.action === 'SELL');
        const hasBuy = legs.some(l => l.action === 'BUY');

        // If only sells with no offsetting buys on same side, loss could be unlimited
        const isSpread = legs.length >= 2;

        if (maxProfit > 1e8) {
            maxProfitEl.textContent = 'Unlimited';
            maxProfitEl.className = 'summary-value summary-credit';
        } else {
            maxProfitEl.textContent = (maxProfit >= 0 ? '+' : '') + '$' + Math.abs(maxProfit).toLocaleString(undefined, {maximumFractionDigits: 0});
            maxProfitEl.className = 'summary-value ' + (maxProfit >= 0 ? 'summary-credit' : 'summary-debit');
        }

        if (maxLoss < -1e8) {
            maxLossEl.textContent = 'Unlimited';
            maxLossEl.className = 'summary-value summary-debit';
        } else {
            maxLossEl.textContent = (maxLoss >= 0 ? '+' : '-') + '$' + Math.abs(maxLoss).toLocaleString(undefined, {maximumFractionDigits: 0});
            maxLossEl.className = 'summary-value ' + (maxLoss >= 0 ? 'summary-credit' : 'summary-debit');
        }

        if (breakevens.length > 0) {
            breakevenEl.textContent = breakevens.join(', ');
        } else {
            breakevenEl.textContent = '-';
        }
    }

    function handleMessage(msg) {
        switch (msg.type) {
            case 'init':
                handleInit(msg.data);
                break;
            case 'bar':
                appendBar(msg.data);
                updateBadges();
                break;
            case 'bar_update':
                updateLastBar(msg.data);
                updateBadges();
                break;
            case 'chain_progress':
                handleChainProgress(msg.data);
                break;
            case 'gex':
                state.gex = msg.data;
                if (msg.data.spot_price > 0) state.currentSpot = msg.data.spot_price;
                state.esDerived = msg.data.es_derived || false;
                updateGexChart();
                updateSmileChart();
                updatePriceChart();
                updateBadges();
                break;
            case 'chain_quotes':
                handleChainQuotes(msg.data);
                break;
            case 'chain_tick':
                handleChainTick(msg.data);
                break;
            case 'status':
                updateStatus(msg.data);
                break;
            case 'account_update':
                handleAccountUpdate(msg.data);
                break;
            case 'order_status':
                handleOrderStatus(msg.data);
                break;
            case 'monthly_gex':
                state.monthlyGex = msg.data;
                if (msg.data && msg.data.expiration) {
                    // Format YYYYMMDD to YYYY-MM-DD for display
                    const e = msg.data.expiration;
                    state.monthlyExpiration = e.length === 8
                        ? `${e.slice(0,4)}-${e.slice(4,6)}-${e.slice(6,8)}`
                        : e;
                    updateGexModeToggle();
                }
                if (state.gexMode === 'monthly') {
                    updateGexChart();
                    updateSmileChart();
                    updatePriceChart();
                    updateBadges();
                }
                break;
            case 'monthly_gex_progress':
                handleMonthlyGexProgress(msg.data);
                break;
            case 'ping':
                break;
            default:
                console.log('Unknown message type:', msg.type);
        }
    }

    function handleInit(data) {
        // Update mode first so chart title is correct before drawing
        if (data.data_mode) {
            state.dataMode = data.data_mode;
            state.historicalDate = data.historical_date || '';
            updateModeBadge();
        }

        // Seed currentSpot and esDerived from init payload immediately
        if (data.spot_price > 0) state.currentSpot = data.spot_price;
        state.esDerived = data.es_derived || false;

        // Restore bar history (OHLC)
        if (data.price_history && data.price_history.length > 0) {
            state.bars = data.price_history;
            updatePriceChart();
        }

        // Restore GEX
        if (data.gex) {
            state.gex = data.gex;
            if (data.gex.spot_price > 0) state.currentSpot = data.gex.spot_price;
            state.esDerived = data.es_derived || false;
            updateGexChart();
            updatePriceChart();
            updateSmileChart();
            // Cached GEX from server - hide loading overlays immediately
            document.getElementById('gexLoading').classList.add('hidden');
            document.getElementById('smileLoading').classList.add('hidden');
        }

        updateStatus(data);
        updateBadges();

        // Restore GEX mode and monthly data
        if (data.gex_mode) {
            state.gexMode = data.gex_mode;
            updateGexModeToggle();
        }
        if (data.monthly_gex) {
            state.monthlyGex = data.monthly_gex;
        }
        if (data.monthly_expiration) {
            state.monthlyExpiration = data.monthly_expiration;
        }
        // Re-render charts with correct mode data
        if (state.gexMode === 'monthly' && state.monthlyGex) {
            updateGexChart();
            updateSmileChart();
        }

        // Restore option chain data for Tab 2
        if (data.chain_quotes) {
            handleChainQuotes(data.chain_quotes);
        }

        // Restore account data for Tab 3
        if (data.account) {
            handleAccountUpdate(data.account);
        }
    }

    // ======================================================================
