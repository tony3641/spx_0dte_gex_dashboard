# tests/test_sim_config.py
import pytest
from sim_config import SimRunConfig, sweep_cells, BAR_SECONDS


def test_defaults_and_steps_per_day():
    cfg = SimRunConfig(strategy_name="Main")
    assert cfg.bar_size == "5m" and cfg.mode == "single"
    assert cfg.steps_per_day() == 78          # 390 min / 5
    assert BAR_SECONDS["1m"] == 60


def test_validate_rejects_bad_inputs():
    with pytest.raises(ValueError):
        SimRunConfig(strategy_name="").validate()
    with pytest.raises(ValueError):
        SimRunConfig(strategy_name="X", mode="both").validate()
    with pytest.raises(ValueError):
        SimRunConfig(strategy_name="X", bar_size="2m").validate()
    with pytest.raises(ValueError):
        SimRunConfig(strategy_name="X", n_paths=0).validate()
    with pytest.raises(ValueError):
        SimRunConfig(strategy_name="X", equity=-1).validate()
    with pytest.raises(ValueError):
        SimRunConfig(strategy_name="X", sl_multipliers=[0.0]).validate()
    SimRunConfig(strategy_name="X", sl_multipliers=[1.5, float("inf")]).validate()


def test_json_round_trip():
    cfg = SimRunConfig(strategy_name="Main", n_paths=99, sl_multipliers=[2.0, float("inf")])
    d = cfg.to_dict()
    assert d["sl_multipliers"][1] == "inf"     # JSON-safe encoding
    cfg2 = SimRunConfig.from_dict(d)
    assert cfg2 == cfg


def test_sweep_cells_product():
    cfg = SimRunConfig(strategy_name="Main", sl_multipliers=[1.5, 3.0],
                       strike_mode="dynamic_k", dynamic_k_values=[0.4, 0.6])
    cells = sweep_cells(cfg)
    assert cells == [
        {"sl_multiplier": 1.5, "k": 0.4}, {"sl_multiplier": 1.5, "k": 0.6},
        {"sl_multiplier": 3.0, "k": 0.4}, {"sl_multiplier": 3.0, "k": 0.6},
    ]


def test_sweep_cells_defaults():
    assert sweep_cells(SimRunConfig(strategy_name="M")) == [{"sl_multiplier": None, "k": None}]
