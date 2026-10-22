from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import install
from tests.helpers import ROOT

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


def test_query_log_setting_is_copied_into_the_entry(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PG_MCP_QUERY_LOG", str(tmp_path / "queries.jsonl"))
    assert install.server_entry()["env"]["PG_MCP_QUERY_LOG"] == str(tmp_path / "queries.jsonl")


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


def test_language_goes_into_the_server_env() -> None:
    assert install.server_entry(language="ru")["env"]["PG_MCP_LANG"] == "ru"


def test_check_speaks_russian_when_asked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, unreachable_env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    use_env(monkeypatch, unreachable_env, tmp_path)
    assert install.main(["--check", "--lang", "ru"]) == 1
    output = capsys.readouterr().out
    assert "протокол 2025-06-18" in output
    assert "база: недоступна" in output


def test_language_comes_from_the_locale_and_is_written_into_the_entry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, unreachable_env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    use_env(monkeypatch, unreachable_env, tmp_path)
    for name in ("PG_MCP_LANG", "LANGUAGE", "LC_ALL", "LC_MESSAGES"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LANG", "ru_RU.UTF-8")
    assert install.main(["--client", "qwen"]) == 1
    settings = read(tmp_path / ".qwen" / "settings.json")
    assert isinstance(settings, dict)
    assert settings["mcpServers"]["pg"]["env"]["PG_MCP_LANG"] == "ru"
    output = capsys.readouterr().out
    assert "MCP-сервер 'pg' зарегистрирован" in output
    assert "дальше: qwen extensions link" in output


@pytest.mark.parametrize(
    "line",
    [
        "database: connected to shop as agent, PostgreSQL 16.4",
        "база: подключено к shop под ролью agent, PostgreSQL 16.4",
    ],
)
def test_a_connection_is_recognised_in_either_language(line: str) -> None:
    assert install.connected(f"pg-mcp 0.3.0 on Python 3.9.6\n{line}\nlimits: read only = on")


def test_an_unavailable_database_is_not_a_connection() -> None:
    assert not install.connected("database: unavailable. Nothing accepts connections at db:5432.")


@pytest.mark.parametrize("codepage", ["cp1252", "cp866"])
def test_a_code_page_without_cyrillic_or_dashes_does_not_crash_the_output(
    codepage: str, unreachable_env: dict[str, str]
) -> None:
    # An agent reads install.py through a pipe, where Python uses the system code page: cp1252 on
    # English Windows has no Cyrillic, and cp866 has no dash, which the profile warning contains.
    env = dict(unreachable_env, PYTHONIOENCODING=codepage, PG_MCP_PROFILE="admin")
    proc = subprocess.run(
        [sys.executable, str(ROOT / "install.py"), "--check", "--lang", "ru"],
        capture_output=True,
        env=env,
        timeout=60,
        check=False,
    )
    output = proc.stdout.decode(codepage) + proc.stderr.decode(codepage, "replace")
    assert "Traceback" not in output
    assert proc.returncode == 1  # the database is unreachable, and the script says so
    assert "PG_MCP_PROFILE='admin'" in output


def test_old_python_is_refused_in_the_chosen_language(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "version_info", (3, 8, 18))
    with pytest.raises(SystemExit, match=r"pg-mcp нужен Python 3\.9 или новее"):
        install.main(["--lang", "ru", "--check"])
