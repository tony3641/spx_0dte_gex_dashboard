# Progress & Change Log

All significant feature additions and bug fixes made to the SPX 0DTE GEX Dashboard.

---

## Session: April 10, 2026 - Current update

- Added a broad backend refactor and new core service modules: `account_manager.py`, `app_state.py`, `chain_manager.py`, `config.py`, `ib_connection.py`, `order_manager.py`, `price_bars.py`, `risk_free.py`, `ws_handler.py`.
- Introduced a full frontend asset refresh in `static/css/` and `static/js/`, including updated option chain behavior, charts, order entry, strategy builder, session handling, and websocket integration.
- Added test coverage under `tests/` for account manager, chain fetcher, config, market hours, order placement, and risk-free functionality.
- Updated tracked code in `.gitignore`, `chain_fetcher.py`, `gex_calculator.py`, `requirements.txt`, `server.py`, and `static/index.html`.
- `.gitignore` now ignores `_old.*` and `*todo*` artifacts.
- Refreshed `requirements.txt` for the revised backend and new test dependencies.

---

## Bug Fixes

### Session: April 7, 2026 - RTH/GTH Mode, Refresh Cache, Graceful Shutdown, Viewport-Centered Chain Stream

#### GTH incorrectly showing LIVE mode and updating SPX intraday bars
**Problem:** During Global Trading Hours (GTH), the dashboard still showed LIVE mode and continued intraday bar updates as if SPX were in-session.

**Root cause:** SPX ticker updates unconditionally flipped `data_mode` to `"live"`, and the bar push loop did not strictly gate updates to RTH-only behavior.

**Fix (`server.py`):**
- SPX tick processing now sets live mode only during RTH (`is_within_rth()`).
- Outside RTH, mode is forced/kept as historical.
- Intraday bar generation now runs only when `data_mode == "live"` and during RTH.
- Status loop includes a safety guard to keep non-RTH mode historical.

Result: in GTH, dashboard mode remains historical and intraday chart stays on the last completed regular session.

#### Off-hours GEX spot did not consistently reflect ES-derived SPX regime
**Problem:** In off-hours contexts, behavior could drift back toward SPX-live assumptions.

**Fix (`server.py`):**
- Preserved ES-derived spot path for non-live mode and prevented SPX off-hours ticks from forcing live mode.

Result: GEX off-hours spot behavior stays aligned with ES-derived SPX logic.

#### Manual option-tab refresh did not guarantee full qualification reset
**Problem:** Manual refresh from the Option Chain tab did not explicitly clear qualification cache state before re-fetching.

**Fix (`chain_fetcher.py`, `server.py`):**
- Added `clear_qualification_cache(reason=...)` helper.
- On `refresh_chain` WebSocket command, server now clears qualification cache before triggering refresh.

Result: manual refresh now forces clean re-qualification from scratch.

#### Ctrl+C / terminate shutdown handling
**Problem:** Server shutdown relied mainly on default behavior and could be less explicit under different termination paths.

**Fix (`server.py`):**
- Added signal handlers for `SIGINT`, `SIGTERM`, and `SIGBREAK` (Windows where available).
- First signal requests graceful exit (`server.should_exit = True`), second signal forces exit.
- Added final loop teardown path: cancel pending tasks, await cancellation, shutdown async generators, close event loop.

Result: cleaner and more deterministic service shutdown on Ctrl+C / terminate.

---

## Features

### Session: April 7, 2026 - Viewport-Centered Option Chain Livestream

**Goal:** Stream live option quotes around the strikes the user is currently viewing, not only around current SPX.

**Backend (`server.py`):**
- Added viewport center state: `viewport_center_strike`, `viewport_center_last_ts`.
- Added WebSocket message handling for `viewport_center:<strike>`.
- Added server-side throttling (`VIEWPORT_CENTER_MIN_INTERVAL`, default 0.2s).
- `chain_stream_loop()` selection center now uses viewport center when available on chain tab, otherwise falls back to spot.

