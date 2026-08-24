"""Tests for Task 10: strategy WebSocket handlers + status-loop vix broadcast.

The handlers live in ws_handler.websocket_endpoint and are driven here by
injecting raw message strings (matching the shape a browser would send), then
inspecting the messages the endpoint pushes back. status_push_loop gains a
vix_update broadcast.
"""

import asyncio
import json

import pytest
from fastapi import WebSocketDisconnect

import strategy_store as store
import ws_handler
from app_state import create_app_state
from strategy_models import Strategy
from ws_handler import status_push_loop, websocket_endpoint


class FakeWS:
    """Minimal WebSocket: queues inbound messages, records outbound JSON."""

    def __init__(self, messages):
        self._queue = list(messages)
        self.sent = []
        self.accepted = False

    async def accept(self):
        self.accepted = True

    async def receive_text(self):
        if self._queue:
            return self._queue.pop(0)
        raise WebSocketDisconnect()

    async def send_text(self, text):
        self.sent.append(json.loads(text))

    def sent_by_type(self, msg_type):
        return [m for m in self.sent if m.get("type") == msg_type]


async def _noop_broadcast(message):
    pass


def _body(name="a"):
    return {
        "name": name,
        "direction": "bull_put",
        "conditions": [{"kind": "short_delta", "enabled": True,
                        "params": {"min": 0.1, "max": 0.3}}],
        "exit_rules": {"take_profit": None, "stop_loss": None, "hold_to_expire": False},
        "auto_execute": False,
        "armed": False,
        "target_expiry": "",
    }


@pytest.mark.asyncio
async def test_set_tab_strategies_sends_strategy_list(app_state, tmp_path, monkeypatch):
    monkeypatch.setattr(store, "STRATEGIES_PATH", tmp_path / "strategies.json")
    ws = FakeWS(["set_tab:strategies"])

    await websocket_endpoint(ws, None, app_state, _noop_broadcast)

    assert app_state.active_tab == "strategies"
    lists = ws.sent_by_type("strategy_list")
    assert len(lists) == 1
    assert lists[0]["data"]["strategies"] == []
    assert lists[0]["data"]["kill_switch"] is False


@pytest.mark.asyncio
async def test_strategy_save_persists_and_sends_strategy_list(app_state, tmp_path, monkeypatch):
    p = tmp_path / "strategies.json"
    monkeypatch.setattr(store, "STRATEGIES_PATH", p)
    ws = FakeWS([f"strategy_save:{json.dumps(_body('a'))}"])

    await websocket_endpoint(ws, None, app_state, _noop_broadcast)

    assert app_state.strategies["a"].direction == "bull_put"
    saved = store.load_strategies()
    assert "a" in saved
    lists = ws.sent_by_type("strategy_list")
    assert len(lists) == 1
    assert lists[0]["data"]["strategies"][0]["name"] == "a"


@pytest.mark.asyncio
async def test_strategy_save_persists_budget(app_state, tmp_path, monkeypatch):
    p = tmp_path / "strategies.json"
    monkeypatch.setattr(store, "STRATEGIES_PATH", p)
    body = _body("b")
    body["budget"] = 8000.0
    ws = FakeWS([f"strategy_save:{json.dumps(body)}"])

    await websocket_endpoint(ws, None, app_state, _noop_broadcast)

    assert app_state.strategies["b"].budget == 8000.0
    assert store.load_strategies()["b"].budget == 8000.0


@pytest.mark.asyncio
async def test_strategy_save_rejects_budget_above_excess(app_state, monkeypatch):
    app_state.account_summary = {"ExcessLiquidity": 5000.0}
    body = _body("over")
    body["budget"] = 20000.0
    ws = FakeWS([f"strategy_save:{json.dumps(body)}"])

    await websocket_endpoint(ws, None, app_state, _noop_broadcast)

    errs = ws.sent_by_type("strategy_error")
    assert len(errs) == 1
    assert "excess" in errs[0]["data"]["message"].lower()
    # Nothing persisted and no strategy entered state.
    assert "over" not in app_state.strategies


@pytest.mark.asyncio
async def test_strategy_save_allows_budget_when_excess_unknown(app_state, monkeypatch, tmp_path):
    # When liquidity is unknown (e.g. disconnected), a budget is not rejected.
    # Isolate STRATEGIES_PATH so the save does not pollute the real config file.
    monkeypatch.setattr(store, "STRATEGIES_PATH", tmp_path / "strategies.json")
    app_state.account_summary = {}
    body = _body("unknown")
    body["budget"] = 20000.0
    ws = FakeWS([f"strategy_save:{json.dumps(body)}"])

    await websocket_endpoint(ws, None, app_state, _noop_broadcast)

    assert app_state.strategies["unknown"].budget == 20000.0
    assert ws.sent_by_type("strategy_error") == []


@pytest.mark.asyncio
async def test_strategy_arm_sets_armed(app_state):
    app_state.strategies["a"] = Strategy(name="a", direction="bull_put", conditions=[])
    ws = FakeWS(["strategy_arm:a"])

    await websocket_endpoint(ws, None, app_state, _noop_broadcast)

    assert app_state.strategies["a"].armed is True


@pytest.mark.asyncio
async def test_strategy_arm_unknown_name_keeps_connection_open(app_state):
    # An unknown strategy name must not raise (and so must not cause the outer
    # handler to discard the client): the connection stays open and a
    # subsequent valid message is still processed.
    app_state.strategies["a"] = Strategy(name="a", direction="bull_put", conditions=[])
    ws = FakeWS(["strategy_arm:nonexistent", "strategy_arm:a"])

    await websocket_endpoint(ws, None, app_state, _noop_broadcast)

    # The later message only reached the handler if the client survived the
    # unknown-name arm message (guarded branch) instead of dropping on KeyError.
    assert app_state.strategies["a"].armed is True


