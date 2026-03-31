"""
GEX (Gamma Exposure) calculator.

Computes:
  - GEX per strike (call/put/net)
  - Call Wall (highest call gamma × OI strike)
  - Put Wall (highest put gamma × OI strike)
  - Gamma Flip Point (where cumulative net GEX crosses zero)
  - Max Pain (strike minimizing total option holder payout)
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


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


def compute_gex(options: List[OptionData], spot_price: float) -> GEXResult:
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
    }