**Frontend (`static/index.html`):**
- Added center-strike detection from `#chainTableWrap` visible midpoint.
- Added throttled center reporting to server (200ms client throttle).
- Sends center updates on:
  - option-chain scroll,
  - chain tab activation,
  - chain table rerender,
  - ATM auto-scroll,
  - WebSocket reconnect,
  - window resize (while on chain tab).

Result: as users scroll the option chain, live subscriptions migrate to strikes near the visible center of the screen.

### OI Always Zero
**Problem:** Server logged "OI is all zeros" despite real open-interest data being visible in TWS.

**Root cause:** `ib.reqMktData(..., snapshot=True)` silently ignores `genericTickList`, so tick type 101 (open interest) was never requested. Additionally the code was reading `ticker.openInterest`, a field that does not exist on ib_insync `Ticker`.

**Fix (`chain_fetcher.py`):**
- Switched `_snapshot_batch` to `snapshot=False` with `genericTickList='101'` and manual `cancelMktData` after a 12-second timeout.
- `_ticker_to_option_data` now reads `ticker.callOpenInterest` (calls) / `ticker.putOpenInterest` (puts) — both are `float`, `nan` when not yet received.
- Added OI summary log line: `"Fetched data for N options — OI: X/N contracts with non-zero OI, total OI=..."`.

---

### Hotfix: Intraday bars gap during RTH
**Problem:** When the server started during Regular Trading Hours (RTH) the price chart only showed a few recent live bars (gap between historical data and now), leaving most of the session empty.

**Root cause:** Historical fetch used a fixed future `endDateTime` (e.g. 16:30), which causes IB to return only fully processed bars (30–60 minute lag) and not the most-recent intraday minutes. Also, startup historically-only logic skipped seeding today's bars when starting during RTH.

**Fixes (server.py / static/index.html):**
- Always fetch intraday bars at startup so the chart is seeded with today's session (or the last session when outside RTH).
- During RTH, call IB historical with `endDateTime = ""` so IB returns bars up to the current minute.
- Preserve `data_mode = "live"` if live ticks have already been detected (avoid regressing to historical mode).
- Prevent duplicate minute bars at the historical/live seam: live push now dedupes before appending and inherits partial last-bar OHLC when appropriate.
- Frontend: fully filled candle bodies (no alpha transparency) and a clear vertical session-start marker with a date label when prior-day bars are present.
- UI accessibility: added IV Smile title help indicator with hover/focus tooltip for delta-decay efficiency definition and fixed literal `\n` to actual line breaks.
- Added GEX chart title help tooltip for GEX meaning, and hover titles for Call Wall/Put Wall/Gamma Flip/Max Pain/Net GEX badges; MM regime badge hover text also present.

Result: the price chart now fills the full session from 09:30 through the current minute immediately after startup, with no 12:30→13:30 hole.

---

### Dashboard Snapshot First, Chain Stream Second
**Problem:** Chain streaming could begin before the initial dashboard snapshot was fully loaded, causing the dashboard to remain empty until the next refresh.

**Fix:** Startup now forces one full chain snapshot before starting option-chain live streaming. The dashboard snapshot refreshes on a fixed 5-minute cadence independent of the option-chain stream.

**Implementation:**
- `server.py` startup now triggers an immediate snapshot and waits for `latest_gex` + `chain_data` before starting `chain_stream_loop()`.
- The snapshot loop uses `SNAPSHOT_REFRESH_SECONDS` (default 300) and is no longer tied to active tab state.
- `gex` messages are broadcast for dashboard updates regardless of whether the chain tab is active.

---

### Error 321 on Snapshot Request
**Problem:** IB returned Error 321 when `genericTickList` was set with `snapshot=True`.

**Fix:** `snapshot=False` is required for any non-empty `genericTickList`. The batch function now streams data and cancels subscriptions manually.

---

