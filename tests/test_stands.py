"""Many stands in one entry, without a database: the registry, the pool and the tools' routing (ADR 0006)."""

from __future__ import annotations

import io
import json
import socket
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from pg_mcp import __main__ as entry
from pg_mcp.compare import (
    RELATIONS_SQL,
    SCHEMA_CONSTRAINTS_SQL,
    SCHEMA_INDEXES_SQL,
    Relation,
    Structure,
    differences,
    read_structure,
)
from pg_mcp.db import Database, DatabaseUnavailableError, QueryFailedError, Settings
from pg_mcp.querylog import QueryLog
from pg_mcp.stand_tools import STAND_ONLY_TOOLS, STAND_PROFILES, build_stand_tools
from pg_mcp.stands import (
    Pool,
    Registry,
    StandError,
    Target,
    load_registry,
    parse_service_file,
    registry_files,
    stands_enabled,
    summarize,
)
from pg_mcp.tools import PROFILES, Tool, ToolError
from tests.helpers import ROOT, rpc

STANDS_ON = {"PG_MCP_STANDS": "*"}
NAMES = (
    "dev1.orders.billing",
    "dev1.orders.shipping",
    "dev1.archive.billing",
    "dev2.orders.billing",
    "ift1.orders.billing",
)


def sections(host: str = "db.example.com", port: str = "5432") -> str:
    return "".join(f"[{name}]\nhost={host}\nport={port}\n\n" for name in NAMES)


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def loaded(env: dict[str, str], files: list[Path]) -> Registry:
    registry = load_registry(env, files)
    assert registry is not None
    return registry


@pytest.fixture
def registry(tmp_path: Path) -> Registry:
    return loaded(STANDS_ON, [write(tmp_path / "stands.conf", sections())])


def by_name(tools: list[Tool]) -> dict[str, Tool]:
    return {tool.name: tool for tool in tools}


class TestServiceFile:
    def test_sections_keys_and_values(self) -> None:
        text = "# psql services\n[dev1.orders.billing]\n Host = db \nOPTIONS=-c search_path=x\n\n[reports]\nport=1\n"
        assert parse_service_file(text) == (
            {"dev1.orders.billing": {"host": "db", "options": "-c search_path=x"}, "reports": {"port": "1"}},
            [],
        )

    def test_lines_it_cannot_read_are_reported_by_number(self) -> None:
        found, problems = parse_service_file("host=orphan\n[dev1.orders.billing]\nhost=db\njust words\n")
        assert found == {"dev1.orders.billing": {"host": "db"}}
        assert problems == ["line 1 is neither [section] nor key=value", "line 4 is neither [section] nor key=value"]


@pytest.mark.parametrize(
    ("raw", "patterns"),
    [("", None), ("  ", None), ("off", None), ("No", None), ("false", None), ("0", None), ("*", ["*"])],
)
def test_pg_mcp_stands_switches_stands_on(raw: str, patterns: list[str] | None) -> None:
    assert stands_enabled({"PG_MCP_STANDS": raw}) == patterns


def test_pg_mcp_stands_takes_patterns_in_any_case() -> None:
    assert stands_enabled({}) is None
    assert stands_enabled({"PG_MCP_STANDS": "Dev*, ift2,"}) == ["dev*", "ift2"]


