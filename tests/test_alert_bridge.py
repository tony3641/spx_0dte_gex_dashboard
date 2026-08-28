from discord_bot import AlertBridge


def _bridge():
    calls = []
    b = AlertBridge(channel_id="123", poster=lambda typ, data: calls.append((typ, data)))
    return b, calls


def test_forwards_order_status():
    b, calls = _bridge()
    b.forward({"type": "order_status", "data": {"status": "Filled", "orderId": 7}})
    assert calls == [("order_status", {"status": "Filled", "orderId": 7})]


def test_suppresses_candidate_too_high_frequency():
    b, calls = _bridge()
    b.forward({"type": "strategy_candidate", "data": {"name": "bp", "candidates": []}})
    assert calls == []


def test_suppresses_gex_and_account():
    b, calls = _bridge()
    b.forward({"type": "gex", "data": {}})
    b.forward({"type": "account_update", "data": {}})
    b.forward({"type": "vix_update", "data": {"vix": 14}})
    assert calls == []


def test_dedup_repeated_event():
    b, calls = _bridge()
    b.forward({"type": "strategy_exit", "data": {"name": "bp", "event": "take_profit"}})
    b.forward({"type": "strategy_exit", "data": {"name": "bp", "event": "take_profit"}})
    assert len(calls) == 1


def test_connection_change_only_forwards_on_transition():
    b, calls = _bridge()
    b.forward({"type": "status", "data": {"connected": True}})   # first -> seed, no post
    b.forward({"type": "status", "data": {"connected": True}})   # same -> no post
    assert calls == []
    b.forward({"type": "status", "data": {"connected": False}})  # transition -> post
    assert any(typ == "connection" for typ, _ in calls)


def test_kill_switch_change_via_strategy_list():
    b, calls = _bridge()
    b.forward({"type": "strategy_list", "data": {"kill_switch": False}})  # seed
    assert calls == []
    b.forward({"type": "strategy_list", "data": {"kill_switch": True}})   # change -> post
    assert any(typ == "kill_switch" for typ, _ in calls)


def test_no_poster_is_noop():
    b = AlertBridge()   # no channel_id, no poster
    b.forward({"type": "order_status", "data": {"status": "Filled", "orderId": 7}})
    # must not raise