### ES Contract Ambiguity on Qualify
**Problem:** Calling `ib.qualifyContracts()` on a generic `Future('ES', 'CME', 'USD')` returned multiple contracts, causing an exception.

**Fix (`server.py` — `setup_es_subscription`):** Use `reqContractDetails` on the generic Future, filter results to unexpired contracts, sort ascending by expiry, and use `details[0].contract` directly — no re-qualification needed.

---

### Historical Bars Duration Format
**Problem:** IB rejected `'10 mins'` as a duration string, causing Error 321 on historical data requests.

**Fix (`server.py`):** Use IB's required format: `'600 S'` (integer + space + unit).

---

## Features

### ±8σ Strike Range Filter
Chain fetch now filters strikes to within **±8 daily standard deviations** of spot (previously ±10σ), using the runtime-computed annualised vol. This reduces the number of contracts fetched while still covering all practically relevant strikes.

**File:** `chain_fetcher.py` — `_strike_range_for_std_devs`, `fetch_option_chain(std_dev_range=8.0)`

---

### Net GEX Value + MM Hedging Regime
The GEX chart and levels strip now display:

- **Net GEX** badge: total net gamma exposure formatted as `+1.23B` / `-450M` / `+12.3K`.
- **MM Regime** badge: `CONVERGING ▼` (green, Net GEX > 0 — market makers are short gamma, hedging acts as a stabiliser) or `DIVERGING ▲` (red, Net GEX < 0 — hedging amplifies moves).
- **Annotation box** (top-right of GEX chart): shows Net GEX value + regime label with a colour-coded border (green/red).

**Files:** `gex_calculator.py` (`GEXResult.net_gex`), `static/index.html` (badges + Plotly annotation).

---

### ES-Derived Off-Hours SPX Price
When markets are outside RTH (09:30–16:15 ET), the dashboard derives a synthetic SPX price from the ES front-month futures move:

```
spx_derived = spx_last_close × (1 + (es_now − es_baseline) / es_baseline)
```

- `es_baseline` = ES price at the last SPX close (~16:15 ET), fetched from 600 seconds of 1-minute TRADES bars.
- ES front-month contract is selected via `reqContractDetails`, filtered to unexpired, sorted by expiry (nearest first).
- The GEX chart spot line label changes to **"ES derived SPX: XXXX"** (yellow) during off-hours and **"SPX: XXXX"** (white) during live.

**Files:** `server.py` (`AppState.es_*`, `setup_es_subscription`, `fetch_es_baseline`, `on_pending_tickers`), `static/index.html` (spot line label colour).

---

### Strategy Builder Delta + Strike Sigma UX
- Added combined strategy delta in the Strategy Builder summary and per-leg delta display in the strategy table.
- Backend now includes expiry-based sigma metadata in `chain_quotes` (`expiration_raw`, `tte_years`, `sigma_move`) plus per-strike `sigma_distance_abs` and `sigma_distance_signed`.
- Frontend renders strike-cell sigma buckets and hover tooltips, and updated wall marker borders so Call Wall has a green left border, Put Wall has a red right border, and the ATM strike is marked with white borders on both sides.

**Files:** `server.py`, `static/index.html`.

---

### Chain Fetch Progress — Startup Spinner Overlay
During the initial chain fetch (before any GEX data is available), the GEX chart panel shows a centred rotating loading circle with a darkened background overlay.

**Behaviour:**
- Spinner appears on startup/reconnect only — recurring background refreshes run silently.
- Phase text updates: *Qualifying contracts…* → *Streaming market data…* → *Computing GEX…*
- Sub-text shows batch progress: *Batch N of M* during the streaming phase.
- Server broadcasts `chain_progress` WebSocket events with `phase` / `batch` / `total_batches` / `pct`.

