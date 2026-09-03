"""Layered intraday bar loading for the simulator: CSV -> yfinance -> IB.

The IB layer is async (it reuses the server's live client) and is therefore only
reachable via load_bars_async; sync load_bars covers csv/yfinance for tests and
offline runs.
"""
import asyncio
import logging
import math
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

import numpy as np

from sim_config import BAR_SECONDS, SimRunConfig

logger = logging.getLogger(__name__)
RTH_START_MIN = 9 * 60 + 30     # 09:30 ET
RTH_END_MIN = 16 * 60           # 16:00 ET (SPXW cease)


@dataclass
class BarSeries:
    closes: np.ndarray
    minute_of_day: np.ndarray
    bar_seconds: int
    source: str
    vix_closes: Optional[np.ndarray] = None
    warnings: List[str] = field(default_factory=list)


def parse_csv(path: str, bar_seconds: int) -> BarSeries:
    """Schema: timestamp,open,high,low,close[,volume]; ET ISO timestamps."""
    if not os.path.isfile(path):
        raise FileNotFoundError(f"CSV not found: {path}")
    stamps, closes = [], []
    with open(path, "r", encoding="utf-8") as f:
        header = f.readline().strip().split(",")
        ti, ci = header.index("timestamp"), header.index("close")
        for line in f:
            parts = line.strip().split(",")
            if len(parts) <= max(ti, ci) or not parts[ci]:
                continue
            stamps.append(datetime.fromisoformat(parts[ti]))
            closes.append(float(parts[ci]))
    if not closes:
        raise ValueError(f"CSV has no usable rows: {path}")
    mods = np.array([t.hour * 60 + t.minute for t in stamps], dtype=int)
    keep = (mods >= RTH_START_MIN + (bar_seconds // 60)) & (mods <= RTH_END_MIN)
    return BarSeries(
        closes=np.asarray(closes, dtype=float)[keep],
        minute_of_day=mods[keep],
        bar_seconds=bar_seconds,
        source="csv",
    )


def load_bars_yfinance(bar_seconds: int, lookback_days: int) -> BarSeries:
    import yfinance as yf   # deferred: keeps startup light when unused

    interval = {60: "1m", 300: "5m"}.get(bar_seconds)
    if interval is None:
        raise ValueError(f"yfinance cannot serve {bar_seconds}s bars (use 1m or 5m, or a CSV)")
    period = "7d" if interval == "1m" else "60d"
    df = yf.download("^SPX", interval=interval, period=period, progress=False, auto_adjust=False)
    if df is None or df.empty:
        raise RuntimeError("yfinance returned no ^SPX intraday data")
    idx = df.index.tz_convert("America/New_York")
    mods = np.array([t.hour * 60 + t.minute for t in idx], dtype=int)
    keep = (mods > RTH_START_MIN) & (mods <= RTH_END_MIN)
    vix = None
    try:
        vdf = yf.download("^VIX", interval="1d", period="6mo", progress=False, auto_adjust=False)
        if vdf is not None and not vdf.empty:
            vix = vdf["Close"].to_numpy().ravel()[-lookback_days:]
    except Exception as e:   # VIX is optional (mapping falls back to a constant)
        logger.warning(f"^VIX fetch failed: {e}")
    return BarSeries(
        closes=df["Close"].to_numpy().ravel()[keep],
        minute_of_day=mods[keep],
        bar_seconds=bar_seconds,
        source="yfinance",
        vix_closes=vix,
        warnings=["yfinance intraday history is shallow; prefer a CSV for stable calibration"],
    )


async def _load_bars_ib(ib, bar_seconds: int, lookback_days: int) -> BarSeries:
    from market_hours import last_trading_date

    size = {5: "5 secs", 15: "15 secs", 30: "30 secs", 60: "1 min", 300: "5 mins"}[bar_seconds]
    days = max(1, min(lookback_days, {5: 1, 15: 2, 30: 4, 60: 7, 300: 60}[bar_seconds]))
    bars = await ib.req_historical_bars(
        contract=None or getattr(ib, "spx_contract", None),
        end_date_time="", duration=f"{days * 2} D", bar_size=size,
        what_to_show="TRADES", use_rth=True,
    ) if hasattr(ib, "spx_contract") else None
    if not bars:
        raise RuntimeError("IB returned no historical bars")
    stamps = [b.date.astimezone() if b.date.tzinfo else b.date for b in bars]
    mods = np.array([t.hour * 60 + t.minute for t in stamps], dtype=int)
    return BarSeries(
        closes=np.array([float(b.close) for b in bars]),
        minute_of_day=mods, bar_seconds=bar_seconds, source="ib",
        warnings=[f"IB depth limited to ~{days}d at {size}"],
    )


def load_bars(cfg: SimRunConfig, bars: Optional[BarSeries] = None) -> BarSeries:
    """Sync layered loader: preloaded -> csv -> yfinance. IB requires load_bars_async."""
    if bars is not None:
        return bars
    bs = cfg.bar_seconds if hasattr(cfg, "bar_seconds") else BAR_SECONDS[cfg.bar_size]
    errors: List[str] = []
    if cfg.source in ("csv", "auto") and cfg.csv_path:
        try:
            return parse_csv(cfg.csv_path, bs)
        except Exception as e:
            errors.append(f"csv: {e}")
            if cfg.source == "csv":
                raise
    if cfg.source in ("yfinance", "auto"):
        try:
            return load_bars_yfinance(bs, cfg.lookback_days)
        except Exception as e:
            errors.append(f"yfinance: {e}")
    raise RuntimeError("No bar data available (" + "; ".join(errors or ["no layer attempted"]) + "). "
                       "Drop a CSV (timestamp,open,high,low,close) and set its path.")


async def load_bars_async(cfg: SimRunConfig, state=None, ib=None) -> BarSeries:
    """Server-side loader: same layering, plus the IB layer on the running loop."""
    if cfg.source in ("ib", "auto") and ib is not None:
        try:
            return await _load_bars_ib(ib, BAR_SECONDS[cfg.bar_size], cfg.lookback_days)
        except Exception as e:
            if cfg.source == "ib":
                raise
            logger.warning(f"IB bar layer failed: {e}")
    return await asyncio.to_thread(load_bars, cfg)
