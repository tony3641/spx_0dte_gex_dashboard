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
