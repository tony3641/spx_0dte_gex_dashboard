"""
Chain fetch/stream loops and chain_quotes payload builder.

All long-running loops accept `ib`, `state`, and `broadcast_fn`.
`build_chain_quotes` is a pure function for easy unit testing.
"""

import asyncio
import math
import logging
from datetime import datetime
from typing import List, Optional

from ib_insync import Option

from config import (
    CHAIN_STREAM_MAX_LINES, CHAIN_STREAM_UPDATE_INTERVAL,
    SNAPSHOT_REFRESH_SECONDS,
)
from market_hours import (
    now_et, is_within_rth, is_cboe_options_open, ET,
    find_next_expiration, get_expiration_display,
)
from chain_fetcher import fetch_option_chain, clear_qualification_cache
from gex_calculator import compute_gex, gex_result_to_dict, GEXResult, OptionData
from price_bars import compute_annual_vol, fetch_historical_bars

logger = logging.getLogger(__name__)


def _safe_stream_float(val):
    if val is None:
        return None
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    return f


def _normalize_stream_iv(iv_val):
    iv = _safe_stream_float(iv_val)
    if iv is None or iv <= 0:
        return None
    if iv > 3.0:
        iv = iv / 100.0
    return iv


def _extract_stream_greeks(ticker):
    """Pick greek fields from model/last/bid/ask greeks with sane fallbacks."""
    gamma = None
    delta = None
    iv_dec = None

    for greeks in (
        getattr(ticker, 'modelGreeks', None),
        getattr(ticker, 'lastGreeks', None),
        getattr(ticker, 'bidGreeks', None),
        getattr(ticker, 'askGreeks', None),
    ):
        if greeks is None:
            continue

        if delta is None:
            delta = _safe_stream_float(getattr(greeks, 'delta', None))
        if gamma is None:
            gamma = _safe_stream_float(getattr(greeks, 'gamma', None))
        if iv_dec is None:
            iv_dec = _normalize_stream_iv(getattr(greeks, 'impliedVol', None))

        if delta is not None and gamma is not None and iv_dec is not None:
            break

    if iv_dec is None:
        iv_dec = _normalize_stream_iv(getattr(ticker, 'impliedVolatility', None))

    return delta, gamma, iv_dec


def build_chain_quotes(options: List[OptionData], spot_price: float,
                       gex_result: Optional[GEXResult] = None,
                       annual_vol: float = 0.20,
                       expiration: str = "") -> dict:
    """Serialize a list of OptionData into the chain_quotes payload."""
    sigma_tte_years = 0.0
    if expiration:
        try:
            exp_date = datetime.strptime(expiration, "%Y%m%d").date()
            now = now_et()
            if exp_date == now.date():
                close_dt = now.replace(hour=16, minute=0, second=0, microsecond=0)
                mins_left = max((close_dt - now).total_seconds() / 60.0, 1.0)
                sigma_tte_years = mins_left / (390.0 * 252.0)
            else:
                days_left = (exp_date - now.date()).days
                sigma_tte_years = max(days_left, 1) / 252.0
        except Exception:
            sigma_tte_years = 0.0

    sigma_move = None
    if spot_price > 0 and annual_vol and sigma_tte_years > 0:
        sigma_move = spot_price * annual_vol * math.sqrt(sigma_tte_years)

    calls = {}
    puts = {}
    for o in options:
        if o.right == 'C':
            calls[o.strike] = o
        else:
            puts[o.strike] = o

    all_strikes = sorted(set(list(calls.keys()) + list(puts.keys())))

    rows = []
    for s in all_strikes:
        sigma_abs = None
        sigma_signed = None
        if sigma_move and sigma_move > 0:
            sigma_signed = (s - spot_price) / sigma_move
            sigma_abs = abs(sigma_signed)

        row = {
            "strike": s,
            "sigma_distance_abs": round(sigma_abs, 4) if sigma_abs is not None else None,
            "sigma_distance_signed": round(sigma_signed, 4) if sigma_signed is not None else None,
        }
        c = calls.get(s)
        p = puts.get(s)
        if c:
            row.update({
                "call_bid": c.bid, "call_ask": c.ask,
                "call_bid_size": c.bid_size, "call_ask_size": c.ask_size,
                "call_last": c.last,
                "call_delta": round(c.delta, 4) if c.delta is not None else None,
                "call_gamma": round(c.gamma, 6) if c.gamma is not None else None,
                "call_oi": c.open_interest, "call_volume": c.volume,
                "call_iv": round(c.implied_vol * 100, 2) if c.implied_vol else None,
            })
        if p:
            row.update({
                "put_bid": p.bid, "put_ask": p.ask,
                "put_bid_size": p.bid_size, "put_ask_size": p.ask_size,
                "put_last": p.last,
                "put_delta": round(p.delta, 4) if p.delta is not None else None,
                "put_gamma": round(p.gamma, 6) if p.gamma is not None else None,
                "put_oi": p.open_interest, "put_volume": p.volume,
                "put_iv": round(p.implied_vol * 100, 2) if p.implied_vol else None,
            })
        rows.append(row)

    call_wall = gex_result.call_wall if gex_result else None
    put_wall = gex_result.put_wall if gex_result else None
    gamma_flip = gex_result.gamma_flip if gex_result else None

    return {
        "strikes": rows,
        "spot_price": round(spot_price, 2),
        "annual_vol": annual_vol,
        "expiration_raw": expiration,
        "tte_years": round(sigma_tte_years, 8),
        "sigma_move": round(sigma_move, 4) if sigma_move is not None else None,
        "call_wall": call_wall,
        "put_wall": put_wall,
        "gamma_flip": gamma_flip,
        "timestamp": now_et().strftime("%H:%M:%S"),
        "timestamp_iso": now_et().isoformat(),
    }