class TestRegistryFiles:
    def test_defaults_are_in_the_home_folder(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        assert registry_files({}) == [Path.home() / ".pg-mcp" / "stands.conf", Path.home() / ".pg_service.conf"]

    def test_windows_keeps_them_in_appdata(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        assert registry_files({"APPDATA": str(tmp_path)}) == [
            tmp_path / "pg-mcp" / "stands.conf",
            tmp_path / "postgresql" / ".pg_service.conf",
        ]

    def test_variables_name_the_files(self, tmp_path: Path) -> None:
        env = {"PG_MCP_STANDS_FILE": "~/stands.conf", "PGSERVICEFILE": str(tmp_path / "service.conf")}
        assert registry_files(env) == [Path.home() / "stands.conf", tmp_path / "service.conf"]


class TestLoadRegistry:
    def test_off_without_pg_mcp_stands_whatever_lies_on_disk(self, tmp_path: Path) -> None:
        assert load_registry({}, [write(tmp_path / "stands.conf", sections())]) is None

    def test_reads_the_connections_named_stand_database_schema(self, tmp_path: Path) -> None:
        own = write(tmp_path / "stands.conf", sections())
        registry = loaded(STANDS_ON, [own])
        assert [target.name for target in registry.targets] == list(NAMES)
        assert registry.stands() == ["dev1", "dev2", "ift1"]
        assert (registry.files, registry.warnings) == ([str(own)], [])
        assert registry.targets[0].source == str(own)

    def test_ordinary_services_are_left_to_psql(self, tmp_path: Path) -> None:
        others = "[reports]\nhost=db\n[dev1.orders]\nhost=db\n[a.b.c.d]\nhost=db\n[dev1.orders.two words]\nhost=db\n"
        registry = loaded(STANDS_ON, [write(tmp_path / "stands.conf", others + sections())])
        assert (len(registry.targets), registry.warnings) == (len(NAMES), [])

    def test_patterns_choose_the_stands_and_names_fold_to_lower_case(self, tmp_path: Path) -> None:
        own = write(tmp_path / "stands.conf", "[DEV7.orders.billing]\nhost=db\n" + sections())
        assert loaded({"PG_MCP_STANDS": "dev*"}, [own]).stands() == ["dev1", "dev2", "dev7"]

    def test_the_import_file_wins_over_pg_service_conf(self, tmp_path: Path) -> None:
        own = write(tmp_path / "stands.conf", "[dev1.orders.billing]\nhost=imported\n")
        service = write(
            tmp_path / "service.conf", "[dev1.orders.billing]\nhost=by-hand\n[dev2.orders.billing]\nhost=db2\n"
        )
        registry = loaded(STANDS_ON, [own, service])
        assert {target.name: target.options["host"] for target in registry.targets} == {
            "dev1.orders.billing": "imported",
            "dev2.orders.billing": "db2",
        }
        assert registry.files == [str(own), str(service)]

    def test_broken_sections_become_warnings_and_the_rest_still_loads(self, tmp_path: Path) -> None:
        own = write(
            tmp_path / "stands.conf",
            "[dev1.orders.nohost]\nport=5432\n"
            "[dev1.orders.badport]\nhost=db\nport=5432x\n"
            "[dev1.orders.slow]\nhost=db\nconnect_timeout=ten\nnonsense\n"
            "[dev1.orders.billing]\nhost=db\n",
        )
        registry = loaded(STANDS_ON, [own])
        assert [target.name for target in registry.targets] == ["dev1.orders.billing"]
        assert registry.warnings == [
            f"{own}: line 9 is neither [section] nor key=value",
            f"stand connection dev1.orders.nohost in {own} has no host, skipped",
            f"stand connection dev1.orders.badport in {own} has port='5432x', not a number, skipped",
            f"stand connection dev1.orders.slow in {own} has connect_timeout='ten', not a number, skipped",
        ]

    def test_missing_files_are_skipped_quietly(self, tmp_path: Path) -> None:
        registry = loaded(STANDS_ON, [tmp_path / "absent.conf", write(tmp_path / "stands.conf", sections())])
        assert (len(registry.targets), registry.warnings) == (len(NAMES), [])

    def test_a_file_it_cannot_read_is_a_warning(self, tmp_path: Path) -> None:
        binary = tmp_path / "binary.conf"
        binary.write_bytes(b"[dev9.orders.billing]\nhost=\xff\n")
        folder = tmp_path / "folder.conf"
        folder.mkdir()
        registry = loaded(STANDS_ON, [binary, folder, write(tmp_path / "stands.conf", sections())])
        assert len(registry.targets) == len(NAMES)
        assert [warning.startswith(f"cannot read the stands registry {path}: ") for path, warning in zip(
            (binary, folder), registry.warnings
        )] == [True, True]  # fmt: skip
        assert len(registry.warnings) == 2

    def test_no_stands_at_all_says_where_it_looked(self, tmp_path: Path) -> None:
        env = dict(STANDS_ON, PG_MCP_STANDS_FILE=str(tmp_path / "a.conf"), PGSERVICEFILE=str(tmp_path / "b.conf"))
        registry = load_registry(env)
        assert registry is not None
        assert registry.targets == []
        assert registry.warnings == [
            f"PG_MCP_STANDS is set, but no stands were found in {tmp_path / 'a.conf'}, {tmp_path / 'b.conf'}; "
            "working with the PG* settings"
        ]

    def test_files_come_from_the_environment(self, tmp_path: Path) -> None:
        own = write(tmp_path / "stands.conf", sections())
        env = dict(STANDS_ON, PG_MCP_STANDS_FILE=str(own), PGSERVICEFILE=str(tmp_path / "absent.conf"))
        registry = load_registry(env)
        assert registry is not None
        assert len(registry.targets) == len(NAMES)


class TestTarget:
    def test_the_name_gives_database_login_and_search_path(self) -> None:
        target = Target("dev1", "orders", "billing", {"host": "db"}, "stands.conf")
        assert (target.name, target.label) == ("dev1.orders.billing", "dev1 / orders / billing")
        base = {
            "PGHOST": "entry-host",
            "PGUSER": "entry-user",
            "PGPASSWORD": "entry-secret",
            "PGDATABASE": "entry-db",
            "PGPASSFILE": "/home/me/.pgpass",
            "PGSSLMODE": "verify-full",
            "PG_MCP_STATEMENT_TIMEOUT_MS": "7000",
            "HOME": "/home/me",
        }
        assert target.env(base) == {
            "PGPASSFILE": "/home/me/.pgpass",
            "PGSSLMODE": "verify-full",
            "PG_MCP_STATEMENT_TIMEOUT_MS": "7000",
            "PG_MCP_SOCKET_TIMEOUT_S": "10",
            "PGHOST": "db",
            "PGPORT": "5432",
            "PGDATABASE": "orders",
            "PGUSER": "billing",
            "PG_MCP_SEARCH_PATH": "billing",
        }

    def test_the_section_overrides_the_entry(self) -> None:
        options = {
            "host": "db",
            "port": "6432",
            "dbname": "orders_db",
            "user": "billing_ro",
            "connect_timeout": "3",
            "sslmode": "require",
            "sslrootcert": "/etc/ca.pem",
            "password": "in-the-section",
        }
        env = Target("dev1", "orders", "billing", options, "stands.conf").env(
            {"PGSSLMODE": "disable", "PG_MCP_SOCKET_TIMEOUT_S": "20"}
        )
        settings = Settings.from_env(env)
        assert (settings.host, settings.port, settings.database, settings.user) == (
            "db",
            6432,
            "orders_db",
            "billing_ro",
        )
        assert (settings.sslmode, settings.sslrootcert, settings.password) == (
            "require",
            "/etc/ca.pem",
            "in-the-section",
        )
        assert (settings.socket_timeout_s, settings.search_path) == (3, "billing")

    def test_the_entry_timeout_applies_when_the_section_has_none(self) -> None:
        env = Target("dev1", "orders", "billing", {"host": "db"}, "x").env({"PG_MCP_SOCKET_TIMEOUT_S": "20"})
        assert env["PG_MCP_SOCKET_TIMEOUT_S"] == "20"


class TestRegistry:
    def test_stands_sort_by_their_numbers(self) -> None:
        targets = [Target(stand, "o", "s", {"host": "db"}, "x") for stand in ("dev10", "ift1", "dev2", "prod")]
        assert Registry(targets).stands() == ["dev2", "dev10", "ift1", "prod"]

    def test_a_stand_is_found_whatever_the_case(self, registry: Registry) -> None:
        assert registry.stand(" DEV1 ") == "dev1"

    @pytest.mark.parametrize(
        ("name", "message"),
        [
            (None, "Pass stand, one of: dev1–dev2, ift1."),
            (" ", "Pass stand, one of: dev1–dev2, ift1."),
            ("dev11", "No stand 'dev11' in the registry. Similar: dev1, dev2."),
            ("prod", "No stand 'prod' in the registry. Stands: dev1–dev2, ift1."),
        ],
    )
    def test_a_wrong_stand_is_explained(self, registry: Registry, name: Any, message: str) -> None:
        with pytest.raises(StandError) as caught:
            registry.stand(name)
        assert str(caught.value) == message

    def test_the_schema_picks_the_login(self, registry: Registry) -> None:
        assert registry.target("dev1", "shipping").name == "dev1.orders.shipping"
        assert registry.target("dev1", '"Shipping"').name == "dev1.orders.shipping"
        assert registry.target("dev1", "billing", " Archive ").name == "dev1.archive.billing"

    @pytest.mark.parametrize(
        ("schema", "database", "message"),
        [
            (None, None, "Pass schema: on stand dev1 the server reads data with the login of the schema you name."),
            ("", None, "Pass schema: on stand dev1 the server reads data with the login of the schema you name."),
            ("billing", None, "Schema 'billing' exists in several databases of dev1: archive, orders. Pass database"),
            ("shipping", "archive", "Schema 'shipping' of dev1 is in orders, not in 'archive'. Pass one of those"),
            ("shiping", None, "Stand dev1 has no schema 'shiping' that the server has a login for. Similar: shipping."),
            ("hr", None, "Stand dev1 has no schema 'hr' that the server has a login for. Call list_schemas"),
        ],
    )
    def test_a_wrong_schema_is_explained(self, registry: Registry, schema: Any, database: Any, message: str) -> None:
        with pytest.raises(StandError) as caught:
            registry.target("dev1", schema, database)
        assert str(caught.value).startswith(message)


@pytest.mark.parametrize(
    ("stands", "text"),
    [
        ([], "none"),
        (["dev1"], "dev1"),
        (["dev1", "dev2", "dev3", "dev5", "dev7", "dev8"], "dev1–dev3, dev5, dev7–dev8"),
        ([*(f"dev{number}" for number in range(1, 51)), "ift1", "ift2", "ift3"], "dev1–dev50, ift1–ift3"),
        (["dev2", "prod", "x1y"], "dev2, prod, x1y"),
    ],
)
def test_summarize_folds_numbered_runs(stands: list[str], text: str) -> None:
    assert summarize(stands) == text


class TestPool:
    def test_connections_open_on_demand_and_only_the_recent_ones_stay(self, registry: Registry, tmp_path: Path) -> None:
        pool = Pool(registry, {"PGPASSFILE": str(tmp_path / "absent")}, limit=2)
        first, second, third = registry.targets[:3]
        assert pool.open_count() == 0
        database = pool.database(first)
        assert pool.database(first) is database
        pool.database(second)
        pool.database(third)  # the first is the least recently used now
        assert pool.open_count() == 2
        assert pool.database(first) is not database
        pool.close()
        assert pool.open_count() == 0

    def test_at_least_one_connection_stays(self, registry: Registry) -> None:
        assert Pool(registry, {}, limit=0).limit == 1

    def test_the_query_log_sees_the_login_in_use(self, registry: Registry, tmp_path: Path) -> None:
        pool = Pool(registry, {"PGPASSFILE": str(tmp_path / "absent")})
        assert (pool.settings, pool.log_context(), pool.statements_run, pool.rows_read) == (None, {}, 0, 0)
        pool.database(registry.targets[1])
        assert pool.log_context() == {"stand": "dev1", "schema": "shipping"}
        assert pool.settings is not None
        assert (pool.settings.user, pool.settings.database) == ("shipping", "orders")


@pytest.fixture
def pool(tmp_path: Path, unreachable_env: dict[str, str]) -> Pool:
    """The registry's stands, every one on a local port where nothing listens."""
    own = write(tmp_path / "stands.conf", sections(host="127.0.0.1", port=unreachable_env["PGPORT"]))
    return Pool(loaded(STANDS_ON, [own]), unreachable_env)


@pytest.fixture
def tools(pool: Pool) -> dict[str, Tool]:
    return by_name(build_stand_tools(pool, str(ROOT)))


class TestOfflineTools:
    """The stand tools with every stand unreachable: routing and messages, no database needed."""

    @pytest.mark.parametrize("profile", list(PROFILES))
    def test_every_profile_has_the_stand_tools_right_after_the_tools_they_follow(
        self, pool: Pool, profile: str
    ) -> None:
        names = [tool.name for tool in build_stand_tools(pool, str(ROOT), profile=profile)]
        assert names == list(STAND_PROFILES[profile])
        assert [name for name in names if name not in STAND_ONLY_TOOLS] == list(PROFILES[profile])
        assert [names[names.index(after) + 1] for after in STAND_ONLY_TOOLS.values()] == list(STAND_ONLY_TOOLS)

    def test_the_schema_profile_in_full(self) -> None:
        assert STAND_PROFILES["schema"] == (
            "status",
            "list_schemas",
            "list_tables",
            "search_schema",
            "describe_table",
            "compare_schema",
        )

    def test_every_tool_but_status_needs_a_stand_and_data_needs_a_schema(self, pool: Pool) -> None:
        required = {
            tool.name: tool.describe()["inputSchema"].get("required", [])
            for tool in build_stand_tools(pool, str(ROOT), profile="analyst")
        }
        assert required == {
            "status": [],
            "list_schemas": ["stand"],
            "list_tables": ["stand"],
            "search_schema": ["stand", "text"],
            "describe_table": ["stand", "table"],
            "compare_schema": ["stand_a", "stand_b", "schema"],
            "sample_rows": ["stand", "table"],
            "explain_query": ["stand", "schema", "query"],
            "run_select": ["stand", "schema", "query"],
        }

    def test_only_the_analyst_can_ask_explain_to_run_the_query(self, pool: Pool, tools: dict[str, Tool]) -> None:
        analyst = by_name(build_stand_tools(pool, str(ROOT), profile="analyst"))
        assert "analyze" in analyst["explain_query"].properties
        assert "analyze" not in tools["explain_query"].properties
        assert "Pass stand, and schema" in tools["describe_table"].description

    def test_status_without_a_stand_lists_the_stands_and_connects_nowhere(
        self, pool: Pool, tools: dict[str, Tool]
    ) -> None:
        lines = tools["status"].call({}).splitlines()
        assert "stands: 3 (dev1–dev2, ift1), connections in the registry: 5" in lines
        assert "pass stand to status to check that stand's connection" in lines
        assert pool.open_count() == 0

    def test_status_with_a_stand_lists_its_schemas_and_checks_one_login(self, tools: dict[str, Tool]) -> None:
        lines = tools["status"].call({"stand": "dev1"}).splitlines()
        assert lines[-4:-1] == [
            "dev1 / archive: billing",
            "dev1 / orders: billing, shipping",
            "checked with dev1 / archive / billing",
        ]
        assert lines[-1].startswith("database: unavailable. Nothing accepts connections at 127.0.0.1:")

    def test_a_login_the_server_turned_down_is_followed_by_the_next_one(
        self, tools: dict[str, Tool], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        logins: list[str] = []

        def query(self: Database, sql: str, **params: Any) -> list[tuple[Any, ...]]:  # noqa: ARG001
            assert self.settings is not None
            logins.append(self.settings.user)
            if self.settings.user == "billing":
                raise DatabaseUnavailableError("Login as 'billing' failed", host_down=False)
            return [("orders", "parcels", "r", 10, None)]

        monkeypatch.setattr(Database, "query", query)
        lines = tools["list_tables"].call({"stand": "dev1", "database": "orders"}).splitlines()
        assert logins == ["billing", "shipping"]
        assert lines == ["dev1 / orders", "orders.parcels | table | ~10 rows"]

    def test_a_host_that_did_not_answer_is_not_tried_again_in_the_call(
        self, tools: dict[str, Tool], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        logins: list[str] = []

        def query(self: Database, sql: str, **params: Any) -> list[tuple[Any, ...]]:  # noqa: ARG001
            assert self.settings is not None
            logins.append(f"{self.settings.database}.{self.settings.user}")
            raise DatabaseUnavailableError("Connecting to db:5432 timed out.", host_down=True)

        monkeypatch.setattr(Database, "query", query)
        lines = tools["search_schema"].call({"stand": "dev1", "text": "parcel"}).splitlines()
        assert logins == ["archive.billing"]  # orders.billing and orders.shipping share the host
        assert lines == [
            "dev1 / archive",
            "  Connecting to db:5432 timed out.",
            "dev1 / orders",
            "  Connecting to db:5432 timed out.",
        ]

    def test_list_schemas_shows_the_databases_of_a_stand(self, tools: dict[str, Tool]) -> None:
        assert tools["list_schemas"].call({"stand": "Dev1"}).splitlines() == [
            "dev1 / archive: billing",
            "dev1 / orders: billing, shipping",
        ]

    def test_one_database_failing_does_not_hide_the_others(self, pool: Pool, tools: dict[str, Tool]) -> None:
        lines = tools["list_tables"].call({"stand": "dev1"}).splitlines()
        assert [line for line in lines if not line.startswith("  ")] == ["dev1 / archive", "dev1 / orders"]
        assert len(lines) == 4
        assert pool.open_count() == 1  # the refused port was tried once, not once per login

    def test_an_unknown_database_lists_the_known_ones(self, tools: dict[str, Tool]) -> None:
        with pytest.raises(
            ToolError, match=r"Stand dev1 has no database 'hr' in the registry\. Databases: archive, orders\."
        ):
            tools["search_schema"].call({"stand": "dev1", "text": "order", "database": "hr"})

    @pytest.mark.parametrize(
        ("tool", "arguments", "message"),
        [
            ("explain_query", {"stand": "dev1", "schema": " ", "query": "SELECT 1"}, "Pass schema: on stand dev1"),
            ("explain_query", {"stand": "dev11", "schema": "billing", "query": "SELECT 1"}, "No stand 'dev11'"),
            ("search_schema", {"stand": "dev1", "text": " "}, "text must be a non-empty string"),
            ("list_schemas", {"stand": " "}, "Pass stand, one of: dev1–dev2, ift1."),
        ],
    )
    def test_wrong_arguments_are_explained(
        self, tools: dict[str, Tool], tool: str, arguments: dict[str, Any], message: str
    ) -> None:
        with pytest.raises(ToolError) as caught:
            tools[tool].call(arguments)
        assert str(caught.value).startswith(message)

    def test_a_table_search_that_cannot_connect_says_so(self, tools: dict[str, Tool]) -> None:
        with pytest.raises(ToolError) as caught:
            tools["describe_table"].call({"stand": "dev2", "table": "invoices"})
        assert "No table or view" not in str(caught.value)

    def test_a_schema_in_the_table_name_picks_the_login(self, pool: Pool, tools: dict[str, Tool]) -> None:
        with pytest.raises(ToolError):
            tools["sample_rows"].call({"stand": "dev1", "table": "shipping.parcels", "schema": ""})
        assert pool.current is not None
        assert pool.current.name == "dev1.orders.shipping"

    def test_the_query_log_names_the_stand_and_schema_of_each_call(self, pool: Pool, tmp_path: Path) -> None:
        log = tmp_path / "queries.jsonl"
        tools = by_name(build_stand_tools(pool, str(ROOT), query_log=QueryLog(str(log), pool)))
        for stand in ("dev1", "dev11"):
            with pytest.raises(ToolError):
                tools["explain_query"].call({"stand": stand, "schema": "shipping", "query": "SELECT 1"})
        first, second = (json.loads(line) for line in log.read_text(encoding="utf-8").splitlines())
        assert (first["stand"], first["schema"], first["role"], first["database"]) == (
            "dev1",
            "shipping",
            "shipping",
            "orders",
        )
        assert first["outcome"] == "unavailable"
        assert ("stand" in second, second["role"]) == (False, None)  # not the previous call's login


class TestCompareSchema:
    """compare_schema without a database: the arguments, failures that name the stand, and the catalog reading."""

    @pytest.mark.parametrize(
        ("arguments", "message"),
        [
            ({"stand_a": "dev1", "stand_b": "dev2"}, "Missing argument(s): schema."),
            ({"stand_a": " ", "stand_b": "dev2", "schema": "billing"}, "Pass stand_a, one of: dev1–dev2, ift1."),
            (
                {"stand_a": "dev1", "stand_b": "dev11", "schema": "billing"},
                "No stand 'dev11' in the registry. Similar: dev1, dev2.",
            ),
            (
                {"stand_a": "dev1", "stand_b": "DEV1 ", "schema": "billing"},
                "stand_a and stand_b are both 'dev1'. Pass two different stands.",
            ),
            (
                {"stand_a": "dev1", "stand_b": "dev2", "schema": "billing"},
                "Schema 'billing' exists in several databases of dev1: archive, orders. Pass database to pick one.",
            ),
            (
                {"stand_a": "dev1", "stand_b": "dev2", "schema": "billing", "database": "archive"},
                "Schema 'billing' of dev2 is in orders, not in 'archive'. Pass one of those as database.",
            ),
            (
                {"stand_a": "dev2", "stand_b": "dev1", "schema": "shipping"},
                "Stand dev2 has no schema 'shipping' that the server has a login for. Call list_schemas to see them.",
            ),
        ],
    )
    def test_wrong_arguments_are_explained(
        self, tools: dict[str, Tool], arguments: dict[str, Any], message: str
    ) -> None:
        with pytest.raises(ToolError) as caught:
            tools["compare_schema"].call(arguments)
        assert str(caught.value) == message

    def test_an_unreachable_stand_is_named_and_logged_as_unavailable(self, pool: Pool, tmp_path: Path) -> None:
        log = tmp_path / "queries.jsonl"
        tools = by_name(build_stand_tools(pool, str(ROOT), query_log=QueryLog(str(log), pool)))
        arguments = {"stand_a": "dev2", "stand_b": "dev1", "schema": "billing", "database": "orders"}
        with pytest.raises(ToolError) as caught:
            tools["compare_schema"].call(arguments)
        assert str(caught.value).startswith("dev2 / orders / billing: Nothing accepts connections at 127.0.0.1:")
        entry = json.loads(log.read_text(encoding="utf-8"))
        assert (entry["outcome"], entry["stand"], entry["schema"]) == ("unavailable", "dev2", "billing")

    def test_a_failed_statement_names_the_stand(self, tools: dict[str, Tool], monkeypatch: pytest.MonkeyPatch) -> None:
        def query(self: Database, sql: str, **params: Any) -> list[tuple[Any, ...]]:  # noqa: ARG001
            raise QueryFailedError("canceling statement due to statement timeout (SQLSTATE 57014)")

        monkeypatch.setattr(Database, "query", query)
        arguments = {"stand_a": "dev1", "stand_b": "dev2", "schema": "billing", "database": "orders"}
        with pytest.raises(ToolError, match=r"^dev1 / orders / billing: canceling statement due to statement timeout"):
            tools["compare_schema"].call(arguments)

    def test_a_schema_the_database_lacks_is_an_error_not_a_difference(
        self, tools: dict[str, Tool], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def query(self: Database, sql: str, **params: Any) -> list[tuple[Any, ...]]:  # noqa: ARG001
            return []

        monkeypatch.setattr(Database, "query", query)
        with pytest.raises(ToolError) as caught:
            tools["compare_schema"].call(
                {"stand_a": "dev1", "stand_b": "dev2", "schema": "billing", "database": "orders"}
            )
        assert str(caught.value) == (
            "Stand dev1 has no schema 'billing' in database orders: the registry lists it, but the database does "
            "not have it. Nothing to compare."
        )

    def test_the_structure_is_read_in_three_queries(self, pool: Pool, monkeypatch: pytest.MonkeyPatch) -> None:
        asked: list[str] = []
        index = "CREATE UNIQUE INDEX invoices_pkey ON billing.invoices USING btree (id)"

        def query(self: Database, sql: str, **params: Any) -> list[tuple[Any, ...]]:  # noqa: ARG001
            asked.append(sql)
            assert params == {"schema": "billing"}
            if sql is RELATIONS_SQL:
                return [
                    ("bare", "r", None, None, None, None),  # a table without columns
                    ("invoices", "r", "id", "integer", True, "nextval('invoices_id_seq'::regclass)"),
                    ("invoices", "r", "note", "text", False, None),
                ]
            if sql is SCHEMA_CONSTRAINTS_SQL:
                return [("invoices", "r", "invoices_pkey", "PRIMARY KEY (id)")]
            return [("invoices", "r", "invoices_pkey", index)]

        monkeypatch.setattr(Database, "query", query)
        structure = read_structure(pool, pool.registry.targets[0])
        assert asked == [RELATIONS_SQL, SCHEMA_CONSTRAINTS_SQL, SCHEMA_INDEXES_SQL]
        assert structure.target.name == "dev1.orders.billing"
        assert structure.relations == {
            "bare": Relation("r"),
            "invoices": Relation(
                "r",
                {"id": "integer NOT NULL DEFAULT nextval('invoices_id_seq'::regclass)", "note": "text"},
                {"invoices_pkey": "PRIMARY KEY (id)"},
                {"invoices_pkey": index},
            ),
        }

    def test_an_empty_schema_has_no_relations(self, pool: Pool, monkeypatch: pytest.MonkeyPatch) -> None:
        def query(self: Database, sql: str, **params: Any) -> list[tuple[Any, ...]]:  # noqa: ARG001
            return [(None,) * 6] if sql is RELATIONS_SQL else []

        monkeypatch.setattr(Database, "query", query)
        assert read_structure(pool, pool.registry.targets[0]).relations == {}


def structure(stand: str, **relations: Relation) -> Structure:
    return Structure(Target(stand, "orders", "shop", {"host": "db"}, "stands.conf"), dict(relations))


PRIMARY_KEY = "CREATE UNIQUE INDEX orders_pkey ON shop.orders USING btree (id)"
CUSTOMER_INDEX = "CREATE INDEX orders_customer_idx ON shop.orders USING btree (customer_id)"


class TestDifferences:
    """The lines of compare_schema, on structures built by hand."""

    def test_the_same_structure_is_one_line(self) -> None:
        def same(stand: str) -> Structure:
            orders = Relation("r", {"id": "integer NOT NULL", "total": "numeric(12,2)"}, {"pk": "PRIMARY KEY (id)"})
            return structure(stand, orders=orders, daily=Relation("v", {"day": "date"}))

        assert differences(same("dev7"), same("dev12")) == [
            "dev7 / orders / shop  vs  dev12 / orders / shop",
            "identical: 2 tables, 3 columns",
        ]

    def test_every_kind_of_difference_has_a_line_of_its_own(self) -> None:
        a = structure(
            "dev7",
            archive=Relation("r", {"id": "integer"}),
            items=Relation("r", {"id": "integer"}),
            orders=Relation(
                "r",
                {"id": "integer NOT NULL", "total": "numeric(12,2) NOT NULL"},
                {"orders_pkey": "PRIMARY KEY (id)", "orders_status_check": "CHECK (status = 'new')"},
                {"orders_pkey": PRIMARY_KEY, "orders_customer_idx": CUSTOMER_INDEX},
            ),
        )
        b = structure(
            "dev12",
            daily=Relation("v", {"day": "date"}),
            items=Relation("v", {"id": "integer"}),
            orders=Relation(
                "r",
                {"id": "integer NOT NULL", "total": "numeric(10,2)", "note": "text DEFAULT ''::text"},
                {
                    "orders_pkey": "PRIMARY KEY (id)",
                    "orders_status_check": "CHECK (status = 'paid')",
                    "orders_total_check": "CHECK (total > 0::numeric)",
                },
                {"orders_pkey": PRIMARY_KEY},
            ),
        )
        assert differences(a, b) == [
            "dev7 / orders / shop  vs  dev12 / orders / shop",
            "only on dev7:",
            "  archive | table",
            "only on dev12:",
            "  daily | view",
            "differ:",
            "  items: dev7 table | dev12 view",
            "  orders.note only on dev12: text DEFAULT ''::text",
            "  orders.total: dev7 numeric(12,2) NOT NULL | dev12 numeric(10,2)",
            "  orders: constraint orders_status_check: dev7 CHECK (status = 'new') | dev12 CHECK (status = 'paid')",
            "  orders: constraint orders_total_check only on dev12: CHECK (total > 0::numeric)",
            f"  orders: index orders_customer_idx only on dev7: {CUSTOMER_INDEX}",
        ]

    def test_definitions_are_compared_whole_and_shortened_only_for_display(self) -> None:
        long_check = "CHECK (" + "x" * 300 + ")"
        a = structure("dev7", orders=Relation("r", constraints={"c": long_check}))
        same = structure("dev12", orders=Relation("r", constraints={"c": long_check}))
        other = structure("dev12", orders=Relation("r", constraints={"c": long_check[:-2] + "y)"}))
        assert differences(a, same)[-1] == "identical: 1 table, 0 columns"
        line = differences(a, other)[-1]
        assert line.startswith("  orders: constraint c: dev7 CHECK (xxx")
        assert line.count("…") == 2
        assert len(line) < 2 * 200 + 40

    @pytest.mark.usefixtures("russian")
    def test_in_russian(self) -> None:
        a = structure("dev7", orders=Relation("r", {"id": "integer"}, indexes={"orders_pkey": PRIMARY_KEY}))
        b = structure("dev12", orders=Relation("r", {"id": "integer"}), log=Relation("r", {"at": "date"}))
        assert differences(a, b) == [
            "dev7 / orders / shop  vs  dev12 / orders / shop",
            "только на dev12:",
            "  log | таблица",
            "отличаются:",
            f"  orders: индекс orders_pkey только на dev7: {PRIMARY_KEY}",
        ]
        assert differences(a, structure("dev12", orders=a.relations["orders"]))[-1] == "одинаково: 1 таблица, 1 колонка"


@pytest.fixture
def silent_port() -> Iterator[str]:
    """A port that accepts TCP connections and never answers: a host behind a dropped VPN."""
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        yield str(server.getsockname()[1])


class TestSilentHost:
    """One timeout per call, not one per login and database, and the short connect timeout applies."""

    @pytest.fixture
    def pool(self, tmp_path: Path, unreachable_env: dict[str, str], silent_port: str) -> Pool:
        text = "".join(
            f"[{name}]\nhost=127.0.0.1\nport={silent_port}\nconnect_timeout=1\n\n"
            for name in ("dev1.orders.billing", "dev1.orders.shipping", "dev1.archive.billing")
        )
        return Pool(loaded(STANDS_ON, [write(tmp_path / "stands.conf", text)]), unreachable_env)

    @pytest.fixture
    def tools(self, pool: Pool) -> dict[str, Tool]:
        return by_name(build_stand_tools(pool, str(ROOT)))

    def test_the_whole_stand_costs_one_timeout(self, pool: Pool, tools: dict[str, Tool]) -> None:
        started = time.monotonic()
        lines = tools["list_tables"].call({"stand": "dev1"}).splitlines()
        assert time.monotonic() - started < 5  # one second of connect timeout, not ten of the query one
        assert pool.open_count() == 1
        assert [line.split(" timed out")[0] for line in lines] == [
            "dev1 / archive",
            f"  Connecting to 127.0.0.1:{pool.registry.targets[0].options['port']}",
            "dev1 / orders",
            f"  Connecting to 127.0.0.1:{pool.registry.targets[0].options['port']}",
        ]

    def test_status_stops_at_the_first_silent_login(self, tools: dict[str, Tool]) -> None:
        lines = tools["status"].call({"stand": "dev1"}).splitlines()
        assert len([line for line in lines if line.startswith("checked with")]) == 1
        assert "timed out" in lines[-1]

    def test_finding_a_table_gives_up_after_one_timeout(self, pool: Pool, tools: dict[str, Tool]) -> None:
        started = time.monotonic()
        with pytest.raises(ToolError, match="timed out"):
            tools["describe_table"].call({"stand": "dev1", "table": "parcels"})
        assert time.monotonic() - started < 5
        assert pool.open_count() == 1


def serve(env: dict[str, str], *requests: dict[str, Any]) -> list[dict[str, Any]]:
    stdin = io.BytesIO(b"".join(json.dumps(request).encode() + b"\n" for request in requests))
    stdout = io.BytesIO()
    assert entry.main(stdin, stdout, env=env) == 0
    return [json.loads(line) for line in stdout.getvalue().splitlines()]


class TestEntry:
    """main(): stands only when PG_MCP_STANDS asks and the registry has some, the PG* settings otherwise."""

    INITIALIZE = rpc(1, "initialize", {"protocolVersion": "2025-11-25", "capabilities": {}})
    TOOLS = rpc(2, "tools/list")
    STATUS = rpc(3, "tools/call", {"name": "status", "arguments": {}})

    @pytest.fixture
    def env(self, tmp_path: Path, unreachable_env: dict[str, str]) -> dict[str, str]:
        own = write(tmp_path / "stands.conf", sections(host="127.0.0.1", port=unreachable_env["PGPORT"]))
        return dict(
            unreachable_env, PG_MCP_STANDS="*", PG_MCP_STANDS_FILE=str(own), PGSERVICEFILE=str(tmp_path / "absent")
        )

    def answers(self, env: dict[str, str]) -> tuple[str, list[str], str]:
        initialize, tools, status = serve(env, self.INITIALIZE, self.TOOLS, self.STATUS)
        names = [tool["name"] for tool in tools["result"]["tools"]]
        return initialize["result"]["instructions"], names, status["result"]["content"][0]["text"]

    def test_one_entry_serves_every_stand(self, env: dict[str, str]) -> None:
        instructions, names, status = self.answers(env)
        assert "This server reaches many stands." in instructions
        assert names == list(STAND_PROFILES["explore"])
        assert "stands: 3 (dev1–dev2, ift1), connections in the registry: 5" in status

    def test_without_pg_mcp_stands_the_entry_works_as_before(self, env: dict[str, str]) -> None:
        del env["PG_MCP_STANDS"]
        instructions, names, status = self.answers(env)
        assert "many stands" not in instructions
        assert names == list(PROFILES["explore"])
        assert "database: unavailable" in status

    def test_no_stands_found_falls_back_to_the_pg_settings(self, env: dict[str, str], tmp_path: Path) -> None:
        write(tmp_path / "stands.conf", "")
        _, names, status = self.answers(env)
        assert names == list(PROFILES["explore"])
        assert "warning: PG_MCP_STANDS is set, but no stands were found in" in status
        assert "database: unavailable" in status

    def test_registry_problems_past_ten_are_counted(self, env: dict[str, str], tmp_path: Path) -> None:
        write(tmp_path / "stands.conf", "".join(f"[dev{number}.orders.billing]\nport=1\n" for number in range(12)))
        status = self.answers(env)[2].splitlines()
        assert len([line for line in status if line.startswith("warning: stand connection")]) == 10
        assert "warning: ... and 3 more problems in the stands registry" in status

    @pytest.mark.parametrize(("raw", "connections"), [("3", 3), ("none", 8), ("0", 8)])
    def test_pg_mcp_max_connections_limits_open_connections(
        self, env: dict[str, str], raw: str, connections: int
    ) -> None:
        status = self.answers(dict(env, PG_MCP_MAX_CONNECTIONS=raw))[2]
        assert f"at most {connections} connections stay open" in status
