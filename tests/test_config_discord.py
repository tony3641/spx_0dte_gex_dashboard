import importlib
import config
import pytest


@pytest.fixture
def fresh_config(monkeypatch, tmp_path):
    for k in ("DISCORD_TOKEN", "DISCORD_GUILD_ID", "DISCORD_CHANNEL_ID",
              "DISCORD_ALLOWED_USER_IDS", "DISCORD_ALLOWED_ROLE"):
        monkeypatch.delenv(k, raising=False)
    # Keep reload-based tests hermetic: never read a real repo-root .env.
    monkeypatch.setenv("DOTENV_PATH", str(tmp_path / ".env"))
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


import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(config.__file__).parent


def test_parse_user_ids_public():
    assert config.parse_user_ids("1, 2;3") == [1, 2, 3]
    assert config.parse_user_ids("") == []
    assert config.parse_user_ids(None) == []
    assert config.parse_user_ids([1, "2"]) == [1, 2]


def _run_config_print(env_extra, env_remove=("DISCORD_TOKEN",)):
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
    for k in env_remove:
        env.pop(k, None)
    env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-c", "import config; print(config.DISCORD_TOKEN)"],
        capture_output=True, text=True, env=env, timeout=60,
    )


def test_dotenv_loaded_at_import(tmp_path):
    (tmp_path / ".env").write_text("DISCORD_TOKEN=fromdotenv\n", encoding="utf-8")
    out = _run_config_print({"DOTENV_PATH": str(tmp_path / ".env")})
    assert out.returncode == 0
    assert out.stdout.strip() == "fromdotenv"


def test_real_env_var_beats_dotenv(tmp_path):
    (tmp_path / ".env").write_text("DISCORD_TOKEN=fromdotenv\n", encoding="utf-8")
    out = _run_config_print({"DOTENV_PATH": str(tmp_path / ".env"),
                             "DISCORD_TOKEN": "fromenv"})
    assert out.stdout.strip() == "fromenv"
