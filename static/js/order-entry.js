    // Order Placement (Strategy Builder)
    // ======================================================================
    function toggleStopLossInput() {
        const cb = document.getElementById('stopLossEnabled');
        const inpStop  = document.getElementById('stopLossStop');
        const inpLimit = document.getElementById('stopLossLimit');
        if (!cb) return;
        const en = cb.checked;
        if (inpStop)  { inpStop.disabled  = !en; if (!en) inpStop.value  = ''; }
        if (inpLimit) { inpLimit.disabled = !en; if (!en) inpLimit.value = ''; }
    }

    function getStrategyLegReferencePrice(quote, fallbackPrice) {
        const bid = Number.isFinite(quote?.bid) ? quote.bid : null;
        const ask = Number.isFinite(quote?.ask) ? quote.ask : null;
        const last = Number.isFinite(quote?.last) ? quote.last : null;
        if (bid !== null && ask !== null) {
            return (bid + ask) / 2.0;
        }
        if (last !== null) {
            return last;
        }
        return Math.abs(fallbackPrice);
    }

    function getStrategyLegSubmitPrice(leg, fallbackPrice) {
        const quote = (typeof getLegQuote === 'function')
            ? getLegQuote(leg)
            : { bid: null, ask: null, last: null };
        const reference = getStrategyLegReferencePrice(quote, fallbackPrice);
        if (leg.action === 'BUY') {
            return Number.isFinite(quote?.ask) ? quote.ask : reference;
        }
        return Number.isFinite(quote?.bid) ? quote.bid : reference;
    }

    function placeStrategyOrder() {
        if (state.strategyLegs.length === 0) return;

        // Gather limit price from input
        const lmtInput = document.getElementById('stratLmtPrice');
        const rawLmt = lmtInput ? parseFloat(lmtInput.value) : NaN;
        if (isNaN(rawLmt) || rawLmt === 0) {
            showOrderToast('Enter a valid limit price (negative for credit spreads)', 'err');
            return;
        }

        // Outside RTH flag
        const msVal = document.getElementById('marketStatus')?.textContent || '';
        const isOutsideRth = (msVal === 'GTH' || msVal === 'CURB');
        const isCombo = state.strategyLegs.length > 1;

        // Stop loss
        const slEnabled = document.getElementById('stopLossEnabled')?.checked;
        const slStop  = parseFloat(document.getElementById('stopLossStop')?.value  || '');
        const slLimit = parseFloat(document.getElementById('stopLossLimit')?.value || '');
        const hasStopLoss = slEnabled && !isNaN(slStop) && slStop !== 0 && !isNaN(slLimit) && slLimit !== 0;
        if (slEnabled && !hasStopLoss) {
            showOrderToast('Enter valid stop and limit prices (negative for credit)', 'err');
            return;
        }

        const comboNorm = isCombo ? normalizeComboLegQty(state.strategyLegs) : { comboQty: 1, ratios: [] };
        let inferredComboPrice = 0.0;

        // For multi-leg, send ratio quantities + comboQuantity so pricing is per-combo.
        // For single-leg, send absolute quantity directly.
        const legs = state.strategyLegs.map((leg, idx) => {
            const ratioQty = isCombo ? comboNorm.ratios[idx] : leg.qty;
            const perLegLmt = isCombo
                ? getStrategyLegSubmitPrice(leg, rawLmt)
                : rawLmt;
            // We need the current expiration from chainMeta
            const expiry = state.chainMeta ? (state.chainMeta.expiration_raw || '') : '';
            if (isCombo) {
                const sign = leg.action === 'BUY' ? 1.0 : -1.0;
                inferredComboPrice += sign * perLegLmt * Math.max(1, ratioQty || 1);
            }
            return {
                symbol: 'SPX',
                expiry: expiry,
                strike: leg.strike,
                right: leg.right,
                action: leg.action,
                qty: ratioQty,
                lmtPrice: parseFloat(perLegLmt.toFixed(2)),
                secType: 'OPT',
            };
        });

        let comboAction = rawLmt < 0 ? 'SELL' : 'BUY';
        if (isCombo) {
            const roundedInferredComboPrice = _roundSpxPrice(inferredComboPrice);
            comboAction = roundedInferredComboPrice < 0 ? 'SELL' : 'BUY';
            if ((comboAction === 'BUY' && rawLmt < 0) || (comboAction === 'SELL' && rawLmt > 0)) {
                const expectedSide = comboAction === 'BUY' ? 'debit' : 'credit';
                showOrderToast(`Entered limit sign does not match this ${expectedSide} combo`, 'err');
                return;
            }
        }

        // Build confirmation modal description
        const legDescs = legs.map(l =>
            `${l.action} ${l.qty}- ${l.strike}${l.right} @ ${l.lmtPrice}`
        );
        const expDisplay = legs[0] && legs[0].expiry ? fmtExpiry(legs[0].expiry) : '-';
        const comboMidEl = document.getElementById('comboMid');
        const comboMid = comboMidEl ? comboMidEl.textContent : '-';
        const netEl = document.getElementById('comboNet');
        const netText = netEl ? netEl.textContent : '-';

        let bodyHtml = '';
        bodyHtml += `<div class="modal-row"><span>Expiry</span><span>${expDisplay}</span></div>`;
        legDescs.forEach(d => {
            bodyHtml += `<div class="modal-row"><span>Leg</span><span>${d}</span></div>`;
        });
        bodyHtml += `<div class="modal-row"><span>Limit Price</span><span style="color:#93c5fd">$${rawLmt.toFixed(2)}</span></div>`;
        if (hasStopLoss) {
            bodyHtml += `<div class="modal-row"><span>Stop Loss</span><span style="color:#f87171">STP LMT stop $${slStop.toFixed(2)} / limit $${slLimit.toFixed(2)}</span></div>`;
        }
        if (isOutsideRth) {
            bodyHtml += `<div class="modal-row"><span>Session</span><span style="color:#eab308">Extended hours (outsideRTH)</span></div>`;
        }
        bodyHtml += `<div class="modal-row" style="margin-top:10px; padding-top:10px; border-top:1px solid #1e293b"><span style="color:#f87171">This order will be submitted to IB. Verify details carefully.</span><span></span></div>`;

        document.getElementById('orderModalTitle').textContent =
            legs.length === 1 ? 'Confirm Order' : `Confirm ${legs.length}-Leg Combo`;
        document.getElementById('orderModalBody').innerHTML = bodyHtml;
        document.getElementById('orderModalBackdrop').classList.remove('hidden');

        // Store payload for when user confirms
        state.pendingOrderPayload = {
            legs,
            orderType: 'LMT',
            tif: 'DAY',
            comboLmtPrice: parseFloat(rawLmt.toFixed(2)),
            comboAction,
            comboQuantity: isCombo ? comboNorm.comboQty : 1,
            outsideRth: isOutsideRth,
            stopLoss: hasStopLoss ? { stopPrice: parseFloat(slStop.toFixed(2)), limitPrice: parseFloat(slLimit.toFixed(2)) } : null,
        };
    }

    function dismissOrderModal() {
        document.getElementById('orderModalBackdrop').classList.add('hidden');
        state.pendingOrderPayload = null;
    }

    function confirmOrder() {
        const payload = state.pendingOrderPayload;
        if (!payload) return;
        dismissOrderModal();
        const btn = document.getElementById('stratPlaceBtn');
        if (btn) { btn.disabled = true; btn.textContent = 'Submitting-'; }

        sendPlaceOrder(payload, (resp) => {
            if (btn) { btn.disabled = false; btn.textContent = 'Place Order'; }
            if (resp && resp.status && resp.status !== 'Error') {
                showOrderToast('Order submitted: ' + (resp.message || ''), 'ok');
                clearStrategy();
            } else {
                showOrderToast('Order failed: ' + (resp?.message || 'Unknown error'), 'err');
            }
        });
    }

    // Send a place_order WS message and call back with the response
    const _orderCallbacks = {};
    function sendPlaceOrder(payload, callback) {
        if (!ws || ws.readyState !== WebSocket.OPEN) {
            showOrderToast('Not connected to server', 'err');
            if (callback) callback({ status: 'Error', message: 'Not connected' });
            return;
        }
        // Temporarily register a one-shot reply handler
        const callbackId = Date.now();
        _orderCallbacks[callbackId] = callback;
        // Attach to state so handleMessage can dispatch
        if (!state._pendingOrderCallback) {
            state._pendingOrderCallback = { id: callbackId, fn: callback };
        }
        ws.send('place_order:' + JSON.stringify(payload));
    }

    function showOrderToast(msg, type) {
        const el = document.getElementById('orderToast');
        if (!el) return;
        el.textContent = msg;
        el.className = `order-toast toast-${type}`;
        if (el._timeout) clearTimeout(el._timeout);
        el._timeout = setTimeout(() => { el.className = 'order-toast hidden'; }, 5000);
    }

    function handleOrderStatus(data) {
        // Dispatch to pending callback if any
        if (state._pendingOrderCallback) {
            const cb = state._pendingOrderCallback.fn;
            state._pendingOrderCallback = null;
            if (cb) cb(data);
        } else {
            // Background status update - order settled asynchronously; show toast
            const st = data.status || '';
            if (st === 'Filled') {
                showOrderToast((data.message || 'Order filled'), 'ok');
            } else if (st === 'Error' || st === 'Unknown') {
                showOrderToast('Order error: ' + (data.message || 'unknown'), 'err');
            } else if (st === 'Cancelled' || st === 'ApiCancelled' || st === 'Inactive') {
                showOrderToast('Order ' + st + ': ' + (data.message || ''), 'info');
            }
        }
        // Refresh account data
        if (state.activeTab === 'account') renderAccountTab();
    }

    function handleIbError(data) {
        const code = data?.errorCode ? `IB ${data.errorCode}` : 'IB error';
        const orderText = data?.orderId ? ` order ${data.orderId}` : '';
        showOrderToast(`${code}${orderText}: ${data?.message || 'Unknown broker error'}`, 'err');
    }

    // ======================================================================
