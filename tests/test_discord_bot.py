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
