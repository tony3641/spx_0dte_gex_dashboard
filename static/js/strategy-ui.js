// Strategy UI — list, editor, scanner results, WS handlers.
function newStrategy() {
    const s = {name: 'New ' + (state.strategies.length + 1), direction: 'bull_put',
        conditions: [], exit_rules: {take_profit: null, stop_loss: null, hold_to_expire: false},
        auto_execute: false, armed: false};
    state.strategies.push(s);
    renderStrategyList(); renderStrategyEditor(s.name);
}

function renderStrategyList() {
    const nav = document.getElementById('strategyNav');
    nav.innerHTML = state.strategies.map(s =>
        `<div class="strat-item ${s.name===selectedStrategy()?'active':''}" onclick="renderStrategyEditor('${s.name}')">${s.name}</div>`).join('');
}

function selectedStrategy() { return window._selStrategy || (state.strategies[0] && state.strategies[0].name); }

function renderStrategyEditor(name) {
    window._selStrategy = name;
    const s = state.strategies.find(x => x.name === name);
    if (!s) return;
    const el = document.getElementById('strategyEditor');
    el.innerHTML = `
    <h3>${s.name}</h3>
    <label>Direction</label>
    <select id="stratDirection">
      <option value="bull_put" ${s.direction==='bull_put'?'selected':''}>Bull Put</option>
      <option value="bear_call" ${s.direction==='bear_call'?'selected':''}>Bear Call</option>
    </select>
    <label>Conditions (priority order)</label>
    <ol id="stratCondList">${s.conditions.map((c,i)=>`<li onclick="editCondition(${i})">${c.kind}</li>`).join('')}</ol>
    <button onclick="addCondition()">+ Add Condition</button>
    <div id="conditionEditor"></div>
    <label>Exit rules</label>
    <div>
      <label><input type="checkbox" id="tpEnabled" onchange="toggleTp(this)"> Take Profit</label>
      <input id="tpValue" placeholder="% of credit or $" />
      <label><input type="checkbox" id="slEnabled"> Stop Loss</label>
      <input id="slMultiplier" type="number" step="0.1" placeholder="multiplier (e.g. 5 = 5x credit)" title="Stop trigger = credit * multiplier" />
    </div>
    <label class="caps"><input type="checkbox" id="autoExec" ${s.auto_execute?'checked':''}> Auto-execute</label>
    <button onclick="saveStrategyFromForm()">Save</button>
    <button onclick="armStrategy()">${s.armed?'Disarm':'Arm'}</button>
    <button onclick="deleteStrategy()">Delete</button>`;
    if (s.exit_rules.take_profit) { document.getElementById('tpEnabled').checked = true; document.getElementById('tpValue').value = s.exit_rules.take_profit.value; }
    if (s.exit_rules.stop_loss) { document.getElementById('slEnabled').checked = true; document.getElementById('slMultiplier').value = s.exit_rules.stop_loss.multiplier; }
}

function saveStrategyFromForm() {
    const s = state.strategies.find(x => x.name === selectedStrategy());
    if (!s) return;
    s.direction = document.getElementById('stratDirection').value;
    s.auto_execute = document.getElementById('autoExec').checked;
    const tp = document.getElementById('tpEnabled').checked ? {mode:'pct_credit', value: parseFloat(document.getElementById('tpValue').value)||0} : null;
    const sl = document.getElementById('slEnabled').checked ? {multiplier: parseFloat(document.getElementById('slMultiplier').value)||0} : null;
    s.exit_rules = {take_profit: tp, stop_loss: sl, hold_to_expire: false};
    sendWsMessage('strategy_save:', s);
    renderStrategyList();
}

function armStrategy() {
    const s = state.strategies.find(x => x.name === selectedStrategy());
    if (!s) return;
    sendWsMessage('strategy_arm:', s.name);
}

function deleteStrategy() {
    const s = state.strategies.find(x => x.name === selectedStrategy());
    if (!s) return;
    sendWsMessage('strategy_delete:', s.name);
    state.strategies = state.strategies.filter(x => x.name !== s.name);
    renderStrategyList();
}

function sendWsMessage(prefix, body) {
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(prefix + JSON.stringify(body));
}

function renderScanner(name) {
    const el = document.getElementById('strategyScanner');
    const cands = state.strategyCandidates[name] || [];
    el.innerHTML = `<h3>Live candidates</h3>` + cands.map(c =>
        `<div class="cand-row">SELL ${c.short_strike} ${c.direction==='bull_put'?'P':'C'} / BUY ${c.long_strike}
         · credit $${c.credit_mid?.toFixed(2)} · d=${c.short_delta?.toFixed(2)} · w=${c.width_points}
         <button onclick="placeStrategyCandidate('${name}')">Place</button></div>`).join('') || '<p>No matches</p>';
}

function handleStrategyMessage(msg) {
    switch (msg.type) {
        case 'strategy_list': state.strategies = (msg.data.strategies||[]); state.killSwitch = msg.data.kill_switch; renderStrategyList(); renderScanner(selectedStrategy()); break;
        case 'strategy_candidate': state.strategyCandidates[msg.data.name] = msg.data.candidates; if (selectedStrategy()===msg.data.name) renderScanner(msg.data.name); break;
        case 'strategy_exit': showOrderToast('strategy ' + msg.data.name + ': ' + msg.data.event, 'info'); break;
        case 'vix_update': state.vix = msg.data.vix; if (document.getElementById('vixBadge')) document.getElementById('vixBadge').textContent = state.vix?.toFixed(2); break;
    }
}
