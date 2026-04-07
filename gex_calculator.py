"""
GEX (Gamma Exposure) calculator.

Computes:
  - GEX per strike (call/put/net)
  - Call Wall (highest call gamma × OI strike)
  - Put Wall (highest put gamma × OI strike)
  - Gamma Flip Point (where cumulative net GEX crosses zero)
  - Max Pain (strike minimizing total option holder payout)
  - IV smile data with charm and delta-decay efficiency
"""

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


# ===========================================================================
# Black-Scholes helpers (no scipy dependency — uses math.erf)
# ===========================================================================

def _norm_cdf(x: float) -> float:
    """Standard normal CDF via erf."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _bsm_delta(S: float, K: float, T: float, r: float, sigma: float, right: str) -> Optional[float]:
    """
    BSM delta for a European option.

    Args:
        S: spot price
        K: strike
        T: time to expiry in years (must be > 0)
        r: risk-free rate (annualized, e.g. 0.053)
        sigma: implied volatility (annualized, e.g. 0.18)
        right: 'C' or 'P'

    Returns:
        delta value, or None if inputs are degenerate.
    """
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return None
    try:
        sqrt_T = math.sqrt(T)
        d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * sqrt_T)
        if right == 'C':
            return _norm_cdf(d1)
        else:
            return _norm_cdf(d1) - 1.0
    except (ValueError, ZeroDivisionError):
        return None


def _compute_charm_fd(
    S: float, K: float, T: float, r: float, sigma: float, right: str,
    dt: float = 15.0 / (390.0 * 252.0),
) -> Optional[float]:
    """
    Compute charm (∂δ/∂t) via finite-difference on BSM delta.

    Uses δ(T) and δ(T - dt) to approximate the rate of delta decay.
    Default dt = 15 minutes expressed in trading-year fractions.

    Returns:
        charm value (positive means delta is decaying toward zero),
        or None if computation fails.
    """
    if T <= dt:
        # Not enough time left for finite difference
        dt = T * 0.5
        if dt <= 0:
            return None

    delta_now = _bsm_delta(S, K, T, r, sigma, right)
    delta_later = _bsm_delta(S, K, T - dt, r, sigma, right)

    if delta_now is None or delta_later is None:
        return None

    # charm = -dδ/dT (positive = delta decaying toward zero as time passes)
    return -(delta_later - delta_now) / dt


@dataclass
class OptionData:
    """Single option contract data point."""
    strike: float
    right: str  # 'C' or 'P'
    gamma: Optional[float] = None
    delta: Optional[float] = None
    open_interest: int = 0
    volume: int = 0
    implied_vol: Optional[float] = None
    bid: Optional[float] = None
    ask: Optional[float] = None
    last: Optional[float] = None
    bid_size: int = 0
    ask_size: int = 0


@dataclass
class GEXResult:
    """Result of a full GEX calculation."""
    gex_by_strike: Dict[float, float] = field(default_factory=dict)   # net GEX per strike
    call_gex_by_strike: Dict[float, float] = field(default_factory=dict)
    put_gex_by_strike: Dict[float, float] = field(default_factory=dict)
    call_wall: Optional[float] = None
    put_wall: Optional[float] = None
    gamma_flip: Optional[float] = None
    max_pain: Optional[float] = None
    spot_price: float = 0.0
    expiration: str = ""
    timestamp: str = ""
    total_call_gex: float = 0.0
    total_put_gex: float = 0.0
    total_net_gex: float = 0.0
    strikes: List[float] = field(default_factory=list)
    call_oi_by_strike: Dict[float, int] = field(default_factory=dict)
    put_oi_by_strike: Dict[float, int] = field(default_factory=dict)
    total_call_oi: int = 0
    total_put_oi: int = 0
    call_vol_by_strike: Dict[float, int] = field(default_factory=dict)
    put_vol_by_strike: Dict[float, int] = field(default_factory=dict)
    total_call_vol: int = 0
    total_put_vol: int = 0
    # IV smile / charm / delta data (unfiltered — all strikes)
    call_iv_by_strike: Dict[float, float] = field(default_factory=dict)
    put_iv_by_strike: Dict[float, float] = field(default_factory=dict)
    call_delta_by_strike: Dict[float, float] = field(default_factory=dict)
    put_delta_by_strike: Dict[float, float] = field(default_factory=dict)
    call_charm_by_strike: Dict[float, float] = field(default_factory=dict)
    put_charm_by_strike: Dict[float, float] = field(default_factory=dict)


def compute_gex(options: List[OptionData], spot_price: float,
                time_to_expiry_years: float = 0.0,
                risk_free_rate: float = 0.053) -> GEXResult:
    """
    Compute GEX metrics from a list of option data.

    GEX per strike formula (dealer perspective):
      Call GEX = gamma_C × OI_C × 100 × spot × 0.01
      Put GEX  = -1 × gamma_P × OI_P × 100 × spot × 0.01
      Net GEX  = Call GEX + Put GEX

    Args:
        options: List of OptionData for all strikes/rights in the chain.
        spot_price: Current underlying price.

    Returns:
        GEXResult with all computed metrics.
    """
    if not options or spot_price <= 0:
        return GEXResult(spot_price=spot_price)

    # Separate calls and puts, group by strike
    calls_by_strike: Dict[float, OptionData] = {}
    puts_by_strike: Dict[float, OptionData] = {}

    for opt in options:
        if opt.right == 'C':
            calls_by_strike[opt.strike] = opt
        elif opt.right == 'P':
            puts_by_strike[opt.strike] = opt

    all_strikes = sorted(set(list(calls_by_strike.keys()) + list(puts_by_strike.keys())))

    if not all_strikes:
        return GEXResult(spot_price=spot_price)

    # Compute GEX per strike
    call_gex: Dict[float, float] = {}
    put_gex: Dict[float, float] = {}
    net_gex: Dict[float, float] = {}
    call_oi_per_strike: Dict[float, int] = {}
    put_oi_per_strike: Dict[float, int] = {}
    call_vol_per_strike: Dict[float, int] = {}
    put_vol_per_strike: Dict[float, int] = {}
    # Smile / charm data
    call_iv_map: Dict[float, float] = {}
    put_iv_map: Dict[float, float] = {}
    call_delta_map: Dict[float, float] = {}
    put_delta_map: Dict[float, float] = {}
    call_charm_map: Dict[float, float] = {}
    put_charm_map: Dict[float, float] = {}

    # For wall detection
    max_call_gex_val = 0.0
    max_call_gex_strike = None
    max_put_gex_val = 0.0
    max_put_gex_strike = None

    multiplier = 100.0  # SPX option multiplier

    for strike in all_strikes:
        c_gex = 0.0
        p_gex = 0.0

        # Call GEX (positive contribution from dealer perspective)
        if strike in calls_by_strike:
            c = calls_by_strike[strike]
            if c.gamma is not None and c.open_interest > 0:
                c_gex = c.gamma * c.open_interest * multiplier * spot_price * 0.01

        # Put GEX (negative contribution - dealers short puts hedge by selling)
        if strike in puts_by_strike:
            p = puts_by_strike[strike]
            if p.gamma is not None and p.open_interest > 0:
                p_gex = -1.0 * p.gamma * p.open_interest * multiplier * spot_price * 0.01

        call_gex[strike] = c_gex
        put_gex[strike] = p_gex
        net_gex[strike] = c_gex + p_gex
        call_oi_per_strike[strike] = calls_by_strike[strike].open_interest if strike in calls_by_strike else 0
        put_oi_per_strike[strike] = puts_by_strike[strike].open_interest if strike in puts_by_strike else 0
        call_vol_per_strike[strike] = (calls_by_strike[strike].volume or 0) if strike in calls_by_strike else 0
        put_vol_per_strike[strike] = (puts_by_strike[strike].volume or 0) if strike in puts_by_strike else 0

        # IV / delta / charm per strike
        if strike in calls_by_strike:
            c = calls_by_strike[strike]
            if c.implied_vol is not None and c.implied_vol > 0:
                call_iv_map[strike] = c.implied_vol
            if c.delta is not None:
                call_delta_map[strike] = c.delta
            if time_to_expiry_years > 0 and c.implied_vol and c.implied_vol > 0:
                ch = _compute_charm_fd(spot_price, strike, time_to_expiry_years,
                                      risk_free_rate, c.implied_vol, 'C')
                if ch is not None:
                    call_charm_map[strike] = ch
        if strike in puts_by_strike:
            p = puts_by_strike[strike]
            if p.implied_vol is not None and p.implied_vol > 0:
                put_iv_map[strike] = p.implied_vol
            if p.delta is not None:
                put_delta_map[strike] = p.delta
            if time_to_expiry_years > 0 and p.implied_vol and p.implied_vol > 0:
                ch = _compute_charm_fd(spot_price, strike, time_to_expiry_years,
                                      risk_free_rate, p.implied_vol, 'P')
                if ch is not None:
                    put_charm_map[strike] = ch

        # Track walls (absolute magnitudes)
        if c_gex > max_call_gex_val:
            max_call_gex_val = c_gex
            max_call_gex_strike = strike

        if abs(p_gex) > max_put_gex_val:
            max_put_gex_val = abs(p_gex)
            max_put_gex_strike = strike

    # Gamma Flip Point: where cumulative net GEX crosses zero
    gamma_flip = _find_gamma_flip(all_strikes, net_gex)

    # Max Pain
    max_pain = _compute_max_pain(all_strikes, calls_by_strike, puts_by_strike)

    total_call = sum(call_gex.values())
    total_put = sum(put_gex.values())

    return GEXResult(
        gex_by_strike=net_gex,
        call_gex_by_strike=call_gex,
        put_gex_by_strike=put_gex,
        call_wall=max_call_gex_strike,
        put_wall=max_put_gex_strike,
        gamma_flip=gamma_flip,
        max_pain=max_pain,
        spot_price=spot_price,
        total_call_gex=total_call,
        total_put_gex=total_put,
        total_net_gex=total_call + total_put,
        strikes=all_strikes,
        call_oi_by_strike=call_oi_per_strike,
        put_oi_by_strike=put_oi_per_strike,
        total_call_oi=sum(call_oi_per_strike.values()),
        total_put_oi=sum(put_oi_per_strike.values()),
        call_vol_by_strike=call_vol_per_strike,
        put_vol_by_strike=put_vol_per_strike,
        total_call_vol=sum(call_vol_per_strike.values()),
        total_put_vol=sum(put_vol_per_strike.values()),
        call_iv_by_strike=call_iv_map,
        put_iv_by_strike=put_iv_map,
        call_delta_by_strike=call_delta_map,
        put_delta_by_strike=put_delta_map,
        call_charm_by_strike=call_charm_map,
        put_charm_by_strike=put_charm_map,
    )


def _find_gamma_flip(strikes: List[float], net_gex: Dict[float, float]) -> Optional[float]:
    """
    Find the price level where cumulative net GEX crosses zero.
    Walk from lowest strike upward, accumulating net GEX.
    The flip point is where the cumulative sum changes sign.
    """
    if len(strikes) < 2:
        return None

    cumulative = 0.0
    prev_cumulative = 0.0

    for i, strike in enumerate(strikes):
        prev_cumulative = cumulative
        cumulative += net_gex.get(strike, 0.0)

        if i > 0 and prev_cumulative != 0 and cumulative != 0:
            # Check for sign change
            if (prev_cumulative < 0 and cumulative > 0) or \
               (prev_cumulative > 0 and cumulative < 0):
                # Linear interpolation between strikes[i-1] and strikes[i]
                prev_strike = strikes[i - 1]
                # Fraction of the way from prev_strike to strike where zero crossing occurs
                fraction = abs(prev_cumulative) / (abs(prev_cumulative) + abs(cumulative))
                flip = prev_strike + fraction * (strike - prev_strike)
                return round(flip, 2)

    return None


def _compute_max_pain(
    strikes: List[float],
    calls_by_strike: Dict[float, OptionData],
    puts_by_strike: Dict[float, OptionData],
) -> Optional[float]:
    """
    Max Pain = strike at which total payout to option holders is minimized.

    For each candidate settlement price P:
      - For each call at strike K_c with OI: if P > K_c, payout = (P - K_c) × OI
      - For each put at strike K_p with OI: if P < K_p, payout = (K_p - P) × OI
      - Total payout = sum of all call + put payouts
    Max Pain = strike P that minimizes total payout.
    """
    if not strikes:
        return None

    min_pain = float('inf')
    max_pain_strike = None

    for settlement in strikes:
        total_pain = 0.0

        # Call pain: holders profit when settlement > strike
        for strike, opt in calls_by_strike.items():
            if settlement > strike and opt.open_interest > 0:
                total_pain += (settlement - strike) * opt.open_interest

        # Put pain: holders profit when settlement < strike
        for strike, opt in puts_by_strike.items():
            if settlement < strike and opt.open_interest > 0:
                total_pain += (strike - settlement) * opt.open_interest

        if total_pain < min_pain:
            min_pain = total_pain
            max_pain_strike = settlement

    return max_pain_strike


def gex_result_to_dict(result: GEXResult) -> dict:
    """Serialize GEXResult to a JSON-friendly dict."""
    # For the GEX bar chart, only send strikes with meaningful GEX
    gex_bars = []
    for strike in result.strikes:
        call_g = result.call_gex_by_strike.get(strike, 0.0)
        put_g = result.put_gex_by_strike.get(strike, 0.0)
        net_g = result.gex_by_strike.get(strike, 0.0)
        if abs(net_g) > 0.001 or abs(call_g) > 0.001 or abs(put_g) > 0.001:
            gex_bars.append({
                "strike": strike,
                "call_gex": round(call_g, 2),
                "put_gex": round(put_g, 2),
                "net_gex": round(net_g, 2),
                "call_oi": result.call_oi_by_strike.get(strike, 0),
                "put_oi": result.put_oi_by_strike.get(strike, 0),
                "call_vol": result.call_vol_by_strike.get(strike, 0),
                "put_vol": result.put_vol_by_strike.get(strike, 0),
            })

    return {
        "gex_bars": gex_bars,
        "call_wall": result.call_wall,
        "put_wall": result.put_wall,
        "gamma_flip": result.gamma_flip,
        "max_pain": result.max_pain,
        "spot_price": result.spot_price,
        "expiration": result.expiration,
        "timestamp": result.timestamp,
        "total_call_gex": round(result.total_call_gex, 2),
        "total_put_gex": round(result.total_put_gex, 2),
        "total_net_gex": round(result.total_net_gex, 2),
        "total_call_oi": result.total_call_oi,
        "total_put_oi": result.total_put_oi,
        "total_call_vol": result.total_call_vol,
        "total_put_vol": result.total_put_vol,
        "smile_data": _build_smile_data(result),
    }


def _build_smile_data(result: GEXResult) -> list:
    """Build smile_data array — unfiltered by GEX, includes all strikes with valid IV."""
    data = []
    for strike in result.strikes:
        c_iv = result.call_iv_by_strike.get(strike)
        p_iv = result.put_iv_by_strike.get(strike)
        # Skip strikes where neither call nor put has IV
        if c_iv is None and p_iv is None:
            continue
        c_delta = result.call_delta_by_strike.get(strike)
        p_delta = result.put_delta_by_strike.get(strike)
        c_charm = result.call_charm_by_strike.get(strike)
        p_charm = result.put_charm_by_strike.get(strike)
        # Delta-decay efficiency = |charm| / |delta|  (clamped when delta ≈ 0)
        c_eff = None
        if c_charm is not None and c_delta is not None and abs(c_delta) > 0.001:
            c_eff = round(abs(c_charm) / abs(c_delta), 4)
        p_eff = None
        if p_charm is not None and p_delta is not None and abs(p_delta) > 0.001:
            p_eff = round(abs(p_charm) / abs(p_delta), 4)
        data.append({
            "strike": strike,
            "call_iv": round(c_iv * 100, 2) if c_iv is not None else None,   # as %
            "put_iv": round(p_iv * 100, 2) if p_iv is not None else None,
            "call_delta": round(c_delta, 4) if c_delta is not None else None,
            "put_delta": round(p_delta, 4) if p_delta is not None else None,
            "call_charm": round(c_charm, 6) if c_charm is not None else None,
            "put_charm": round(p_charm, 6) if p_charm is not None else None,
            "call_efficiency": c_eff,
            "put_efficiency": p_eff,
        })
    return data
