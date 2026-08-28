"""Tests for the Discord position-liquidate interaction.

The /positions command sends a View with one Liquidate button per liquidatable
position. Clicking calls ``DiscordBot._liquidate_poskey``, which builds the same
close-order payload as the web UI and runs it through ``handle_place_order``.
These tests exercise the bot methods with real state and a stubbed order call.
"""
import asyncio

import order_manager
from app_state import create_app_state
from discord_bot import make_discord_bot


def _bot(state):
    return make_discord_bot(None, state, allowed_user_ids=[1], allowed_role="")


def _state():
    state = create_app_state()
    state.positions = [
        {"contract": {"symbol": "SPX", "localSymbol": "SPXW", "secType": "OPT",
                      "expiry": "20260626", "strike": 7700.0, "right": "C"},
         "position": -4, "averageCost": 1.85, "unrealizedPNL": -40.0,
         "marketPrice": 1.75},
        {"contract": {"symbol": "QCOM", "localSymbol": "QCOM", "secType": "STK",
                      "expiry": "", "strike": None, "right": ""},
         "position": -100, "averageCost": 175.2, "unrealizedPNL": 320.0,
         "marketPrice": 178.4},
    ]
    return state


def _std_status():
    return {"type": "order_status", "data": {"status": "Submitted"}}


def test_liquidate_poskey_places_close_order(monkeypatch):
    state = _state()
    bot = _bot(state)
    calls = []

    async def fake_handle(ib, state_, payload, ws=None, refresh_fn=None):
        calls.append(payload)
        return _std_status()

    monkeypatch.setattr(order_manager, "handle_place_order", fake_handle)

    ok, msg = asyncio.run(bot._liquidate_poskey("SPX-OPT-20260626-7700-C"))
    assert ok is True
    assert "submitted" in msg.lower()
    assert len(calls) == 1
    payload = calls[0]
    assert payload["legs"][0]["action"] == "BUY"      # short position -> buy back
    assert payload["legs"][0]["qty"] == 4
    assert payload["legs"][0]["secType"] == "OPT"
    assert payload["outsideRth"] is True and payload["dynamicFill"] is True


def test_liquidate_poskey_rejects_unknown_position(monkeypatch):
    bot = _bot(_state())
    calls = []

    async def fake_handle(ib, state_, payload, ws=None, refresh_fn=None):
        calls.append(payload)
        return _std_status()

    monkeypatch.setattr(order_manager, "handle_place_order", fake_handle)
    ok, msg = asyncio.run(bot._liquidate_poskey("SPX-OPT-20260626-9999-C"))
    assert ok is False
    assert "not found" in msg.lower()
    assert calls == []


def test_liquidate_poskey_dedupes_duplicate_click(monkeypatch):
    state = _state()
    bot = _bot(state)
    calls = []

    async def fake_handle(ib, state_, payload, ws=None, refresh_fn=None):
        calls.append(payload)
        return _std_status()

    monkeypatch.setattr(order_manager, "handle_place_order", fake_handle)
    key = "SPX-OPT-20260626-7700-C"
    asyncio.run(bot._liquidate_poskey(key))
    ok, msg = asyncio.run(bot._liquidate_poskey(key))
    assert ok is False
    assert "already" in msg.lower()
    assert len(calls) == 1                        # order NOT sent a second time


def test_positions_view_builds_button_per_liquidatable():
    state = _state()
    bot = _bot(state)
    view = bot._build_positions_view({"positions": state.positions})
    assert view is not None
    assert len(view.children) == 2                # both SPX OPT and QCOM STK
    # First button's custom_id encodes the SPX position key (Discord-safe chars)
    assert view.children[0].custom_id == "liquidate-SPX-OPT-20260626-7700-C"
    # Label carries the row index so it maps to the table's # column
    assert view.children[0].label == "Liquidate 1"


def test_positions_view_empty_returns_none():
    bot = _bot(_state())
    assert bot._build_positions_view({"positions": []}) is None


# ---------------------------------------------------------------------------
# /strategy list -> Arm buttons (same index pattern as positions)
# ---------------------------------------------------------------------------
def _strat_state():
    from strategy_models import Strategy, Condition
    state = create_app_state()
    state.strategies["Main"] = Strategy(
        name="Main", direction="bull_put",
        conditions=[Condition(kind="short_delta", params={"min": 0.1, "max": 0.4})])
    state.strategies["Aux"] = Strategy(
        name="Aux", direction="bear_call",
        conditions=[Condition(kind="credit", params={"min": 0.2})])
    return state


def _strat_list_result(state):
    return {"ok": True,
            "strategies": [s.to_dict() for s in state.strategies.values()],
            "kill_switch": False}


def test_strategy_list_builds_arm_buttons():
    state = _strat_state()
    bot = _bot(state)
    view = bot._build_view("strategy", _strat_list_result(state))
    assert view is not None
    assert len(view.children) == 2
    assert view.children[0].label == "Arm 1"
    assert view.children[1].label == "Arm 2"
    assert view.children[0].custom_id.startswith("arm-")


def test_strategy_detail_has_no_arm_buttons():
    state = _strat_state()
    bot = _bot(state)
    detail = {"ok": True, "name": "Main", "strategy": state.strategies["Main"].to_dict()}
    assert bot._build_view("strategy", detail) is None


def test_strategy_list_table_has_index_column():
    from discord_bot import strategies_view, _fmt_strategy
    state = _strat_state()
    out = _fmt_strategy(strategies_view(state))
    assert "#" in out
    assert "Main" in out and "Aux" in out


class _FakeUser:
    def __init__(self, uid):
        self.id = uid
        self.roles = []


class _FakeResponse:
    def __init__(self):
        self.sent = []

    async def send_message(self, content=None, **kwargs):
        self.sent.append(content)

    async def defer(self, **kwargs):
        pass


class _FakeFollowup:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, **kwargs):
        self.sent.append(content)


class _FakeInteraction:
    def __init__(self, uid=1):
        self.user = _FakeUser(uid)
        self.response = _FakeResponse()
        self.followup = _FakeFollowup()


def test_handle_arm_arms_strategy_and_responds():
    state = _strat_state()
    bot = _bot(state)
    inter = _FakeInteraction(uid=1)
    asyncio.run(bot._handle_arm(inter, "Main"))
    assert state.strategies["Main"].armed is True
    assert state.strategies["Aux"].armed is False
    assert inter.followup.sent, "expected a follow-up message"
