from __future__ import annotations

import getpass
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import install
from pg_explorer_mcp import clients, exports, i18n, imports, passwords, user_files
from pg_explorer_mcp.protocol import SUPPORTED_PROTOCOL_VERSIONS
from pg_explorer_mcp.stands import registry_files
from tests.helpers import ROOT, Memory
from tests.test_imports import data_source, datagrip_files, dbeaver_workspace, postgres, ssh_configs, ssh_properties

ENTRY = {"command": "/usr/bin/python3", "args": ["-I", "/opt/pg-explorer-mcp/run.py"]}


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
    monkeypatch.setenv("PG_EXPLORER_MCP_QUERY_LOG", str(tmp_path / "queries.jsonl"))
    assert install.server_entry()["env"]["PG_EXPLORER_MCP_QUERY_LOG"] == str(tmp_path / "queries.jsonl")


def test_profile_option_goes_into_the_server_env() -> None:
    assert install.server_entry("analyst")["env"]["PG_EXPLORER_MCP_PROFILE"] == "analyst"


def test_old_setting_names_of_the_shell_go_into_the_entry_under_the_new_ones(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PG_MCP_STATEMENT_TIMEOUT_MS", "5000")
    monkeypatch.delenv("PG_EXPLORER_MCP_STATEMENT_TIMEOUT_MS", raising=False)
    env = install.server_entry("analyst", "ru")["env"]
    assert env["PG_EXPLORER_MCP_STATEMENT_TIMEOUT_MS"] == "5000"
    assert env["PG_EXPLORER_MCP_PROFILE"] == "analyst"
    assert [name for name in env if name.startswith("PG_MCP_")] == []


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
    assert f"protocol {SUPPORTED_PROTOCOL_VERSIONS[0]}" in output
    assert "database: unavailable" in output
    assert not (tmp_path / ".gigacode").exists()


def test_register_writes_the_entry_then_checks_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, unreachable_env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    use_env(monkeypatch, unreachable_env, tmp_path)
    assert install.main(["--client", "qwen", "--profile", "schema"]) == 1
    entry = read(tmp_path / ".qwen" / "settings.json")
    assert isinstance(entry, dict)
    assert entry["mcpServers"]["pg"]["env"]["PG_EXPLORER_MCP_PROFILE"] == "schema"
    output = capsys.readouterr().out
    assert "qwen extensions link" in output
    assert "linked as pg-mcp" not in output


def test_an_extension_linked_as_pg_mcp_is_pointed_out(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, unreachable_env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    use_env(monkeypatch, unreachable_env, tmp_path)
    old = tmp_path / ".qwen" / "extensions" / "pg-mcp"
    old.mkdir(parents=True)
    install.main(["--client", "qwen"])
    assert f"{old} still holds the extension linked as pg-mcp before 0.8.0" in capsys.readouterr().out
    assert old.is_dir()  # the client's own folder: install.py only says what to delete


def test_check_reports_a_server_that_does_not_start(capsys: pytest.CaptureFixture[str]) -> None:
    assert install.check({"command": sys.executable, "args": ["-c", "import sys; sys.exit(3)"]}) is False
    assert "did not answer" in capsys.readouterr().out
    assert install.check({"command": "/no/such/python", "args": []}) is False
    assert "did not run" in capsys.readouterr().out


def test_language_goes_into_the_server_env() -> None:
    assert install.server_entry(language="ru")["env"]["PG_EXPLORER_MCP_LANG"] == "ru"


def test_check_speaks_russian_when_asked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, unreachable_env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    use_env(monkeypatch, unreachable_env, tmp_path)
    assert install.main(["--check", "--lang", "ru"]) == 1
    output = capsys.readouterr().out
    assert f"протокол {SUPPORTED_PROTOCOL_VERSIONS[0]}" in output
    assert "база: недоступна" in output


def test_language_comes_from_the_locale_and_is_written_into_the_entry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, unreachable_env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    use_env(monkeypatch, unreachable_env, tmp_path)
    for name in ("PG_EXPLORER_MCP_LANG", "LANGUAGE", "LC_ALL", "LC_MESSAGES"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LANG", "ru_RU.UTF-8")
    assert install.main(["--client", "qwen"]) == 1
    settings = read(tmp_path / ".qwen" / "settings.json")
    assert isinstance(settings, dict)
    assert settings["mcpServers"]["pg"]["env"]["PG_EXPLORER_MCP_LANG"] == "ru"
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
    assert install.ready(f"pg-explorer-mcp 0.3.0 on Python 3.9.6\n{line}\nlimits: read only = on")


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
    assert install.ready(f"pg-explorer-mcp 0.3.0 on Python 3.9.6\nserver name: pg-stands\n{line}")


@pytest.mark.parametrize("codepage", ["cp1252", "cp866"])
def test_a_code_page_without_cyrillic_or_dashes_does_not_crash_the_output(
    codepage: str, unreachable_env: dict[str, str]
) -> None:
    # An agent reads install.py through a pipe, where Python uses the system code page: cp1252 on
    # English Windows has no Cyrillic, and cp866 has no dash, which the profile warning contains.
    env = dict(unreachable_env, PYTHONIOENCODING=codepage, PG_EXPLORER_MCP_PROFILE="admin")
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
    assert "PG_EXPLORER_MCP_PROFILE='admin'" in output


def test_old_python_is_refused_in_the_chosen_language(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "version_info", (3, 8, 18))
    with pytest.raises(SystemExit, match=r"pg-explorer-mcp нужен Python 3\.9 или новее"):
        install.main(["--lang", "ru", "--check"])


def test_the_entry_names_the_server() -> None:
    assert install.server_entry()["env"]["PG_EXPLORER_MCP_NAME"] == "pg"
    assert install.server_entry(name="sales")["env"]["PG_EXPLORER_MCP_NAME"] == "pg-sales"


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
    assert (servers["pg"]["env"]["PGDATABASE"], servers["pg"]["env"]["PG_EXPLORER_MCP_NAME"]) == ("pg_mcp_test", "pg")
    assert (servers["pg-sales"]["env"]["PGDATABASE"], servers["pg-sales"]["env"]["PG_EXPLORER_MCP_NAME"]) == (
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
    monkeypatch.setenv("PG_EXPLORER_MCP_STANDS", "*")
    monkeypatch.setenv("PG_EXPLORER_MCP_STANDS_FILE", str(stands))
    monkeypatch.setenv("PGSERVICEFILE", str(tmp_path / "absent.conf"))
    assert install.main(["--client", "qwen", "--name", "stands"]) == 0
    settings = read(tmp_path / ".qwen" / "settings.json")
    assert isinstance(settings, dict)
    env = settings["mcpServers"]["pg-stands"]["env"]
    assert (env["PG_EXPLORER_MCP_STANDS"], env["PG_EXPLORER_MCP_STANDS_FILE"], env["PG_EXPLORER_MCP_NAME"]) == (
        "*",
        str(stands),
        "pg-stands",
    )
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
    monkeypatch.setenv("PG_EXPLORER_MCP_STANDS_FILE", str(tmp_path / "stands.conf"))
    monkeypatch.setenv("PGPASSFILE", str(tmp_path / "pgpass"))
    assert install.main(["import", "dbeaver", "--workspace", str(dbeaver_folder(tmp_path)), "--lang", "en"]) == 0
    out = capsys.readouterr().out
    assert "2 connections go into" in out
    assert "[dev7.orders.billing]  dev7-db.example:5432" in out
    assert "This was a preview, nothing was written." in out
    assert "s3cret" not in out
    assert "pw" not in out.replace("pg-explorer-mcp", "")
    assert not (tmp_path / "stands.conf").exists()
    assert not (tmp_path / "pgpass").exists()


def test_import_write_registers_pg_stands_next_to_pg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    unreachable_env: dict[str, str],
    own_password_store: Memory,
    capsys: pytest.CaptureFixture[str],
) -> None:
    use_env(monkeypatch, unreachable_env, tmp_path)
    monkeypatch.setenv("PG_EXPLORER_MCP_STANDS_FILE", str(tmp_path / "stands.conf"))
    monkeypatch.setenv("PGPASSFILE", str(tmp_path / "pgpass"))
    offers: list[dict[str, object]] = []
    monkeypatch.setattr(install, "offer_export", lambda **kwargs: offers.append(kwargs))
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
    assert "2 passwords are in the password store (the memory of the tests)" in out
    assert "s3cret" not in out
    assert own_password_store.text.splitlines() == [
        "dev7-db.example:5432:orders:billing:s3cret",
        "ift2-db.example:5432:shop:shop:pw",
    ]
    assert not (tmp_path / "pgpass").exists()  # the import writes no file of passwords (ADR 0012)
    settings = read(tmp_path / ".qwen" / "settings.json")
    assert isinstance(settings, dict)
    servers = settings["mcpServers"]
    assert set(servers) == {"pg", "pg-stands"}
    assert servers["pg-stands"]["env"]["PG_EXPLORER_MCP_STANDS"] == "*"
    assert "PG_EXPLORER_MCP_STANDS" not in servers["pg"]["env"]
    assert offers == [{}, {"besides": "dbeaver"}]  # the stands are in DBeaver already


def test_import_write_refuses_to_lose_the_passwords_where_there_is_no_store(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    unreachable_env: dict[str, str],
    own_password_store: Memory,
    capsys: pytest.CaptureFixture[str],
) -> None:
    use_env(monkeypatch, unreachable_env, tmp_path)
    monkeypatch.setenv("PG_EXPLORER_MCP_STANDS_FILE", str(tmp_path / "stands.conf"))
    monkeypatch.setattr(install, "offer_export", lambda **_kwargs: None)
    passwords.use(None)
    argv = ["import", "dbeaver", "--workspace", str(dbeaver_folder(tmp_path)), "--write", "--lang", "en"]
    assert install.main(argv) == 1
    assert "the passwords of the import would go nowhere: nothing was written" in capsys.readouterr().out
    assert not (tmp_path / "stands.conf").exists()
    assert own_password_store.text == ""


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


def test_import_datagrip_reads_the_ides_their_projects_and_the_project_named(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, unreachable_env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    use_env(monkeypatch, unreachable_env, tmp_path)
    monkeypatch.setattr(sys, "platform", "darwin")  # the settings of the IDEs: ~/Library/Application Support
    monkeypatch.setenv("PG_EXPLORER_MCP_STANDS_FILE", str(tmp_path / "stands.conf"))
    monkeypatch.setenv("PGPASSFILE", str(tmp_path / "pgpass"))
    assert install.main(["import", "datagrip", "--lang", "en"]) == 2  # HOME is empty here
    assert "pass --project with the folder of the project" in capsys.readouterr().out
    options = tmp_path / "Library" / "Application Support" / "JetBrains" / "IntelliJIdea2026.2" / "options"
    orders = data_source("orders on dev7", "jdbc:postgresql://dev7-db.example/orders", uuid="u1", user="billing")
    datagrip_files(options, [orders], application=True)
    shop = tmp_path / "IdeaProjects" / "shop"
    tunnelled = data_source(
        "IFT3", "jdbc:postgresql://localhost/shop", uuid="u3", user="shop", ssh=ssh_properties("c1")
    )  # its SSH configuration is one the IDE shares between projects
    datagrip_files(
        shop / ".idea", [data_source("IFT2", "jdbc:postgresql://ift2-db:6432/shop", uuid="u2", user="shop"), tunnelled]
    )
    ssh_configs(options, {"c1": "bastion.example:22"}, application=True)
    broken = tmp_path / "DataGripProjects" / "old" / ".idea"
    broken.mkdir(parents=True)
    (broken / "dataSources.xml").write_text("<project", encoding="utf-8")
    assert install.main(["import", "datagrip", "--project", str(shop), "--lang", "en"]) == 0
    out = capsys.readouterr().out
    assert f"reading {shop / '.idea'}\nreading {broken}\ncannot read {broken / 'dataSources.xml'}" in out
    assert f"reading {options}\n2 connections go into" in out  # the broken project leaves the others to read
    assert (
        "  [ift2.shop.shop]  ift2-db:6432  no saved password: put it in with "
        "`python3 install.py password --stand ift2 --schema shop`"
    ) in out
    assert "  [dev7.orders.billing]  dev7-db.example:5432  no saved password" in out
    assert "  DataGrip: IFT3  through an SSH tunnel via bastion.example:22: pg-explorer-mcp opens no tunnels" in out
    assert "This was a preview, nothing was written." in out
    assert not (tmp_path / "stands.conf").exists()


def test_a_terminal_user_is_offered_the_dbeaver_preview(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, unreachable_env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    use_env(monkeypatch, unreachable_env, tmp_path)
    monkeypatch.setenv("PG_EXPLORER_MCP_STANDS_FILE", str(tmp_path / "stands.conf"))
    monkeypatch.setenv("PGPASSFILE", str(tmp_path / "pgpass"))
    folder = dbeaver_folder(tmp_path)
    monkeypatch.setattr(imports, "dbeaver_workspace", lambda env, home=None: folder)  # noqa: ARG005
    monkeypatch.setattr(imports, "datagrip_folders", lambda env, home, project=None: [])  # noqa: ARG005
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    answers = iter(["y", "n"])
    monkeypatch.setattr("builtins.input", lambda prompt: (sys.stdout.write(prompt), next(answers))[1])
    install.offer_import("en")
    out = capsys.readouterr().out
    assert "Show what an import would bring into pg-explorer-mcp?" in out
    assert "2 connections go into" in out
    assert "To import them, run:" in out
    assert "--write" in out
    assert not (tmp_path / "stands.conf").exists()
    install.offer_import("en")
    out = capsys.readouterr().out
    assert "2 connections go into" not in out
    assert "To import them, run:" in out


def test_an_agent_is_told_the_command_that_shows_the_dbeaver_preview(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, unreachable_env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    """/pg:setup runs install.py with no terminal to answer the question; the output used to say nothing of DBeaver."""
    use_env(monkeypatch, unreachable_env, tmp_path)
    folder = dbeaver_folder(tmp_path)
    monkeypatch.setattr(imports, "dbeaver_workspace", lambda env, home=None: folder)  # noqa: ARG005
    monkeypatch.setattr(imports, "datagrip_folders", lambda env, home, project=None: [])  # noqa: ARG005
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail(f"asked {prompt!r} with no terminal"))
    install.offer_import("en")
    assert capsys.readouterr().out == (
        f"DBeaver connections found in {folder}. To see what an import would bring into pg-explorer-mcp: "
        f"{sys.executable} {install.ROOT / 'install.py'} import dbeaver --lang en\n"
    )
    monkeypatch.setattr(imports, "dbeaver_workspace", lambda env, home=None: None)  # noqa: ARG005
    install.offer_import("en")
    assert capsys.readouterr().out == ""


def test_a_terminal_user_is_asked_once_about_dbeaver_and_the_jetbrains_ides(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, unreachable_env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    """Both found make one question, not two in a row, and a yes shows what each import would bring."""
    use_env(monkeypatch, unreachable_env, tmp_path)
    monkeypatch.setenv("PG_EXPLORER_MCP_STANDS_FILE", str(tmp_path / "stands.conf"))
    monkeypatch.setenv("PGPASSFILE", str(tmp_path / "pgpass"))
    folder = dbeaver_folder(tmp_path)
    options = tmp_path / "JetBrains" / "IntelliJIdea2026.2" / "options"
    shop = data_source("IFT3", "jdbc:postgresql://ift3-db:6432/shop", uuid="u3", user="shop")
    datagrip_files(options, [shop], application=True)
    monkeypatch.setattr(imports, "dbeaver_workspace", lambda env, home=None: folder)  # noqa: ARG005
    monkeypatch.setattr(imports, "datagrip_folders", lambda env, home, project=None: [options])  # noqa: ARG005
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    answers = iter(["y", "n"])
    monkeypatch.setattr("builtins.input", lambda prompt: (sys.stdout.write(prompt), next(answers))[1])
    install.offer_import("en")
    out = capsys.readouterr().out
    assert out.startswith(
        f"DBeaver connections found in {folder}. Data sources of DataGrip or another JetBrains IDE found in "
        f"{options}. Show what an import would bring into pg-explorer-mcp? [y/N] \n"
    )
    assert out.count("[y/N]") == 1
    assert out.index("[dev7.orders.billing]  dev7-db.example:5432") < out.index(f"reading {options}")
    assert "  [ift3.shop.shop]  ift3-db:6432  no saved password" in out
    command = f"{sys.executable} {install.ROOT / 'install.py'} import"
    assert out.endswith(
        f"To import them, run:\n  {command} dbeaver --write --lang en\n  {command} datagrip --write --lang en\n"
    )
    assert not (tmp_path / "stands.conf").exists()
    monkeypatch.setattr(imports, "dbeaver_workspace", lambda env, home=None: None)  # noqa: ARG005
    install.offer_import("en")
    assert capsys.readouterr().out == (
        f"Data sources of DataGrip or another JetBrains IDE found in {options}. Show what an import would bring "
        f"into pg-explorer-mcp? [y/N] To import them, run:\n  {command} datagrip --write --lang en\n"
    )


def test_an_agent_is_told_the_command_that_shows_the_jetbrains_preview(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, unreachable_env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    """A user of DataGrip or IntelliJ IDEA hears of the import too; with DBeaver as well, each tool gets its line."""
    use_env(monkeypatch, unreachable_env, tmp_path)
    folders = [
        tmp_path / "DataGripProjects" / "stands" / ".idea",
        tmp_path / "JetBrains" / "DataGrip2026.2" / "options",
    ]
    monkeypatch.setattr(imports, "dbeaver_workspace", lambda env, home=None: None)  # noqa: ARG005
    monkeypatch.setattr(imports, "datagrip_folders", lambda env, home, project=None: folders)  # noqa: ARG005
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail(f"asked {prompt!r} with no terminal"))
    install.offer_import("en")
    command = f"{sys.executable} {install.ROOT / 'install.py'} import"
    jetbrains = (
        f"Data sources of DataGrip or another JetBrains IDE found in {folders[0]}, {folders[1]}. "
        f"To see what an import would bring into pg-explorer-mcp: {command} datagrip --lang en\n"
    )
    assert capsys.readouterr().out == jetbrains
    folder = tmp_path / ".dbeaver"
    monkeypatch.setattr(imports, "dbeaver_workspace", lambda env, home=None: folder)  # noqa: ARG005
    install.offer_import("en")
    assert capsys.readouterr().out == (
        f"DBeaver connections found in {folder}. To see what an import would bring into pg-explorer-mcp: "
        f"{command} dbeaver --lang en\n{jetbrains}"
    )


@pytest.mark.parametrize("language", i18n.LANGUAGES)
def test_setup_knows_every_line_that_names_an_import_preview(
    language: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Step 5 of /pg:setup offers /pg:import when the output has one of the phrases it quotes."""
    step = (ROOT / "commands" / "pg" / "setup.md").read_text(encoding="utf-8").split("\n5. ")[1].split("\n6. ")[0]
    phrases = [" ".join(quoted.split()) for quoted in re.findall(r'"([^"]+)"', step)]
    monkeypatch.setattr(imports, "dbeaver_workspace", lambda env, home=None: tmp_path)  # noqa: ARG005
    monkeypatch.setattr(imports, "datagrip_folders", lambda env, home, project=None: [tmp_path])  # noqa: ARG005
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    with i18n.speaking(language):
        install.offer_import(language)
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 2
    assert [line for line in lines if not any(phrase in line for phrase in phrases)] == []


def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, unreachable_env: dict[str, str]) -> None:
    """use_env, and the folders of desktop apps under the temporary home on every system."""
    use_env(monkeypatch, unreachable_env, tmp_path)
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))


def test_print_shows_the_entry_for_cursor_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, unreachable_env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    isolated(monkeypatch, tmp_path, unreachable_env)
    assert install.main(["--client", "cursor", "--print", "--lang", "en"]) == 0
    out = capsys.readouterr().out
    first, _, body = out.partition("\n")
    assert first == f"for Cursor, add this to {tmp_path / '.cursor' / 'mcp.json'}:"
    entry = json.loads(body)["mcpServers"]["pg"]
    assert entry["args"] == ["-I", install.RUN_PY]
    assert "timeout" not in entry
    assert not (tmp_path / ".cursor").exists()


def test_print_for_jetbrains_says_where_to_paste(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, unreachable_env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    isolated(monkeypatch, tmp_path, unreachable_env)
    assert install.main(["--client", "jetbrains", "--print", "--lang", "en"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("JetBrains AI Assistant keeps its servers in the IDE: Settings | Tools | AI Assistant")
    assert '"mcpServers"' in out


def test_registering_with_vscode_writes_its_own_format(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, unreachable_env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    isolated(monkeypatch, tmp_path, unreachable_env)
    assert install.main(["--client", "vscode", "--lang", "en"]) == 1  # registered; the database is unreachable
    path = clients.CLIENTS["vscode"].path(os.environ, Path.home(), "pg")
    assert path is not None
    assert path.is_relative_to(tmp_path)
    server = json.loads(path.read_text(encoding="utf-8"))["servers"]["pg"]
    assert (server["type"], server["command"]) == ("stdio", sys.executable)
    out = capsys.readouterr().out
    assert f"registered MCP server 'pg' in {path}" in out
    assert "extensions link" not in out  # only GigaCode and Qwen Code link the extension


def test_a_client_file_with_comments_gets_the_snippet_instead(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, unreachable_env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    isolated(monkeypatch, tmp_path, unreachable_env)
    settings = tmp_path / ".cursor" / "mcp.json"
    settings.parent.mkdir()
    settings.write_text('{\n  // mine\n  "mcpServers": {}\n}\n', encoding="utf-8")
    assert not install.install_into("cursor", "pg", install.server_entry())
    out = capsys.readouterr().out
    assert f"{settings} is not plain JSON" in out
    assert '"mcpServers"' in out
    assert settings.read_text(encoding="utf-8") == '{\n  // mine\n  "mcpServers": {}\n}\n'


def test_the_other_installed_clients_are_offered_at_a_terminal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, unreachable_env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    isolated(monkeypatch, tmp_path, unreachable_env)
    (tmp_path / ".cursor").mkdir()
    (tmp_path / ".lmstudio").mkdir()
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    answers = iter(["n", "y"])
    monkeypatch.setattr("builtins.input", lambda prompt: (sys.stdout.write(prompt), next(answers))[1])
    entry = install.server_entry()
    install.offer_clients("pg", entry, besides="gigacode")
    out = capsys.readouterr().out
    assert "Also found Cursor, LM Studio. Register pg there too? [y/N]" in out
    assert "--client cursor|lmstudio" in out
    assert not (tmp_path / ".cursor" / "mcp.json").exists()
    install.offer_clients("pg", entry, besides="gigacode")
    for path in (tmp_path / ".cursor" / "mcp.json", tmp_path / ".lmstudio" / "mcp.json"):
        assert set(json.loads(path.read_text(encoding="utf-8"))["mcpServers"]) == {"pg"}
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    capsys.readouterr()
    install.offer_clients("pg", entry, besides="gigacode")
    assert capsys.readouterr().out == ""


def stands_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, unreachable_env: dict[str, str]) -> Path:
    """isolated(), with two stands in the registry; the path of the pg_service.conf that is not there yet."""
    isolated(monkeypatch, tmp_path, unreachable_env)
    stands = tmp_path / "stands.conf"
    stands.write_text(
        "[dev7.orders.billing]\nhost=dev7-db.example\n\n[ift2.orders.billing]\nhost=ift2-db.example\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PG_EXPLORER_MCP_STANDS_FILE", str(stands))
    monkeypatch.setenv("PGSERVICEFILE", str(tmp_path / "pg_service.conf"))
    monkeypatch.setenv("PGPASSFILE", str(tmp_path / "pgpass"))
    return tmp_path / "pg_service.conf"


def both_stands_in_dbeaver(folder: Path) -> Path:
    return dbeaver_workspace(
        folder,
        {
            "pg-1": postgres("dev7", "dev7-db.example", "orders", user="billing"),
            "pg-2": postgres("ift2", "ift2-db.example", "orders", user="billing"),
        },
        {},
    )


def test_export_previews_then_writes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, unreachable_env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    service = stands_home(monkeypatch, tmp_path, unreachable_env)
    assert install.main(["export", "pg-service", "--lang", "en"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "psql, pgcli (pg_service.conf): 2 connections (dev7, ift2) go into:",
        f"  {service}",
        "This was a preview, nothing was written. Add --write to write the files.",
    ]
    assert not service.exists()
    assert install.main(["export", "pg-service", "--stands", "ift*", "--write", "--lang", "ru"]) == 0
    out = capsys.readouterr().out
    assert f"записан {service}" in out
    assert 'psql "service=ift2.orders.billing"' in out
    assert service.read_text(encoding="utf-8").startswith(
        "# pg-explorer-mcp export\n[ift2.orders.billing]\nhost=ift2-db.example\n"
    )


def test_export_pgpass_writes_into_the_password_file_what_the_store_has(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    unreachable_env: dict[str, str],
    own_password_store: Memory,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """psql reads no other store: --pgpass is the user's own decision to have the passwords in clear there."""
    service = stands_home(monkeypatch, tmp_path, unreachable_env)
    passfile = tmp_path / "pgpass"
    passfile.write_text("# mine\n", encoding="utf-8")
    own_password_store.text = passwords.line_of(("dev7-db.example", "5432", "orders", "billing"), "s3cret") + "\n"
    argv = ["export", "pg-service", "--write", "--pgpass", "--lang", "en"]
    assert install.main(argv) == 0
    out = capsys.readouterr().out
    assert (
        f"1 passwords are in {passfile}, in clear, for psql and pgAdmin; 1 of these connections had none in "
        "the password store."
    ) in out
    assert "записан" not in out
    [backup] = tmp_path.glob("pgpass.bak-*")
    assert f"written {passfile}; the old file is kept as {backup}" in out
    assert passfile.read_text(encoding="utf-8") == (
        "# mine\n" + passwords.line_of(("dev7-db.example", "5432", "orders", "billing"), "s3cret") + "\n"
    )
    assert backup.read_text(encoding="utf-8") == "# mine\n"
    assert service.exists()


def test_export_pgpass_leaves_the_password_file_alone_when_the_store_has_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, unreachable_env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    service = stands_home(monkeypatch, tmp_path, unreachable_env)
    argv = ["export", "pg-service", "--write", "--pgpass", "--lang", "en"]
    assert install.main(argv) == 1
    passfile = tmp_path / "pgpass"
    assert (
        f"no password of these connections is in the password store: {passfile} is left as it is (2 connections)"
    ) in capsys.readouterr().out
    assert not passfile.exists()
    assert service.exists()  # the export itself was written before that


def test_export_pgpass_needs_a_password_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, unreachable_env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    stands_home(monkeypatch, tmp_path, unreachable_env)
    passwords.use(None)
    argv = ["export", "pg-service", "--write", "--pgpass", "--lang", "en"]
    assert install.main(argv) == 1
    assert "no password store the server can use" in capsys.readouterr().out
    assert not (tmp_path / "pgpass").exists()


def test_export_says_why_it_cannot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, unreachable_env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    isolated(monkeypatch, tmp_path, unreachable_env)
    monkeypatch.setenv("PG_EXPLORER_MCP_STANDS_FILE", str(tmp_path / "stands.conf"))
    monkeypatch.setenv("PGSERVICEFILE", str(tmp_path / "pg_service.conf"))
    assert install.main(["export", "pgadmin", "--lang", "en"]) == 1
    assert "no stands to export" in capsys.readouterr().out
    stands_home(monkeypatch, tmp_path, unreachable_env)
    assert install.main(["export", "dbeaver", "--lang", "en"]) == 1
    assert "DBeaver's connections were not found" in capsys.readouterr().out
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where a folder should be", encoding="utf-8")
    assert install.main(["export", "datagrip", "--project", str(blocker / "project"), "--write", "--lang", "en"]) == 1
    assert "cannot write" in capsys.readouterr().out
    assert install.main(["export", "datagrip", "--project", "~/new-project", "--lang", "en"]) == 0
    assert f"  {tmp_path / 'new-project' / '.idea' / 'dataSources.xml'}" in capsys.readouterr().out.splitlines()


def test_a_tool_with_every_connection_gets_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, unreachable_env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    stands_home(monkeypatch, tmp_path, unreachable_env)
    folder = both_stands_in_dbeaver(tmp_path / "dbeaver")
    assert install.main(["export", "dbeaver", "--workspace", str(folder), "--write", "--lang", "en"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "left out, already in DBeaver with the same host, port, database and user: 2",
        "DBeaver has all of these connections already; nothing to write",
    ]
    assert not (folder / exports.DBEAVER_FILE).exists()


def test_the_database_tools_found_here_are_offered_the_stands(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, unreachable_env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    stands_home(monkeypatch, tmp_path, unreachable_env)
    monkeypatch.setattr(shutil, "which", lambda name: None)  # noqa: ARG005  (no psql on this machine)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    def unexpected(prompt: str) -> str:
        raise AssertionError(prompt)

    monkeypatch.setattr("builtins.input", unexpected)
    install.offer_export()  # no tools here: no question
    assert capsys.readouterr().out == ""

    app = clients.app_data(os.environ, Path.home())
    both_stands_in_dbeaver(tmp_path / ".local" / "share" / "DBeaverData" / "workspace6" / "General" / ".dbeaver")
    (tmp_path / ".vscode" / "extensions" / "ms-ossdata.vscode-pgsql-1.4.0").mkdir(parents=True)
    (app / "Code" / "User").mkdir(parents=True)
    (app / "Code" / "User" / "settings.json").write_text("{\n  // mine\n}\n", encoding="utf-8")
    (tmp_path / ".pgadmin").mkdir()
    (tmp_path / ".cursor" / "extensions" / "mtxr.sqltools-0.28.4").mkdir(parents=True)
    (app / "Cursor" / "User").mkdir(parents=True)
    answers = iter(["n", "y", "n"])
    monkeypatch.setattr("builtins.input", lambda prompt: (sys.stdout.write(prompt), next(answers))[1])
    install.offer_export()  # DBeaver has both stands, and settings with comments are not offered
    out = capsys.readouterr().out
    command = f"{sys.executable} {install.ROOT / 'install.py'} export"
    assert out.startswith("Found pgAdmin 4, VS Code: SQLTools. Add the stand connections there (2)? [y/N] ")
    assert out.endswith(f"  {command} pgadmin --write\n  {command} sqltools --editor cursor --write\n")
    assert not (tmp_path / "pgadmin-servers.json").exists()
    install.offer_export()
    out = capsys.readouterr().out
    assert "pgAdmin 4: 2 connections (dev7, ift2) go into:" in out
    assert (tmp_path / "pgadmin-servers.json").exists()
    settings = json.loads((app / "Cursor" / "User" / "settings.json").read_text(encoding="utf-8"))
    assert [entry["name"] for entry in settings["sqltools.connections"]] == [
        "dev7 / orders / billing",
        "ift2 / orders / billing",
    ]
    install.offer_export(besides="pgadmin")
    assert capsys.readouterr().out.startswith("Found VS Code: SQLTools. Add the stand connections there (2)?")

    def closed(prompt: str) -> str:
        raise EOFError(prompt)

    monkeypatch.setattr("builtins.input", closed)
    install.offer_export()
    assert capsys.readouterr().out == ""
    monkeypatch.setattr("builtins.input", unexpected)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    install.offer_export()
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setenv("PG_EXPLORER_MCP_STANDS_FILE", str(tmp_path / "none.conf"))
    install.offer_export()  # no stands: nothing to offer
    assert capsys.readouterr().out == ""


def test_both_extensions_of_one_editor_get_their_connections(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, unreachable_env: dict[str, str]
) -> None:
    stands_home(monkeypatch, tmp_path, unreachable_env)
    monkeypatch.setattr(shutil, "which", lambda name: None)  # noqa: ARG005
    user = clients.app_data(os.environ, Path.home()) / "Code" / "User"
    user.mkdir(parents=True)
    (user / "settings.json").write_text('{"editor.fontSize": 13}\n', encoding="utf-8")
    for extension in ("ms-ossdata.vscode-pgsql-1.4.0", "mtxr.sqltools-0.28.4"):
        (tmp_path / ".vscode" / "extensions" / extension).mkdir(parents=True)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "y")  # noqa: ARG005
    install.offer_export()  # both change settings.json: the second must see what the first wrote
    settings = json.loads((user / "settings.json").read_text(encoding="utf-8"))
    assert settings["editor.fontSize"] == 13
    assert len(settings["pgsql.connections"]) == len(settings["sqltools.connections"]) == 2
    assert len(list(user.glob("settings.json.bak-*"))) == 2


@pytest.mark.usefixtures("shell")
class TestPassword:
    """`install.py password` (ADR 0012): the typed password goes into the store, and nowhere else."""

    LOGIN = "billing@db.example:5432/orders"

    @pytest.fixture
    def shell(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """A terminal to type into, and a shell that names one database, whatever the machine's shell has."""
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
        monkeypatch.setenv("PG_EXPLORER_MCP_LANG", "en")
        # The command keeps its lock next to the registry: in a folder of the test's own, not the user's.
        monkeypatch.setenv("PG_EXPLORER_MCP_STANDS_FILE", str(tmp_path / "stands.conf"))
        for key in ("PGPORT", "PGPASSWORD"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("PGHOST", "db.example")
        monkeypatch.setenv("PGDATABASE", "orders")
        monkeypatch.setenv("PGUSER", "billing")

    @staticmethod
    def typed(monkeypatch: pytest.MonkeyPatch, *answers: str) -> None:
        given = iter(answers)
        monkeypatch.setattr(getpass, "getpass", lambda _prompt: next(given))

    def test_the_typed_password_goes_into_the_store_and_is_printed_nowhere(
        self, monkeypatch: pytest.MonkeyPatch, own_password_store: Memory, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self.typed(monkeypatch, "s3cret", "s3cret")
        assert install.main(["password"]) == 0
        assert own_password_store.text == "db.example:5432:orders:billing:s3cret\n"
        out = capsys.readouterr().out
        assert f"the password of {self.LOGIN} is in the password store (the memory of the tests)" in out
        assert "s3cret" not in out

    def test_two_different_entries_write_nothing(
        self, monkeypatch: pytest.MonkeyPatch, own_password_store: Memory, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self.typed(monkeypatch, "s3cret", "s3creT")
        assert install.main(["password"]) == 1
        assert (own_password_store.text, capsys.readouterr().out.strip()) == (
            "",
            "the two entries differ: nothing was written",
        )

    def test_an_empty_password_writes_nothing(
        self, monkeypatch: pytest.MonkeyPatch, own_password_store: Memory, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self.typed(monkeypatch, "", "")
        assert install.main(["password"]) == 1
        assert own_password_store.text == ""
        assert "the password is empty: nothing was written" in capsys.readouterr().out

    def test_without_a_terminal_it_asks_for_one(
        self, monkeypatch: pytest.MonkeyPatch, own_password_store: Memory, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
        assert install.main(["password"]) == 2
        assert "run it in a terminal, not through the agent" in capsys.readouterr().out
        assert own_password_store.text == ""

    def test_nothing_names_a_database(
        self, monkeypatch: pytest.MonkeyPatch, own_password_store: Memory, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.delenv("PGHOST")
        assert install.main(["password"]) == 2
        assert "name the login" in capsys.readouterr().out
        assert own_password_store.text == ""

    def test_the_options_name_the_login_and_each_host_gets_a_line_of_its_own(
        self, monkeypatch: pytest.MonkeyPatch, own_password_store: Memory
    ) -> None:
        self.typed(monkeypatch, "s3cret", "s3cret")
        options = ["--host", "h1,h2", "--port", "6544,6545", "--database", "d2", "--user", "u2"]
        assert install.main(["password", *options]) == 0
        assert own_password_store.text == "h1:6544:d2:u2:s3cret\nh2:6545:d2:u2:s3cret\n"

    def test_a_stands_login_comes_from_the_registry(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        unreachable_env: dict[str, str],
        own_password_store: Memory,
    ) -> None:
        stands_home(monkeypatch, tmp_path, unreachable_env)
        monkeypatch.setenv("PG_EXPLORER_MCP_STANDS", "*")
        self.typed(monkeypatch, "s3cret", "s3cret")
        assert install.main(["password", "--stand", "dev7", "--schema", "billing"]) == 0
        assert own_password_store.text == "dev7-db.example:5432:orders:billing:s3cret\n"

    def test_a_stand_that_is_not_in_the_registry_is_refused(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        unreachable_env: dict[str, str],
        own_password_store: Memory,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        stands_home(monkeypatch, tmp_path, unreachable_env)
        monkeypatch.setenv("PG_EXPLORER_MCP_STANDS", "*")
        self.typed(monkeypatch, "s3cret", "s3cret")
        assert install.main(["password", "--stand", "dev9", "--schema", "billing"]) == 1
        assert "No stand 'dev9'" in capsys.readouterr().out
        assert own_password_store.text == ""

    def test_move_carries_the_password_over_and_drops_the_exact_line_only(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        own_password_store: Memory,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        passfile = tmp_path / "pgpass"
        # The line of this login comes first, as libpq reads the file: what is moved is what the server used.
        before = "# mine\ndb.example:5432:orders:billing:m0ved-secret\n*:*:*:billing:star\nother:5432:x:y:z\n"
        passfile.write_text(before, encoding="utf-8")
        if sys.platform != "win32":
            passfile.chmod(0o600)
        monkeypatch.setenv("PGPASSFILE", str(passfile))
        monkeypatch.setattr(getpass, "getpass", lambda _prompt: pytest.fail("nothing is typed"))
        assert install.main(["password", "--move"]) == 0
        assert own_password_store.text == "db.example:5432:orders:billing:m0ved-secret\n"
        assert passfile.read_text(encoding="utf-8") == "# mine\n*:*:*:billing:star\nother:5432:x:y:z\n"
        [backup] = tmp_path.glob("pgpass.bak-*")
        assert backup.read_text(encoding="utf-8") == before
        assert "1 line(s) of the password file moved into the password store" in capsys.readouterr().out

    def test_move_with_nothing_to_move_writes_nothing(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        own_password_store: Memory,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("PGPASSFILE", str(tmp_path / "pgpass"))
        assert install.main(["password", "--move"]) == 1
        assert (own_password_store.text, capsys.readouterr().out.strip()) == (
            "",
            f"the password file has no line for {self.LOGIN}: nothing to move",
        )

    def test_without_a_store_on_this_system_nothing_can_be_written(
        self, monkeypatch: pytest.MonkeyPatch, own_password_store: Memory, capsys: pytest.CaptureFixture[str]
    ) -> None:
        passwords.use(None)
        self.typed(monkeypatch, "s3cret", "s3cret")
        assert install.main(["password"]) == 1
        assert "no password store the server can use" in capsys.readouterr().out
        assert own_password_store.text == ""

    def test_a_stands_login_comes_from_the_registry_whatever_the_shell_says_of_stands(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        unreachable_env: dict[str, str],
        own_password_store: Memory,
    ) -> None:
        """PG_EXPLORER_MCP_STANDS is the agent's setting: the user who types --stand need not have it."""
        stands_home(monkeypatch, tmp_path, unreachable_env)
        monkeypatch.delenv("PG_EXPLORER_MCP_STANDS", raising=False)
        self.typed(monkeypatch, "s3cret", "s3cret")
        assert install.main(["password", "--stand", "dev7", "--schema", "billing"]) == 0
        assert own_password_store.text == "dev7-db.example:5432:orders:billing:s3cret\n"

    def test_a_section_with_a_dbname_keys_the_password_by_what_its_server_connects_to(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        unreachable_env: dict[str, str],
        own_password_store: Memory,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        stands_home(monkeypatch, tmp_path, unreachable_env)
        stands = tmp_path / "stands.conf"
        stands.write_text(
            "[dev7.orders.billing]\nhost=dev7-db.example\n\n"
            "[dev7.archive.billing]\nhost=dev7-db.example\ndbname=archive_ift\n",
            encoding="utf-8",
        )
        self.typed(monkeypatch, "s3cret", "s3cret")
        # The schema billing is in two databases of the stand: the registry wants to know which.
        assert install.main(["password", "--stand", "dev7", "--schema", "billing"]) == 1
        assert "in several databases" in capsys.readouterr().out
        assert install.main(["password", "--stand", "dev7", "--schema", "billing", "--database", "archive"]) == 0
        assert own_password_store.text == "dev7-db.example:5432:archive_ift:billing:s3cret\n"

    def test_the_lock_is_held_while_the_password_is_put_into_the_store_and_let_go_after(
        self, monkeypatch: pytest.MonkeyPatch, own_password_store: Memory
    ) -> None:
        """What the command reads and writes is one cycle, made under the lock of user_files (ADR 0012)."""
        put = passwords.Passwords.put
        held: list[bool] = []

        def putting(store: passwords.Passwords, found: dict[passwords.Login, str]) -> None:
            held.append(lock_is_held_by_the_command())
            put(store, found)

        monkeypatch.setattr(passwords.Passwords, "put", putting)
        self.typed(monkeypatch, "s3cret", "s3cret")
        assert not lock_is_held_by_the_command()
        assert install.main(["password"]) == 0
        assert held == [True]
        assert not lock_is_held_by_the_command()
        assert own_password_store.text == "db.example:5432:orders:billing:s3cret\n"

    def test_a_command_that_cannot_get_the_lock_in_time_writes_nothing_and_says_why(
        self,
        monkeypatch: pytest.MonkeyPatch,
        own_password_store: Memory,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setattr(user_files, "LOCK_TIMEOUT_S", 0.3)
        folder = registry_files(install.shell_env())[0].parent
        before = subprocess.Popen(  # another command, still writing
            [sys.executable, "-I", "-c", HOLDER, str(ROOT / "src"), str(folder)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            assert before.stdout is not None
            assert before.stdout.readline().strip() == "held"
            self.typed(monkeypatch, "s3cret", "s3cret")
            assert install.main(["password"]) == 1
            out = capsys.readouterr().out
            assert (
                "another command of pg-explorer-mcp is writing these files and did not finish in 0.3 seconds. "
                "Nothing was written: try again when it has."
            ) in out
            assert "s3cret" not in out
            assert own_password_store.text == ""
        finally:
            before.kill()
            before.wait(timeout=10)


def test_the_switch_of_the_store_is_copied_into_the_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PG_EXPLORER_MCP_PASSWORD_STORE", "off")
    # The next line compares the name of a variable, not a password.
    assert install.server_entry()["env"]["PG_EXPLORER_MCP_PASSWORD_STORE"] == "off"  # noqa: S105


# --- one command at a time over the user's files (user_files.exclusive) ---


HOLDER = """
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from pg_explorer_mcp.user_files import exclusive

with exclusive(Path(sys.argv[2])):
    print("held", flush=True)
    sys.stdin.readline()  # until the test says so
"""

PROBE = """
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from pg_explorer_mcp.user_files import StillLockedError, exclusive

try:
    with exclusive(Path(sys.argv[2]), timeout=0):
        print("free")
except StillLockedError:
    print("held")
"""


def lock_is_held_by_the_command() -> bool:
    """Whether another process can take the lock right now, asked of a process of its own: no while a command writes."""
    folder = registry_files(install.shell_env())[0].parent
    probe = subprocess.run(
        [sys.executable, "-I", "-c", PROBE, str(ROOT / "src"), str(folder)],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    assert probe.stdout.strip() in {"free", "held"}, probe.stdout + probe.stderr
    return probe.stdout.strip() == "held"


def test_import_write_holds_the_lock_while_it_reads_merges_and_writes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, unreachable_env: dict[str, str], own_password_store: Memory
) -> None:
    """Two imports at once would each write the registry from what they read before the other wrote."""
    use_env(monkeypatch, unreachable_env, tmp_path)
    monkeypatch.setenv("PG_EXPLORER_MCP_STANDS_FILE", str(tmp_path / "stands.conf"))
    monkeypatch.setattr(install, "offer_export", lambda **_kwargs: None)
    write = imports.write
    held: list[bool] = []

    def writing(*args: object, **options: object) -> list[str]:
        held.append(lock_is_held_by_the_command())
        return write(*args, **options)  # type: ignore[arg-type]

    monkeypatch.setattr(imports, "write", writing)
    argv = ["import", "dbeaver", "--workspace", str(dbeaver_folder(tmp_path)), "--write", "--lang", "en"]
    assert install.main(argv) == 0
    assert held == [True]
    assert not lock_is_held_by_the_command()
    assert own_password_store.text != ""  # the import itself went as it does


def test_export_write_holds_the_lock_from_the_reading_to_the_writing_and_a_preview_takes_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, unreachable_env: dict[str, str]
) -> None:
    stands_home(monkeypatch, tmp_path, unreachable_env)
    prepare, write = exports.prepare, exports.write
    held: list[bool] = []

    def preparing(*args: object, **options: object) -> exports.Export:
        held.append(lock_is_held_by_the_command())  # the files it merges with are read under the lock too
        return prepare(*args, **options)  # type: ignore[arg-type]

    def writing(export: exports.Export) -> list[str]:
        held.append(lock_is_held_by_the_command())
        return write(export)

    monkeypatch.setattr(exports, "prepare", preparing)
    monkeypatch.setattr(exports, "write", writing)
    assert install.main(["export", "pg-service", "--write", "--lang", "en"]) == 0
    assert held == [True, True]
    assert not lock_is_held_by_the_command()
    held.clear()
    assert install.main(["export", "pg-service", "--lang", "en"]) == 0  # a preview writes nothing
    assert held == [False]


# --- the settings of the agents and editors: written as every user's file is, under the lock -------------------


def test_two_registrations_within_a_second_keep_both_copies(
    settings_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both copies used to take the name of the second they were made in: the second wrote over the first."""
    monkeypatch.setattr(user_files, "_now", lambda: "20261126-120000")
    settings_path.parent.mkdir()
    settings_path.write_text('{"theme": "dark"}\n', encoding="utf-8")
    install.register(str(settings_path), ENTRY)
    install.register(str(settings_path), ENTRY, "pg-sales")
    assert sorted(child.name for child in settings_path.parent.iterdir()) == [
        "settings.json",
        "settings.json.bak-20261126-120000",
        "settings.json.bak-20261126-120000-2",
    ]
    first, second = (
        read(settings_path.parent / name)
        for name in ("settings.json.bak-20261126-120000", "settings.json.bak-20261126-120000-2")
    )
    assert isinstance(first, dict)
    assert isinstance(second, dict)
    assert (first, set(second["mcpServers"])) == ({"theme": "dark"}, {"pg"})


def test_registering_with_a_client_holds_the_lock_while_its_settings_are_written(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, unreachable_env: dict[str, str]
) -> None:
    """The agent rewrites its settings itself, and another command of ours may: one at a time over the file."""
    use_env(monkeypatch, unreachable_env, tmp_path)
    register = install.register
    held: list[bool] = []

    def registering(path: str | Path, entry: dict[str, object], name: str = install.SERVER_NAME) -> None:
        held.append(lock_is_held_by_the_command())
        register(path, entry, name)

    monkeypatch.setattr(install, "register", registering)
    assert install.install_into("gigacode", "pg", ENTRY) is True
    assert held == [True]
    assert not lock_is_held_by_the_command()
    settings = read(tmp_path / ".gigacode" / "settings.json")
    assert isinstance(settings, dict)
    assert set(settings["mcpServers"]) == {"pg"}


def test_registering_with_an_editor_holds_the_lock_too(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, unreachable_env: dict[str, str]
) -> None:
    use_env(monkeypatch, unreachable_env, tmp_path)
    (clients.app_data(os.environ, Path.home()) / "Code" / "User").mkdir(parents=True)
    write = clients.write
    held: list[bool] = []

    def writing(*args: object, **options: object) -> Path | None:
        held.append(lock_is_held_by_the_command())
        return write(*args, **options)  # type: ignore[arg-type]

    monkeypatch.setattr(clients, "write", writing)
    assert install.install_into("vscode", "pg", {**ENTRY, "env": {}}) is True
    assert held == [True]
    assert not lock_is_held_by_the_command()


def test_a_command_that_cannot_get_the_lock_registers_nothing_and_says_why(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    unreachable_env: dict[str, str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    use_env(monkeypatch, unreachable_env, tmp_path)
    monkeypatch.setattr(user_files, "LOCK_TIMEOUT_S", 0.3)
    folder = registry_files(install.shell_env())[0].parent
    before = subprocess.Popen(  # another command, still writing
        [sys.executable, "-I", "-c", HOLDER, str(ROOT / "src"), str(folder)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert before.stdout is not None
        assert before.stdout.readline().strip() == "held"
        assert install.install_into("gigacode", "pg", ENTRY) is False
        assert "another command of pg-explorer-mcp is writing these files" in capsys.readouterr().out
        assert not (tmp_path / ".gigacode" / "settings.json").exists()
    finally:
        before.kill()
        before.wait(timeout=10)


@pytest.mark.parametrize(
    "client",
    [("vscode", ("Code", "User"), "servers"), ("cursor", (".cursor",), "mcpServers")],
    ids=["vscode", "cursor"],
)
def test_a_client_whose_settings_cannot_be_written_fails_the_command(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    unreachable_env: dict[str, str],
    capsys: pytest.CaptureFixture[str],
    client: tuple[str, tuple[str, ...], str],
) -> None:
    """The command used to answer 0: it paid no attention to whether the server got registered at all."""
    name, where, key = client
    use_env(monkeypatch, unreachable_env, tmp_path)
    base = Path.home() if name == "cursor" else clients.app_data(os.environ, Path.home())
    folder = base.joinpath(*where)
    folder.mkdir(parents=True)
    settings = '{\n  // comments are fine for the editor, not for json\n  "' + key + '": {}\n}\n'
    (folder / "mcp.json").write_text(settings, encoding="utf-8")
    checked: list[object] = []

    def checking(entry: dict[str, object]) -> bool:
        checked.append(entry)
        return True

    monkeypatch.setattr(install, "check", checking)
    assert install.main(["--client", name, "--lang", "en"]) == 1
    assert checked == []  # nothing was registered, so there was nothing to check the reach of
    out = capsys.readouterr().out
    assert '"pg"' in out  # the text to paste by hand
    assert '"type": "stdio"' in out
    assert (folder / "mcp.json").read_text(encoding="utf-8") == settings
    assert list(folder.iterdir()) == [folder / "mcp.json"]  # and no dated copy of a writing that did not happen


def test_a_lock_another_command_holds_fails_the_command_too(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    unreachable_env: dict[str, str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    use_env(monkeypatch, unreachable_env, tmp_path)
    monkeypatch.setattr(user_files, "LOCK_TIMEOUT_S", 0.3)
    folder = registry_files(install.shell_env())[0].parent
    before = subprocess.Popen(  # another command, still writing
        [sys.executable, "-I", "-c", HOLDER, str(ROOT / "src"), str(folder)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    checked: list[object] = []

    def checking(entry: dict[str, object]) -> bool:
        checked.append(entry)
        return True

    monkeypatch.setattr(install, "check", checking)
    try:
        assert before.stdout is not None
        assert before.stdout.readline().strip() == "held"
        assert install.main(["--client", "gigacode", "--lang", "en"]) == 1
        assert checked == []
        assert "another command of pg-explorer-mcp is writing these files" in capsys.readouterr().out
        assert not (tmp_path / ".gigacode" / "settings.json").exists()
    finally:
        before.kill()
        before.wait(timeout=10)
