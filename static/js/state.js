    // State
    // ======================================================================
    const state = {
        bars: [],          // [{time, open, high, low, close}]
        gex: null,         // latest gex data
        wsConnected: false,
        ibConnected: false,
        priceChartReady: false,
        gexChartReady: false,
        smileChartReady: false,
        dataMode: 'initializing',   // 'live' | 'historical' | 'initializing'
        historicalDate: '',
        currentSpot: 0,    // most up-to-date spot (live SPX or ES-derived)
        esDerived: false,  // true when currentSpot is ES-derived
        // Option chain tab state
        activeTab: 'dashboard',
        chainData: {},        // keyed by strike: {strike, call_bid, call_ask, ...}
        chainMeta: null,      // {spot_price, call_wall, put_wall, gamma_flip}
        strategyLegs: [],     // [{id, action, strike, right, qty, ...}]
        nextLegId: 1,
        prevCellValues: {},   // for flash animation: "strike_right_field" - prev value
        chainLastUpdateMs: null,
        chainAgeTimer: null,
        chainViewportCenterStrike: null,
        chainViewportPendingStrike: null,
        chainViewportLastSentMs: 0,
        chainViewportSendTimer: null,
        // GEX mode toggle (dashboard)
        gexMode: '0dte',        // '0dte' | 'monthly'
        monthlyGex: null,       // cached monthly GEX data
        monthlyExpiration: '',  // display string for monthly expiration
        // Account tab state
        accountSummary: {},
        positions: [],
        openOrders: [],
        executions: [],
        // Pending order confirmation callback
        pendingOrderPayload: null,
        // Tracks positions currently being liquidated (prevents duplicate orders)
        liquidatingPositions: new Set(),
    };

    const TAB_KEY = 'spx0dte.activeTab';
    const VALID_TABS = new Set(['dashboard', 'chain', 'account']);

    function getValidTab(tab) {
        return VALID_TABS.has(tab) ? tab : 'dashboard';
    }

    function loadActiveTab() {
        const hashTab = window.location.hash.replace(/^#/, '');
        if (VALID_TABS.has(hashTab)) {
            return hashTab;
        }
        const savedTab = localStorage.getItem(TAB_KEY);
        return VALID_TABS.has(savedTab) ? savedTab : 'dashboard';
    }

    function saveActiveTab(tab) {
        const validTab = getValidTab(tab);
        localStorage.setItem(TAB_KEY, validTab);
        if (history.replaceState) {
            history.replaceState(null, '', `#${validTab}`);
        } else {
            window.location.hash = validTab;
        }
    }

    const CHAIN_VIEWPORT_SEND_THROTTLE_MS = 200;
    const CHAIN_VIEWPORT_CENTER_THRESHOLD = 30; // points below previous center within which we keep existing stream center

    // ======================================================================
