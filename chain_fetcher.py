"""
Batched option chain fetcher for IB.

Fetches the full SPXW 0DTE option chain using snapshot requests in batches
to stay within IB's 100 simultaneous market-data-line limit.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import List, Optional, Set, Tuple

from ib_insync import IB, Option, Contract, Ticker

from gex_calculator import OptionData

logger = logging.getLogger(__name__)

# IB limits: ~100 simultaneous market data lines.
# We use 45 per batch to leave headroom for the streaming SPX quote + safety margin.
BATCH_SIZE = 200

# Max contracts to qualify in one call
QUALIFY_BATCH_SIZE = 150

# Re-qualify only when spot moves more than this many points from cached anchor.
QUAL_CACHE_REQUALIFY_MOVE = 20.0


@dataclass
class QualificationCache:
    """Cache of qualified contracts and unknown contract keys for one expiration."""
    expiration: str = ""
    anchor_spot: float = 0.0
    qualified: List[Option] = field(default_factory=list)
    unknown_keys: Set[str] = field(default_factory=set)


_qualification_cache = QualificationCache()
_monthly_qualification_cache = QualificationCache()


def clear_qualification_cache(reason: str = "manual refresh", monthly: bool = False) -> None:
    """Clear qualified/unknown contract cache so next fetch re-qualifies from scratch."""
    global _qualification_cache, _monthly_qualification_cache
    if monthly:
        _monthly_qualification_cache = QualificationCache()
        logger.info(f"Monthly qualification cache cleared ({reason})")
    else:
        _qualification_cache = QualificationCache()
        logger.info(f"Qualification cache cleared ({reason})")


def _contract_key(expiration: str, strike: float, right: str) -> str:
    return f"{expiration}:{strike:.1f}:{right}"


# Default assumed annualised implied volatility when we have no better estimate.
DEFAULT_ANNUAL_VOL = 0.20          # 20 %
TRADING_DAYS_PER_YEAR = 252


def _strike_range_for_std_devs(
    spot: float,
    n_std: float = 5.0,
    annual_vol: float = DEFAULT_ANNUAL_VOL,
) -> Tuple[float, float]:
    """Return (low, high) strike bounds = spot ± n_std daily standard deviations."""
    import math
    daily_std = spot * annual_vol / math.sqrt(TRADING_DAYS_PER_YEAR)
    margin = n_std * daily_std
    return (spot - margin, spot + margin)


async def fetch_option_chain(
    ib: IB,
    underlying: Contract,
    expiration: str,
    strikes: List[float],
    spot_price: float,
    std_dev_range: float = 5.0,
    annual_vol: float = DEFAULT_ANNUAL_VOL,
    progress_callback=None,
    force_requalify: bool = False,
    allow_unknown_retry: bool = False,
    trading_class: str = 'SPXW',
) -> List[OptionData]:
    """
    Fetch the option chain for the given expiration using batched snapshots.

    Args:
        ib: Connected IB instance.
        underlying: Qualified SPX Index contract.
        expiration: Expiration string 'YYYYMMDD'.
        strikes: List of available strikes from reqSecDefOptParams.
        spot_price: Current SPX price.
        std_dev_range: Number of daily standard deviations around spot to include.
                       Default 8 → covers ≈ ±5-6 % of spot.
        annual_vol: Annualised implied volatility estimate (default 20 %).

    Returns:
        List of OptionData for all fetched contracts.
    """
    # Filter strikes: only multiples of 5, within N std-dev range of spot
    filtered_strikes = [s for s in strikes if s % 5 == 0]

    if spot_price > 0:
        low, high = _strike_range_for_std_devs(spot_price, std_dev_range, annual_vol)
        filtered_strikes = [s for s in filtered_strikes if low <= s <= high]

    filtered_strikes.sort()
    range_lo = filtered_strikes[0] if filtered_strikes else '?'
    range_hi = filtered_strikes[-1] if filtered_strikes else '?'
    logger.info(
        f"Chain fetch: {len(filtered_strikes)} strikes (±{std_dev_range:.0f}σ, vol={annual_vol:.1%}), "
        f"expiration={expiration}, range=[{range_lo}..{range_hi}]"
    )

    # Build all option contracts (calls + puts), skipping known-unknown contracts
    # unless this fetch is an explicit manual retry.
    global _qualification_cache, _monthly_qualification_cache
    cache = _monthly_qualification_cache if trading_class == 'SPX' else _qualification_cache
    if allow_unknown_retry:
        cache.unknown_keys.clear()

    contracts: List[Option] = []
    for strike in filtered_strikes:
        for right in ('C', 'P'):
            key = _contract_key(expiration, strike, right)
            if key in cache.unknown_keys and not allow_unknown_retry:
                continue
            contracts.append(
                Option(
                    symbol='SPX',
                    lastTradeDateOrContractMonth=expiration,
                    strike=strike,
                    right=right,
                    exchange='SMART',
                    multiplier='100',
                    currency='USD',
                    tradingClass=trading_class,
                )
            )

    logger.info(f"Total contracts to fetch: {len(contracts)}")

    need_requalify = force_requalify
    if cache.expiration != expiration:
        need_requalify = True
    elif cache.anchor_spot <= 0:
        need_requalify = True
    elif abs(spot_price - cache.anchor_spot) > QUAL_CACHE_REQUALIFY_MOVE:
        need_requalify = True
    elif not cache.qualified:
        need_requalify = True

    qualified: List[Option] = []
    if not need_requalify:
        valid_keys = {
            _contract_key(expiration, c.strike, c.right)
            for c in contracts
        }
        qualified = [
            c for c in cache.qualified
            if _contract_key(expiration, c.strike, c.right) in valid_keys
        ]
        logger.info(
            f"Using cached qualified contracts: {len(qualified)} "
            f"(anchor={cache.anchor_spot:.2f}, spot={spot_price:.2f})"
        )
    else:
        # Phase 1: Qualify contracts in batches
        logger.info("Re-qualifying contracts (cache miss / spot moved / manual retry)")
        newly_qualified: List[Option] = []
        unknown_keys: Set[str] = set(cache.unknown_keys)

        for i in range(0, len(contracts), QUALIFY_BATCH_SIZE):
            batch = contracts[i:i + QUALIFY_BATCH_SIZE]
            batch_num = i // QUALIFY_BATCH_SIZE + 1
            try:
                result = ib.qualifyContracts(*batch)
                result_ok = [c for c in result if c.conId > 0]
                newly_qualified.extend(result_ok)

                qualified_keys = {
                    _contract_key(expiration, c.strike, c.right)
                    for c in result_ok
                }
                for c in batch:
                    key = _contract_key(expiration, c.strike, c.right)
                    if key not in qualified_keys:
                        unknown_keys.add(key)

            except Exception as e:
                logger.warning(f"Qualify batch {batch_num} failed: {e}")
                # Conservative fallback: mark all contracts in failed batch unknown
                for c in batch:
                    unknown_keys.add(_contract_key(expiration, c.strike, c.right))

            # Small delay to avoid hammering IB
            await asyncio.sleep(0.1)

        cache.expiration = expiration
        cache.anchor_spot = spot_price
        cache.qualified = newly_qualified
        cache.unknown_keys = unknown_keys
        qualified = newly_qualified

        if trading_class == 'SPX':
            _monthly_qualification_cache = cache
        else:
            _qualification_cache = cache

        logger.info(
            f"Qualification cache updated: qualified={len(newly_qualified)}, "
            f"unknown_blacklist={len(unknown_keys)}, anchor={spot_price:.2f}"
        )

    logger.info(f"Qualified {len(qualified)} / {len(contracts)} contracts")

    if progress_callback and need_requalify:
        await progress_callback('qualifying', 1, 1, 10)

    # Phase 2: Snapshot market data in batches
    all_option_data: List[OptionData] = []
    for i in range(0, len(qualified), BATCH_SIZE):
        batch = qualified[i:i + BATCH_SIZE]
        batch_num = i // BATCH_SIZE + 1
        total_batches = (len(qualified) + BATCH_SIZE - 1) // BATCH_SIZE
        logger.info(f"Fetching snapshot batch {batch_num}/{total_batches} ({len(batch)} contracts)")

        if progress_callback:
            pct = 10 + int(80 * (batch_num - 1) / total_batches)
            await progress_callback('fetching', batch_num, total_batches, pct)

        try:
            tickers = await _snapshot_batch(ib, batch)
            for ticker in tickers:
                opt_data = _ticker_to_option_data(ticker)
                if opt_data is not None:
                    all_option_data.append(opt_data)
        except Exception as e:
            logger.warning(f"Snapshot batch {batch_num} failed: {e}")

        # Brief pause between batches
        await asyncio.sleep(0.5)

    if progress_callback:
        await progress_callback('computing', 1, 1, 95)

    total_oi = sum(o.open_interest for o in all_option_data)
    nonzero_oi = sum(1 for o in all_option_data if o.open_interest > 0)
    logger.info(
        f"Fetched data for {len(all_option_data)} options — "
        f"OI: {nonzero_oi}/{len(all_option_data)} contracts with non-zero OI, "
        f"total OI={total_oi:,}"
    )
    return all_option_data


async def _snapshot_batch(ib: IB, contracts: List[Option], timeout: float = 12.0) -> List[Ticker]:
    """
    Request streaming market data for a batch of contracts (snapshot=False so that
    genericTickList='101' for open interest is accepted by IB), then wait for data
    to arrive and cancel all subscriptions.

    Note: IB rejects genericTickList with snapshot=True (Error 321). We use
    streaming mode and cancel manually after `timeout` seconds.
    """
    tickers: List[Ticker] = []

    for contract in contracts:
        # snapshot=False required when using genericTickList; we cancel manually below
        ticker = ib.reqMktData(contract, genericTickList='101', snapshot=False)
        tickers.append(ticker)

    # Wait for streaming data to arrive
    await asyncio.sleep(timeout)

    # Cancel all subscriptions
    for contract in contracts:
        try:
            ib.cancelMktData(contract)
        except Exception:
            pass

    return tickers


def _safe_int(val) -> int:
    """Convert to int, treating None/NaN/inf/negative sentinel values as 0."""
    if val is None:
        return 0
    try:
        import math
        if math.isnan(val) or math.isinf(val):
            return 0
    except (TypeError, ValueError):
        pass
    try:
        result = int(val)
        if result < 0:
            return 0
        return result
    except (TypeError, ValueError):
        return 0


def _safe_float(val):
    """Return float or None if not a valid finite number."""
    if val is None:
        return None
    try:
        import math
        if math.isnan(val) or math.isinf(val):
            return None
    except (TypeError, ValueError):
        return None
    return val


def _normalize_iv(iv_val):
    """Normalize IV to decimal form (e.g. 0.18), tolerating percent-like inputs."""
    iv = _safe_float(iv_val)
    if iv is None or iv <= 0:
        return None
    if iv > 3.0:
        iv = iv / 100.0
    return iv


def _pick_greek_value(ticker: Ticker, field: str):
    """Pick first valid greek field from model/last/bid/ask greeks."""
    for source in (
        getattr(ticker, 'modelGreeks', None),
        getattr(ticker, 'lastGreeks', None),
        getattr(ticker, 'bidGreeks', None),
        getattr(ticker, 'askGreeks', None),
    ):
        if source is None:
            continue
        value = _safe_float(getattr(source, field, None))
        if value is not None:
            return value
    return None


def _ticker_to_option_data(ticker: Ticker) -> Optional[OptionData]:
    """Convert an IB Ticker to our OptionData model."""
    contract = ticker.contract
    if not hasattr(contract, 'strike') or not hasattr(contract, 'right'):
        return None

    gamma = _pick_greek_value(ticker, 'gamma')
    delta = _pick_greek_value(ticker, 'delta')
    implied_vol = _normalize_iv(_pick_greek_value(ticker, 'impliedVol'))
    if implied_vol is None:
        implied_vol = _normalize_iv(getattr(ticker, 'impliedVolatility', None))

    # Open interest — generic tick 101 populates callOpenInterest (tick 27) / putOpenInterest (tick 28)
    # on the individual option Ticker. Some IB responses may populate the other field
    # or use `openInterest` instead.
    if contract.right == 'P':
        oi = _safe_int(getattr(ticker, 'putOpenInterest', None))
        if oi == 0:
            oi = _safe_int(getattr(ticker, 'callOpenInterest', None))
    else:
        oi = _safe_int(getattr(ticker, 'callOpenInterest', None))
        if oi == 0:
            oi = _safe_int(getattr(ticker, 'putOpenInterest', None))

    if oi == 0:
        oi = _safe_int(getattr(ticker, 'openInterest', None))

    volume = _safe_int(ticker.volume)

    bid = _safe_float(ticker.bid) if ticker.bid not in (None, -1) else None
    ask = _safe_float(ticker.ask) if ticker.ask not in (None, -1) else None
    last = _safe_float(ticker.last) if ticker.last not in (None, -1) else None
    bid_size = _safe_int(ticker.bidSize)
    ask_size = _safe_int(ticker.askSize)

    return OptionData(
        strike=contract.strike,
        right=contract.right,
        gamma=gamma,
        delta=delta,
        open_interest=oi,
        volume=volume,
        implied_vol=implied_vol,
        bid=bid,
        ask=ask,
        last=last,
        bid_size=bid_size,
        ask_size=ask_size,
    )


async def get_chain_params(ib: IB, underlying: Contract) -> Tuple[List[str], List[float]]:
    """
    Get available SPXW expirations and strikes.

    Returns:
        (expirations, strikes) - sorted lists.
    """
    chains = await ib.reqSecDefOptParamsAsync(
        underlying.symbol, '', underlying.secType, underlying.conId
    )

    # Filter for SPXW (0DTE capable) on SMART exchange
    spxw_chain = None
    for chain in chains:
        if chain.tradingClass == 'SPXW' and chain.exchange == 'SMART':
            spxw_chain = chain
            break

    if spxw_chain is None:
        # Fallback: try any SPXW chain
        for chain in chains:
            if chain.tradingClass == 'SPXW':
                spxw_chain = chain
                break

    if spxw_chain is None:
        logger.error("No SPXW chain found!")
        return [], []

    expirations = sorted(spxw_chain.expirations)
    strikes = sorted(spxw_chain.strikes)

    logger.info(
        f"SPXW chain: {len(expirations)} expirations, {len(strikes)} strikes, "
        f"exchange={spxw_chain.exchange}"
    )

    return expirations, strikes


async def get_monthly_chain_params(ib: IB, underlying: Contract) -> Tuple[List[str], List[float]]:
    """
    Get available SPX monthly expirations and strikes.

    Returns:
        (expirations, strikes) - sorted lists for tradingClass='SPX'.
    """
    chains = await ib.reqSecDefOptParamsAsync(
        underlying.symbol, '', underlying.secType, underlying.conId
    )

    spx_chain = None
    for chain in chains:
        if chain.tradingClass == 'SPX' and chain.exchange == 'SMART':
            spx_chain = chain
            break

    if spx_chain is None:
        for chain in chains:
            if chain.tradingClass == 'SPX':
                spx_chain = chain
                break

    if spx_chain is None:
        logger.error("No SPX monthly chain found!")
        return [], []

    expirations = sorted(spx_chain.expirations)
    strikes = sorted(spx_chain.strikes)

    logger.info(
        f"SPX monthly chain: {len(expirations)} expirations, {len(strikes)} strikes, "
        f"exchange={spx_chain.exchange}"
    )

    return expirations, strikes


def find_monthly_expiration(expirations: List[str]) -> Optional[str]:
    """
    Find the current month's standard monthly expiration (3rd Friday).

    If past this month's expiry, returns next month's 3rd Friday.
    Falls back to searching the expirations list for the nearest future date.
    """
    from datetime import date as _date, timedelta
    from market_hours import now_et

    if not expirations:
        return None

    today = now_et().date()
    sorted_exps = sorted(expirations)

    # Calculate 3rd Friday of current month and next month
    def third_friday(year: int, month: int) -> _date:
        # 1st of month
        first = _date(year, month, 1)
        # Day of week (0=Mon, 4=Fri)
        dow = first.weekday()
        # First Friday
        first_fri = first + timedelta(days=(4 - dow) % 7)
        # 3rd Friday = first Friday + 14 days
        return first_fri + timedelta(days=14)

    tf_this = third_friday(today.year, today.month)

    if tf_this >= today:
        # This month's 3rd Friday is still in the future (or today)
        target_str = tf_this.strftime("%Y%m%d")
        if target_str in sorted_exps:
            return target_str
    else:
        # This month's 3rd Friday has passed, try next month
        if today.month == 12:
            tf_next = third_friday(today.year + 1, 1)
        else:
            tf_next = third_friday(today.year, today.month + 1)
        target_str = tf_next.strftime("%Y%m%d")
        if target_str in sorted_exps:
            return target_str

    # Fallback: find the first expiration >= today
    today_str = today.strftime("%Y%m%d")
    for exp in sorted_exps:
        if exp >= today_str:
            return exp

    return None
