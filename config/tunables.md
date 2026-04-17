# Tunables Inventory

Generated: 2026-04-16
Scope: backend + frontend runtime/configuration knobs in the current workspace.

## 1) Environment-driven settings (primary)

Source: config.py

- IB_HOST = "127.0.0.1"
- IB_PORT = 7497
- IB_CLIENT_ID = 1
- CHAIN_REFRESH_SECONDS = 10
- DASHBOARD_CHAIN_REFRESH_SECONDS = 300
- CHAIN_TAB_FULL_REFRESH_SECONDS = 300
- SNAPSHOT_REFRESH_SECONDS = 300
- PRICE_PUSH_INTERVAL = 1.0
- SERVER_HOST = "0.0.0.0"
- SERVER_PORT = 8000
- CHAIN_STREAM_MAX_LINES = 96
- CHAIN_STREAM_UPDATE_INTERVAL = 0.5
- VIEWPORT_CENTER_MIN_INTERVAL = 0.2

## 2) Backend hardcoded tunables (not env-wired today)

### server.py
- Initial chain snapshot wait loop: 120 iterations
- Initial chain snapshot poll sleep: 0.5 s
- Reconnect endpoint valid port range: 1..65535

### app_state.py
- price_history maxlen: 28800
- annual_vol default: 0.20
- risk_free_rate default: 0.043

### chain_fetcher.py
- BATCH_SIZE: 200
- QUALIFY_BATCH_SIZE: 150
- QUAL_CACHE_REQUALIFY_MOVE: 20.0 points
- DEFAULT_ANNUAL_VOL: 0.20
- TRADING_DAYS_PER_YEAR: 252
- fetch_option_chain std_dev_range default: 5.0
- _snapshot_batch timeout default: 12.0 s
- qualify batch delay: 0.1 s
- snapshot inter-batch delay: 0.5 s

### chain_manager.py
- build_chain_quotes annual_vol default: 0.20
- same-day minimum minutes-left floor: 1.0 min
- annualization basis: 390 minutes/day, 252 days/year
- connection/expiration retry sleep: 10 s
- missing spot retry sleep: 30 s
- CBOE daily gap skip sleep: 10 s
- compute_annual_vol lookback_days: 30
- fetch_option_chain std_dev_range during snapshot fetch: 8.0
- chain stream startup delay: 2 s
- chain stream no-data sleep: 5 s
- chain stream no strikes sleep: 10 s
- chain stream qualify batch size: 40
- chain stream qualify batch delay: 0.05 s
- chain stream tick-log cadence: 10.0 s
- chain stream update cadence: CHAIN_STREAM_UPDATE_INTERVAL (from config.py)
- monthly cache TTL: 600 s
- monthly fetch std_dev_range: 8.0

### ib_connection.py
- connectAsync timeout: 15 s
- SPX generic ticks: "233"
- ES baseline end time: 16:20:00 ET
- ES baseline history duration: 600 s
- ES baseline bar size: 1 min

### market_hours.py
- RTH open: 09:30 ET
- RTH close: 16:15 ET
- SPXW cease (expiration day): 16:00 ET
- Daily options gap: 17:00..20:15 ET

### account_manager.py
- Invalid IB sentinel for prices: 1.7976931348623157e+308
- FORCE_REFRESH_INTERVAL: 10.0 s
- account push loop base sleep: 1.0 s
- account push error backoff: 2 s

### order_manager.py
- await_order_status timeout default: 5.0 s
- await_order_status poll sleep: 0.1 s
- watch_and_push_status timeout: 30.0 s
- watch_and_push_status poll sleep: 0.5 s
- watch_parent_and_cancel_child timeout: 86400.0 s
- watch_parent_and_cancel_child poll sleep: 0.5 s
- default repriceIntervalSec fallback: 0.3 s
- midpoint quote attempts: 8
- midpoint quote poll sleep: 0.1 s
- post stop attach settle sleep: 0.05 s
- dynamic fill ack wait timeout: 3.0 s
- normal ack wait timeout: 10.0 s
- pending recheck sleep after reqOpenOrders: 0.5 s
- dynamic fill max runtime: 300.0 s
- dynamic fill max iterations: 10
- dynamic fill min reprice sleep clamp: 0.05 s
- combo pending recheck sleep: 0.5 s
- cancel order settle sleep: 0.1 s

### price_bars.py
- compute_annual_vol lookback_days default: 30
- minimum bars needed for vol calc: 5
- annualization trading days: 252
- historical bars duration: 1 D
- historical bars size: 1 min
- historical fetch off-hours end time: 16:30:00 ET
- price_push_loop cadence: PRICE_PUSH_INTERVAL (from config.py)
- price push error backoff: 1 s

### risk_free.py
- SGOV source URL: https://finance.yahoo.com/quote/SGOV?p=SGOV
- DEFAULT_RISK_FREE_RATE: 0.043
- fetch_sgov_7_day_yield timeout default: 5.0 s

### ws_handler.py
- status_push_loop cadence: 5 s
- keepalive ib.sleep cadence: 0.1 s
- keepalive asyncio sleep cadence: 0.1 s
- keepalive error sleep: 0.5 s
- websocket receive timeout: 30 s

## 3) Frontend hardcoded tunables (static/js)

### static/js/state.js
- TAB_KEY: "spx0dte.activeTab"
- VALID_TABS: dashboard, chain, account
- CHAIN_VIEWPORT_SEND_THROTTLE_MS: 200
- CHAIN_VIEWPORT_CENTER_THRESHOLD: 30

### static/js/ws.js
- Initial viewport report delay after open: 120 ms
- Reconnect delay: 3000 ms

### static/js/main.js
- Chain age update interval: 1000 ms

### static/js/tabs.js
- Dashboard resize delay after switch: 50 ms
- Chain viewport recenter delay after switch: 80 ms

### static/js/chain-table.js
- Visible strike window: +/-5 sigma plus +/-60 points
- Gamma flip row highlight tolerance: 2.5 points

### static/js/charts.js
- Mobile breakpoint: 600 px
- Theme colors:
  - CHART_BG = #111827
  - GRID_COLOR = #1e293b
  - TEXT_COLOR = #94a3b8
- Axis/title legend default font sizes: 9, 10, 11
- Common line widths used for overlays/traces: 1.5, 2

### static/js/badges.js
- GEX display scaling thresholds: 1e3, 1e6, 1e9
- Reconnect IB prompt default port: 7497

### static/js/order-entry.js
- Toast auto-hide timeout: 5000 ms

### static/js/strategy-builder.js
- SPX tick rule: >2 uses 0.10, otherwise 0.05
- Payoff scan range buffer: minStrike-100 to maxStrike+100
- Payoff scan step: 0.5
- Unlimited PnL threshold sentinel: 1e8

## 4) Duplicate/overlap notes

- There are existing env keys in config.py that appear to be legacy/unused in current runtime path:
  - CHAIN_REFRESH_SECONDS
  - DASHBOARD_CHAIN_REFRESH_SECONDS
  - CHAIN_TAB_FULL_REFRESH_SECONDS
- The active periodic snapshot loop currently keys off SNAPSHOT_REFRESH_SECONDS.

## 5) Suggested normalization path (optional next step)

If you want every tunable centralized and runtime-editable, the next pass should:

1. Add remaining hardcoded backend values into config.py as env-backed constants.
2. Add a frontend settings object (single static/js config module).
3. Replace in-file literals with imports/references from those central modules.
