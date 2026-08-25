    // Log console tab — renders framework log records streamed over the socket.
    // ======================================================================
    let _logBuf = [];          // [{seq, ts, level, name, msg}]
    let _logMax = 500;         // keep the console bounded on the client too
    let _logSeen = new Set();  // dedupe overlap between log_history backlog and live log records
    let _logAutoScroll = true;

    function _logEmptyHtml() {
        return '<div class="log-empty">No framework logs yet.</div>';
    }

    function _applyLogEmpty(c) {
        const empty = document.getElementById('logEmpty');
        if (empty) empty.remove();
        if (!c.querySelector('.log-line')) {
            c.insertAdjacentHTML('afterbegin', _logEmptyHtml());
        }
    }

    function _updateLogCount() {
        const el = document.getElementById('logCount');
        if (el) el.textContent = _logBuf.length + ' lines';
    }

    function _buildLogLine(e) {
        const div = document.createElement('div');
        div.className = 'log-line log-' + String(e.level || 'info').toLowerCase();
        div.textContent = e.msg || '';
        return div;
    }

    function _scrollLogToBottom() {
        const c = document.getElementById('logConsole');
        if (c) c.scrollTop = c.scrollHeight;
    }

    function handleLogMessage(msg) {
        if (msg.type === 'log_history') {
            _logBuf = (msg.data || []).slice(-_logMax);
            _logSeen = new Set(_logBuf.map(e => e.seq));
            const c = document.getElementById('logConsole');
            if (!c) return;
            c.innerHTML = '';
            for (const e of _logBuf) c.appendChild(_buildLogLine(e));
            _applyLogEmpty(c);
            _updateLogCount();
            _scrollLogToBottom();
        } else if (msg.type === 'log') {
            const e = msg.data;
            if (!e || _logSeen.has(e.seq)) return;
            _logSeen.add(e.seq);
            _logBuf.push(e);
            if (_logBuf.length > _logMax) {
                _logBuf = _logBuf.slice(-_logMax);
            }
            const c = document.getElementById('logConsole');
            if (c) {
                const empty = document.getElementById('logEmpty');
                if (empty) empty.remove();
                c.appendChild(_buildLogLine(e));
                _trimDom(c);
                _updateLogCount();
                if (_logAutoScroll) _scrollLogToBottom();
            }
        }
    }

    // Keep the DOM bounded too (appendChild only grows it; the in-memory buffer
    // is already capped). Drop the oldest .log-line nodes past the cap.
    function _trimDom(c) {
        const kids = c.querySelectorAll('.log-line');
        const excess = kids.length - _logMax;
        for (let i = 0; i < excess; i++) kids[i].remove();
    }

    function clearLogConsole() {
        _logBuf = [];
        _logSeen = new Set();
        const c = document.getElementById('logConsole');
        if (c) c.innerHTML = _logEmptyHtml();
        _updateLogCount();
    }

    function _initLogConsoleScroll() {
        const c = document.getElementById('logConsole');
        if (!c) return;
        c.addEventListener('scroll', () => {
            const gap = c.scrollHeight - c.scrollTop - c.clientHeight;
            _logAutoScroll = gap < 24;   // stick to bottom unless user scrolled up
        });
    }

    document.addEventListener('DOMContentLoaded', _initLogConsoleScroll);

    // ======================================================================
