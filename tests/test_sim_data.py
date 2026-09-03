# tests/test_sim_data.py
import os
import numpy as np
import pytest

from sim_config import SimRunConfig
from sim_data import BarSeries, load_bars, parse_csv

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "sim_bars_5m.csv")


def test_parse_csv_fixture():
    bs = parse_csv(FIXTURE, 300)
    assert bs.source == "csv" and bs.bar_seconds == 300
    assert len(bs.closes) == 780
    # 10 consecutive weekdays x 78 bars; minute_of_day repeats 09:35..16:00
    mods = bs.minute_of_day[:78]
    assert mods[0] == 9 * 60 + 35 and mods[-1] == 16 * 60
    assert np.isfinite(bs.closes).all() and (bs.closes > 0).all()
    # bars outside RTH are absent by construction, but guard the filter anyway
    assert (bs.minute_of_day >= 570).all() and (bs.minute_of_day <= 960).all()


def test_load_bars_csv_layer():
    cfg = SimRunConfig(strategy_name="Main", source="csv", csv_path=FIXTURE)
    bs = load_bars(cfg)
    assert bs.source == "csv" and len(bs.closes) == 780


def test_load_bars_preloaded_bypass():
    cfg = SimRunConfig(strategy_name="Main", source="csv", csv_path=FIXTURE)
    pre = BarSeries(closes=np.array([1.0, 2.0]), minute_of_day=np.array([575, 580]),
                    bar_seconds=300, source="csv")
    assert load_bars(cfg, bars=pre) is pre


def test_load_bars_missing_csv_raises():
    cfg = SimRunConfig(strategy_name="Main", source="csv", csv_path="Z:/nope.csv")
    with pytest.raises(FileNotFoundError):
        load_bars(cfg)
