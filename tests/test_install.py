from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import install

ENTRY = {"command": "/usr/bin/python3", "args": ["-I", "/opt/pg-mcp/run.py"]}


@pytest.fixture
def settings_path(tmp_path: Path) -> Path:
    return tmp_path / ".gigacode" / "settings.json"


def read(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def test_creates_the_settings_file_when_there_is_none(settings_path: Path) -> None:
    install.register(str(settings_path), ENTRY)
    assert read(settings_path) == {"mcpServers": {"pg": ENTRY}}


def test_keeps_other_settings_and_servers_and_backs_up_the_old_file(settings_path: Path) -> None:
    settings_path.parent.mkdir()
    settings_path.write_text(
        json.dumps({"theme": "dark", "mcpServers": {"playwright": {"command": "npx"}}}), encoding="utf-8"
    )
    install.register(str(settings_path), ENTRY)
    settings = read(settings_path)
    assert isinstance(settings, dict)
    assert settings["theme"] == "dark"
    assert set(settings["mcpServers"]) == {"playwright", "pg"}
    assert len(list(settings_path.parent.glob("settings.json.bak-*"))) == 1


def test_refuses_to_rewrite_settings_it_cannot_parse(settings_path: Path) -> None:
    settings_path.parent.mkdir()
    original = '{\n  // comments are allowed by the agent, not by json\n  "theme": "dark"\n}\n'
    settings_path.write_text(original, encoding="utf-8")
    with pytest.raises(SystemExit, match='"pg"'):
        install.register(str(settings_path), ENTRY)
    assert settings_path.read_text(encoding="utf-8") == original


def test_entry_runs_this_interpreter_isolated_and_never_copies_the_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PGPASSWORD", "secret")
    monkeypatch.setenv("PGHOST", "db.example")
    entry = install.server_entry()
    assert entry["command"] == sys.executable
    assert entry["args"] == ["-I", install.RUN_PY]
    assert entry["env"]["PGHOST"] == "db.example"
    assert "PGPASSWORD" not in entry["env"]


def test_profile_option_goes_into_the_server_env() -> None:
    assert install.server_entry("analyst")["env"]["PG_MCP_PROFILE"] == "analyst"


def use_env(monkeypatch: pytest.MonkeyPatch, env: dict[str, str], home: Path) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))


def test_check_starts_the_server_and_reports_an_unreachable_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, unreachable_env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    use_env(monkeypatch, unreachable_env, tmp_path)
    assert install.main(["--check"]) == 1
    output = capsys.readouterr().out
    assert "protocol 2025-06-18" in output
    assert "database: unavailable" in output
    assert not (tmp_path / ".gigacode").exists()


def test_register_writes_the_entry_then_checks_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, unreachable_env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    use_env(monkeypatch, unreachable_env, tmp_path)
    assert install.main(["--client", "qwen", "--profile", "schema"]) == 1
    entry = read(tmp_path / ".qwen" / "settings.json")
    assert isinstance(entry, dict)
    assert entry["mcpServers"]["pg"]["env"]["PG_MCP_PROFILE"] == "schema"
    assert "qwen extensions link" in capsys.readouterr().out


def test_check_reports_a_server_that_does_not_start(capsys: pytest.CaptureFixture[str]) -> None:
    assert install.check({"command": sys.executable, "args": ["-c", "import sys; sys.exit(3)"]}) is False
    assert "did not answer" in capsys.readouterr().out
    assert install.check({"command": "/no/such/python", "args": []}) is False
    assert "did not run" in capsys.readouterr().out
