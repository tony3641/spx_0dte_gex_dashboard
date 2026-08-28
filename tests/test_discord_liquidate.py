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
