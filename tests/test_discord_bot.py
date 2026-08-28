from app_state import create_app_state
from discord_bot import make_discord_bot, AlertBridge


def test_make_discord_bot_builds_bot_and_bridge():
    state = create_app_state()
    bot = make_discord_bot(None, state)
    assert bot.bot is not None
    assert isinstance(bot.alert_bridge, AlertBridge)
    # The registered slash command names we intend to expose:
    for name in ("status", "account", "positions", "orders", "strategy",
                 "candidates", "arm", "disarm", "killswitch", "place"):
        assert name in bot.command_names


def test_alert_bridge_wired_to_channel():
    state = create_app_state()
    bot = make_discord_bot(None, state, channel_id="987")
    assert bot.alert_bridge.channel_id == "987"


import config
from discord.app_commands import CommandNotFound
from discord_bot import account_view


def test_allowlist_defaults_to_config():
    state = create_app_state()
    bot = make_discord_bot(None, state)
    assert bot.allowed_user_ids == config.DISCORD_ALLOWED_USER_IDS
    assert bot.allowed_role == config.DISCORD_ALLOWED_ROLE


def test_allowlist_overridable_per_instance():
    state = create_app_state()
    bot = make_discord_bot(None, state, allowed_user_ids=[42], allowed_role="trader")
    assert bot.allowed_user_ids == [42]
    assert bot.allowed_role == "trader"


# --- interaction responder regression tests --------------------------------
# Discord times out an interaction with "The application did not respond" when
# the callback raises before posting any response. These tests pin down that
# error paths ALWAYS reach the responder so the interaction is never left open.


class _MockResponse:
    def __init__(self):
        self.sent = []

    async def send_message(self, content=None, **kwargs):
        self.sent.append({"content": content, "kwargs": kwargs})


class _MockInteraction:
    def __init__(self, *, user_id, command=None, data=None, guild_id=None):
        class User:
            def __init__(self, uid):
                self.id = uid
                self.roles = []
        self.user = User(user_id)
        self.response = _MockResponse()
        self.command = command
        self.data = data or {}
        self.guild_id = guild_id


def test_respond_handles_command_none_without_crash():
    """interaction.command can be None before the callback runs; _respond must fall
    back to data['name'] instead of raising AttributeError (a silent timeout)."""
    import asyncio
    state = create_app_state()
    bot = make_discord_bot(None, state,
                           allowed_user_ids=[config.DISCORD_ALLOWED_USER_IDS[0] or 1])
    inter = _MockInteraction(user_id=config.DISCORD_ALLOWED_USER_IDS[0] or 1,
                             command=None, data={"name": "account"}, guild_id=123)
    asyncio.run(bot._respond(inter, account_view, {}))
    assert len(inter.response.sent) == 1
    assert "embed" in inter.response.sent[0]["kwargs"]  # a response was actually sent


def test_on_tree_error_responds_to_command_not_found():
    """A CommandNotFound (guild/scope mismatch) previously went to discord.py's default
    on_error, which only logs — leaving the interaction unanswered. It must now respond."""
    import asyncio
    state = create_app_state()
    bot = make_discord_bot(None, state)
    inter = _MockInteraction(user_id=1, command=None,
                             data={"name": "account"}, guild_id=999999)
    asyncio.run(bot._on_tree_error(inter, CommandNotFound("account", [])))
    assert len(inter.response.sent) == 1
    assert "different server" in inter.response.sent[0]["content"]