async def chain_fetch_loop(ib, state, broadcast_fn):
    """Periodically fetch full-chain snapshot for dashboard/GEX state."""
    await asyncio.sleep(1)

    if state.force_chain_fetch_event is None:
        state.force_chain_fetch_event = asyncio.Event()

    while True:
        try:
            force_manual_refresh = state.manual_refresh_requested
            state.manual_refresh_requested = False

            if not state.connected or not state.expiration:
                logger.info("Waiting for connection/expiration...")
                await asyncio.sleep(10)
                continue

            if state.spx_price <= 0:
                logger.info("No reference price yet, fetching historical bars...")
                await fetch_historical_bars(ib, state)
                if state.spx_price <= 0:
                    await asyncio.sleep(30)
                    continue

            new_exp = find_next_expiration(state.expirations)
            if new_exp and new_exp != state.expiration:
                state.expiration = new_exp
                logger.info(f"Expiration updated to: {get_expiration_display(new_exp)}")

            if not is_cboe_options_open():
                logger.info("SPX options in daily gap (5:00–8:15 PM ET) — skipping chain fetch")
                await asyncio.sleep(10)
                continue

            state.chain_fetching = True

            # Pause live chain streaming to free market data lines
            if state.chain_fetch_active is not None:
                state.chain_fetch_active.clear()
                for key, contract in list(state.chain_stream_contracts.items()):
                    try:
                        ib.cancelMktData(contract)
                    except Exception:
                        pass
                state.chain_stream_tickers.clear()
                state.chain_stream_contracts.clear()

            mode_label = "LIVE" if state.data_mode == "live" else "HIST"
            logger.info(
                f"[{mode_label}] Starting chain fetch: exp={state.expiration}, "
                f"spot={state.spx_price:.2f}, "
                f"{len(state.strikes)} total strikes available"
            )

            if state.active_tab != "chain":
                await broadcast_fn({"type": "chain_progress", "data": {
                    "phase": "starting", "batch": 0, "total_batches": 1, "pct": 0
                }})

            async def _on_progress(phase, batch, total_batches, pct):
                if state.active_tab != "chain":
                    await broadcast_fn({"type": "chain_progress", "data": {
                        "phase": phase, "batch": batch,
                        "total_batches": total_batches, "pct": pct,
                    }})

            await compute_annual_vol(ib, state, lookback_days=30)

            options = await fetch_option_chain(
                ib=ib,
                underlying=state.spx_contract,
                expiration=state.expiration,
                strikes=state.strikes,
                spot_price=state.spx_price,
                std_dev_range=8.0,
                annual_vol=state.annual_vol,
                progress_callback=_on_progress,
                force_requalify=force_manual_refresh,
                allow_unknown_retry=force_manual_refresh,
            )

            if options:
                total_oi = sum(o.open_interest for o in options)
                if total_oi == 0:
                    logger.info("OI is all zeros, using volume as proxy for GEX weight")
                    for o in options:
                        o.open_interest = o.volume

                tte_years = 0.0
                if state.expiration:
                    try:
                        exp_date = datetime.strptime(state.expiration, "%Y%m%d").date()
                        now = now_et()
                        if exp_date == now.date():
                            close_dt = now.replace(hour=16, minute=0, second=0, microsecond=0)
                            mins_left = max((close_dt - now).total_seconds() / 60.0, 1.0)
                            tte_years = mins_left / (390.0 * 252.0)
                        else:
                            days_left = (exp_date - now.date()).days
                            tte_years = max(days_left, 1) * 390.0 / (390.0 * 252.0)
                    except Exception:
                        tte_years = 0.0

                gex_result = compute_gex(options, state.spx_price,
                                         time_to_expiry_years=tte_years,
                                         risk_free_rate=state.risk_free_rate)
                gex_result.expiration = state.expiration
                gex_result.timestamp = now_et().isoformat()

                state.gex_result = gex_result
                state.latest_gex = gex_result_to_dict(gex_result)
                state.last_chain_update = now_et().strftime("%H:%M:%S")

                logger.info(
                    f"GEX computed: Call Wall={gex_result.call_wall}, "
                    f"Put Wall={gex_result.put_wall}, "
                    f"Gamma Flip={gex_result.gamma_flip}, "
                    f"Max Pain={gex_result.max_pain}"
                )

                gex_payload = dict(state.latest_gex)
                gex_payload["es_derived"] = state.es_derived
                await broadcast_fn({"type": "gex", "data": gex_payload})

                state.chain_data = options
                state.chain_quotes_cache = build_chain_quotes(
                    options, state.spx_price, gex_result, state.annual_vol, state.expiration)
                state.chain_quotes_cache["scope"] = "full"
                logger.info(
                    f"Broadcasting full chain_quotes: rows={len(state.chain_quotes_cache.get('strikes', []))}, "
                    f"spot={state.chain_quotes_cache.get('spot_price')}"
                )
                await broadcast_fn({"type": "chain_quotes", "data": state.chain_quotes_cache})
            else:
                logger.warning("No option data returned from chain fetch")

            if state.active_tab != "chain":
                await broadcast_fn({"type": "chain_progress", "data": {
                    "phase": "done", "batch": 1, "total_batches": 1, "pct": 100
                }})
            state.chain_fetching = False

            if state.chain_fetch_active is not None:
                state.chain_fetch_active.set()

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Chain fetch error: {e}", exc_info=True)
            state.chain_fetching = False
            if state.chain_fetch_active is not None:
                state.chain_fetch_active.set()

        refresh_timeout = SNAPSHOT_REFRESH_SECONDS
        try:
            await asyncio.wait_for(state.force_chain_fetch_event.wait(), timeout=refresh_timeout)
            state.force_chain_fetch_event.clear()
            logger.info("Manual chain refresh triggered")
        except asyncio.TimeoutError:
            pass


