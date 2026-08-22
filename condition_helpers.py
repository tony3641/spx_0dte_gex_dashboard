"""Pure math helpers for strategy conditions. No IO, no state."""
from typing import List, Optional


def wilder_rsi(closes: List[float], period: int = 14) -> Optional[float]:
    """Wilder's RSI over the newest 'period'+1 closes. Returns None if too few."""
    if len(closes) < period + 1:
        return None
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    avg_gain, avg_loss = gains / period, losses / period
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(d, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-d, 0.0)) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def percent_change(closes: List[float], minutes: int = 5) -> Optional[float]:
    """% move of the newest close vs `minutes` bars back. Returns None if short."""
    if len(closes) <= minutes:
        return None
    past = closes[-(minutes + 1)]
    if not past:
        return None
    return (closes[-1] - past) / past * 100.0


def nearest_row(rows: List[dict], spot: float) -> Optional[dict]:
    """Row whose strike is nearest to spot."""
    if not rows:
        return None
    return min(rows, key=lambda r: abs(r["strike"] - spot))


def atm_iv(rows: List[dict], spot: float) -> Optional[float]:
    """Average call+put IV (%) at the strike nearest spot."""
    row = nearest_row(rows, spot)
    if row is None:
        return None
    vals = [v for v in (row.get("call_iv"), row.get("put_iv")) if v is not None]
    if not vals:
        return None
    return sum(vals) / len(vals)


def spread_width(direction: str, short_strike: float, long_strike: float) -> float:
    """Strike distance in points (positive)."""
    return abs(round(short_strike - long_strike, 2))


def spread_margin(width_points: float) -> float:
    """Margin for a credit vertical = width * 100 (multiplier)."""
    return width_points * 100.0


def _side(row: dict, right: str, field: str):
    return row.get(f"{'call' if right == 'C' else 'put'}_{field}")


def _mid(bid, ask):
    if bid is None or ask is None or bid <= 0 or ask <= 0:
        return None
    return (bid + ask) / 2.0


def combo_credit(short_row: dict, long_row: dict, right: str) -> dict:
    """Net credit of a credit vertical (positive = credit). ``right`` is 'C' or 'P'."""
    s_bid, s_ask = _side(short_row, right, "bid"), _side(short_row, right, "ask")
    l_bid, l_ask = _side(long_row, right, "bid"), _side(long_row, right, "ask")
    s_mid, l_mid = _mid(s_bid, s_ask), _mid(l_bid, l_ask)
    if s_mid is None or l_mid is None:
        return {"bid": None, "ask": None, "mid": None}
    return {
        "mid": round(s_mid - l_mid, 4),
        "bid": round(s_bid - l_ask, 4),
        "ask": round(s_ask - l_bid, 4),
    }
