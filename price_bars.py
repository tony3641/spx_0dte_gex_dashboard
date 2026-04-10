"""
Historical bar seeding, OHLC bar aggregation, and annualized volatility
computation.

All functions accept `ib`, `state`, and `broadcast_fn` explicitly.
"""

import asyncio
import math
import logging
from datetime import datetime

from config import PRICE_PUSH_INTERVAL
from market_hours import now_et, is_within_rth, last_trading_date, ET

logger = logging.getLogger(__name__)


async def compute_annual_vol(ib, state, lookback_days: int = 30) -> float:
    """Compute annualised realised volatility from IB daily close bars."""
    if state.spx_contract is None:
        return state.annual_vol

    try:
        bars = await ib.reqHistoricalDataAsync(
            contract=state.spx_contract,
            endDateTime='',
            durationStr=f'{lookback_days} D',
            barSizeSetting='1 day',
            whatToShow='TRADES',
            useRTH=True,
            formatDate=1,
        )
    except Exception as e:
        logger.warning(f"Vol fetch failed, keeping {state.annual_vol:.1%}: {e}")
        return state.annual_vol

    if not bars or len(bars) < 5:
        logger.warning(f"Only {len(bars) if bars else 0} daily bars — not enough for vol calc")
        return state.annual_vol

    closes = [b.close for b in bars]
    log_returns = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    daily_std = (sum(r ** 2 for r in log_returns) / len(log_returns)) ** 0.5
    annual = daily_std * math.sqrt(252)
    state.annual_vol = annual
    logger.info(
        f"Realised vol computed from {len(log_returns)} daily bars: "
        f"daily σ={daily_std:.4f}, annual σ={annual:.2%}"
    )
    return annual


async def fetch_historical_bars(ib, state):
    """Fetch 1-min intraday bars for the last RTH session."""
    if state.spx_contract is None:
        return

    session_date = last_trading_date()

    if is_within_rth():
        end_dt = ""
    else:
        end_dt = datetime(
            session_date.year, session_date.month, session_date.day,
            16, 30, 0,
        ).strftime("%Y%m%d-%H:%M:%S")

    logger.info(f"Fetching 1-min historical bars for {session_date.isoformat()} "
                f"(endDateTime={'now' if end_dt == '' else end_dt})...")

    try:
        bars = await ib.reqHistoricalDataAsync(
            contract=state.spx_contract,
            endDateTime=end_dt,
            durationStr='1 D',
            barSizeSetting='1 min',
            whatToShow='TRADES',
            useRTH=True,
            formatDate=1,
        )
    except Exception as e:
        logger.error(f"Historical bar fetch failed: {e}")
        return

    if not bars:
        logger.warning("No historical bars returned")
        return

    state.price_history.clear()
    for bar in bars:
        bar_dt = bar.date.astimezone(ET) if bar.date.tzinfo else bar.date.replace(tzinfo=ET)
        state.price_history.append({
            "time": bar_dt.isoformat(),
            "time_short": bar_dt.strftime("%H:%M"),
            "open": round(bar.open, 2),
            "high": round(bar.high, 2),
            "low": round(bar.low, 2),
            "close": round(bar.close, 2),
        })

    last_close = bars[-1].close
    state.spx_price = last_close
    state.spx_last_close = last_close
    state.historical_date = session_date.isoformat()
    if state.data_mode != "live":
        state.data_mode = "historical"

    logger.info(
        f"Loaded {len(bars)} historical bars for {session_date.isoformat()}, "
        f"last close={last_close:.2f}"
    )


async def price_push_loop(ib, state, broadcast_fn):
    """Aggregate live ticks into 1-minute OHLC bars and push to clients."""
    current_bar = None
    current_minute = None

    while True:
        try:
            await asyncio.sleep(PRICE_PUSH_INTERVAL)

            if state.active_tab == "chain":
                current_bar = None
                current_minute = None
                continue

            if state.live_price <= 0 or not is_within_rth() or state.data_mode != "live":
                continue

            now = now_et()
            minute_key = (now.hour, now.minute)
            price = round(state.live_price, 2)

            if minute_key != current_minute:
                if current_bar is not None:
                    if not state.price_history or state.price_history[-1]["time"] != current_bar["time"]:
                        state.price_history.append(current_bar)
                    await broadcast_fn({"type": "bar", "data": current_bar})

                bar_time = now.replace(second=0, microsecond=0)
                bar_time_iso = bar_time.isoformat()
                if state.price_history and state.price_history[-1]["time"] == bar_time_iso:
                    existing = state.price_history[-1]
                    current_bar = {
                        "time": bar_time_iso,
                        "time_short": bar_time.strftime("%H:%M"),
                        "open": existing["open"],
                        "high": max(existing["high"], price),
                        "low": min(existing["low"], price),
                        "close": price,
                    }
                else:
                    current_bar = {
                        "time": bar_time_iso,
                        "time_short": bar_time.strftime("%H:%M"),
                        "open": price,
                        "high": price,
                        "low": price,
                        "close": price,
                    }
                current_minute = minute_key
            else:
                current_bar["high"] = max(current_bar["high"], price)
                current_bar["low"] = min(current_bar["low"], price)
                current_bar["close"] = price
                await broadcast_fn({"type": "bar_update", "data": current_bar})

        except asyncio.CancelledError:
            if current_bar is not None:
                state.price_history.append(current_bar)
            break
        except Exception as e:
            logger.error(f"Price push error: {e}")
            await asyncio.sleep(1)
