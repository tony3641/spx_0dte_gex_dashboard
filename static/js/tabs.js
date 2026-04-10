    // Tab switching
    // ======================================================================
    function switchTab(tab) {
        state.activeTab = getValidTab(tab);
        saveActiveTab(state.activeTab);
        document.querySelectorAll('.tab-btn').forEach(btn => {
            btn.classList.toggle('active', btn.dataset.tab === state.activeTab);
        });
        if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(`set_tab:${state.activeTab}`);
        }
        const dashboard = document.getElementById('dashboardTab');
        const chain = document.getElementById('chainTab');
        const account = document.getElementById('accountTab');
        // Hide all first
        dashboard.style.display = 'none';
        dashboard.classList.remove('active');
        chain.classList.remove('active');
        account.classList.remove('active');

        if (tab === 'dashboard') {
            dashboard.style.display = '';
            dashboard.classList.add('active');
            // Trigger Plotly resize since charts were hidden
            setTimeout(() => {
                Plotly.Plots.resize('priceChart');
                Plotly.Plots.resize('gexChart');
                Plotly.Plots.resize('smileChart');
            }, 50);
        } else if (tab === 'chain') {
            chain.classList.add('active');
            scrollToATM();
            setTimeout(() => { reportChainViewportCenter(true); }, 80);
        } else if (tab === 'account') {
            account.classList.add('active');
            renderAccountTab();
        }
    }

    // ======================================================================
