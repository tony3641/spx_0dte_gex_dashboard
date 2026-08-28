    // Settings modal (Discord + IB). Values persist to the server-side .env;
    // applies are hot-applied per section (Discord bot restart / IB reconnect).
    // ======================================================================
    let settingsLoaded = false;

    async function openSettings() {
        document.getElementById('settingsModalBackdrop').classList.remove('hidden');
        if (!settingsLoaded) await loadSettings();
    }

    function closeSettings() {
        document.getElementById('settingsModalBackdrop').classList.add('hidden');
    }

    async function loadSettings() {
        try {
            const [discord, ib] = await Promise.all([
                fetch('/api/settings/discord').then(r => {
                    if (!r.ok) throw new Error(r.statusText);
                    return r.json();
                }),
                fetch('/api/settings/ib').then(r => {
                    if (!r.ok) throw new Error(r.statusText);
                    return r.json();
                }),
            ]);
            const tok = document.getElementById('setDiscordToken');
            tok.value = '';
            tok.placeholder = discord.token_set
                ? ('•••• ' + (discord.token_hint || ''))
                : 'Not set';
            document.getElementById('setDiscordGuildId').value = discord.guild_id || '';
            document.getElementById('setDiscordChannelId').value = discord.channel_id || '';
            document.getElementById('setDiscordUserIds').value = (discord.allowed_user_ids || []).join(',');
            document.getElementById('setDiscordRole').value = discord.allowed_role || '';
            document.getElementById('setDiscordClearToken').checked = false;
            const status = document.getElementById('setDiscordStatus');
            status.textContent = discord.running ? '● Running'
                : (discord.token_set ? '○ Stopped' : '○ Disabled (no token)');
            status.className = 'settings-status ' + (discord.running ? 'ok' : 'off');
            document.getElementById('setIbPort').value = ib.port;
            document.getElementById('setDiscordResult').textContent = '';
            document.getElementById('setIbResult').textContent = '';
            document.getElementById('setWatchlistResult').textContent = '';
            await loadWatchlists();          // self-contained; own error handling
            settingsLoaded = true;
        } catch (e) {
            document.getElementById('setDiscordResult').textContent =
                'Failed to load settings: ' + e.message;
        }
    }

    function setSettingsResult(id, text, ok) {
        const el = document.getElementById(id);
        el.textContent = text;
        el.className = 'settings-result ' + (ok ? 'ok' : 'err');
    }

    async function applyDiscordSettings() {
        const body = {
            guild_id: document.getElementById('setDiscordGuildId').value.trim(),
            channel_id: document.getElementById('setDiscordChannelId').value.trim(),
            allowed_user_ids: document.getElementById('setDiscordUserIds').value.trim(),
            allowed_role: document.getElementById('setDiscordRole').value.trim(),
        };
        if (document.getElementById('setDiscordClearToken').checked) {
            body.token = '';                     // clear = disable
        } else {
            const tok = document.getElementById('setDiscordToken').value.trim();
            if (tok) body.token = tok;           // omitted = keep existing
        }
        const btn = document.getElementById('setDiscordApplyBtn');
        btn.disabled = true;
        btn.textContent = 'Applying...';
        try {
            const r = await fetch('/api/settings/discord', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            const data = await r.json().catch(() => ({}));
            if (!r.ok) throw new Error(data.detail || r.statusText || 'Apply failed');
            let msg = data.running ? 'Applied — bot running.' : 'Applied — Discord disabled.';
            if (data.persisted === false) msg += ' Warning: could not write .env — settings lost on restart.';
            setSettingsResult('setDiscordResult', msg, true);
            settingsLoaded = false;              // reload status on next open
        } catch (e) {
            setSettingsResult('setDiscordResult', 'Apply failed: ' + e.message, false);
        } finally {
            btn.disabled = false;
            btn.textContent = 'Apply Discord';
        }
    }

    async function applyIbSettings() {
        const port = parseInt(document.getElementById('setIbPort').value, 10);
        if (!Number.isInteger(port) || port <= 0 || port > 65535) {
            setSettingsResult('setIbResult', 'Port must be an integer between 1 and 65535.', false);
            return;
        }
        const btn = document.getElementById('setIbApplyBtn');
        btn.disabled = true;
        document.getElementById('overlayMsg').textContent = `Reconnecting IB on port ${port}...`;
        document.getElementById('loadingOverlay').classList.remove('hidden');
        try {
            const r = await fetch('/api/settings/ib', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ port }),
            });
            const data = await r.json().catch(() => ({}));
            if (!r.ok) throw new Error(data.detail || r.statusText || 'Reconnect failed');
            let msg = `Applied — IB reconnected on port ${data.port}.`;
            if (data.persisted === false) msg += ' Warning: could not write .env — port reverts on restart.';
            setSettingsResult('setIbResult', msg, true);
        } catch (e) {
            setSettingsResult('setIbResult',
                'Apply failed: ' + e.message + ' (previous port still saved — retry recovers)', false);
        } finally {
            document.getElementById('loadingOverlay').classList.add('hidden');
            btn.disabled = false;
        }
    }

    // Watchlists ------------------------------------------------------------
    // Config-only editor. No live quotes are fetched/rendered here; the state is
    // a full copy of GET /api/watchlist and Save POSTs the ENTIRE set back.
    let watchlistsState = [];

    async function loadWatchlists() {
        try {
            const res = await fetch('/api/watchlist');
            if (!res.ok) throw new Error('HTTP ' + res.status + ' ' + res.statusText);
            const data = await res.json();
            watchlistsState = (data.watchlists || []).map(w => ({
                name: w.name || '',
                entries: (w.entries || []).map(e => ({
                    symbol: e.symbol || '',
                    sec_type: e.sec_type || 'STK',
                    exchange: e.exchange || 'SMART',
                    display_name: e.display_name || '',
                })),
            }));
            setSettingsResult('setWatchlistResult', '', true);
            renderWatchlistEditor();
        } catch (e) {
            setSettingsResult('setWatchlistResult', 'Failed to load watchlists: ' + e.message, false);
            renderWatchlistEditor();
        }
    }

    function renderWatchlistEditor() {
        const container = document.getElementById('watchlistEditor');
        container.textContent = '';
        if (watchlistsState.length === 0) {
            const hint = document.createElement('div');
            hint.className = 'settings-status off';
            hint.textContent = 'No watchlists yet. Click "+ New watchlist" to add one.';
            container.appendChild(hint);
            return;
        }
        watchlistsState.forEach((wl, wi) => container.appendChild(buildWatchlistBlock(wl, wi)));
    }

    function buildWatchlistBlock(wl, wi) {
        const block = document.createElement('div');
        block.className = 'settings-section';

        // Editable name
        const nameField = document.createElement('label');
        nameField.className = 'settings-field';
        const nameSpan = document.createElement('span');
        nameSpan.textContent = 'Name';
        const nameInput = document.createElement('input');
        nameInput.type = 'text';
        nameInput.value = wl.name || '';
        nameInput.placeholder = 'Watchlist name';
        nameInput.addEventListener('input', () => { watchlistsState[wi].name = nameInput.value; });
        nameField.appendChild(nameSpan);
        nameField.appendChild(nameInput);
        block.appendChild(nameField);

        // Entries (symbol + sec_type + exchange, with Remove)
        wl.entries.forEach((e, ei) => block.appendChild(buildEntryRow(wi, ei)));

        // "Add symbol" control
        const addTitle = document.createElement('div');
        addTitle.className = 'settings-section-title';
        addTitle.style.marginTop = '10px';
        addTitle.textContent = 'Add symbol';
        block.appendChild(addTitle);

        const symInput = fieldWithInput(block, 'Symbol', 'wl-sym', 'text', 'e.g. SPY');
        const typeSel = fieldWithSelect(block, 'Type', 'wl-type');
        const exchInput = fieldWithInput(block, 'Exchange', 'wl-exch', 'text', 'SMART');
        exchInput.value = 'SMART';
        const dnInput = fieldWithInput(block, 'Display name', 'wl-dn', 'text', 'Optional');

        const addBtn = document.createElement('button');
        addBtn.type = 'button';
        addBtn.className = 'order-modal-confirm';
        addBtn.style.padding = '5px 16px';
        addBtn.style.marginRight = '8px';
        addBtn.textContent = '+ Add';
        addBtn.addEventListener('click', () => addWatchlistSymbol(wi, symInput, typeSel, exchInput, dnInput));
        block.appendChild(addBtn);

        const delBtn = document.createElement('button');
        delBtn.type = 'button';
        delBtn.className = 'order-modal-cancel';
        delBtn.style.padding = '5px 16px';
        delBtn.textContent = 'Delete watchlist';
        delBtn.addEventListener('click', () => deleteWatchlist(wi));
        block.appendChild(delBtn);

        return block;
    }

    function buildEntryRow(wi, ei) {
        const e = watchlistsState[wi].entries[ei];
        const row = document.createElement('div');
        row.className = 'settings-field';
        const span = document.createElement('span');
        let label = [e.symbol || '', e.sec_type || '', e.exchange || 'SMART']
            .filter(Boolean).join(' · ');
        if (e.display_name) label += ' (' + e.display_name + ')';
        span.textContent = label;
        span.title = label;
        const rm = document.createElement('button');
        rm.type = 'button';
        rm.className = 'order-modal-cancel';
        rm.style.padding = '4px 12px';
        rm.textContent = 'Remove';
        rm.addEventListener('click', () => removeWatchlistEntry(wi, ei));
        row.appendChild(span);
        row.appendChild(rm);
        return row;
    }

    function fieldWithInput(block, labelText, cls, type, placeholder) {
        const field = document.createElement('label');
        field.className = 'settings-field';
        const span = document.createElement('span');
        span.textContent = labelText;
        const input = document.createElement('input');
        input.type = type;
        input.className = cls;
        input.placeholder = placeholder || '';
        field.appendChild(span);
        field.appendChild(input);
        block.appendChild(field);
        return input;
    }

    function fieldWithSelect(block, labelText, cls) {
        const field = document.createElement('label');
        field.className = 'settings-field';
        const span = document.createElement('span');
        span.textContent = labelText;
        const sel = document.createElement('select');
        sel.className = cls;
        // Match .settings-field input theme (no select rule exists in settings.css).
        sel.style.background = '#0b1220';
        sel.style.color = '#e2e8f0';
        sel.style.border = '1px solid #334155';
        sel.style.borderRadius = '5px';
        sel.style.padding = '6px 8px';
        sel.style.fontSize = '13px';
        sel.style.flex = '1';
        sel.style.minWidth = '0';
        ['STK', 'IND'].forEach(t => {
            const opt = document.createElement('option');
            opt.value = t;
            opt.textContent = t;
            sel.appendChild(opt);
        });
        sel.value = 'STK';
        field.appendChild(span);
        field.appendChild(sel);
        block.appendChild(field);
        return sel;
    }

    function addWatchlistSymbol(wi, symInput, typeSel, exchInput, dnInput) {
        const symbol = symInput.value.trim();
        if (!symbol) {
            setSettingsResult('setWatchlistResult', 'Symbol is required.', false);
            return;
        }
        const upper = symbol.toUpperCase();
        if (watchlistsState[wi].entries.some(e => (e.symbol || '').toUpperCase() === upper)) {
            setSettingsResult('setWatchlistResult', 'Duplicate symbol in this watchlist.', false);
            return;
        }
        watchlistsState[wi].entries.push({
            symbol: upper,
            sec_type: typeSel.value || 'STK',
            exchange: (exchInput.value.trim() || 'SMART').toUpperCase(),
            display_name: dnInput.value.trim(),
        });
        setSettingsResult('setWatchlistResult', '', true);
        renderWatchlistEditor();
    }

    function removeWatchlistEntry(wi, ei) {
        watchlistsState[wi].entries.splice(ei, 1);
        renderWatchlistEditor();
    }

    function deleteWatchlist(wi) {
        watchlistsState.splice(wi, 1);
        renderWatchlistEditor();
    }

    function addNewWatchlist() {
        const used = new Set(watchlistsState.map(w => (w.name || '').toLowerCase()));
        let n = 1;
        let name = 'New Watchlist';
        while (used.has(name.toLowerCase())) { n += 1; name = 'New Watchlist ' + n; }
        watchlistsState.push({ name, entries: [] });
        setSettingsResult('setWatchlistResult', '', true);
        renderWatchlistEditor();
    }

    async function saveWatchlists() {
        const btn = document.getElementById('setWatchlistSaveBtn');
        const payload = {
            watchlists: watchlistsState.map(w => ({
                name: (w.name || '').trim(),
                entries: (w.entries || []).map(e => ({
                    symbol: (e.symbol || '').trim(),
                    sec_type: e.sec_type || 'STK',
                    exchange: (e.exchange || 'SMART').trim() || 'SMART',
                    display_name: (e.display_name || '').trim(),
                })),
            })),
        };
        btn.disabled = true;
        btn.textContent = 'Saving...';
        try {
            const res = await fetch('/api/watchlist', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
            });
            const text = await res.text();
            let data = null;
            try { data = JSON.parse(text); } catch (_) { data = null; }
            if (!res.ok) {
                const detail = (data && data.detail) ? data.detail : text;
                throw new Error(detail || 'Save failed (HTTP ' + res.status + ')');
            }
            // Server returns the normalized store — adopt it so the editor
            // reflects what was actually persisted.
            if (data && Array.isArray(data.watchlists)) {
                watchlistsState = data.watchlists;
                renderWatchlistEditor();
            }
            setSettingsResult('setWatchlistResult', 'Watchlists saved.', true);
        } catch (e) {
            setSettingsResult('setWatchlistResult', 'Save failed: ' + e.message, false);
        } finally {
            btn.disabled = false;
            btn.textContent = 'Save Watchlists';
        }
    }
