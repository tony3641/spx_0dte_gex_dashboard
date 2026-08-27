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
