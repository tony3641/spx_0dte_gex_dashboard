from app_state import create_app_state


def test_strategy_state_defaults():
    s = create_app_state()
    assert s.strategies == {}
    assert s.strategy_candidates == {}
    assert s.vix is None
    assert s.auto_trade_kill_switch is False
    assert s.strategy_log == []
    assert s.strategy_open_positions == {}


def test_subsequent_runtime_state_defaults():
    s = create_app_state()
    assert s.runtime == {}
    assert s.day_key == ""
