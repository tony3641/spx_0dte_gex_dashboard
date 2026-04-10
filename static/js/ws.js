    // WebSocket
    // ======================================================================
    let ws = null;
    let reconnectTimer = null;

    function connectWS() {
        const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
        const url = `${proto}//${location.host}/ws`;
        console.log('Connecting to', url);

        ws = new WebSocket(url);

        ws.onopen = () => {
            console.log('WebSocket connected');
            state.wsConnected = true;
            document.getElementById('loadingOverlay').classList.add('hidden');
            ws.send(`set_tab:${getValidTab(state.activeTab)}`);
            if (state.activeTab === 'chain') {
                setTimeout(() => { reportChainViewportCenter(true); }, 120);
            }
            if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
        };

        ws.onclose = () => {
            console.log('WebSocket closed');
            state.wsConnected = false;
            document.getElementById('connDot').className = 'dot dot-red';
            document.getElementById('connStatus').textContent = 'Disconnected';
            // Show overlay with reconnecting message
            document.getElementById('overlayMsg').textContent = 'Reconnecting...';
            document.getElementById('loadingOverlay').classList.remove('hidden');
            scheduleReconnect();
        };

        ws.onerror = (e) => {
            console.error('WebSocket error', e);
        };

        ws.onmessage = (event) => {
            try {
                const msg = JSON.parse(event.data);
                handleMessage(msg);
            } catch (e) {
                const snippet = typeof event.data === 'string' ? event.data.slice(0, 240) : '[non-string payload]';
                console.error('Failed to parse message', e, snippet);
            }
        };
    }

    function scheduleReconnect() {
        if (reconnectTimer) return;
        reconnectTimer = setTimeout(() => {
            reconnectTimer = null;
            connectWS();
        }, 3000);
    }

    // ======================================================================
