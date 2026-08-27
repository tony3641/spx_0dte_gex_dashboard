# tests/test_discord_settings.py
import asyncio
from types import SimpleNamespace

import discord
import pytest

import discord_settings
from discord_settings import DiscordSettings, DiscordSettingsManager


async def _ready():
    return True


class FakeBot:
    def __init__(self, error=None, ready_hangs=False):
        self.alert_bridge = {"bridge": True}
        self.closed = False
        self.kw = {}
        self._error = error
        if ready_hangs:
            self.bot = SimpleNamespace(wait_until_ready=lambda: asyncio.Event().wait())
        else:
            self.bot = SimpleNamespace(wait_until_ready=_ready)

    async def start(self):
        if self._error is not None:
            raise self._error
        await asyncio.Event().wait()   # runs until cancelled (like the real bot)

    async def close(self):
        self.closed = True


def _manager_seq(sequence, persist_calls=None):
    """Manager whose factory yields bots per `sequence` entries:
    "ok" = ready, "hang" = never ready, else the exception to raise on start.
    The last entry repeats once exhausted."""
    made = []

    def factory(ib, state, **kw):
        entry = sequence[min(len(made), len(sequence) - 1)]
        error = None if entry in ("ok", "hang") else entry
        b = FakeBot(error=error, ready_hangs=(entry == "hang"))
        b.kw = kw
        made.append(b)
        return b

    calls = [] if persist_calls is None else persist_calls
    state = SimpleNamespace(alert_bridge=None, background_tasks=[])
    m = DiscordSettingsManager(None, state, bot_factory=factory,
                               persist=lambda u: calls.append(dict(u)))
    return m, made, calls, state


def test_apply_success_swaps_bridge_and_persists():
    m, made, calls, state = _manager_seq(["ok", "ok"])
    s1 = DiscordSettings(token="tok1", guild_id="1", channel_id="2",
                         allowed_user_ids=[7])
    assert asyncio.run(m.apply(s1)) == {"ok": True, "running": True, "persisted": True}
    assert state.alert_bridge == {"bridge": True}
    assert m.running is True
    assert m._task not in state.background_tasks

    s2 = DiscordSettings(token="tok2")
    assert asyncio.run(m.apply(s2))["ok"] is True
    assert made[0].closed is True          # old bot replaced
    assert made[1].closed is False
    assert calls[-1] == {"DISCORD_TOKEN": "tok2", "DISCORD_GUILD_ID": "",
                         "DISCORD_CHANNEL_ID": "", "DISCORD_ALLOWED_USER_IDS": "",
                         "DISCORD_ALLOWED_ROLE": ""}
    asyncio.run(m.stop())


def test_apply_passes_settings_to_factory():
    m, made, calls, state = _manager_seq(["ok"])
    asyncio.run(m.apply(DiscordSettings(token="t", guild_id="5", channel_id="6",
                                        allowed_user_ids=[1, 2], allowed_role="r")))
    kw = made[0].kw
    assert kw["token"] == "t"
    assert kw["guild_id"] == "5"
    assert kw["channel_id"] == "6"
    assert kw["allowed_user_ids"] == [1, 2]
    assert kw["allowed_role"] == "r"
    asyncio.run(m.stop())


def test_apply_passes_cleared_channel_through_and_guild_none():
    m, made, calls, state = _manager_seq(["ok"])
    asyncio.run(m.apply(DiscordSettings(token="t", channel_id="")))
    kw = made[0].kw
    assert kw["channel_id"] == ""       # cleared channel passes through, not None
    assert kw["guild_id"] is None       # empty guild still maps to None
    asyncio.run(m.stop())


def test_apply_bad_token_rolls_back():
    m, made, calls, state = _manager_seq(["ok", discord.LoginFailure()])
    assert asyncio.run(m.apply(DiscordSettings(token="good")))["ok"] is True
    result = asyncio.run(m.apply(DiscordSettings(token="bad")))
    assert result["ok"] is False
    assert result["error"] == "Discord rejected the token."
    assert m.bot is made[0] and made[0].closed is False   # old bot untouched
    assert made[1].closed is True                          # new bot torn down
    assert state.alert_bridge == {"bridge": True}          # still the old bridge
    assert len(calls) == 1                                 # nothing persisted
    asyncio.run(m.stop())


def test_apply_timeout_rolls_back(monkeypatch):
    monkeypatch.setattr(discord_settings, "READY_TIMEOUT_S", 0.05)
    m, made, calls, state = _manager_seq(["ok", "hang"])
    assert asyncio.run(m.apply(DiscordSettings(token="good")))["ok"] is True
    result = asyncio.run(m.apply(DiscordSettings(token="slow")))
    assert result["ok"] is False
    assert "ready" in result["error"]
    assert m.bot is made[0]
    assert len(calls) == 1
    asyncio.run(m.stop())


def test_empty_token_stops_bot_and_persists():
    m, made, calls, state = _manager_seq(["ok"])
    assert asyncio.run(m.apply(DiscordSettings(token="t")))["running"] is True
    result = asyncio.run(m.apply(DiscordSettings(token="")))
    assert result == {"ok": True, "running": False, "persisted": True}
    assert m.running is False
    assert made[0].closed is True
    assert state.alert_bridge is None
    assert calls[-1]["DISCORD_TOKEN"] == ""


def test_stop_is_idempotent():
    m, made, calls, state = _manager_seq([])
    asyncio.run(m.stop())
    asyncio.run(m.stop())
    assert m.running is False
    assert state.alert_bridge is None