**Files:**
- `server.py`: broadcasts `chain_progress` events at start, each batch, and completion.
- `chain_fetcher.py`: `fetch_option_chain(progress_callback=...)` — async callback called after each batch.
- `static/index.html`: `.gex-loading` CSS overlay, `#gexLoading` HTML element, `handleChainProgress(data)` JS function.

**Suppression logic (`index.html`):**
```js
// only show overlay if no GEX data received yet
if (state.gex) return;
overlay.classList.remove('hidden');
```

---

## Session: April 1, 2026 — IV Smile & Synchronized Charts

### Added 3rd Chart with IV Smile & Delta-Decay Efficiency
**New dashboard chart:** IV Smile & Delta-Decay Efficiency — displays implied volatility curves and delta-decay efficiency metrics across strikes.

**UI Layout:**
- **Top subplot (Calls):** Call IV curve (green) + Call efficiency = |charm| / |delta| (yellow, dotted)
- **Bottom subplot (Puts):** Put IV curve (red) + Put efficiency (yellow, dotted)
- **Key lines:** Spot price (white dotted), Call Wall, Put Wall, Gamma Flip (dashed)
- **Hover tooltip:** Strike, IV %, Delta, Charm, Efficiency
- **Loading spinner:** Synced with GEX chart during initial option chain fetch

**Data Computation (`gex_calculator.py`):**
- Added **Black-Scholes delta calculation** — no scipy dependency (uses `math.erf` for norm CDF)
  - `_norm_cdf()` — standard normal CDF
  - `_bsm_delta()` — European option delta at any S, K, T, σ, r
  - `_compute_charm_fd()` — delta decay rate via 15-minute finite-difference

- Extended `GEXResult` dataclass to hold per-strike IV, delta, charm, and efficiency data
- Updated `compute_gex()` signature to accept `time_to_expiry_years` and `risk_free_rate` parameters
- Added `_build_smile_data()` function — formats smile data for frontend (strike-level IV, delta, charm, efficiency)

**Time-to-Expiry Calculation (`server.py`):**
- For 0DTE: minutes remaining until 16:00 ET close, converted to trading-year fractions
- For multi-day expirations: calendar days remaining × (390 min/day) / (390 × 252 trading days/year)
- Passed to `compute_gex()` for charm calculation

**Frontend (`static/index.html`):**
- New `initSmileChart()`, `updateSmileChart()` functions
- Grid layout: 3 rows (price:gex:smile = 1:1:1)
- Responsive margins and font sizing for mobile/desktop

### Synchronized Horizontal Zoom Between GEX & Smile Charts
Both charts compute a **common x-axis range** from the smile data strikes and apply it to their respective x-axes.

**Sync Logic:**
- GEX chart zoom event → updates Smile chart's xaxis + xaxis2 range
- Smile chart zoom event → updates GEX chart's xaxis range
- Both charts always show identical strike price positions (pixel-perfect alignment)
- Example: strike 5600 is now at the same horizontal pixel location in both charts

**Implementation:**
- Event listeners on `plotly_relayout` for both charts
- Sync applies to both zoom and auto-range operations
- Common range calculated as `[min(strikes) - 1%, max(strikes) + 1%]` for padding

### Enhanced GEX Chart Metrics
**Updates to GEX chart:**
- Added **Net GEX line** (yellow) overlaid on call/put bars for visual clarity
- Enhanced top-right annotation box with:
  - **Call OI** (green) / **Put OI** (red) — total for all strikes
  - **P/C OI Ratio** — put OI / call OI (green if ≤1, red if >1)
  - **Call GEX %** — call GEX / gross GEX (green if ≥50%, red if <50%)
- Hover tooltips now include:
  - Call/Put GEX values
  - Call/Put OI per strike
  - Call/Put volume per strike

**Extended `GEXResult` dataclass:**
- Added `call_oi_by_strike`, `put_oi_by_strike`, totals
- Added `call_vol_by_strike`, `put_vol_by_strike`, totals
- Added IV, delta, charm maps: `call_iv_by_strike`, `put_iv_by_strike`, etc.

