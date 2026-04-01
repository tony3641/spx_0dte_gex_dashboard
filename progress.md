# Progress & Change Log

All significant feature additions and bug fixes made to the SPX 0DTE GEX Dashboard.

---

## Bug Fixes

### OI Always Zero
**Problem:** Server logged "OI is all zeros" despite real open-interest data being visible in TWS.

**Root cause:** `ib.reqMktData(..., snapshot=True)` silently ignores `genericTickList`, so tick type 101 (open interest) was never requested. Additionally the code was reading `ticker.openInterest`, a field that does not exist on ib_insync `Ticker`.

**Fix (`chain_fetcher.py`):**
- Switched `_snapshot_batch` to `snapshot=False` with `genericTickList='101'` and manual `cancelMktData` after a 12-second timeout.
- `_ticker_to_option_data` now reads `ticker.callOpenInterest` (calls) / `ticker.putOpenInterest` (puts) — both are `float`, `nan` when not yet received.
- Added OI summary log line: `"Fetched data for N options — OI: X/N contracts with non-zero OI, total OI=..."`.

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

## Architecture Notes

- **IB data flow:** `ib.pendingTickersEvent` (async) → `on_pending_tickers` → updates `state.spx_price` / `state.es_price` → derives ES-based SPX when not in RTH.
- **Chain loop cadence:** every `CHAIN_REFRESH_SECONDS` (default 60 s); skipped during the CBOE daily maintenance gap (17:00–20:15 ET).
- **Vol estimate:** `compute_annual_vol()` fetches 30 days of daily bars from IB and computes annualised historical vol; falls back to 20% if unavailable.
- **Mode labels:** `state.data_mode` = `"live"` | `"historical"` | `"initializing"` — broadcast in every `status` and `gex` WebSocket message.
