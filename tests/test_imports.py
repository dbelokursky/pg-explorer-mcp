"""Import of stand connections from DBeaver and pg_service.conf: reading, the plan, the preview, writing."""

from __future__ import annotations

import json
import os
import re
import stat
import sys
from pathlib import Path

import pytest

from pg_explorer_mcp import imports
from pg_explorer_mcp.aes import encrypt_cbc
from pg_explorer_mcp.db import read_pgpass
from pg_explorer_mcp.imports import (
    Connection,
    ImportRefusedError,
    compile_pattern,
    plan,
    preview,
    read_dbeaver,
    read_pg_service,
    stand_of,
    write,
)
from pg_explorer_mcp.stands import load_registry

PATTERN = compile_pattern(imports.DEFAULT_STAND_PATTERN)


def connection(**overrides: object) -> Connection:
    fields: dict[str, object] = {
        "source": "test",
        "name": "orders on dev7",
        "folder": "",
        "host": "db.example",
        "port": "5432",
        "database": "orders",
        "user": "billing",
        "password": "s3cret",
    }
    fields.update(overrides)
    return Connection(**fields)  # type: ignore[arg-type]


def dbeaver_workspace(
    folder: Path, connections: dict[str, dict[str, object]], credentials: dict[str, dict[str, str]]
) -> Path:
    """A DBeaver folder: data-sources.json and credentials-config.json encrypted the way DBeaver does."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "data-sources.json").write_text(json.dumps({"folders": {}, "connections": connections}), encoding="utf-8")
    iv = os.urandom(16)
    body = json.dumps({identifier: {"#connection": saved} for identifier, saved in credentials.items()}).encode()
    (folder / "credentials-config.json").write_bytes(iv + encrypt_cbc(imports.DBEAVER_KEY, iv, body))
    return folder


def postgres(
    name: str, host: str, database: str, *, folder: str = "", user: str | None = None, port: str = "5432"
) -> dict[str, object]:
    configuration: dict[str, object] = {
        "host": host,
        "port": port,
        "database": database,
        "url": f"jdbc:postgresql://{host}:{port}/{database}",
    }
    if user:
        configuration["user"] = user
    entry: dict[str, object] = {
        "provider": "postgresql",
        "driver": "postgres-jdbc",
        "name": name,
        "configuration": configuration,
    }
    if folder:
        entry["folder"] = folder
    return entry


class TestStandOf:
    def test_the_folder_then_the_name_then_the_host(self) -> None:
        assert stand_of(connection(folder="Stands/DEV7", name="ift1 orders", host="ift2-db"), PATTERN) == "dev7"
        assert stand_of(connection(name="IFT1 orders", host="ift2-db"), PATTERN) == "ift1"
        assert stand_of(connection(name="orders", host="pg.ift2.corp"), PATTERN) == "ift2"
        assert stand_of(connection(name="orders", host="db.example"), PATTERN) is None

    def test_a_named_group_picks_the_part_that_is_the_stand(self) -> None:
        pattern = compile_pattern(r"stand[-_](?P<stand>[a-z]+\d+)")
        assert stand_of(connection(name="stand-Dev12 orders"), pattern) == "dev12"

    def test_a_broken_pattern_is_refused(self) -> None:
        with pytest.raises(ImportRefusedError, match="not a regular expression"):
            compile_pattern("(dev")


class TestDBeaver:
    def test_reads_postgres_connections_with_their_saved_logins(self, tmp_path: Path) -> None:
        folder = dbeaver_workspace(
            tmp_path / ".dbeaver",
            {
                "pg-1": postgres("orders", "dev7-db.example", "orders", folder="Stands/dev7"),
                "pg-2": postgres("IFT2 shop", "ift2-db.example", "shop", user="shop_ro"),
                "my-1": {"provider": "mysql", "name": "legacy", "configuration": {"host": "x"}},
                "pg-3": postgres("no password", "dev8-db.example", "orders"),
            },
            {"pg-1": {"user": "billing", "password": "s3cret"}},
        )
        found = {c.name: c for c in read_dbeaver(folder)}
        assert set(found) == {"orders", "IFT2 shop", "no password"}
        assert (found["orders"].folder, found["orders"].user, found["orders"].password) == (
            "Stands/dev7",
            "billing",
            "s3cret",
        )
        assert (found["IFT2 shop"].user, found["IFT2 shop"].password) == (
            "shop_ro",
            None,
        )  # the user is in the configuration
        assert (found["no password"].user, found["no password"].password) == ("", None)

    def test_the_address_falls_back_to_the_jdbc_url(self, tmp_path: Path) -> None:
        entry = postgres("x", "", "", port="5432")
        entry["configuration"] = {"url": "jdbc:postgresql://dev3-db.example:6432/sales?ssl=true"}
        folder = dbeaver_workspace(tmp_path / ".dbeaver", {"pg-1": entry}, {})
        found = read_dbeaver(folder)[0]
        assert (found.host, found.port, found.database) == ("dev3-db.example", "6432", "sales")

    def test_credentials_in_secure_storage_or_absent_leave_the_logins_empty(self, tmp_path: Path) -> None:
        folder = dbeaver_workspace(tmp_path / ".dbeaver", {"pg-1": postgres("orders", "db", "orders")}, {})
        (folder / "credentials-config.json").write_bytes(os.urandom(48))  # not our key
        assert read_dbeaver(folder)[0].password is None
        (folder / "credentials-config.json").unlink()
        assert read_dbeaver(folder)[0].password is None

    def test_a_missing_or_broken_data_sources_file_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ImportRefusedError, match="cannot read"):
            read_dbeaver(tmp_path)
        (tmp_path / "data-sources.json").write_text("{", encoding="utf-8")
        with pytest.raises(ImportRefusedError, match="cannot read"):
            read_dbeaver(tmp_path)

    def test_the_workspace_is_found_where_this_os_keeps_it(self, tmp_path: Path) -> None:
        assert imports.dbeaver_workspace({}, tmp_path) is None
        folder = tmp_path / ".local" / "share" / "DBeaverData" / "workspace6" / "General" / ".dbeaver"
        dbeaver_workspace(folder, {}, {})
        assert imports.dbeaver_workspace({}, tmp_path) == folder
        windows = tmp_path / "AppData" / "DBeaverData" / "workspace6" / "General" / ".dbeaver"
        dbeaver_workspace(windows, {}, {})
        assert imports.dbeaver_workspace({"APPDATA": str(tmp_path / "AppData")}, tmp_path) == windows


class TestPgService:
    def test_sections_become_connections_with_their_options(self, tmp_path: Path) -> None:
        path = tmp_path / "service.conf"
        path.write_text(
            "[dev7-orders]\nhost=dev7-db\nport=6432\ndbname=orders\nuser=billing\npassword=pw\nsslmode=require\n"
            "[reports]\nhost=db\n",
            encoding="utf-8",
        )
        first, second = read_pg_service(path)
        assert (first.name, first.host, first.port, first.database, first.user, first.password) == (
            "dev7-orders", "dev7-db", "6432", "orders", "billing", "pw"
        )  # fmt: skip
        assert first.options == {"sslmode": "require"}
        assert (second.database, second.user, second.password) == ("", "", None)

    def test_the_copies_an_export_wrote_are_not_brought_back(self, tmp_path: Path) -> None:
        path = tmp_path / "service.conf"
        path.write_text("# pg-mcp export\n[dev7.orders.billing]\nhost=db\n\n[reports]\nhost=db\n", encoding="utf-8")
        assert [connection.name for connection in read_pg_service(path)] == ["reports"]

    def test_an_unreadable_file_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ImportRefusedError, match="cannot read"):
            read_pg_service(tmp_path / "absent.conf")


class TestPlan:
    def test_targets_passwords_and_what_is_left_out(self) -> None:
        result = plan(
            [
                connection(),
                connection(name="shop on dev7", database="shop", user="shop", password=None),
                connection(name="copy of orders on dev7"),
                connection(name="orders", host="db.example"),
                connection(name="no host on dev7", host=""),
                connection(name="no user on dev7", user=""),
                connection(name="odd on dev7", database="my db"),
                connection(name="ift1 with port", host="ift1-db", port="6432", options={"sslmode": "require"}),
            ],
            PATTERN,
        )
        assert [target.name for target in result.targets] == [
            "dev7.orders.billing",
            "dev7.shop.shop",
            "ift1.orders.billing",
        ]
        assert result.targets[2].options == {"host": "ift1-db", "port": "6432", "sslmode": "require"}
        assert result.passwords == {
            ("db.example", "5432", "orders", "billing"): "s3cret",
            ("ift1-db", "6432", "orders", "billing"): "s3cret",
        }
        assert [target.name for target in result.without_password] == ["dev7.shop.shop"]
        assert [(c.name, reason) for c, reason in result.skipped] == [
            (
                "copy of orders on dev7",
                "the same stand, database and user as 'orders on dev7': [dev7.orders.billing] is already taken",
            ),
            ("orders", "no stand in the folder, the name or the host"),
            ("no host on dev7", "no host"),
            ("no user on dev7", "no user: pg-mcp reads a schema with the login of the same name"),
            ("odd on dev7", "'my db' cannot be a part of a section name (letters, digits, _ $ - only)"),
        ]

    def test_the_preview_never_shows_a_password(self, tmp_path: Path) -> None:
        result = plan(
            [
                connection(),
                connection(name="shop on dev7", database="shop", user="shop", password=None),
                connection(name="x"),
            ],
            PATTERN,
        )
        lines = preview(result, tmp_path / "stands.conf", tmp_path / "pgpass")
        assert lines[0] == f"2 connections go into {tmp_path / 'stands.conf'}:"
        assert lines[1] == "  [dev7.orders.billing]  db.example:5432"
        assert lines[2] == "  [dev7.shop.shop]  db.example:5432  no saved password: add a line to the password file"
        assert lines[3] == f"1 passwords go into {tmp_path / 'pgpass'}"
        assert lines[4:] == ["left out: 1", "  test: x  no stand in the folder, the name or the host"]
        assert "s3cret" not in "\n".join(lines)


class TestWrite:
    def test_writes_the_registry_and_the_password_file_the_server_can_read(self, tmp_path: Path) -> None:
        registry_file, passfile = tmp_path / "pg-mcp" / "stands.conf", tmp_path / "pgpass"
        odd = connection(name="ift1 orders", host="ift1-db", password="p:w\\d")  # noqa: S106
        result = plan([connection(), odd], PATTERN)
        lines = write(result, registry_file, passfile)
        assert lines == [f"written {registry_file}", f"written {passfile}"]
        text = registry_file.read_text(encoding="utf-8")
        assert "[dev7.orders.billing]\nhost=db.example\n" in text
        assert "[ift1.orders.billing]\nhost=ift1-db\n" in text
        assert (
            passfile.read_text(encoding="utf-8")
            == "db.example:5432:orders:billing:s3cret\nift1-db:5432:orders:billing:p\\:w\\\\d\n"
        )
        assert read_pgpass(str(passfile), "ift1-db", 5432, "orders", "billing") == "p:w\\d"
        if sys.platform != "win32":
            assert stat.S_IMODE(passfile.stat().st_mode) == 0o600
            assert stat.S_IMODE(registry_file.stat().st_mode) == 0o600
        registry = load_registry({"PG_MCP_STANDS": "*"}, [registry_file])
        assert registry is not None
        assert [target.name for target in registry.targets] == ["dev7.orders.billing", "ift1.orders.billing"]

    def test_adds_to_what_is_there_and_keeps_backups(self, tmp_path: Path) -> None:
        registry_file, passfile = tmp_path / "stands.conf", tmp_path / "pgpass"
        registry_file.write_text("[dev1.orders.billing]\nhost=old-db\n\n[reports]\nhost=rep\n", encoding="utf-8")
        passfile.write_text("# mine\ndb.example:5432:orders:billing:old\nother:5432:*:me:keep\n", encoding="utf-8")
        passfile.chmod(0o600)
        result = plan([connection()], PATTERN)
        lines = write(result, registry_file, passfile)
        assert all(line.startswith("written ") and ".bak-" in line for line in lines)
        text = registry_file.read_text(encoding="utf-8")
        assert "[dev1.orders.billing]\nhost=old-db\n" in text
        assert "[reports]\nhost=rep\n" in text
        assert "[dev7.orders.billing]\nhost=db.example\n" in text
        assert (
            passfile.read_text(encoding="utf-8")
            == "# mine\ndb.example:5432:orders:billing:s3cret\nother:5432:*:me:keep\n"
        )
        backups = sorted(tmp_path.glob("*.bak-*"))
        assert [path.name.split(".bak-")[0] for path in backups] == ["pgpass", "stands.conf"]
        if sys.platform != "win32":
            assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in backups)

    def test_prune_replaces_the_registry_but_never_the_password_file(self, tmp_path: Path) -> None:
        registry_file, passfile = tmp_path / "stands.conf", tmp_path / "pgpass"
        registry_file.write_text("[dev1.orders.billing]\nhost=old-db\n", encoding="utf-8")
        passfile.write_text("other:5432:*:me:keep\n", encoding="utf-8")
        write(plan([connection()], PATTERN), registry_file, passfile, prune=True)
        assert "dev1" not in registry_file.read_text(encoding="utf-8")
        assert passfile.read_text(encoding="utf-8").splitlines() == [
            "other:5432:*:me:keep",
            "db.example:5432:orders:billing:s3cret",
        ]

    def test_a_failure_leaves_both_files_as_they_were(self, tmp_path: Path) -> None:
        registry_file = tmp_path / "stands.conf"
        registry_file.write_text("[dev1.orders.billing]\nhost=old-db\n", encoding="utf-8")
        blocker = tmp_path / "blocker"
        blocker.write_text("a file where the password folder should be", encoding="utf-8")
        passfile = blocker / "pgpass"
        with pytest.raises(ImportRefusedError, match="Nothing was changed"):
            write(plan([connection()], PATTERN), registry_file, passfile)
        assert registry_file.read_text(encoding="utf-8") == "[dev1.orders.billing]\nhost=old-db\n"
        assert list(tmp_path.glob("*.pg-mcp-new")) == []

    def test_nothing_to_write_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ImportRefusedError, match="nothing to import"):
            write(plan([], PATTERN), tmp_path / "stands.conf", tmp_path / "pgpass")

    def test_the_default_files_follow_the_environment(self, tmp_path: Path) -> None:
        env = {"PG_MCP_STANDS_FILE": str(tmp_path / "s.conf"), "PGPASSFILE": str(tmp_path / "p")}
        assert imports.default_files(env) == (tmp_path / "s.conf", tmp_path / "p")


def test_the_registry_text_sorts_stands_naturally(tmp_path: Path) -> None:
    result = plan(
        [connection(name=f"orders on {stand}", host=f"{stand}-db") for stand in ("dev10", "dev2", "ift1")], PATTERN
    )
    write(result, tmp_path / "stands.conf", tmp_path / "pgpass")
    names = re.findall(r"^\[(.+)\]$", (tmp_path / "stands.conf").read_text(encoding="utf-8"), re.MULTILINE)
    assert names == ["dev2.orders.billing", "dev10.orders.billing", "ift1.orders.billing"]
