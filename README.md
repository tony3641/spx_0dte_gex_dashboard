# SPX 0DTE Option Dashboard

A real-time Gamma Exposure (GEX) dashboard for SPX 0DTE options, powered by Interactive Brokers TWS and served as a browser app.

## Stack

| Layer | Technology |
|---|---|
| Broker API | native `ibapi` → IB TWS/Gateway (port 7497) |
| Backend | Python 3.10, FastAPI, uvicorn |
| Real-time push | WebSocket broadcast |
| Frontend | Vanilla JS + Plotly 2.32 |

## Features

- **GEX dashboard with 0DTE / Monthly toggle** — switch the GEX calculation between the current 0DTE SPXW expiry and the monthly SPX expiry.
- **Real-time option chain streaming** — live 0DTE chain with greeks, wall/flip markers, and a strike filter.
- **Account / Order Management tab** — account summary, portfolio positions, and order placement with **Stop Limit** support (also accessible as an order-entry widget on the dashboard).
- **Strategies tab** — define automated multi-leg strategies and let the server watch the market for you:
  - Conditions (entry window, short delta, spread width, credit, trend, volatility), triggers, and a candidate scanner.
  - **Budget-based position sizing** with total-credit preview per candidate.
  - Live candidates ranked best-first, each with a one-click **Place** button.
  - **Take-profit** (idempotent close loop, per-leg limit prices) and **stop-loss** as a single credit multiplier.
  - One-shot eval loop with re-entry guards, margin checks, and a kill switch.
  - **Subsequent strategies** — trigger children off a parent trade's state (parent close / time window), with acyclic tree validation.
- **Simulation tab** — intraday Monte Carlo stress-testing for 0DTE strategies: GJR-GARCH + Student-t paths with U-shape volatility, BSM smile marking, tick-rule fills, family re-entry, SL/strike sweeps and stress dials (ν, γ, λ), PnL/CVaR/max-DD/ruin analytics.
- **Logging tab** — server-side framework log streamed to the browser.

## Quick Start

1. Open IB TWS / Gateway and enable API access on port 7497.
2. Install dependencies:
   - The native broker API client (`ibapi`), from your local TWS API source:
     ```
     pip install -e "C:\TWS API\source\pythonclient"
     ```
   - Python dependencies:
     ```
     pip install -r requirements.txt
     ```
3. Start the server:
   ```
   python server.py
   ```
4. Open `http://localhost:8000` in a browser.

### Tests

```
python tests/run_tests.py --pretty     # structured JSON results
pytest tests/ -v --tb=long             # standard pytest flow
```

## Network Access

The server binds to all network interfaces (`0.0.0.0`), so it's accessible from both localhost and your local IP address:

- **Localhost only**: `http://localhost:8000`
- **Local network**: `http://<your-local-ip>:8000`

The exact URLs are printed to the console when the server starts. You can override the listening address with the `SERVER_HOST` environment variable if needed.

## Screenshot

![Dashboard Screenshot](docs/screenshot.png)
![Option Chain w/ Strategy Builder](docs/screenshot2.png)
![Account Management](docs/screenshot3.png)

## Dashboard Tabs

- **Dashboard** — intraday chart, GEX bars, IV smile, level badges, status bar.
- **Option Chain** — full streaming chain table with greeks and order entry.
- **Account** — account summary, positions, executions, order placement.
- **Strategies** — strategy list/editor, live candidates, triggers, and arm/disarm controls.
- **Simulation** — run intraday MC stress tests, sweep stop-loss multipliers and dynamic strike distances, A/B stress dials, read the report (SPX percentile fan + per-cell charts), force-clear it between runs, and export a full AI-readable JSON report.
- **Log** — real-time framework log.

## Simulation

The Simulation tab runs an intraday Monte Carlo stress test of a saved strategy (or a
parent + children family) against synthetic GJR-GARCH + Student-t paths. It only **reads**
`Strategy` objects — it never places orders or touches live trading state.

### Known limitations

- **Family mode** uses placeholder per-path stats (`mtm=None`) and does **not** support
  SL/k sweeps — a family-mode config with `sl_multipliers` or `dynamic_k_values` is rejected.
- **IB live bars are not yet implemented** in the simulator — use `csv` or `yfinance`
  (`source="ib"` is rejected).
- **trend / pmove / RSI / atm_iv entry gates are not supported.** A strategy that enables one
  is rejected with a validation error rather than silently simulated without the gate
  (`vix_enabled` volatility conditions are supported).