### Tightened Strike Filtering
Changed default `std_dev_range` from **8.0 to 5.0** in `chain_fetcher.py`:
- Covers ±5 daily standard deviations (~99.99% of probability mass)
- Reduces fetch time by ~30-40% compared to ±8σ
- Lower memory footprint, faster computation

### Minor Updates
- Updated FastAPI title: "SPX 0DTE GEX Dashboard" → "SPX 0DTE Option Dashboard" (reflects broader feature set)
- Updated [README.md](README.md) title to match

---

## Session: April 14-15, 2026 — Monthly SPX GEX Toggle

### 0DTE ↔ Monthly SPX GEX Mode Toggle on Dashboard

**Goal:** Add a segmented toggle ("0DTE | Monthly") on the Dashboard GEX chart panel so both the GEX-by-strike chart and IV Smile chart can display either the current 0DTE SPXW chain or the monthly SPX option chain (3rd Friday expiry). The price chart and header badges remain fixed to 0DTE data.

**Implementation:**

#### `chain_fetcher.py`
- `fetch_option_chain()` now accepts a `trading_class: str = 'SPXW'` parameter; the IB `Option` contract constructor uses the passed value instead of hardcoded `'SPXW'`.
- Added `_monthly_qualification_cache = QualificationCache()` — a separate qualification cache for SPX contracts to prevent collisions with the SPXW cache. Cache selection is driven by `trading_class`.
- `clear_qualification_cache(monthly: bool = False)` — clears either cache.
- Added `get_monthly_chain_params(ib, underlying)` — calls `reqSecDefOptParamsAsync`, filters for `tradingClass='SPX'` and `exchange='SMART'`, returns expirations/strikes.
- Added `find_monthly_expiration(expirations)` — calculates the 3rd Friday of the current month using `timedelta`. If that date is already past, returns next month's 3rd Friday. Falls back to the first expiration ≥ today. Verified: April 2026 → `20260417`.

#### `app_state.py`
- Added 8 new fields: `gex_mode`, `monthly_expiration`, `monthly_expirations`, `monthly_strikes`, `monthly_gex_result`, `monthly_latest_gex`, `monthly_chain_data`, `monthly_last_fetch_ts`.

#### `ib_connection.py`
- Added `setup_monthly_chain_info(ib, state)` function — calls `get_monthly_chain_params()` and `find_monthly_expiration()` at startup to populate `state.monthly_expirations`, `state.monthly_strikes`, `state.monthly_expiration`.

#### `chain_manager.py`
- Added `MONTHLY_CACHE_TTL = 600` (10 minutes).
- Added `monthly_gex_fetch(ib, state, broadcast_fn)` — on-demand async function that:
  - Returns cached data immediately if fresh (< TTL).
  - Otherwise calls `fetch_option_chain(..., trading_class='SPX')`, handles OI=0 fallback, computes `tte_years` (minutes-to-close for same-day expirations, else calendar days/252), calls `compute_gex()`, stores in `state.monthly_*` fields.
  - Broadcasts `{"type": "monthly_gex", ...}` and `{"type": "monthly_gex_progress", ...}`.

#### `ws_handler.py`
- `init` payload extended with `"gex_mode"`, `"monthly_gex"`, and `"monthly_expiration"`.
- New message handlers:
  - `set_gex_mode:monthly` → sets `state.gex_mode = "monthly"`, fires `asyncio.create_task(monthly_gex_fetch(...))`.
  - `set_gex_mode:0dte` → sets `state.gex_mode = "0dte"`, re-broadcasts cached 0DTE GEX if available.

#### `server.py`
- Startup lifespan and `/api/reconnect_ib` endpoint call `await setup_monthly_chain_info(ib, state)` after `setup_chain_info`.
- `/api/state` response includes `"gex_mode"`, `"monthly_gex"`, `"monthly_expiration"`.

