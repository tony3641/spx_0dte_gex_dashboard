    // Strategy UI — list, editor, scanner results, WS handlers.
    // ======================================================================
    let _selStrategy = null;

    function selectedStrategy() { return _selStrategy; }

    function newStrategy() {
        const s = {
            name: 'New ' + (state.strategies.length + 1),
            direction: 'bull_put',
            conditions: [],
            exit_rules: { take_profit: null, stop_loss: null, hold_to_expire: false },
            auto_execute: false, armed: false,
        };
        state.strategies.push(s);
        _selStrategy = s.name;
        renderStrategyList();
        renderStrategyEditor(s.name);
    }

    function renderStrategyList() {
        const nav = document.getElementById('strategyNav');
        if (!nav) return;
        nav.innerHTML = state.strategies.map(s => `
            <div class="strat-item ${s.name === _selStrategy ? 'active' : ''}" onclick="selectStrategy('${s.name}')">
                <span class="strat-name">${s.name}</span>
                <span class="strat-status">${s.armed ? (s.auto_execute ? 'auto' : 'scan') : 'off'}</span>
                <button onclick="event.stopPropagation(); armStrategy('${s.name}')">${s.armed ? 'Disarm' : 'Arm'}</button>
                <button onclick="event.stopPropagation(); deleteStrategy('${s.name}')">Del</button>
            </div>`).join('') || '<p>No strategies yet. Create one.</p>';
    }

    function selectStrategy(name) {
        _selStrategy = name;
        renderStrategyList();
        renderStrategyEditor(name);
    }

    function renderStrategyEditor(name) {
        _selStrategy = name;
        const s = state.strategies.find(x => x.name === name);
        const el = document.getElementById('strategyEditor');
        if (!s || !el) return;
        el.innerHTML = `
            <h3>Strategy</h3>
            <label>Name</label><input id="stratName" value="${s.name}" />
            <label>Direction</label>
            <select id="stratDirection">
              <option value="bull_put" ${s.direction === 'bull_put' ? 'selected' : ''}>Bull Put</option>
              <option value="bear_call" ${s.direction === 'bear_call' ? 'selected' : ''}>Bear Call</option>
            </select>
            <label>Conditions (priority order)</label>
            <ol id="stratCondList">${s.conditions.map((c, i) => `<li>${c.kind}
                <button onclick="removeCondition(${i})">x</button>
                <button onclick="moveCondition(${i},-1)">up</button>
                <button onclick="moveCondition(${i},1)">down</button>
                <button onclick="editCondition(${i})">edit</button></li>`).join('')}</ol>
            <button onclick="addCondition()">+ Add Condition</button>
            <div id="conditionEditor"></div>
            <label>Exit rules</label>
            <div>
              <label><input type="checkbox" id="tpEnabled" onchange="toggleTp(document.getElementById('tpEnabled'))"> Take Profit</label>
              <input id="tpValue" placeholder="% of credit or $" />
              <label><input type="checkbox" id="slEnabled"> Stop Loss (multiplier of credit)</label>
              <input id="slMultiplier" type="number" step="0.1" placeholder="multiplier (e.g. 5 = 5x credit)" />
            </div>
            <label class="caps"><input type="checkbox" id="autoExec" ${s.auto_execute ? 'checked' : ''}> Auto-execute</label>
            <button onclick="saveStrategyFromForm()">Save</button>`;
        if (s.exit_rules.take_profit) { document.getElementById('tpEnabled').checked = true; document.getElementById('tpValue').value = s.exit_rules.take_profit.value; }
        if (s.exit_rules.stop_loss) { document.getElementById('slEnabled').checked = true; document.getElementById('slMultiplier').value = s.exit_rules.stop_loss.multiplier; }
        toggleTp(document.getElementById('tpEnabled'));
    }

    function addCondition() {
        const s = state.strategies.find(x => x.name === selectedStrategy());
        if (!s) return;
        s.conditions.push({ kind: 'short_delta', enabled: true, params: { min: 0.10, max: 0.30 } });
        renderStrategyEditor(s.name);
    }

    function editCondition(i) {
        const s = state.strategies.find(x => x.name === selectedStrategy());
        if (!s) return;
        const c = s.conditions[i];
        const el = document.getElementById('conditionEditor');
        if (!c || !el) return;
        el.innerHTML = `
            <label>Condition type</label>
            <select id="condKind">
              ${['entry_window', 'short_delta', 'spread_width', 'credit', 'trend', 'volatility'].map(k => `<option value="${k}" ${c.kind === k ? 'selected' : ''}>${k}</option>`).join('')}
            </select>
            <div><label>Enabled</label><input type="checkbox" id="condEnabled" ${c.enabled ? 'checked' : ''} /></div>
            <label>Params (JSON)</label><textarea id="condParams" rows="3">${JSON.stringify(c.params)}</textarea>
            <button onclick="applyCondition(${i})">Apply</button>`;
    }

    function applyCondition(i) {
        const s = state.strategies.find(x => x.name === selectedStrategy());
        if (!s) return;
        const c = s.conditions[i];
        if (!c) return;
        c.kind = document.getElementById('condKind').value;
        c.enabled = document.getElementById('condEnabled').checked;
        try { c.params = JSON.parse(document.getElementById('condParams').value || '{}'); }
        catch (e) { showOrderToast('Invalid params JSON', 'err'); return; }
        renderStrategyEditor(s.name);
    }

    function removeCondition(i) {
        const s = state.strategies.find(x => x.name === selectedStrategy());
        if (!s) return;
        s.conditions.splice(i, 1);
        renderStrategyEditor(s.name);
    }

    function moveCondition(i, dir) {
        const s = state.strategies.find(x => x.name === selectedStrategy());
        if (!s) return;
        const j = i + dir;
        if (j < 0 || j >= s.conditions.length) return;
        const tmp = s.conditions[i];
        s.conditions[i] = s.conditions[j];
        s.conditions[j] = tmp;
        renderStrategyEditor(s.name);
    }

    function toggleTp(cb) {
        const inp = document.getElementById('tpValue');
        if (!inp) return;
        inp.disabled = !cb.checked;
        if (!cb.checked) inp.value = '';
    }

    function saveStrategyFromForm() {
        const s = state.strategies.find(x => x.name === selectedStrategy());
        if (!s) return;
        const newName = document.getElementById('stratName').value.trim() || s.name;
        const tp = document.getElementById('tpEnabled').checked
            ? { mode: 'pct_credit', value: parseFloat(document.getElementById('tpValue').value) || 0 } : null;
        const sl = document.getElementById('slEnabled').checked
            ? { multiplier: parseFloat(document.getElementById('slMultiplier').value) || 0 } : null;
        const updated = Object.assign({}, s, {
            name: newName,
            direction: document.getElementById('stratDirection').value,
            exit_rules: { take_profit: tp, stop_loss: sl, hold_to_expire: false },
            auto_execute: document.getElementById('autoExec').checked,
        });
        const idx = state.strategies.findIndex(x => x.name === s.name);
        if (idx >= 0) state.strategies[idx] = updated;
        _selStrategy = newName;
        sendWsMessage('strategy_save:', updated);
        renderStrategyList();
        renderStrategyEditor(newName);
    }

    function armStrategy(name) {
        sendWsMessage('strategy_arm:', name);
    }

    function deleteStrategy(name) {
        sendWsMessage('strategy_delete:', name);
        state.strategies = state.strategies.filter(x => x.name !== name);
        if (_selStrategy === name) _selStrategy = state.strategies[0] ? state.strategies[0].name : null;
        renderStrategyList();
        if (_selStrategy) renderStrategyEditor(_selStrategy);
        else { const el = document.getElementById('strategyEditor'); if (el) el.innerHTML = ''; }
    }

    function sendWsMessage(prefix, body) {
        if (ws && ws.readyState === WebSocket.OPEN) ws.send(prefix + JSON.stringify(body));
    }

    function renderScanner(name) {
        const el = document.getElementById('strategyScanner');
        if (!el) return;
        const cands = state.strategyCandidates[name] || [];
        const s = state.strategies.find(x => x.name === name);
        const rows = cands.map(c => `
            <div class="cand-row">SELL ${c.short_strike} ${c.direction === 'bull_put' ? 'P' : 'C'} / BUY ${c.long_strike}
             · credit $${(c.credit_mid || 0).toFixed(2)} · d=${(c.short_delta || 0).toFixed(2)} · w=${c.width_points}
             ${s && s.auto_execute ? '<span>(auto)</span>' : `<button onclick="placeStrategyCandidate('${name}')">Place</button>`}</div>`).join('');
        el.innerHTML = `<h3>Live candidates</h3>` + (cands.length ? rows : '<p>No matches</p>');
    }

    function placeStrategyCandidate(name) {
        const s = state.strategies.find(x => x.name === name);
        const cands = state.strategyCandidates[name] || [];
        if (!cands.length) { showOrderToast('No candidate to place', 'info'); return; }
        if (s && s.auto_execute) { showOrderToast('Auto-execute on; strategy places automatically', 'info'); return; }
        const c = cands[0];
        const right = (s && s.direction === 'bull_put') ? 'P' : 'C';
        const expiry = state.chainMeta ? (state.chainMeta.expiration_raw || '') : '';
        const sq = state.chainData[c.short_strike], lq = state.chainData[c.long_strike];
        const shortLmt = sq ? ((right === 'C' ? sq.call_bid : sq.put_bid) || c.credit_mid) : c.credit_mid;
        const longLmt = lq ? ((right === 'C' ? lq.call_ask : lq.put_ask) || c.credit_mid) : c.credit_mid;
        const legs = [
            { symbol: 'SPX', expiry, strike: c.short_strike, right, action: 'SELL', qty: 1, lmtPrice: shortLmt, secType: 'OPT' },
            { symbol: 'SPX', expiry, strike: c.long_strike, right, action: 'BUY', qty: 1, lmtPrice: longLmt, secType: 'OPT' },
        ];
        let stopLoss = null;
        if (s && s.exit_rules && s.exit_rules.stop_loss) {
            const mag = (c.credit_mid || 0) * s.exit_rules.stop_loss.multiplier;
            stopLoss = { stopPrice: -mag, limitPrice: -mag };
        }
        const payload = {
            legs, orderType: 'LMT', tif: 'DAY', comboAction: 'BUY',
            comboLmtPrice: -Math.round((c.credit_mid || 0) * 100) / 100,
            comboQuantity: 1, outsideRth: false, stopLoss,
        };
        if (typeof sendPlaceOrder === 'function') {
            sendPlaceOrder(payload, (resp) => {
                if (resp && resp.status && resp.status !== 'Error') showOrderToast('Order submitted: ' + (resp.message || ''), 'ok');
                else showOrderToast('Order failed: ' + (resp && resp.message || 'Unknown error'), 'err');
            });
        }
    }

    function handleStrategyMessage(msg) {
        switch (msg.type) {
            case 'strategy_list':
                state.strategies = (msg.data.strategies || []);
                state.killSwitch = msg.data.kill_switch;
                renderStrategyList();
                if (_selStrategy) renderStrategyEditor(_selStrategy);
                else if (state.strategies[0]) selectStrategy(state.strategies[0].name);
                renderScanner(_selStrategy);
                break;
            case 'strategy_candidate':
                state.strategyCandidates[msg.data.name] = msg.data.candidates;
                if (msg.data.name === _selStrategy) renderScanner(msg.data.name);
                break;
            case 'strategy_exit':
                showOrderToast('strategy ' + msg.data.name + ': ' + msg.data.event, 'info');
                break;
            case 'vix_update':
                state.vix = msg.data.vix;
                if (document.getElementById('vixBadge')) document.getElementById('vixBadge').textContent = (state.vix != null) ? state.vix.toFixed(2) : '-';
                break;
            default:
                console.warn('Unknown strategy message:', msg.type);
        }
    }

    // ======================================================================