@pytest.mark.asyncio
async def test_strategy_disarm_clears_armed(app_state):
    app_state.strategies["a"] = Strategy(name="a", direction="bull_put", conditions=[],
                                         armed=True)
    ws = FakeWS(["strategy_disarm:a"])

    await websocket_endpoint(ws, None, app_state, _noop_broadcast)

    assert app_state.strategies["a"].armed is False


@pytest.mark.asyncio
async def test_strategy_kill_switch_enables(app_state):
    ws = FakeWS(["strategy_kill_switch:true"])

    await websocket_endpoint(ws, None, app_state, _noop_broadcast)

    assert app_state.auto_trade_kill_switch is True


@pytest.mark.asyncio
async def test_strategy_kill_switch_toggles_off(app_state):
    app_state.auto_trade_kill_switch = True
    ws = FakeWS(["strategy_kill_switch:false"])

    await websocket_endpoint(ws, None, app_state, _noop_broadcast)

    assert app_state.auto_trade_kill_switch is False


@pytest.mark.asyncio
async def test_strategy_delete_removes_from_state_and_store(app_state, tmp_path, monkeypatch):
    p = tmp_path / "strategies.json"
    monkeypatch.setattr(store, "STRATEGIES_PATH", p)
    app_state.strategies["a"] = Strategy(name="a", direction="bull_put", conditions=[])
    store.save_strategy(None, app_state.strategies["a"])
    ws = FakeWS(["strategy_delete:a"])

    await websocket_endpoint(ws, None, app_state, _noop_broadcast)

    assert "a" not in app_state.strategies
    assert "a" not in store.load_strategies()


@pytest.mark.asyncio
async def test_strategy_delete_handles_quoted_name(app_state, tmp_path, monkeypatch):
    """Regression: a JSON-stringifying client sends strategy_delete:\"a\"; the
    quoted name must still delete the strategy so it does NOT return on refresh."""
    p = tmp_path / "strategies.json"
    monkeypatch.setattr(store, "STRATEGIES_PATH", p)
    app_state.strategies["a"] = Strategy(name="a", direction="bull_put", conditions=[])
    store.save_strategy(None, app_state.strategies["a"])
    ws = FakeWS(['strategy_delete:"a"'])   # quoted name, as the old client sent

    await websocket_endpoint(ws, None, app_state, _noop_broadcast)

    assert "a" not in app_state.strategies
    assert "a" not in store.load_strategies()


@pytest.mark.asyncio
async def test_strategy_arm_handles_quoted_name(app_state):
    app_state.strategies["a"] = Strategy(name="a", direction="bull_put", conditions=[])
    ws = FakeWS(['strategy_arm:"a"'])   # quoted name, as the old client sent

    await websocket_endpoint(ws, None, app_state, _noop_broadcast)

    assert app_state.strategies["a"].armed is True


@pytest.mark.asyncio
async def test_status_push_loop_broadcasts_vix_update(monkeypatch):
    state = create_app_state()
    state.vix = 18.5
    messages = []

    async def broadcast_fn(message):
        messages.append(message)

    calls = {"n": 0}

    async def sleep_once_then_cancel(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] > 1:
            raise asyncio.CancelledError()

    monkeypatch.setattr(ws_handler.asyncio, "sleep", sleep_once_then_cancel)

    await status_push_loop(state, broadcast_fn)

    vix_msgs = [m for m in messages if m["type"] == "vix_update"]
    assert len(vix_msgs) >= 1
    assert vix_msgs[0]["data"]["vix"] == 18.5


@pytest.mark.asyncio
async def test_strategy_arm_resets_runtime(app_state):
    from strategy_engine import get_runtime
    app_state.strategies["a"] = Strategy(name="a", direction="bull_put", conditions=[])
    rt = get_runtime(app_state, "a")
    rt.entered = True
    rt.done = True
    ws = FakeWS(["strategy_arm:a"])

    await websocket_endpoint(ws, None, app_state, _noop_broadcast)

    assert app_state.strategies["a"].armed is True
    assert rt.entered is False      # re-arm resets the one-shot latch
    assert rt.trade is None


@pytest.mark.asyncio
async def test_strategy_arm_sends_strategy_list(app_state, tmp_path, monkeypatch):
    """Ack the arm back to the client so the button/status repaint (the missing
    feedback loop that made clicking Arm look like a no-op)."""
    monkeypatch.setattr(store, "STRATEGIES_PATH", tmp_path / "strategies.json")
    app_state.strategies["a"] = Strategy(name="a", direction="bull_put", conditions=[])
    ws = FakeWS(["strategy_arm:a"])

    await websocket_endpoint(ws, None, app_state, _noop_broadcast)

    assert app_state.strategies["a"].armed is True
    lists = ws.sent_by_type("strategy_list")
    assert len(lists) == 1
    assert lists[0]["data"]["strategies"][0]["armed"] is True


@pytest.mark.asyncio
async def test_strategy_disarm_sends_strategy_list(app_state, tmp_path, monkeypatch):
    """Ack the disarm back so the button returns to "Arm"."""
    monkeypatch.setattr(store, "STRATEGIES_PATH", tmp_path / "strategies.json")
    app_state.strategies["a"] = Strategy(name="a", direction="bull_put", conditions=[], armed=True)
    ws = FakeWS(["strategy_disarm:a"])

    await websocket_endpoint(ws, None, app_state, _noop_broadcast)

    assert app_state.strategies["a"].armed is False
    lists = ws.sent_by_type("strategy_list")
    assert len(lists) == 1
    assert lists[0]["data"]["strategies"][0]["armed"] is False