- **`bear_call` is not simulated** — a non-`bull_put` strategy is rejected rather than
  mis-simulated with bull-put geometry.
- **Single-mode day-PnL stats count entered days only**; family-mode totals include zero-PnL
  days for never-entered children paths (a definition mismatch between the two modes).
- **Engine-mode exit scans are per-path Python loops**; "~10k paths in seconds" is optimistic
  for large sweeps with many cells.
- **Sweep cells use independent RNG streams** (no common random numbers), so cross-cell
  differences include sampling noise — an experiment-quality tradeoff, not a paired A/B.
- **Bars and the fitted model are cached per `(source, csv_path, bar size, lookback)`** —
  *not* per file content. If you replace a CSV's contents on disk, restart the server or
  the sim keeps simulating the previously loaded bars.
- **Implied vs realized vol are not tied together.** Options are priced at the smile
  snapshot (ATM IV 20% by default) while the underlying moves at the *data's* realized
  vol; see "Reading results: win-rate sanity" below before trusting absolute win rates.

### How the pricer marks options

- Every contract is a **0DTE put** expiring at the 16:00 close; time-to-expiry decays bar
  by bar (`bar_seconds / (252 × 6.5h)` per bar).
- **IV** = quadratic smile in log-moneyness, loaded from the captured smile snapshot
  (`config/sim_smile.json`; else `sim_smile_default.json`, ATM IV 20%), plus a small
  vol-level link term.
- **Spread mark** = Black-Scholes put mid difference; the **entry fill** is the
  tick-floored conservative side (never better than the natural); **expiry settles at
  intrinsic value**. Stops trigger at mark ≥ multiplier × collected credit (+ slippage).

### Reading results: win-rate sanity

The engine is deterministic (same seed + same inputs = identical results) and covered by
tests, but absolute win rates can still mislead:

- **Stale calibration** (above) silently simulates old data after a file change — restart
  the server when in doubt.
- **A calm CSV + a high smile** makes far-OTM strikes unreachable: a 0.03-delta short put
  sits ~2–2.5% OTM at 20% IV, but data moving only ~0.4%/day rarely gets there, producing
  near-100% win rates that reflect the vol-world mismatch, not strategy edge.
- Mitigations: **capture a live smile** (matches the IV level you actually trade), use
  data whose realized vol is realistic, and compare sweep rows *relatively* rather than
  trusting absolute levels.

### Stress dials & experiments reference

| Control | Meaning |
| --- | --- |
| ν override | Student-t dof for per-bar shocks (blank = fitted; lower = fatter tails; must be > 2) |
| γ × | GJR leverage multiplier — extra vol after *negative* returns (1.0 = as fitted) |
| λ (vol-beta) | IV↔path-vol link; currently a subtle nudge — the smile *level* comes from the snapshot |
| Flat IV | Sanity mode: price everything at ATM IV, ignoring skew |
| SL multipliers | Sweep stop-loss = mult × credit, one table row per value; `inf` holds past the stop |
| Strike mode | `engine` = strategy's delta/width/credit gates; `dynamic_k` = short strike at k·σ below spot |
| k values | Short-strike distance in daily σ (`dynamic_k` mode only) |

Every run-form control also carries an inline “?” tooltip in the UI with the same
guidance.

### Reading the report

- **Simulated SPX paths (percentile fan, top)** — the market simulation itself: twenty
  SPX price curves at every 5th percentile (p0…p95, gold median) through the session.
  Strategy-independent, so it is identical for every sweep row.
- **Tiles, sweep table, per-cell charts** — the selected row's outcome distribution:
  day-PnL histogram, spread MTM quantile fan, and bootstrap max-drawdown.
- **Clear** — wipes the report back to its initial blank state (form inputs are kept; a
  run in flight keeps running server-side). Every new Run also starts from a blank
  report automatically, so a re-run can never mix with the previous result.
- **Export report** — downloads `sim_report.json`, a self-contained, AI-agent-readable
  snapshot of everything the page shows: the exact run config, run meta (source, bar
  size, GARCH fit + warnings, smile, dials), all per-cell stats and plotted series
  (histograms, MTM fan, bootstrap DDs, SPX fan), and a glossary reusing the UI's "?"
  explanations.

## Charts

### 1. SPX Intraday (top)
Candlestick chart with key GEX levels overlaid:
- Call Wall, Put Wall, Gamma Flip, Max Pain

