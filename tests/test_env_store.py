# tests/test_env_store.py
from env_store import read_env, update_env


def test_update_creates_file(tmp_path):
    p = tmp_path / ".env"
    update_env({"DISCORD_TOKEN": "abc"}, path=p)
    assert p.read_text(encoding="utf-8") == "DISCORD_TOKEN=abc\n"


def test_roundtrip_preserves_other_lines(tmp_path):
    p = tmp_path / ".env"
    p.write_text(
        "# my settings\n"
        "IB_PORT=7497\n"
        "DISCORD_TOKEN=old\n"
        "\n"
        "OTHER=keep\n",
        encoding="utf-8",
    )
    update_env({"DISCORD_TOKEN": "new", "IB_PORT": "4002"}, path=p)
    text = p.read_text(encoding="utf-8")
    assert "# my settings\n" in text
    assert "OTHER=keep\n" in text
    assert "DISCORD_TOKEN=new" in text
    assert "IB_PORT=4002" in text
    assert "old" not in text


def test_update_appends_missing_keys(tmp_path):
    p = tmp_path / ".env"
    p.write_text("A=1\n", encoding="utf-8")
    update_env({"B": "2"}, path=p)
    assert p.read_text(encoding="utf-8") == "A=1\nB=2\n"


def test_empty_value_written_as_bare_key(tmp_path):
    p = tmp_path / ".env"
    update_env({"DISCORD_TOKEN": ""}, path=p)
    assert p.read_text(encoding="utf-8") == "DISCORD_TOKEN=\n"
    assert read_env(p) == {"DISCORD_TOKEN": ""}


def test_read_strips_quotes_and_ignores_junk(tmp_path):
    p = tmp_path / ".env"
    p.write_text(
        "# comment\n"
        "A=\"quoted\"\n"
        "B='single'\n"
        "noequals\n"
        "\n"
        "C=plain\n",
        encoding="utf-8",
    )
    assert read_env(p) == {"A": "quoted", "B": "single", "C": "plain"}


def test_read_missing_file(tmp_path):
    assert read_env(tmp_path / "nope.env") == {}


def test_atomic_write_leaves_no_temp_file(tmp_path):
    p = tmp_path / ".env"
    update_env({"K": "v"}, path=p)
    assert list(tmp_path.iterdir()) == [p]
