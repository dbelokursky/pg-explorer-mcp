"""Many stands against a real PostgreSQL: stands emulated on one server, a login per schema (ADR 0006).

Run them with PG_MCP_IT=1 and the usual PG* variables pointing at a database where the tests may
create and drop the schemas mcp_it_billing and mcp_it_orders and the roles of the same names. Every
stand of the test registry points at that database, and each schema's role logs in with a password
from a password file of the test's own.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

import pg8000.native
import pytest

from pg_mcp.db import Settings
from pg_mcp.querylog import QueryLog
from pg_mcp.stand_tools import build_stand_tools
from pg_mcp.stands import Pool, load_registry
from pg_mcp.tools import Tool, ToolError
from tests.helpers import ROOT, unreachable_database_env

pytestmark = pytest.mark.integration

BILLING = "mcp_it_billing"
ORDERS = "mcp_it_orders"
PASSWORDS = {BILLING: "billing-secret", ORDERS: "orders-secret"}

FIXTURES = (
    f"DROP SCHEMA IF EXISTS {BILLING}, {ORDERS} CASCADE",
    f"CREATE SCHEMA {BILLING}",
    f"CREATE SCHEMA {ORDERS}",
    f"CREATE TABLE {BILLING}.invoices (id int PRIMARY KEY, amount numeric(10,2) NOT NULL)",
    f"COMMENT ON TABLE {BILLING}.invoices IS 'Счета'",
    f"INSERT INTO {BILLING}.invoices VALUES (1, 100), (2, 250.5)",
    f"CREATE TABLE {ORDERS}.orders (id int PRIMARY KEY, invoice_id int NOT NULL)",
    f"INSERT INTO {ORDERS}.orders VALUES (10, 1), (11, 2), (12, 2)",
    f"CREATE TABLE {ORDERS}.invoices (id int)",  # the same name as in billing
)


def connect() -> pg8000.native.Connection:
    s = Settings.from_env(os.environ)
    return pg8000.native.Connection(user=s.user, password=s.password, host=s.host, port=s.port, database=s.database)


def drop_role(admin: pg8000.native.Connection, role: str) -> None:
    if admin.run("SELECT 1 FROM pg_roles WHERE rolname = :name", name=role):
        admin.run(f"DROP OWNED BY {role}")
        admin.run(f"DROP ROLE {role}")


def registry_text(settings: Settings, dead_port: str) -> str:
    live = f"host={settings.host}\nport={settings.port}\ndbname={settings.database}\n"
    dead = f"host=127.0.0.1\nport={dead_port}\ndbname={settings.database}\nconnect_timeout=2\n"
    return (
        f"[dev1.shop.{BILLING}]\n{live}\n"
        f"[dev1.shop.{ORDERS}]\n{live}\n"
        f"[dev2.shop.mcp_it_aaa]\n{dead}\n"  # a login that cannot connect, first in line
        f"[dev2.shop.{BILLING}]\n{live}\n"
        f"[dev3.shop.mcp_it_ghost]\n{live}user={BILLING}\n\n"  # a schema the database does not have
        f"[dev9.shop.{BILLING}]\n{dead}\n"
    )


@pytest.fixture(scope="module")
def pool(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Pool]:
    admin = connect()
    for statement in FIXTURES:
        admin.run(statement)
    for role, password in PASSWORDS.items():
        drop_role(admin, role)
        admin.run(f"CREATE ROLE {role} LOGIN PASSWORD '{password}'")
        admin.run(f"GRANT USAGE ON SCHEMA {role} TO {role}")
        admin.run(f"GRANT SELECT ON ALL TABLES IN SCHEMA {role} TO {role}")
    folder = tmp_path_factory.mktemp("stands")
    passfile = folder / "pgpass"
    passfile.write_text("".join(f"*:*:*:{role}:{password}\n" for role, password in PASSWORDS.items()), encoding="utf-8")
    passfile.chmod(0o600)
    stands_file = folder / "stands.conf"
    dead_port = unreachable_database_env()["PGPORT"]
    stands_file.write_text(registry_text(Settings.from_env(os.environ), dead_port), encoding="utf-8")
    registry = load_registry({"PG_MCP_STANDS": "*"}, [stands_file])
    assert registry is not None
    assert registry.warnings == []
    stands = Pool(registry, dict(os.environ, PGPASSFILE=str(passfile)), limit=2)
    yield stands
    stands.close()
    admin.run(f"DROP SCHEMA {BILLING}, {ORDERS} CASCADE")
    for role in PASSWORDS:
        drop_role(admin, role)
    admin.close()


def by_name(tools: list[Tool]) -> dict[str, Tool]:
    return {tool.name: tool for tool in tools}


@pytest.fixture(scope="module")
def tools(pool: Pool) -> dict[str, Tool]:
    return by_name(build_stand_tools(pool, str(ROOT), profile="analyst"))


def lines_of(tool: Tool, **arguments: object) -> list[str]:
    return tool.call(arguments).splitlines()


def names_of(lines: list[str]) -> list[str]:
    return [line.split(" | ")[0] for line in lines]


class TestStatus:
    def test_lists_the_stands(self, tools: dict[str, Tool]) -> None:
        assert "stands: 4 (dev1–dev3, dev9), connections in the registry: 6" in lines_of(tools["status"])

    def test_checks_a_stand_with_a_login_of_its_own(self, tools: dict[str, Tool]) -> None:
        lines = lines_of(tools["status"], stand="dev1")
        assert lines[-3] == f"checked with dev1 / shop / {BILLING}"
        assert lines[-2].startswith("database: connected to ")
        assert f" as {BILLING}, PostgreSQL " in lines[-2]
        assert "read only = on" in lines[-1]

    def test_a_broken_login_does_not_hide_a_working_one(self, tools: dict[str, Tool]) -> None:
        lines = lines_of(tools["status"], stand="dev2")
        assert lines[-5] == "checked with dev2 / shop / mcp_it_aaa"
        assert lines[-4].startswith("database: unavailable.")
        assert lines[-3] == f"checked with dev2 / shop / {BILLING}"
        assert f" as {BILLING}, PostgreSQL " in lines[-2]

    def test_an_unreachable_stand_is_reported(self, tools: dict[str, Tool]) -> None:
        assert lines_of(tools["status"], stand="dev9")[-1].startswith("database: unavailable.")


class TestStructure:
    def test_list_tables_covers_the_schemas_of_the_registry(self, tools: dict[str, Tool]) -> None:
        assert names_of(lines_of(tools["list_tables"], stand="dev1")) == [
            "dev1 / shop",
            f"{BILLING}.invoices",
            f"{ORDERS}.invoices",
            f"{ORDERS}.orders",
        ]

    def test_one_schema_is_listed_with_its_own_login(self, tools: dict[str, Tool]) -> None:
        assert names_of(lines_of(tools["list_tables"], stand="dev1", schema=ORDERS)) == [
            f"dev1 / shop / {ORDERS}",
            f"{ORDERS}.invoices",
            f"{ORDERS}.orders",
        ]

    def test_the_catalog_falls_back_to_the_next_login(self, tools: dict[str, Tool]) -> None:
        assert names_of(lines_of(tools["list_tables"], stand="dev2")) == ["dev2 / shop", f"{BILLING}.invoices"]

    def test_schemas_missing_from_the_database_are_named(self, tools: dict[str, Tool]) -> None:
        assert lines_of(tools["list_tables"], stand="dev3") == [
            "dev3 / shop",
            "  No tables or views in mcp_it_ghost: these schemas are empty or missing in this database.",
        ]

    def test_an_unreachable_stand_is_a_line_not_an_error(self, tools: dict[str, Tool]) -> None:
        lines = lines_of(tools["list_tables"], stand="dev9")
        assert (len(lines), lines[0]) == (2, "dev9 / shop")

    def test_search_covers_the_whole_stand(self, tools: dict[str, Tool]) -> None:
        found = names_of(lines_of(tools["search_schema"], stand="dev1", text="invoice"))
        assert found[0] == "dev1 / shop"
        assert {f"{BILLING}.invoices", f"{ORDERS}.invoices"} <= set(found)
        assert any(name.startswith(f"{ORDERS}.orders.invoice_id") for name in found)

    def test_search_in_one_schema_uses_its_login(self, tools: dict[str, Tool]) -> None:
        found = names_of(lines_of(tools["search_schema"], stand="dev1", text="invoice", schema=ORDERS))
        assert found[0] == f"dev1 / shop / {ORDERS}"
        assert all(name.startswith(ORDERS) for name in found[1:])

    def test_database_keeps_to_one_database_whatever_the_case(self, tools: dict[str, Tool]) -> None:
        assert lines_of(tools["list_tables"], stand="dev1", database="SHOP")[0] == "dev1 / shop"

    def test_describe_finds_the_schema_of_a_table(self, tools: dict[str, Tool]) -> None:
        lines = lines_of(tools["describe_table"], stand="dev1", table="Orders")
        assert lines[0] == f"dev1 / shop / {ORDERS}"
        assert lines[1].startswith(f"{ORDERS}.orders")

    def test_a_table_in_two_schemas_asks_which(self, tools: dict[str, Tool]) -> None:
        options = rf"shop\.{BILLING}\.invoices, shop\.{ORDERS}\.invoices"
        with pytest.raises(ToolError, match=rf"'invoices' exists in several schemas of dev1: {options}\."):
            tools["describe_table"].call({"stand": "dev1", "table": "invoices"})
        assert lines_of(tools["describe_table"], stand="dev1", table=f"{BILLING}.invoices")[0] == (
            f"dev1 / shop / {BILLING}"
        )

    def test_a_missing_table_is_not_found(self, tools: dict[str, Tool]) -> None:
        with pytest.raises(ToolError, match="No table or view 'parcels' in the schemas of dev1"):
            tools["describe_table"].call({"stand": "dev1", "table": "parcels"})


class TestData:
    def test_names_resolve_in_the_schema_of_the_login(self, tools: dict[str, Tool]) -> None:
        lines = lines_of(tools["run_select"], stand="dev1", schema=BILLING, query="SELECT count(*) FROM invoices")
        assert lines[:3] == [f"dev1 / shop / {BILLING}", "count", "2"]

    def test_the_session_is_the_schema_login_and_read_only(self, tools: dict[str, Tool]) -> None:
        query = "SELECT current_user, current_setting('search_path'), current_setting('transaction_read_only')"
        assert lines_of(tools["run_select"], stand="dev1", schema=ORDERS, query=query)[2] == (
            f"{ORDERS} | {ORDERS}, public | on"
        )

    def test_a_login_reads_only_its_own_schema(self, tools: dict[str, Tool]) -> None:
        with pytest.raises(ToolError, match=f"permission denied for schema {ORDERS}"):
            tools["run_select"].call({"stand": "dev1", "schema": BILLING, "query": f"SELECT * FROM {ORDERS}.orders"})

    def test_sample_rows_reads_with_the_login_of_the_table_schema(self, tools: dict[str, Tool]) -> None:
        lines = lines_of(tools["sample_rows"], stand="dev1", table="orders", limit=2)
        assert lines[:3] == [f"dev1 / shop / {ORDERS}", f"{ORDERS}.orders:", "id | invoice_id"]

    def test_explain_runs_with_the_schema_login(self, tools: dict[str, Tool]) -> None:
        query = "SELECT * FROM invoices"
        lines = lines_of(tools["explain_query"], stand="dev1", schema=BILLING, query=query, analyze=True)
        assert lines[0] == f"dev1 / shop / {BILLING}"
        assert any("Seq Scan on invoices" in line for line in lines)


def test_only_the_recent_connections_stay_open(pool: Pool, tools: dict[str, Tool]) -> None:
    for stand, schema in (("dev1", BILLING), ("dev1", ORDERS), ("dev2", BILLING)):
        tools["run_select"].call({"stand": stand, "schema": schema, "query": "SELECT 1"})
    assert pool.open_count() == 2


def test_the_query_log_names_the_stand_schema_and_login(pool: Pool, tmp_path: Path) -> None:
    log = tmp_path / "queries.jsonl"
    tools = by_name(build_stand_tools(pool, str(ROOT), profile="analyst", query_log=QueryLog(str(log), pool)))
    tools["run_select"].call({"stand": "dev1", "schema": ORDERS, "query": "SELECT * FROM orders"})
    entry = json.loads(log.read_text(encoding="utf-8"))
    assert (entry["stand"], entry["schema"], entry["role"], entry["outcome"], entry["rows"]) == (
        "dev1",
        ORDERS,
        ORDERS,
        "ok",
        3,
    )