### 2. Gamma Exposure (GEX) by Strike (middle)
Bar chart showing Call GEX (green) and Put GEX (red) by strike with:
- **Net GEX line** (yellow) — cumulative gamma exposure
- **Spot price indicator** (white dotted line)
- **Key level markers** — visual anchors for walls and flip points
- **Annotation box** — Net GEX value, MM regime (CONVERGING/DIVERGING), Call OI, Put OI, P/C OI Ratio, Call GEX % skew

### 3. IV Smile & Delta-Decay Efficiency (bottom)
Two-row subplot showing:
- **Top (Calls):** Call IV curve (green) + Call efficiency (yellow, dotted)
- **Bottom (Puts):** Put IV curve (red) + Put efficiency (yellow, dotted)
- All subplots share synchronized x-axes (strike ranges aligned)
- Hover displays: Strike, IV %, Delta, Charm (delta decay rate), Efficiency metric

**Real-time zoom sync:** Pan/zoom either the GEX or Smile chart → both charts update their x-axis range simultaneously.

### 4. Real-time Option Chain Streaming
- Live streaming 0DTE option chain with greeks
- Markers on Put Wall, Call Wall, and Gamma Flip location

## Status Bar

Real-time status indicators:
- **IB Connection** — green dot when connected
- **Market Status** — RTH (green) / GTH (yellow) / closed (gray)
- **Expiration** — target SPXW expiration date
- **Last GEX Update** — timestamp of most recent chain fetch

## Level Badges

Key strike prices and conditions:
- **SPX** — current spot price
- **Call Wall / Put Wall** — highest gamma-OI strikes
- **Gamma Flip** — price level where net gamma crosses zero
- **Max Pain** — strike minimizing option holder payoff
- **Net GEX** — total gamma exposure (with regime label)
- **Data Mode** — LIVE, HISTORICAL, or ES-DERIVED

## Files

| File | Purpose |
|---|---|
| `server.py` | FastAPI app, IB connection, state management, WebSocket endpoint |
| `ws_handler.py` | WebSocket message routing (tabs, GEX mode, strategies, orders, viewport sync) |
| `ib_client.py` / `ib_connection.py` | Native `ibapi` wrapper: contract resolution, streaming quotes, connection lifecycle |
| `chain_fetcher.py` | Batched SPXW option chain fetcher (streaming mode, ±8σ strike filter) |
| `chain_manager.py` | Chain caching, qualification, streaming state, monthly/0DTE coordination |
| `gex_calculator.py` | GEX computation: Call/Put Wall, Gamma Flip, Max Pain, Net GEX, MM regime |
| `market_hours.py` | Market-hours helpers, ET timezone, expiration, FOMC/NFP day utilities |
| `price_bars.py` / `risk_free.py` | Historical bars and risk-free-rate (SGOV) helpers |
| `order_manager.py` | Order placement, take-profit close loop, stop-loss handling |
| `account_manager.py` | Account values, portfolio positions, executions serialization |
| `strategy_models.py` | Strategy/Condition/Trigger/TakeProfit/StopLoss/RuntimeState dataclasses |
| `strategy_engine.py` | Candidate generation, condition eval, sizing, entry payloads, triggers, parent/child logic |
| `strategy_store.py` | Strategy persistence to `config/strategies.json` |
| `app_state.py` | Shared AppState runtime, day key, kill switch |
| `log_buffer.py` | Ring-buffer framework log for the Log tab |
| `config.py` | Centralized settings: env var → repo-root `.env` → `config/params.yaml` → defaults |
| `sim_config.py` | Simulation run config: validation, JSON round-trip, sweep cells |
| `sim_data.py` | Layered intraday bar loaders: CSV → yfinance → IB |
| `sim_calibrate.py` | GJR-GARCH(1,1)-t MLE, U-shape profile, smile snapshot, VIX mapping |
| `sim_paths.py` | Chunked vectorized path generation (stress dials: ν, γ×) |
| `sim_pricing.py` | Vectorized BSM, vol-linked smile, spreads, tick fill rules |
| `sim_engine.py` | Entry/exit scans, single + family simulation, experiment modes |
| `sim_risk.py` | CVaR/exit breakdown/max-DD/bootstrap ruin metrics, SPX path fan |
| `sim_jobs.py` | Background job registry, progress, cancel, memoized calibration |
| `static/` | Browser app: `index.html`, `css/`, `js/` (charts, chain table, order entry, strategy UI, tabs, WS) |
| `tests/` | Pytest suite + `run_tests.py` structured runner |