#### `static/index.html`
- GEX chart title replaced with `<span id="gexChartLabel">` + segmented toggle:
  ```html
  <span class="gex-mode-toggle" id="gexModeToggle">
      <button class="gex-mode-btn active" data-mode="0dte" onclick="setGexMode('0dte')">0DTE</button>
      <button class="gex-mode-btn" data-mode="monthly" onclick="setGexMode('monthly')">Monthly</button>
  </span>
  ```
- IV Smile chart title wrapped in `<span id="smileChartLabel">` for dynamic text updates.

#### `static/js/state.js`
- Added 3 fields: `gexMode: '0dte'`, `monthlyGex: null`, `monthlyExpiration: ''`.

#### `static/js/charts.js`
- `updateGexChart()` and `updateSmileChart()` now resolve `gexData = state.gexMode === 'monthly' ? state.monthlyGex : state.gex` before rendering — all `state.gex.*` references replaced with `gexData.*`.
- Added `setGexMode(mode)` — validates mode, updates `state.gexMode`, calls `updateGexModeToggle()`, sends `set_gex_mode:<mode>` over WS, re-renders charts.
- Added `updateGexModeToggle()` — syncs `.active` class on toggle buttons; updates `gexChartLabel` and `smileChartLabel` text to include monthly expiration date when in Monthly mode.
- Added `handleMonthlyGexProgress(data)` — shows/hides `gexLoading`/`smileLoading` overlays during monthly fetch; suppressed if `monthlyGex` data already cached.

#### `static/js/strategy-builder.js`
- Added `monthly_gex` message case — stores `state.monthlyGex`, formats expiration display string, calls `updateGexModeToggle()`, triggers chart re-renders if in Monthly mode.
- Added `monthly_gex_progress` message case — calls `handleMonthlyGexProgress(msg.data)`.
- `handleInit()` restores `gex_mode`, `monthlyGex`, `monthlyExpiration` from init payload and calls `updateGexModeToggle()`.

#### `static/css/charts.css`
- Added `.gex-mode-toggle` and `.gex-mode-btn` styles — segmented button control with dark theme (`#1e293b` background, `#22c55e` active state with `#0f172a` text).

**Design Decisions:**
- Monthly data is fetched **on-demand** (not looped) to conserve IB market data lines.
- 10-minute TTL prevents redundant full chain fetches while keeping data reasonably fresh.
- Separate qualification caches prevent SPXW/SPX contract metadata from colliding.
- Price chart (1st graph) and header badges are always driven by 0DTE data regardless of mode.

**Files:** `chain_fetcher.py`, `chain_manager.py`, `app_state.py`, `ib_connection.py`, `ws_handler.py`, `server.py`, `static/index.html`, `static/js/charts.js`, `static/js/strategy-builder.js`, `static/js/state.js`, `static/css/charts.css`

**Result:** Toggle appears in the GEX chart title bar. Clicking "Monthly" triggers an on-demand fetch of the SPX monthly chain (3rd Friday expiry), renders GEX-by-strike and IV Smile for that contract, and labels both chart titles with the expiration date. Clicking "0DTE" instantly reverts to the live 0DTE SPXW data. 91/92 tests pass (1 pre-existing BSM precision failure unrelated to this feature).

---

## Architecture Notes

- **IB data flow:** `ib.pendingTickersEvent` (async) → `on_pending_tickers` → updates `state.spx_price` / `state.es_price` → derives ES-based SPX when not in RTH.
- **Chain loop cadence:** every `CHAIN_REFRESH_SECONDS` (default 60 s); skipped during the CBOE daily maintenance gap (17:00–20:15 ET).
- **Vol estimate:** `compute_annual_vol()` fetches 30 days of daily bars from IB and computes annualised historical vol; falls back to 20% if unavailable.
- **Mode labels:** `state.data_mode` = `"live"` | `"historical"` | `"initializing"` — broadcast in every `status` and `gex` WebSocket message.
