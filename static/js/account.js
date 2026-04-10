    // Account Tab (Tab 3)
    // ======================================================================
    function fmtCurrency(v) {
        if (v === null || v === undefined || !isFinite(v)) return '-';
        const abs = Math.abs(v);
        let s;
        if (abs >= 1e6) s = '$' + (v / 1e6).toFixed(2) + 'M';
        else if (abs >= 1e3) s = '$' + (v / 1e3).toFixed(1) + 'K';
        else s = '$' + v.toFixed(2);
        return s;
    }
    function fmtCurrencyFull(v) {
        if (v === null || v === undefined || !isFinite(v)) return '-';
        return '$' + v.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2});
    }
    function pnlClass(v) {
        if (v === null || v === undefined || !isFinite(v)) return '';
        return v > 0 ? 'pnl-pos' : v < 0 ? 'pnl-neg' : '';
    }
    function fmtExpiry(s) {
        if (!s || s.length < 8) return s || '-';
        return s.slice(0, 4) + '-' + s.slice(4, 6) + '-' + s.slice(6, 8);
    }

    function handleAccountUpdate(data) {
        if (data.summary) state.accountSummary = data.summary;
        if (data.positions) state.positions = data.positions;
        if (data.orders) state.openOrders = data.orders;
        if (data.executions) state.executions = data.executions;
        if (state.activeTab === 'account') renderAccountTab();
        // Always update summary badges (visible on all tabs)
        renderAccountSummaryBadges();
    }

    function renderAccountSummaryBadges() {
        const s = state.accountSummary;
        function set(id, val) {
            const el = document.getElementById(id);
            if (!el) return;
            el.textContent = fmtCurrency(val);
        }
        function setPnl(id, val) {
            const el = document.getElementById(id);
            if (!el) return;
            el.textContent = fmtCurrency(val);
            el.className = 'acct-badge-value ' + pnlClass(val);
        }
        set('acctNetLiq', s.NetLiquidation);
        set('acctExcessLiq', s.ExcessLiquidity);
        set('acctAvailFunds', s.FullAvailableFunds);
        set('acctBuyingPower', s.BuyingPower);
        set('acctMaintMargin', s.MaintMarginReq);
        set('acctGrossPV', s.GrossPositionValue);
        setPnl('acctUnPnl', s.UnrealizedPnL);
        setPnl('acctRePnl', s.RealizedPnL);
    }

    function renderAccountTab() {
        renderPositionsTable();
        renderOrdersTable();
        renderExecutionsTable();
    }

    function renderPositionsTable() {
        const tbody = document.getElementById('positionsBody');
        const count = document.getElementById('posCount');
        if (!tbody) return;
        const rows = state.positions;
        count.textContent = rows.length > 0 ? `(${rows.length})` : '';
        if (!rows || rows.length === 0) {
            tbody.innerHTML = '<tr><td colspan="12" class="acct-empty">No positions</td></tr>';
            return;
        }

        // Clean up liquidatingPositions for positions that have been closed
        if (state.liquidatingPositions.size > 0) {
            const currentPosKeys = new Set(rows.map(p => {
                const c = p.contract || {};
                return `${c.symbol}|${c.secType}|${c.expiry || ''}|${c.strike || ''}|${c.right || ''}`;
            }));
            for (const key of state.liquidatingPositions) {
                if (!currentPosKeys.has(key)) state.liquidatingPositions.delete(key);
            }
        }
        let html = '';
        rows.forEach((p, idx) => {
            const c = p.contract || {};
            const sym = c.symbol || '-';
            const secType = c.secType || '-';
            const expiry = fmtExpiry(c.expiry);
            const strike = c.strike ? c.strike.toFixed(0) : '-';
            const right = c.right || '-';
            const isOptLiquidatable =
                secType === 'OPT' &&
                !!c.expiry &&
                c.strike !== null && c.strike !== undefined &&
                !!c.right;
            const isStkLiquidatable = secType === 'STK' && !!c.symbol;
            const canLiquidate = (isOptLiquidatable || isStkLiquidatable) && p.position !== 0;
            const posKey = `${sym}|${secType}|${c.expiry || ''}|${c.strike || ''}|${c.right || ''}`;
            const isLiquidating = state.liquidatingPositions.has(posKey);
            const qty = p.position;
            const avgCost = p.averageCost;
            const mktPrice = p.marketPrice;
            const mktVal = p.marketValue;
            const unPnl = p.unrealizedPNL;
            const rePnl = p.realizedPNL;
            const unClass = pnlClass(unPnl);
            const reClass = pnlClass(rePnl);
            const qtyClass = qty > 0 ? 'side-buy' : qty < 0 ? 'side-sell' : '';
            html += `<tr>
                <td>${sym}</td>
                <td>${secType}</td>
                <td>${expiry}</td>
                <td>${strike}</td>
                <td>${right}</td>
                <td class="${qtyClass}">${qty > 0 ? '+' : ''}${qty}</td>
                <td>${fmtCurrencyFull(avgCost)}</td>
                <td>${fmtCurrencyFull(mktPrice)}</td>
                <td>${fmtCurrencyFull(mktVal)}</td>
                <td class="${unClass}">${fmtCurrencyFull(unPnl)}</td>
                <td class="${reClass}">${fmtCurrencyFull(rePnl)}</td>
                <td><button class="btn-liquidate${isLiquidating ? ' sending' : ''}" id="liqBtn${idx}" onclick="liquidatePosition(${idx})" title="${canLiquidate ? 'Close this position via adaptive fill' : 'Requires a supported contract (OPT or STK) and non-zero position'}" ${(!canLiquidate || isLiquidating) ? 'disabled' : ''}>${canLiquidate ? (isLiquidating ? 'Sent' : 'Liquidate') : 'N/A'}</button></td>
            </tr>`;
        });
        tbody.innerHTML = html;
    }

    function renderOrdersTable() {
        const tbody = document.getElementById('ordersBody');
        const count = document.getElementById('ordersCount');
        if (!tbody) return;
        const rows = state.openOrders;
        count.textContent = rows.length > 0 ? `(${rows.length})` : '';
        if (!rows || rows.length === 0) {
            tbody.innerHTML = '<tr><td colspan="13" class="acct-empty">No open orders</td></tr>';
            return;
        }
        let html = '';
        rows.forEach(o => {
            const c = o.contract || {};
            const actionClass = o.action === 'BUY' ? 'side-buy' : 'side-sell';
            const statusClass = o.status === 'Filled' ? 'pnl-pos' : o.status === 'Cancelled' ? 'pnl-neg' : '';
            const lmt = o.lmtPrice !== null && o.lmtPrice !== undefined ? o.lmtPrice.toFixed(2) : '-';
            const avgFill = o.avgFillPrice ? o.avgFillPrice.toFixed(2) : '-';
            html += `<tr>
                <td>${o.orderId}</td>
                <td>${c.symbol || '-'}</td>
                <td>${fmtExpiry(c.expiry)}</td>
                <td>${c.strike ? c.strike.toFixed(0) : '-'}</td>
                <td>${c.right || (c.secType==='BAG' ? 'BAG' : '-')}</td>
                <td class="${actionClass}">${o.action}</td>
                <td>${o.totalQty}</td>
                <td>${o.orderType}</td>
                <td>${lmt}</td>
                <td class="${statusClass}">${o.status}</td>
                <td>${o.filled || 0}</td>
                <td>${avgFill}</td>
                <td><button class="btn-cancel-order" onclick="cancelOrder(${o.orderId})" ${['Filled','Cancelled','ApiCancelled'].includes(o.status) ? 'disabled' : ''}>Cancel</button></td>
            </tr>`;
        });
        tbody.innerHTML = html;
    }

    function formatExecutionTime(timeValue) {
        if (!timeValue) return '-';
        const text = String(timeValue).trim();
        const match = text.match(/^(?:\d{4}-\d{2}-\d{2}[ T])(.+)$/);
        return match ? match[1] : text;
    }

    function renderExecutionsTable() {
        const tbody = document.getElementById('executionsBody');
        const count = document.getElementById('exeCount');
        if (!tbody) return;
        const rows = state.executions;
        count.textContent = rows.length > 0 ? `(${rows.length})` : '';
        if (!rows || rows.length === 0) {
            tbody.innerHTML = '<tr><td colspan="9" class="acct-empty">No executions today</td></tr>';
            return;
        }
        let html = '';
        rows.forEach(e => {
            const sideClass = e.side === 'BOT' ? 'side-buy' : 'side-sell';
            const comm = e.commission !== null && e.commission !== undefined ? '$' + e.commission.toFixed(2) : '-';
            const t = formatExecutionTime(e.time);
            html += `<tr>
                <td>${t}</td>
                <td>${e.symbol || '-'}</td>
                <td>${fmtExpiry(e.expiry)}</td>
                <td>${e.strike ? e.strike.toFixed(0) : '-'}</td>
                <td>${e.right || '-'}</td>
                <td class="${sideClass}">${e.side}</td>
                <td>${e.shares}</td>
                <td>$${parseFloat(e.price).toFixed(2)}</td>
                <td>${comm}</td>
            </tr>`;
        });
        tbody.innerHTML = html;
    }

    // Emergency liquidation: close position via adaptive mid-price fill (no confirmation)
    function liquidatePosition(idx) {
        const p = state.positions[idx];
        if (!p) return;
        const c = p.contract;
        const btn = document.getElementById('liqBtn' + idx);

        const isOpt = !!c && c.secType === 'OPT' && !!c.expiry && c.strike !== null && c.strike !== undefined && !!c.right;
        const isStk = !!c && c.secType === 'STK' && !!c.symbol;
        if (!isOpt && !isStk) {
            showOrderToast('Liquidation supports OPT and STK positions only', 'err');
            return;
        }
        if (!p.position || p.position === 0) {
            showOrderToast('Position size is zero', 'err');
            return;
        }

        // Prevent duplicate liquidation orders for the same position
        const posKey = `${c.symbol}|${c.secType}|${c.expiry || ''}|${c.strike || ''}|${c.right || ''}`;
        if (state.liquidatingPositions.has(posKey)) return;
        state.liquidatingPositions.add(posKey);

        // Determine close side
        const closeAction = p.position > 0 ? 'SELL' : 'BUY';
        const absQty = Math.abs(p.position);

        if (btn) { btn.className = 'btn-liquidate sending'; btn.disabled = true; btn.textContent = 'Sending-'; }

        const leg = {
            symbol: c.symbol,
            action: closeAction,
            qty: absQty,
            lmtPrice: null,  // backend fetches mid price for adaptive fill
            secType: c.secType,
        };
        if (isOpt) {
            leg.expiry = c.expiry;
            leg.strike = c.strike;
            leg.right = c.right;
        }

        const payload = {
            legs: [leg],
            orderType: 'LMT',
            tif: 'DAY',
            outsideRth: true,
            dynamicFill: true,
            repriceIntervalSec: 0.3,
        };

        sendPlaceOrder(payload, (resp) => {
            if (resp && resp.status && resp.status !== 'Error') {
                if (btn) { btn.className = 'btn-liquidate done'; btn.textContent = 'Sent'; }
                showOrderToast(`${closeAction} ${absQty} ${c.localSymbol || c.symbol} submitted`, 'ok');
                // Keep posKey in Set - position will disappear via account_update re-render
            } else {
                // Allow retry on failure
                state.liquidatingPositions.delete(posKey);
                if (btn) { btn.className = 'btn-liquidate failed'; btn.disabled = false; btn.textContent = 'Failed'; }
                showOrderToast('Liquidation failed: ' + (resp?.message || 'Unknown error'), 'err');
                setTimeout(() => {
                    if (btn && !state.liquidatingPositions.has(posKey)) {
                        btn.className = 'btn-liquidate'; btn.disabled = false; btn.textContent = 'Liquidate';
                    }
                }, 5000);
            }
        });
    }

    function cancelOrder(orderId) {
        if (!ws || ws.readyState !== WebSocket.OPEN) {
            showOrderToast('Not connected to server', 'err');
            return;
        }
        ws.send('cancel_order:' + orderId);
        showOrderToast('Cancel request sent for order ' + orderId, 'info');
    }

    // ======================================================================