## Configuration

Settings are resolved in order: **environment variable → repo-root `.env` → `config/params.yaml` → hardcoded default**.

| Variable | Default | Description |
|---|---|---|
| `IB_HOST` | `127.0.0.1` | TWS host |
| `IB_PORT` | `7497` | TWS API port |
| `IB_CLIENT_ID` | `1` | IB client ID |
| `CHAIN_REFRESH_SECONDS` | `10` | How often to re-fetch the option chain |
| `DASHBOARD_CHAIN_REFRESH_SECONDS` | `300` | Dashboard GEX chain refresh cadence |
| `CHAIN_TAB_FULL_REFRESH_SECONDS` | `300` | Chain tab full refresh cadence |
| `SNAPSHOT_REFRESH_SECONDS` | `300` | Snapshot refresh cadence |
| `SERVER_HOST` | `0.0.0.0` | Server listen address (all interfaces) |
| `SERVER_PORT` | `8000` | Server listen port |
| `DEFAULT_ANNUAL_VOL` | `0.20` | Assumed annualized volatility for unlisted IV |
| `MONTHLY_CACHE_TTL` | `600` | Monthly chain cache TTL (seconds) |
| `SGOV_TICKER` | `SGOV` | Ticker used for risk-free rate |
| `DEFAULT_RISK_FREE_RATE` | `0.043` | Fallback risk-free rate |
| `RTH_OPEN` / `RTH_CLOSE` | `09:30` / `16:15` | Regular trading hours window (ET) |
| `FOMC_DATES` | `[]` | FOMC meeting dates (via `params.yaml`) |

Additional tunables (chain streaming, batch sizes, viewport sync, SPXW cease/gap windows) live in `config.py` and `config/params.yaml`.

`numpy` and `scipy` are new runtime requirements; `requirements-dev.txt` adds `pytest-playwright` for the UI E2E tier.

## Discord Bot

Optional in-process bot that lets an allowlisted user query the dashboard and
control strategies from Discord. **Orders are never placed from Discord** —
the `/place` command shows the candidate and directs you to confirm in the web UI.

### Settings UI

Configure Discord — and the IB Gateway port — from the dashboard: click the
gear icon in the header. Apply hot-applies the change (the Discord bot restarts
in place; the IB port reconnects immediately) and persists it to the repo-root
`.env`, which overrides `config/params.yaml` but loses to real environment
variables. Settings endpoints only accept localhost connections. The header
connection badge is display-only now.

### Manual setup (headless alternative)

1. Create a bot in the Discord Developer Portal, copy its token, and add the
   `applications.commands` scope. Invite it to your server.
2. Provide the token and options via env var or `config/params.yaml` (never
   commit a real token):

| Variable | Example | Notes |
|---|---|---|
| `DISCORD_TOKEN` | `abc...` | Bot token. Presence enables the bot. |
| `DISCORD_GUILD_ID` | `123456789` | Reserved for alerts; slash commands are registered globally (available in any server the bot is in). New global commands can take up to ~1 hour to appear after first sync. |
| `DISCORD_CHANNEL_ID` | `987654321` | Optional; alert-stream channel. |
| `DISCORD_ALLOWED_USER_IDS` | `111,222` | Allowed Discord user IDs. |
| `DISCORD_ALLOWED_ROLE` | `trader` | Optional role name or ID that is also allowed. |

3. Restart the server.

### Commands

`/status` `/account` `/positions` `/orders` `/strategy` `/candidates <name>`
`/arm <name>` `/disarm <name>` `/killswitch on|off` `/place <name> <index>`

- Read commands query live dashboard state.
- `/arm` warns when the strategy `auto_execute`s, and honors the global kill switch.
- `/place` intentionally refuses — place orders in the browser.
- A curated stream (fills, strategy exits, take-profit closes, IB errors,
  connection and kill-switch changes) is posted to `DISCORD_CHANNEL_ID`.

## Data Modes

- **LIVE** — SPX streaming quote from IB during RTH (09:30–16:15 ET).
- **ES-DERIVED** — Off-hours SPX price inferred from ES front-month futures movement relative to the last SPX close.
- **HISTORICAL** — Last available historical bars when markets are closed and ES is unavailable.