async def initial_dashboard_snapshot(ib, state):
    """Load one full snapshot before starting live option-chain streaming."""
    if not state.connected or not state.expiration:
        return
    if state.spx_price <= 0:
        await fetch_historical_bars(ib, state)
    state.manual_refresh_requested = True
    if state.force_chain_fetch_event is None:
        state.force_chain_fetch_event = asyncio.Event()
    state.force_chain_fetch_event.set()


async def chain_stream_loop(ib, state, broadcast_fn):
    """Maintain persistent market data subscriptions for nearest strikes."""
    state.chain_fetch_active = asyncio.Event()
    state.chain_fetch_active.set()

    await asyncio.sleep(2)
    last_expiration = ""
    last_sub_count = -1
    last_tick_log_ts = 0.0
    last_center_log = ""

    def _norm_key(strike: float, right: str):
        return (round(float(strike), 1), str(right).upper())

    def _finite_or_none(val):
        if val is None:
            return None
        try:
            f = float(val)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(f):
            return None
        if f == -1.0:
            return None
        return f

    while True:
        try:
            await state.chain_fetch_active.wait()

            if not state.connected or not state.expiration or state.spx_price <= 0:
                await asyncio.sleep(5)
                continue

            if not is_cboe_options_open():
                await asyncio.sleep(5)
                continue

            if state.expiration != last_expiration:
                state.chain_stream_unknown_keys.clear()
                for _, contract in list(state.chain_stream_contracts.items()):
                    try:
                        ib.cancelMktData(contract)
                    except Exception:
                        pass
                state.chain_stream_tickers.clear()
                state.chain_stream_contracts.clear()
                last_expiration = state.expiration
                logger.info(f"Chain stream expiration switched to {state.expiration}; reset subscriptions")

            spot = state.spx_price
            viewport_center = state.viewport_center_strike if state.active_tab == "chain" else 0.0
            focus_center = viewport_center if viewport_center > 0 else spot
            center_source = "viewport" if viewport_center > 0 else "spot"
            center_log = f"{center_source}:{focus_center:.1f}"
            if center_log != last_center_log:
                last_center_log = center_log
                logger.info(f"Chain stream center -> {center_source} {focus_center:.1f}")

            available_pairs = {
                _norm_key(o.strike, o.right) for o in state.chain_data
            }
            avail = sorted({s for (s, _) in available_pairs if s % 5 == 0})
            if not avail:
                avail = [s for s in state.strikes if s % 5 == 0]
            if not avail:
                await asyncio.sleep(10)
                continue

            max_strikes = max(1, CHAIN_STREAM_MAX_LINES // 2)
            nearest_strikes = sorted(avail, key=lambda s: (abs(s - focus_center), s))[:max_strikes]
            desired = set(nearest_strikes)

            current_keys = set(state.chain_stream_tickers.keys())
            desired_keys = set()
            for s in desired:
                desired_keys.add(_norm_key(s, 'C'))
                desired_keys.add(_norm_key(s, 'P'))

            if available_pairs:
                desired_keys = {k for k in desired_keys if k in available_pairs}
            desired_keys = {k for k in desired_keys if k not in state.chain_stream_unknown_keys}

            for key in current_keys - desired_keys:
                try:
                    contract = state.chain_stream_contracts.pop(key, None)
                    if contract:
                        ib.cancelMktData(contract)
                except Exception:
                    pass
                state.chain_stream_tickers.pop(key, None)

            new_keys = desired_keys - current_keys
            if new_keys:
                new_contracts = []
                for (strike, right) in new_keys:
                    c = Option(
                        symbol='SPX',
                        lastTradeDateOrContractMonth=state.expiration,
                        strike=strike, right=right,
                        exchange='SMART', multiplier='100',
                        currency='USD', tradingClass='SPXW',
                    )
                    new_contracts.append(((strike, right), c))

                try:
                    batch_size = 40
                    for i in range(0, len(new_contracts), batch_size):
                        batch = new_contracts[i:i + batch_size]
                        raw = [c for _, c in batch]
                        result = ib.qualifyContracts(*raw)
                        qualified_by_key = {}
                        for qc in result:
                            if qc is not None and getattr(qc, 'conId', 0) > 0:
                                qualified_by_key[_norm_key(qc.strike, qc.right)] = qc

                        for (key, _) in batch:
                            qc = qualified_by_key.get(key)
                            if qc is None:
                                state.chain_stream_unknown_keys.add(key)
                                continue
                            ticker = ib.reqMktData(qc, genericTickList='101', snapshot=False)
                            state.chain_stream_tickers[key] = ticker
                            state.chain_stream_contracts[key] = qc

                        if qualified_by_key:
                            logger.info(
                                f"Chain stream subscribed {len(qualified_by_key)}/{len(batch)} in batch; "
                                f"active_subs={len(state.chain_stream_tickers)}"
                            )
                        await asyncio.sleep(0.05)
                except Exception as e:
                    logger.warning(f"Chain stream subscribe error: {e}")

            if len(state.chain_stream_tickers) != last_sub_count:
                last_sub_count = len(state.chain_stream_tickers)
                logger.info(
                    f"Chain stream active subscriptions: {last_sub_count} "
                    f"(unknown_blacklist={len(state.chain_stream_unknown_keys)})"
                )

            await asyncio.sleep(CHAIN_STREAM_UPDATE_INTERVAL)

            ticks = []
            live_options: List[OptionData] = []
            oi_fallback = {
                (o.strike, o.right): o.open_interest
                for o in state.chain_data
            }
            for (strike, right), ticker in state.chain_stream_tickers.items():
                bid = _finite_or_none(ticker.bid)
                ask = _finite_or_none(ticker.ask)
                last_val = _finite_or_none(ticker.last)
                bid_sz = int(ticker.bidSize) if ticker.bidSize not in (None, -1) and not (isinstance(ticker.bidSize, float) and math.isnan(ticker.bidSize)) else 0
                ask_sz = int(ticker.askSize) if ticker.askSize not in (None, -1) and not (isinstance(ticker.askSize, float) and math.isnan(ticker.askSize)) else 0
                vol = int(ticker.volume) if ticker.volume not in (None, -1) and not (isinstance(ticker.volume, float) and math.isnan(ticker.volume)) else 0

                delta_raw, gamma_raw, iv_dec = _extract_stream_greeks(ticker)
                delta = round(delta_raw, 4) if delta_raw is not None else None
                gamma = round(gamma_raw, 6) if gamma_raw is not None else None
                iv = round(iv_dec * 100, 2) if iv_dec is not None else None

                if right == 'C':
                    oi_raw = ticker.callOpenInterest
                else:
                    oi_raw = ticker.putOpenInterest
                oi = int(oi_raw) if oi_raw not in (None, -1) and not (isinstance(oi_raw, float) and math.isnan(oi_raw)) else oi_fallback.get((strike, right), 0)

                ticks.append({
                    "strike": strike, "right": right,
                    "bid": round(bid, 2) if bid is not None else None,
                    "ask": round(ask, 2) if ask is not None else None,
                    "bid_size": bid_sz, "ask_size": ask_sz,
                    "last": round(last_val, 2) if last_val is not None else None,
                    "volume": vol,
                    "delta": delta, "gamma": gamma, "iv": iv,
                })

                live_options.append(OptionData(
                    strike=strike, right=right,
                    delta=delta, gamma=gamma,
                    implied_vol=iv_dec,
                    open_interest=oi, volume=vol,
                    bid=round(bid, 2) if bid is not None else None,
                    ask=round(ask, 2) if ask is not None else None,
                    last=round(last_val, 2) if last_val is not None else None,
                    bid_size=bid_sz, ask_size=ask_sz,
                ))

            if ticks:
                now_iso = now_et().isoformat()
                await broadcast_fn({
                    "type": "chain_tick",
                    "data": {"ticks": ticks, "timestamp_iso": now_iso}
                })

                live_quotes = build_chain_quotes(
                    options=live_options,
                    spot_price=state.spx_price,
                    gex_result=state.gex_result,
                    annual_vol=state.annual_vol,
                    expiration=state.expiration,
                )
                live_quotes["timestamp_iso"] = now_iso
                live_quotes["scope"] = "stream"
                state.last_chain_update = now_et().strftime("%H:%M:%S")

                now_monotonic = asyncio.get_event_loop().time()
                if now_monotonic - last_tick_log_ts >= 10.0:
                    last_tick_log_ts = now_monotonic
                    with_quotes = sum(1 for t in ticks if t.get("bid") is not None or t.get("ask") is not None or t.get("last") is not None)
                    logger.info(
                        f"Chain stream ticks: {len(ticks)} contracts, "
                        f"quotes_present={with_quotes}, active_subs={len(state.chain_stream_tickers)}"
                    )

                logger.debug(
                    f"Broadcasting stream chain_quotes: rows={len(live_quotes.get('strikes', []))}, "
                    f"active_subs={len(state.chain_stream_tickers)}"
                )
                await broadcast_fn({"type": "chain_quotes", "data": live_quotes})

        except asyncio.CancelledError:
            for key, contract in state.chain_stream_contracts.items():
                try:
                    ib.cancelMktData(contract)
                except Exception:
                    pass
            state.chain_stream_tickers.clear()
            state.chain_stream_contracts.clear()
            break
        except Exception as e:
            logger.error(f"Chain stream error: {e}")
            await asyncio.sleep(5)


# ---------------------------------------------------------------------------
# Monthly GEX fetch (on-demand, not a loop)
# ---------------------------------------------------------------------------
MONTHLY_CACHE_TTL = 600  # 10 minutes

async def monthly_gex_fetch(ib, state, broadcast_fn):
    """Fetch SPX monthly option chain and compute GEX, broadcast result.

    Skips fetch if cached data is less than MONTHLY_CACHE_TTL seconds old.
    """
    import time as _time

    now_mono = _time.monotonic()
    if (state.monthly_latest_gex is not None
            and (now_mono - state.monthly_last_fetch_ts) < MONTHLY_CACHE_TTL):
        logger.info("Monthly GEX cache still fresh, re-broadcasting cached data")
        await broadcast_fn({"type": "monthly_gex", "data": state.monthly_latest_gex})
        return

    if not state.connected or not state.monthly_expiration:
        logger.warning("Cannot fetch monthly GEX: not connected or no monthly expiration")
        return
    if state.spx_price <= 0:
        logger.warning("Cannot fetch monthly GEX: no reference price")
        return

    logger.info(
        f"Starting monthly GEX fetch: exp={state.monthly_expiration}, "
        f"spot={state.spx_price:.2f}, {len(state.monthly_strikes)} strikes"
    )

    await broadcast_fn({"type": "monthly_gex_progress", "data": {"phase": "starting"}})

    try:
        options = await fetch_option_chain(
            ib=ib,
            underlying=state.spx_contract,
            expiration=state.monthly_expiration,
            strikes=state.monthly_strikes,
            spot_price=state.spx_price,
            std_dev_range=8.0,
            annual_vol=state.annual_vol,
            trading_class='SPX',
        )

        if not options:
            logger.warning("No option data returned from monthly chain fetch")
            await broadcast_fn({"type": "monthly_gex_progress", "data": {"phase": "done"}})
            return

        total_oi = sum(o.open_interest for o in options)
        if total_oi == 0:
            logger.info("Monthly OI all zeros, using volume as proxy")
            for o in options:
                o.open_interest = o.volume

        # Compute time to expiry
        exp_date = datetime.strptime(state.monthly_expiration, "%Y%m%d").date()
        now = now_et()
        if exp_date == now.date():
            close_dt = now.replace(hour=16, minute=0, second=0, microsecond=0)
            mins_left = max((close_dt - now).total_seconds() / 60.0, 1.0)
            tte_years = mins_left / (390.0 * 252.0)
        else:
            days_left = (exp_date - now.date()).days
            tte_years = max(days_left, 1) / 252.0

        gex_result = compute_gex(
            options, state.spx_price,
            time_to_expiry_years=tte_years,
            risk_free_rate=state.risk_free_rate,
        )
        gex_result.expiration = state.monthly_expiration
        gex_result.timestamp = now_et().isoformat()

        state.monthly_gex_result = gex_result
        state.monthly_latest_gex = gex_result_to_dict(gex_result)
        state.monthly_latest_gex["es_derived"] = state.es_derived
        state.monthly_chain_data = options
        state.monthly_last_fetch_ts = _time.monotonic()

        logger.info(
            f"Monthly GEX computed: Call Wall={gex_result.call_wall}, "
            f"Put Wall={gex_result.put_wall}, Gamma Flip={gex_result.gamma_flip}, "
            f"Max Pain={gex_result.max_pain}"
        )

        await broadcast_fn({"type": "monthly_gex", "data": state.monthly_latest_gex})

    except Exception as e:
        logger.error(f"Monthly GEX fetch error: {e}", exc_info=True)
    finally:
        await broadcast_fn({"type": "monthly_gex_progress", "data": {"phase": "done"}})
