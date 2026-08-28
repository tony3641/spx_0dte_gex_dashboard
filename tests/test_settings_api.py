import asyncio
import json
from types import SimpleNamespace

import discord
import pytest
from starlette.requests import Request

import server
from discord_settings import DiscordSettings, DiscordSettingsManager


def _req(host="127.0.0.1"):
    return Request({"type": "http", "client": (host, 5000)})


async def _ready():
    return True


class FakeBot:
    def __init__(self, error=None, ready_hangs=False):
        self.alert_bridge = {"bridge": True}
        self.closed = False
        self._error = error
        if ready_hangs:
            self.bot = SimpleNamespace(wait_until_ready=lambda: asyncio.Event().wait())
        else:
            self.bot = SimpleNamespace(wait_until_ready=_ready)

    async def start(self):
        if self._error is not None:
            raise self._error
        await asyncio.Event().wait()

    async def close(self):
        self.closed = True


def _manager(monkeypatch, persist_calls, bot_error=None):
    state = SimpleNamespace(alert_bridge=None, background_tasks=[])
    m = DiscordSettingsManager(
        None, state,
        bot_factory=lambda ib, st, **kw: FakeBot(bot_error),
        persist=lambda u: persist_calls.append(dict(u)))
    monkeypatch.setattr(server, "discord_manager", m)
    # Guard the .env writer: the IB settings endpoint calls server.update_env
    # directly, so route it into the same recorder — no test may touch the
    # real .env file.
    monkeypatch.setattr(server, "update_env",
                        lambda u, path=None: persist_calls.append(dict(u)))
    return m


def _run(coro):
    return asyncio.run(coro)


# ---- localhost guard -------------------------------------------------------
def test_settings_403_for_remote(monkeypatch):
    _manager(monkeypatch, [])
    for call in (
        lambda: server.get_discord_settings(_req("10.0.0.9")),
        lambda: server.post_discord_settings(server.DiscordSettingsIn(token="x"), _req("10.0.0.9")),
        lambda: server.get_ib_settings(_req("10.0.0.9")),
        lambda: server.post_ib_settings(server.IbSettingsIn(port=7497), _req("10.0.0.9")),
    ):
        with pytest.raises(server.HTTPException) as ei:
            _run(call())
        assert ei.value.status_code == 403


# ---- discord endpoints -----------------------------------------------------
def test_get_discord_masks_token(monkeypatch):
    calls = []
    m = _manager(monkeypatch, calls)
    m.settings = DiscordSettings(token="abcdwxyz", guild_id="1", channel_id="2",
                                 allowed_user_ids=[5], allowed_role="trader")
    m.bot = object()
    resp = _run(server.get_discord_settings(_req()))
    assert resp["token_set"] is True
    assert resp["token_hint"] == "…wxyz"
    assert resp["running"] is True
    assert "abcdwxyz" not in json.dumps(resp)


def test_get_discord_no_token(monkeypatch):
    m = _manager(monkeypatch, [])
    m.settings = DiscordSettings()
    resp = _run(server.get_discord_settings(_req()))
    assert resp["token_set"] is False
    assert resp["token_hint"] is None
    assert resp["running"] is False


def test_post_sets_token_and_persists(monkeypatch):
    calls = []
    m = _manager(monkeypatch, calls)
    resp = _run(server.post_discord_settings(server.DiscordSettingsIn(
        token="newtok", guild_id="111", channel_id="222",
        allowed_user_ids="1,2", allowed_role="trader"), _req()))
    assert resp == {"ok": True, "running": True, "persisted": True}
    assert m.settings.token == "newtok"
    assert m.settings.allowed_user_ids == [1, 2]
    assert calls[-1]["DISCORD_TOKEN"] == "newtok"


def test_post_without_token_keeps_existing(monkeypatch):
    calls = []
    m = _manager(monkeypatch, calls)
    m.settings = DiscordSettings(token="oldtok")
    _run(server.post_discord_settings(server.DiscordSettingsIn(guild_id="9"), _req()))
    assert m.settings.token == "oldtok"
    assert calls[-1]["DISCORD_TOKEN"] == "oldtok"


def test_post_empty_token_clears_and_stops(monkeypatch):
    calls = []
    m = _manager(monkeypatch, calls)
    _run(server.post_discord_settings(server.DiscordSettingsIn(token="t"), _req()))
    assert m.running is True
    resp = _run(server.post_discord_settings(server.DiscordSettingsIn(token=""), _req()))
    assert resp["running"] is False
    assert m.running is False
    assert calls[-1]["DISCORD_TOKEN"] == ""


def test_post_invalid_user_ids_400(monkeypatch):
    _manager(monkeypatch, [])
    with pytest.raises(server.HTTPException) as ei:
        _run(server.post_discord_settings(
            server.DiscordSettingsIn(token="t", allowed_user_ids="abc"), _req()))
    assert ei.value.status_code == 400


def test_post_bad_token_400_no_persist_old_kept(monkeypatch):
    calls = []
    state = SimpleNamespace(alert_bridge=None, background_tasks=[])
    m = DiscordSettingsManager(
        None, state,
        bot_factory=lambda ib, st, **kw: FakeBot(discord.LoginFailure()),
        persist=lambda u: calls.append(dict(u)))
    monkeypatch.setattr(server, "discord_manager", m)
    monkeypatch.setattr(server, "update_env",
                        lambda u, path=None: calls.append(dict(u)))
    initial_token = m.settings.token
    with pytest.raises(server.HTTPException) as ei:
        _run(server.post_discord_settings(server.DiscordSettingsIn(token="bad"), _req()))
    assert ei.value.status_code == 400
    assert "rejected" in ei.value.detail
    assert calls == []
    assert m.settings.token == initial_token   # unchanged


# ---- ib endpoints ----------------------------------------------------------
@pytest.fixture
def ib_state(monkeypatch):
    st = SimpleNamespace(ib_port=7497, connected=True)
    monkeypatch.setattr(server, "state", st)
    return st


def test_get_ib_settings(monkeypatch, ib_state):
    _manager(monkeypatch, [])
    assert _run(server.get_ib_settings(_req())) == {"port": 7497, "connected": True}


def test_post_ib_persists_port(monkeypatch, ib_state):
    calls = []
    _manager(monkeypatch, calls)
    reconnected = []

    async def fake_reconnect(port):
        reconnected.append(port)

    monkeypatch.setattr(server, "reconnect_ib_on", fake_reconnect)
    resp = _run(server.post_ib_settings(server.IbSettingsIn(port=4002), _req()))
    assert resp == {"ok": True, "port": 4002, "persisted": True}
    assert reconnected == [4002]
    assert calls[-1] == {"IB_PORT": "4002"}


def test_post_ib_invalid_port(monkeypatch, ib_state):
    _manager(monkeypatch, [])
    with pytest.raises(server.HTTPException) as ei:
        _run(server.post_ib_settings(server.IbSettingsIn(port=70000), _req()))
    assert ei.value.status_code == 400


def test_post_ib_reconnect_failure_no_persist(monkeypatch, ib_state):
    calls = []
    _manager(monkeypatch, calls)

    async def boom(port):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(server, "reconnect_ib_on", boom)
    with pytest.raises(server.HTTPException) as ei:
        _run(server.post_ib_settings(server.IbSettingsIn(port=4002), _req()))
    assert ei.value.status_code == 400
    assert all("IB_PORT" not in c for c in calls)
