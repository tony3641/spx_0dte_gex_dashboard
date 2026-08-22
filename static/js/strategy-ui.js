    // Strategy UI — list, editor, scanner results, WS handlers.
    // ======================================================================
    let _selStrategy = null;

    const COND_OPS = ['below', 'above', 'range'];
    const DAY_LABELS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri'];

    // Per-condition-type field schema. `optional` fields are blank or "n/a" =>
    // the bound is dropped (i.e. unbounded on that side); `required` fields must
    // be filled and cannot be "n/a". The engine maps op 'above'->hi, 'below'->lo,
    // 'range'->[lo,hi], so op-driven levels expose a single `value` for
    // above/below and `low`+`high` for range.
    const CONDITION_DEFS = {
        entry_window: {
            help: 'Time-of-day window (ET) you are willing to enter.',
            opDriven: false,
            fields: [
                { key: 'start', label: 'Start (HH:MM)', type: 'text', default: '09:30', required: true },
                { key: 'end', label: 'End (HH:MM)', type: 'text', default: '15:30', required: true },
            ],
        },
        short_delta: {
            help: 'Abs delta range for the short leg. Leave a side blank/n-a for no bound.',
            opDriven: false,
            fields: [
                { key: 'min', label: 'Min delta', type: 'number', step: '0.01', default: 0.05, optional: true },
                { key: 'max', label: 'Max delta', type: 'number', step: '0.01', default: 0.35, optional: true },
            ],
        },
        spread_width: {
            help: 'Strike distance (points) between the legs.',
            opDriven: false,
            fields: [
                { key: 'min', label: 'Min width', type: 'number', step: '1', default: 5, optional: true },
                { key: 'max', label: 'Max width', type: 'number', step: '1', default: 50, optional: true },
            ],
        },
        credit: {
            help: 'Net credit per spread (points). Leave a side blank/n-a for no bound.',
            opDriven: false,
            fields: [
                { key: 'min', label: 'Min credit', type: 'number', step: '0.05', default: 0.30, optional: true },
                { key: 'max', label: 'Max credit', type: 'number', step: '0.05', default: null, optional: true },
            ],
        },
        trend: {
            help: 'RSI (rsi) or % move (pmove) over price history. Range uses low/high; above/below use Level.',
            opDriven: true,
            config: [
                { key: 'indicator', label: 'Indicator', type: 'enum', options: ['rsi', 'pmove'], default: 'rsi' },
                { key: 'period', label: 'Period (rsi)', type: 'number', default: 14, optional: true },
                { key: 'minutes', label: 'Minutes (pmove)', type: 'number', default: 5, optional: true },
            ],
            opKey: 'op',
            defaultOp: 'range',
            levels: [
                { key: 'value', label: 'Level', type: 'number', step: '0.1', optional: true },
                { key: 'low', label: 'Low', type: 'number', step: '0.1', optional: true },
                { key: 'high', label: 'High', type: 'number', step: '0.1', optional: true },
            ],
            levelsForOp: { below: ['value'], above: ['value'], range: ['low', 'high'] },
        },
        volatility: {
            help: 'VIX and/or ATM-IV buckets. Enable a bucket, then set op + level(s).',
            opDriven: true,
            config: [],   // each bucket group renders its own enabled toggle + op + levels
            buckets: [
                { prefix: 'vix', label: 'VIX', opKey: 'vix_op', defaultOp: 'below' },
                { prefix: 'atm_iv', label: 'ATM IV', opKey: 'atm_iv_op', defaultOp: 'below' },
            ],
            levels: [
                { key: 'value', label: 'Level', type: 'number', step: '0.1', optional: true },
                { key: 'low', label: 'Low', type: 'number', step: '0.1', optional: true },
                { key: 'high', label: 'High', type: 'number', step: '0.1', optional: true },
            ],
            levelsForOp: { below: ['value'], above: ['value'], range: ['low', 'high'] },
        },
    };

    let _condEditing = null;   // {index, kind} while a condition editor is open

    function esc(s) { return String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }
    function formatExcess() {
        const v = state.accountSummary && state.accountSummary.ExcessLiquidity;
        return (v != null && isFinite(v)) ? '$' + Number(v).toLocaleString() : 'unknown';
    }

    function selectedStrategy() { return _selStrategy; }

    function newStrategy() {
        const s = {
            name: 'New ' + (state.strategies.length + 1),
            direction: 'bull_put',
            conditions: [],
            exit_rules: { take_profit: null, stop_loss: null, hold_to_expire: false },
            auto_execute: false, armed: false,
            run_days: [0, 1, 2, 3, 4],
            short_day_enabled: false,
            run_on_fomc: true,
            run_on_nfp: true,
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
            <label>Name</label><input id="stratName" value="${esc(s.name)}" />
            <label>Direction</label>
            <select id="stratDirection">
              <option value="bull_put" ${s.direction === 'bull_put' ? 'selected' : ''}>Bull Put</option>
              <option value="bear_call" ${s.direction === 'bear_call' ? 'selected' : ''}>Bear Call</option>
            </select>
            <label>Conditions (priority order)</label>
            <ol id="stratCondList">${s.conditions.map((c, i) => `<li>${esc(c.kind)} ${c.enabled ? '' : '(disabled)'}
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
            <label>Budget (per-trade margin cap)</label>
            <input id="stratBudget" type="number" step="0.01" placeholder="n/a" value="${s.budget != null ? s.budget : ''}" />
            <div class="budget-liquidity">Excess liquidity: <span id="excessLiquidity">${formatExcess()}</span></div>
            <label>Run days</label>
            <div class="run-days" id="runDayRow">${[0,1,2,3,4].map(d => `<button class="day-btn ${(s.run_days || []).includes(d) ? 'active' : ''}" onclick="toggleRunDay(${d})">${DAY_LABELS[d]}</button>`).join('')}</div>
            <label class="caps"><input type="checkbox" id="shortDayEnabled" ${s.short_day_enabled ? 'checked' : ''}> Short/half trading day</label>
            <label class="caps"><input type="checkbox" id="runOnFomc" ${s.run_on_fomc ? 'checked' : ''}> Execute on FOMC</label>
            <label class="caps"><input type="checkbox" id="runOnNfp" ${s.run_on_nfp ? 'checked' : ''}> Execute on NFP</label>
            <label class="caps"><input type="checkbox" id="autoExec" ${s.auto_execute ? 'checked' : ''}> Auto-execute</label>
            <button onclick="validateStrategyFromForm()">Validate</button>
            <button onclick="saveStrategyFromForm()">Save</button>`;
        if (s.exit_rules.take_profit) { document.getElementById('tpEnabled').checked = true; document.getElementById('tpValue').value = s.exit_rules.take_profit.value; }
        if (s.exit_rules.stop_loss) { document.getElementById('slEnabled').checked = true; document.getElementById('slMultiplier').value = s.exit_rules.stop_loss.multiplier; }
        toggleTp(document.getElementById('tpEnabled'));
    }

    function toggleRunDay(day) {
        // Mutate the selected strategy's run_days in place and repaint only the
        // day row — re-rendering the whole editor would wipe in-progress fields.
        const s = state.strategies.find(x => x.name === selectedStrategy());
        if (!s) return;
        if (!s.run_days) s.run_days = [0, 1, 2, 3, 4];
        const i = s.run_days.indexOf(day);
        if (i >= 0) s.run_days.splice(i, 1); else s.run_days.push(day);
        s.run_days.sort((a, b) => a - b);
        renderRunDayRow(s);
    }

    function renderRunDayRow(s) {
        const row = document.getElementById('runDayRow');
        if (!row) return;
        row.innerHTML = [0, 1, 2, 3, 4].map(d =>
            `<button class="day-btn ${s.run_days.includes(d) ? 'active' : ''}" onclick="toggleRunDay(${d})">${DAY_LABELS[d]}</button>`).join('');
    }

    function addCondition() {
        const s = state.strategies.find(x => x.name === selectedStrategy());
        if (!s) return;
        s.conditions.push({ kind: 'short_delta', enabled: true, params: { min: 0.05, max: 0.35 } });
        renderStrategyEditor(s.name);
    }

    function _condFieldHtml(f, val, dflt, prefix, onChange) {
        prefix = prefix || '';
        onChange = onChange || '';
        const id = 'cf_' + prefix + f.key;
        const value = (val !== undefined && val !== null) ? val : dflt;
        const ph = f.optional ? 'n/a' : '';
        if (f.type === 'bool') {
            return `<div class="cond-field"><label><input type="checkbox" id="${id}" ${value ? 'checked' : ''}/> ${esc(f.label)}</label></div>`;
        }
        if (f.type === 'enum') {
            return `<div class="cond-field"><label>${esc(f.label)}</label><select id="${id}"${onChange ? ` onchange="${onChange}"` : ''}>${f.options.map(o => `<option ${o === value ? 'selected' : ''}>${esc(o)}</option>`).join('')}</select></div>`;
        }
        return `<div class="cond-field"><label>${esc(f.label)}${f.required ? ' <span class="cond-required">*</span>' : ''}</label><input id="${id}" type="${f.type}" ${f.step ? `step="${f.step}"` : ''} ${ph ? `placeholder="${ph}"` : ''} value="${value == null ? '' : value}" /></div>`;
    }

    function _condBucketHtml(bucket, p, def) {
        const enabled = !!p[bucket.prefix + '_enabled'];
        const opVal = (p[bucket.opKey] != null) ? p[bucket.opKey] : bucket.defaultOp;
        let html = `<div class="cond-group"><h4>${esc(bucket.label)}</h4>`;
        html += `<div class="cond-field"><label><input type="checkbox" id="cf_${bucket.prefix}_enabled" ${enabled ? 'checked' : ''}/> Enabled</label></div>`;
        html += `<div class="cond-field"><label>Op</label><select id="cf_${bucket.opKey}" onchange="condOpChanged('${bucket.prefix}')">${COND_OPS.map(o => `<option ${o === opVal ? 'selected' : ''}>${o}</option>`).join('')}</select></div>`;
        html += `<div class="cond-levels" id="cf_${bucket.prefix}_levels">`;
        html += def.levelsForOp[opVal].map(k => {
            const lv = def.levels.find(l => l.key === k);
            return _condFieldHtml(lv, p[`${bucket.prefix}_${k}`], lv.default, bucket.prefix + '_');
        }).join('');
        html += '</div></div>';
        return html;
    }

    function renderCondFields(i, kind, params) {
        const container = document.getElementById('condFields');
        const def = CONDITION_DEFS[kind];
        if (!container || !def) return;
        const p = params || {};
        let html = def.help ? `<div class="cond-help">${esc(def.help)}</div>` : '';

        if (!def.opDriven) {
            html += def.fields.map(f => _condFieldHtml(f, p[f.key], f.default)).join('');
            container.innerHTML = html;
            return;
        }

        html += (def.config || []).map(f => _condFieldHtml(f, p[f.key], f.default)).join('');

        if (def.buckets) {
            html += def.buckets.map(b => _condBucketHtml(b, p, def)).join('');
            container.innerHTML = html;
            return;
        }

        // Single op-driven bucket (trend).
        const opVal = (p[def.opKey] != null) ? p[def.opKey] : def.defaultOp;
        html += `<div class="cond-field"><label>Op</label><select id="cf_${def.opKey}" onchange="condOpChanged()">${COND_OPS.map(o => `<option ${o === opVal ? 'selected' : ''}>${o}</option>`).join('')}</select></div>`;
        html += `<div class="cond-levels" id="condLevels">${def.levelsForOp[opVal].map(k => {
            const lv = def.levels.find(l => l.key === k);
            return _condFieldHtml(lv, p[k], lv.default);
        }).join('')}</div>`;
        container.innerHTML = html;
    }

    function condKindChanged(i) {
        const kind = document.getElementById('condKind').value;
        const s = state.strategies.find(x => x.name === selectedStrategy());
        if (!s) return;
        _condEditing = { index: i, kind };
        renderCondFields(i, kind, s.conditions[i] && s.conditions[i].params);
    }

    function condOpChanged(prefix) {
        if (!_condEditing) return;
        const s = state.strategies.find(x => x.name === selectedStrategy());
        const c = s && s.conditions[_condEditing.index];
        if (!c) return;
        const def = CONDITION_DEFS[_condEditing.kind];
        if (!def || !def.opDriven) return;
        if (prefix) {
            const bucket = def.buckets.find(b => b.prefix === prefix);
            if (!bucket) return;
            const opVal = document.getElementById('cf_' + bucket.opKey).value;
            const lvC = document.getElementById('cf_' + prefix + '_levels');
            if (lvC) lvC.innerHTML = def.levelsForOp[opVal].map(k => {
                const lv = def.levels.find(l => l.key === k);
                return _condFieldHtml(lv, c.params[`${prefix}_${k}`], lv.default, prefix + '_');
            }).join('');
        } else {
            const opVal = document.getElementById('cf_' + def.opKey).value;
            const lvC = document.getElementById('condLevels');
            if (lvC) lvC.innerHTML = def.levelsForOp[opVal].map(k => {
                const lv = def.levels.find(l => l.key === k);
                return _condFieldHtml(lv, c.params[k], lv.default);
            }).join('');
        }
    }

    function editCondition(i) {
        const s = state.strategies.find(x => x.name === selectedStrategy());
        if (!s) return;
        const c = s.conditions[i];
        const el = document.getElementById('conditionEditor');
        if (!c || !el) return;
        _condEditing = { index: i, kind: c.kind };
        el.innerHTML = `
            <label>Condition type</label>
            <select id="condKind" onchange="condKindChanged(${i})">
              ${Object.keys(CONDITION_DEFS).map(k => `<option value="${k}" ${c.kind === k ? 'selected' : ''}>${esc(k)}</option>`).join('')}
            </select>
            <div><label>Enabled</label><input type="checkbox" id="condEnabled" ${c.enabled ? 'checked' : ''} /></div>
            <div id="condFields"></div>
            <button onclick="applyCondition(${i})">Apply</button>`;
        renderCondFields(i, c.kind, c.params);
    }

    function applyCondition(i) {
        const s = state.strategies.find(x => x.name === selectedStrategy());
        if (!s) return;
        const c = s.conditions[i];
        if (!c) return;
        const kind = document.getElementById('condKind').value;
        const def = CONDITION_DEFS[kind];
        if (!def) { showOrderToast('Unknown condition type', 'err'); return; }
        const params = {};

        function readField(key, label, opts) {
            const el = document.getElementById('cf_' + key);
            if (!el) return true;
            const raw = String(el.value || '').trim();
            if (opts.bool) { params[key] = el.checked; return true; }
            if (raw === '' || /^n\/?a$/i.test(raw)) {
                if (opts.required) { showOrderToast(label + ' is required', 'err'); return false; }
                return true;   // dropped => unbounded / engine default
            }
            if (opts.number) {
                const n = Number(raw);
                if (!Number.isFinite(n)) { showOrderToast(label + ' must be a number or n/a', 'err'); return false; }
                params[key] = n;
            } else {
                params[key] = raw;
            }
            return true;
        }

        if (!def.opDriven) {
            for (const f of def.fields) {
                if (readField(f.key, f.label, { required: f.required, number: f.type === 'number' }) === false) return;
            }
        } else {
            for (const f of (def.config || [])) {
                if (f.type === 'bool') { params[f.key] = document.getElementById('cf_' + f.key).checked; continue; }
                if (readField(f.key, f.label, { required: f.required, number: f.type === 'number' }) === false) return;
            }
            if (def.buckets) {
                for (const bucket of def.buckets) {
                    params[bucket.prefix + '_enabled'] = document.getElementById('cf_' + bucket.prefix + '_enabled').checked;
                    params[bucket.opKey] = document.getElementById('cf_' + bucket.opKey).value;
                    const opVal = params[bucket.opKey];
                    for (const k of def.levelsForOp[opVal]) {
                        const lv = def.levels.find(l => l.key === k);
                        if (readField(bucket.prefix + '_' + k, `${bucket.label} ${lv.label}`, { number: true }) === false) return;
                    }
                }
            } else {
                params[def.opKey] = document.getElementById('cf_' + def.opKey).value;
                const opVal = params[def.opKey];
                for (const k of def.levelsForOp[opVal]) {
                    const lv = def.levels.find(l => l.key === k);
                    if (readField(k, lv.label, { required: lv.required, number: lv.type === 'number' }) === false) return;
                }
            }
        }

        c.kind = kind;
        c.enabled = document.getElementById('condEnabled').checked;
        c.params = params;
        _condEditing = null;
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

    function readFormStrategy() {
        const s = state.strategies.find(x => x.name === selectedStrategy());
        if (!s) return { strategy: null, errors: ['No selected strategy'] };
        const errors = [];
        const name = document.getElementById('stratName').value.trim() || s.name;
        const direction = document.getElementById('stratDirection').value;
        const tpEnabled = document.getElementById('tpEnabled').checked;
        const slEnabled = document.getElementById('slEnabled').checked;
        let tp = null, sl = null, budget = null;

        if (tpEnabled) {
            const raw = document.getElementById('tpValue').value.trim();
            const v = Number(raw);
            if (!raw || !Number.isFinite(v) || v <= 0) errors.push('Take profit must be a positive number');
            else tp = { mode: 'pct_credit', value: v };
        }
        if (slEnabled) {
            const raw = document.getElementById('slMultiplier').value.trim();
            const v = Number(raw);
            if (!raw || !Number.isFinite(v) || v <= 0) errors.push('Stop loss multiplier must be a positive number');
            else sl = { multiplier: v };
        }
        const budgetRaw = document.getElementById('stratBudget').value.trim();
        if (budgetRaw !== '' && !/^n\/?a$/i.test(budgetRaw)) {
            const v = Number(budgetRaw);
            if (!Number.isFinite(v) || v < 0) errors.push('Budget must be a non-negative number or n/a');
            else {
                budget = v;
                const excess = state.accountSummary && state.accountSummary.ExcessLiquidity;
                if (excess != null && isFinite(excess) && v > Number(excess)) {
                    errors.push(`Budget $${Number(v).toLocaleString()} exceeds excess liquidity $${Number(excess).toLocaleString()}`);
                }
            }
        }
        const run_days = (s.run_days || []).slice().sort((a, b) => a - b);
        if (run_days.length === 0) errors.push('Select at least one run day');
        const strategy = Object.assign({}, s, {
            name, direction,
            conditions: s.conditions,
            exit_rules: { take_profit: tp, stop_loss: sl, hold_to_expire: false },
            auto_execute: document.getElementById('autoExec').checked,
            budget,
            run_days,
            short_day_enabled: document.getElementById('shortDayEnabled').checked,
            run_on_fomc: document.getElementById('runOnFomc').checked,
            run_on_nfp: document.getElementById('runOnNfp').checked,
        });
        return { strategy, errors };
    }

    function saveStrategyFromForm() {
        const s = state.strategies.find(x => x.name === selectedStrategy());
        if (!s) return;
        const { strategy, errors } = readFormStrategy();
        if (!strategy) return;
        if (errors.length) { showOrderToast(errors.join('; '), 'err'); return; }
        const idx = state.strategies.findIndex(x => x.name === s.name);
        if (idx >= 0) state.strategies[idx] = strategy;
        _selStrategy = strategy.name;
        sendWsMessage('strategy_save:', strategy);
        renderStrategyList();
        renderStrategyEditor(strategy.name);
    }

    function validateStrategyFromForm() {
        const { strategy, errors } = readFormStrategy();
        if (!strategy) return;
        openStrategyValidateModal(strategy, errors);
    }

    function openStrategyValidateModal(strategy, errors) {
        const body = document.getElementById('stratValidateBody');
        const backdrop = document.getElementById('stratValidateBackdrop');
        if (!body || !backdrop) return;
        let html = errors.length
            ? `<div class="strat-validate-errors"><strong>Validation errors:</strong><ul>${errors.map(e => `<li>${esc(e)}</li>`).join('')}</ul></div>`
            : '<div class="strat-validate-ok">&#10003; Format valid</div>';

        html += '<div class="modal-row"><span>Name</span><span>' + esc(strategy.name) + '</span></div>';
        html += '<div class="modal-row"><span>Direction</span><span>' + esc(strategy.direction) + '</span></div>';
        html += '<div class="modal-row"><span>Auto-execute</span><span>' + (strategy.auto_execute ? 'Yes' : 'No') + '</span></div>';
        html += '<div class="modal-row"><span>Budget</span><span>' + (strategy.budget != null ? '$' + Number(strategy.budget).toLocaleString() : 'n/a') + '</span></div>';
        html += '<div class="modal-row"><span>Run days</span><span>' + esc((strategy.run_days || []).map(d => DAY_LABELS[d]).join(', ')) + '</span></div>';
        html += '<div class="modal-row"><span>Short/half day</span><span>' + (strategy.short_day_enabled ? 'Allowed' : 'Skipped') + '</span></div>';
        html += '<div class="modal-row"><span>FOMC</span><span>' + (strategy.run_on_fomc ? 'Allowed' : 'Skipped') + '</span></div>';
        html += '<div class="modal-row"><span>NFP</span><span>' + (strategy.run_on_nfp ? 'Allowed' : 'Skipped') + '</span></div>';

        html += '<h4 class="strat-validate-h">Conditions (priority order)</h4><ol class="strat-validate-list">';
        html += (strategy.conditions || []).map((c) => {
            const params = Object.entries(c.params || {}).map(([k, v]) => `${k}=${v}`).join(', ');
            return `<li>${c.enabled ? '' : '(disabled) '}${esc(c.kind)}${params ? ' — ' + esc(params) : ''}</li>`;
        }).join('');
        html += '</ol>';

        const tp = strategy.exit_rules && strategy.exit_rules.take_profit;
        const sl = strategy.exit_rules && strategy.exit_rules.stop_loss;
        html += '<h4 class="strat-validate-h">Exit rules</h4><ul class="strat-validate-list">';
        if (tp) html += '<li>Take profit: ' + esc(tp.value) + (tp.mode === 'pct_credit' ? 'x credit' : '') + '</li>';
        if (sl) html += '<li>Stop loss: ' + esc(sl.multiplier) + 'x credit</li>';
        if (!tp && !sl) html += '<li>None</li>';
        html += '</ul>';

        body.innerHTML = html;
        backdrop.classList.remove('hidden');
    }

    function closeStrategyValidate() {
        const b = document.getElementById('stratValidateBackdrop');
        if (b) b.classList.add('hidden');
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
        if (ws && ws.readyState === WebSocket.OPEN) {
            // Object bodies go through JSON (strategy_save:). Plain-string names
            // (strategy_delete:/strategy_arm:) must NOT be quoted — the server
            // splits on ':' and uses the raw remainder as the strategy name.
            ws.send(prefix + (typeof body === 'string' ? body : JSON.stringify(body)));
        }
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
        const tradingClass = (state.chainMeta && (state.chainMeta.trading_class || 'SPXW')) || 'SPXW';
        const sq = state.chainData[c.short_strike], lq = state.chainData[c.long_strike];
        const shortLmt = sq ? ((right === 'C' ? sq.call_bid : sq.put_bid) || c.credit_mid) : c.credit_mid;
        const longLmt = lq ? ((right === 'C' ? lq.call_ask : lq.put_ask) || c.credit_mid) : c.credit_mid;
        const legs = [
            { symbol: 'SPX', expiry, strike: c.short_strike, right, action: 'SELL', qty: 1, lmtPrice: shortLmt, secType: 'OPT', trading_class: tradingClass },
            { symbol: 'SPX', expiry, strike: c.long_strike, right, action: 'BUY', qty: 1, lmtPrice: longLmt, secType: 'OPT', trading_class: tradingClass },
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
            case 'strategy_error':
                showOrderToast(msg.data && msg.data.message || 'Strategy rejected', 'err');
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
