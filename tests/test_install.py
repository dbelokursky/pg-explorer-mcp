from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import install
from pg_mcp import imports
from tests.helpers import ROOT
from tests.test_imports import dbeaver_workspace, postgres

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
    assert install.ready(f"pg-mcp 0.3.0 on Python 3.9.6\n{line}\nlimits: read only = on")


def test_an_unavailable_database_is_not_a_connection() -> None:
    assert not install.ready("database: unavailable. Nothing accepts connections at db:5432.")


@pytest.mark.parametrize(
    "line",
    [
        "stands: 53 (dev1–dev50, ift1–ift3), connections in the registry: 2100",
        "стенды: 2 (dev1–dev2), подключений в реестре: 3",
    ],
)
def test_an_entry_for_stands_is_ready_once_it_finds_them(line: str) -> None:
    assert install.ready(f"pg-mcp 0.3.0 on Python 3.9.6\nserver name: pg-stands\n{line}")


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


def test_the_entry_names_the_server() -> None:
    assert install.server_entry()["env"]["PG_MCP_NAME"] == "pg"
    assert install.server_entry(name="sales")["env"]["PG_MCP_NAME"] == "pg-sales"


def test_a_further_database_registers_next_to_pg(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, unreachable_env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    use_env(monkeypatch, unreachable_env, tmp_path)
    assert install.main(["--client", "qwen"]) == 1
    monkeypatch.setenv("PGDATABASE", "sales")
    assert install.main(["--client", "qwen", "--name", "sales"]) == 1
    settings = read(tmp_path / ".qwen" / "settings.json")
    assert isinstance(settings, dict)
    servers = settings["mcpServers"]
    assert set(servers) == {"pg", "pg-sales"}
    assert (servers["pg"]["env"]["PGDATABASE"], servers["pg"]["env"]["PG_MCP_NAME"]) == ("pg_mcp_test", "pg")
    assert (servers["pg-sales"]["env"]["PGDATABASE"], servers["pg-sales"]["env"]["PG_MCP_NAME"]) == (
        "sales",
        "pg-sales",
    )
    assert "registered MCP server 'pg-sales'" in capsys.readouterr().out


def test_an_entry_for_many_stands_is_ready_without_reaching_them(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, unreachable_env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    use_env(monkeypatch, unreachable_env, tmp_path)
    stands = tmp_path / "stands.conf"
    stands.write_text(f"[dev1.orders.billing]\nhost=127.0.0.1\nport={unreachable_env['PGPORT']}\n", encoding="utf-8")
    monkeypatch.setenv("PG_MCP_STANDS", "*")
    monkeypatch.setenv("PG_MCP_STANDS_FILE", str(stands))
    monkeypatch.setenv("PGSERVICEFILE", str(tmp_path / "absent.conf"))
    assert install.main(["--client", "qwen", "--name", "stands"]) == 0
    settings = read(tmp_path / ".qwen" / "settings.json")
    assert isinstance(settings, dict)
    env = settings["mcpServers"]["pg-stands"]["env"]
    assert (env["PG_MCP_STANDS"], env["PG_MCP_STANDS_FILE"], env["PG_MCP_NAME"]) == ("*", str(stands), "pg-stands")
    assert "stands: 1 (dev1), connections in the registry: 1" in capsys.readouterr().out


@pytest.mark.parametrize("name", ["Sales", "sales_db", "-sales", "", "x" * 31])
def test_names_that_would_not_make_a_clean_entry_are_refused(name: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        install.main(["--check", "--lang", "en", f"--name={name}"])  # = keeps "-sales" from looking like an option
    assert "cannot be a server name" in capsys.readouterr().err


def dbeaver_folder(tmp_path: Path) -> Path:
    """A DBeaver workspace with two stands, written the way DBeaver writes it."""
    return dbeaver_workspace(
        tmp_path / ".dbeaver",
        {
            "pg-1": postgres("orders", "dev7-db.example", "orders", folder="Stands/dev7"),
            "pg-2": postgres("IFT2 shop", "ift2-db.example", "shop", user="shop"),
        },
        {"pg-1": {"user": "billing", "password": "s3cret"}, "pg-2": {"user": "shop", "password": "pw"}},
    )


def test_import_previews_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, unreachable_env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    use_env(monkeypatch, unreachable_env, tmp_path)
    monkeypatch.setenv("PG_MCP_STANDS_FILE", str(tmp_path / "stands.conf"))
    monkeypatch.setenv("PGPASSFILE", str(tmp_path / "pgpass"))
    assert install.main(["import", "dbeaver", "--workspace", str(dbeaver_folder(tmp_path)), "--lang", "en"]) == 0
    out = capsys.readouterr().out
    assert "2 connections go into" in out
    assert "[dev7.orders.billing]  dev7-db.example:5432" in out
    assert "This was a preview, nothing was written." in out
    assert "s3cret" not in out
    assert "pw" not in out.replace("pg-mcp", "")
    assert not (tmp_path / "stands.conf").exists()
    assert not (tmp_path / "pgpass").exists()


def test_import_write_registers_pg_stands_next_to_pg(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, unreachable_env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    use_env(monkeypatch, unreachable_env, tmp_path)
    monkeypatch.setenv("PG_MCP_STANDS_FILE", str(tmp_path / "stands.conf"))
    monkeypatch.setenv("PGPASSFILE", str(tmp_path / "pgpass"))
    assert install.main(["--client", "qwen"]) == 1  # the plain entry: its database is unreachable
    argv = [
        "import",
        "dbeaver",
        "--workspace",
        str(dbeaver_folder(tmp_path)),
        "--write",
        "--client",
        "qwen",
        "--lang",
        "en",
    ]
    assert install.main(argv) == 0  # the stands are found, which is all the check asks of a stands entry
    out = capsys.readouterr().out
    assert "stands: 2 (dev7, ift2), connections in the registry: 2" in out
    assert "s3cret" not in out
    assert (tmp_path / "pgpass").read_text(encoding="utf-8").splitlines() == [
        "dev7-db.example:5432:orders:billing:s3cret",
        "ift2-db.example:5432:shop:shop:pw",
    ]
    settings = read(tmp_path / ".qwen" / "settings.json")
    assert isinstance(settings, dict)
    servers = settings["mcpServers"]
    assert set(servers) == {"pg", "pg-stands"}
    assert servers["pg-stands"]["env"]["PG_MCP_STANDS"] == "*"
    assert "PG_MCP_STANDS" not in servers["pg"]["env"]


def test_import_pg_service_needs_a_file_and_dbeaver_a_workspace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, unreachable_env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    use_env(monkeypatch, unreachable_env, tmp_path)
    assert install.main(["import", "pg-service", "--lang", "en"]) == 2
    assert "pg-service needs the file" in capsys.readouterr().out
    assert install.main(["import", "dbeaver", "--lang", "en"]) == 2  # HOME is empty here
    assert "were not found on this machine" in capsys.readouterr().out
    assert install.main(["import", "dbeaver", "--workspace", str(tmp_path), "--lang", "en"]) == 1
    assert "cannot read" in capsys.readouterr().out
    assert (
        install.main(
            ["import", "dbeaver", "--workspace", str(dbeaver_folder(tmp_path)), "--stand-pattern", "(x", "--lang", "en"]
        )
        == 1
    )
    assert "not a regular expression" in capsys.readouterr().out


def test_a_terminal_user_is_offered_the_dbeaver_preview(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, unreachable_env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    use_env(monkeypatch, unreachable_env, tmp_path)
    monkeypatch.setenv("PG_MCP_STANDS_FILE", str(tmp_path / "stands.conf"))
    monkeypatch.setenv("PGPASSFILE", str(tmp_path / "pgpass"))
    folder = dbeaver_folder(tmp_path)
    monkeypatch.setattr(imports, "dbeaver_workspace", lambda env, home=None: folder)  # noqa: ARG005
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    answers = iter(["y", "n"])
    monkeypatch.setattr("builtins.input", lambda prompt: (sys.stdout.write(prompt), next(answers))[1])
    install.offer_import("en")
    out = capsys.readouterr().out
    assert "Show what an import would bring into pg-mcp?" in out
    assert "2 connections go into" in out
    assert "To import them, run:" in out
    assert "--write" in out
    assert not (tmp_path / "stands.conf").exists()
    install.offer_import("en")
    out = capsys.readouterr().out
    assert "2 connections go into" not in out
    assert "To import them, run:" in out
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    install.offer_import("en")
    assert capsys.readouterr().out == ""
