import importlib
import config
import pytest


@pytest.fixture
def fresh_config(monkeypatch):
    for k in ("DISCORD_TOKEN", "DISCORD_GUILD_ID", "DISCORD_CHANNEL_ID",
              "DISCORD_ALLOWED_USER_IDS", "DISCORD_ALLOWED_ROLE"):
        monkeypatch.delenv(k, raising=False)
    importlib.reload(config)
    return config


def test_defaults_disabled(fresh_config):
    assert fresh_config.DISCORD_ENABLED is False
    assert fresh_config.DISCORD_TOKEN == ""
    assert fresh_config.DISCORD_GUILD_ID == ""
    assert fresh_config.DISCORD_CHANNEL_ID == ""
    assert fresh_config.DISCORD_ALLOWED_USER_IDS == []
    assert fresh_config.DISCORD_ALLOWED_ROLE == ""


def test_token_env_enables(fresh_config, monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "abc.def.ghi")
    importlib.reload(config)
    assert config.DISCORD_ENABLED is True
    assert config.DISCORD_TOKEN == "abc.def.ghi"


def test_user_ids_env_csv(fresh_config, monkeypatch):
    monkeypatch.setenv("DISCORD_ALLOWED_USER_IDS", "123,456")
    importlib.reload(config)
    assert config.DISCORD_ALLOWED_USER_IDS == [123, 456]
