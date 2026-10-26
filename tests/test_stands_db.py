"""Many stands against a real PostgreSQL: stands emulated on one server, a login per schema (ADR 0006).

Run them with PG_EXPLORER_MCP_IT=1 and the usual PG* variables pointing at a database where the tests may
create and drop the schemas mcp_it_billing and mcp_it_orders and the roles of the same names, with a
superuser login that may also create and drop the database mcp_it_stand next to it. Most stands of
the test registry point at the first database; dev4 and ift1 point at the second, where billing
holds other rows and orders is built differently, so that stands answer
differently; each schema's role logs in with a password from a password file of the test's own.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from pathlib import Path

import pg8000.native
import pytest

from pg_explorer_mcp import sqlguard
from pg_explorer_mcp.db import Settings
from pg_explorer_mcp.querylog import QueryLog
from pg_explorer_mcp.stand_tools import build_stand_tools
from pg_explorer_mcp.stands import Pool, load_registry
from pg_explorer_mcp.tools import Tool, ToolError
from tests.helpers import ROOT, unreachable_database_env

pytestmark = pytest.mark.integration

BILLING = "mcp_it_billing"
ORDERS = "mcp_it_orders"
OTHER_DATABASE = "mcp_it_stand"  # a second database: the stand that points at it answers differently
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
# The second database. Stand dev4 reads its billing: the table again, with other rows. Stand ift1 reads
# its orders, built differently, and the schema the first database lacks, for compare_schema.
OTHER_FIXTURES = (
    f"CREATE SCHEMA {BILLING}",
    f"CREATE TABLE {BILLING}.invoices (id int PRIMARY KEY, amount numeric(10,2) NOT NULL)",
    f"INSERT INTO {BILLING}.invoices VALUES (1, 10), (2, 20), (3, 30)",
    f"CREATE SCHEMA {ORDERS}",
    f"CREATE TABLE {ORDERS}.orders (id int PRIMARY KEY, invoice_id int CHECK (invoice_id > 0), note text DEFAULT '')",
    f"CREATE INDEX orders_invoice_idx ON {ORDERS}.orders (invoice_id)",
    f"CREATE TABLE {ORDERS}.shipments ()",  # a table without columns
    "CREATE SCHEMA mcp_it_ghost",  # exists, holds nothing
)


def connect(database: str | None = None) -> pg8000.native.Connection:
    s = Settings.from_env(os.environ)
    return pg8000.native.Connection(
        user=s.user, password=s.password, host=s.host, port=s.port, database=database or s.database
    )


def drop_role(admin: pg8000.native.Connection, role: str) -> None:
    if admin.run("SELECT 1 FROM pg_roles WHERE rolname = :name", name=role):
        admin.run(f"DROP OWNED BY {role}")
        admin.run(f"DROP ROLE {role}")


def registry_text(settings: Settings, dead_port: str) -> str:
    live = f"host={settings.host}\nport={settings.port}\ndbname={settings.database}\n"
    other = f"host={settings.host}\nport={settings.port}\ndbname={OTHER_DATABASE}\n"
    dead = f"host=127.0.0.1\nport={dead_port}\ndbname={settings.database}\nconnect_timeout=2\n"
    return (
        f"[dev1.shop.{BILLING}]\n{live}\n"
        f"[dev1.shop.{ORDERS}]\n{live}\n"
        f"[dev2.shop.mcp_it_aaa]\n{live}\n"  # a role the server does not have, first in line
        f"[dev2.shop.{BILLING}]\n{live}\n"
        f"[dev3.shop.mcp_it_ghost]\n{live}user={BILLING}\n\n"  # a schema the database does not have
        f"[dev4.shop.{BILLING}]\n{other}\n"  # the same schema in the second database: other rows
        f"[dev9.shop.{BILLING}]\n{dead}\n"
        f"[ift1.shop.{ORDERS}]\n{other}\n"  # the second database again: the same schema built differently
        f"[ift1.shop.mcp_it_ghost]\n{other}user={ORDERS}\n\n"  # what the first database lacks
    )


@pytest.fixture(scope="module")
def pool(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Pool]:
    admin = connect()
    admin.run(f"DROP DATABASE IF EXISTS {OTHER_DATABASE} WITH (FORCE)")  # its grants would keep the roles alive
    for statement in FIXTURES:
        admin.run(statement)
    for role, password in PASSWORDS.items():
        drop_role(admin, role)
        admin.run(f"CREATE ROLE {role} LOGIN PASSWORD '{password}'")
    # As the Windows job creates its database: the cluster's own encoding may not be UTF-8.
    admin.run(f"CREATE DATABASE {OTHER_DATABASE} TEMPLATE template0 ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C'")
    other = connect(OTHER_DATABASE)
    for statement in OTHER_FIXTURES:
        other.run(statement)
    for database in (admin, other):  # the roles belong to the cluster, their privileges to each database
        for role in PASSWORDS:
            database.run(f"GRANT USAGE ON SCHEMA {role} TO {role}")
            database.run(f"GRANT SELECT ON ALL TABLES IN SCHEMA {role} TO {role}")
    other.close()
    folder = tmp_path_factory.mktemp("stands")
    passfile = folder / "pgpass"
    passfile.write_text("".join(f"*:*:*:{role}:{password}\n" for role, password in PASSWORDS.items()), encoding="utf-8")
    passfile.chmod(0o600)
    stands_file = folder / "stands.conf"
    dead_port = unreachable_database_env()["PGPORT"]
    stands_file.write_text(registry_text(Settings.from_env(os.environ), dead_port), encoding="utf-8")
    registry = load_registry({"PG_EXPLORER_MCP_STANDS": "*"}, [stands_file])
    assert registry is not None
    assert registry.warnings == []
    stands = Pool(registry, dict(os.environ, PGPASSFILE=str(passfile)), limit=2)
    yield stands
    stands.close()
    admin.run(f"DROP DATABASE {OTHER_DATABASE} WITH (FORCE)")
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
        assert "stands: 6 (dev1–dev4, dev9, ift1), connections in the registry: 9" in lines_of(tools["status"])

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


class TestCompareSchema:
    """One schema on two stands: ift1 reads the second database, where orders is built differently."""

    def test_differences_are_listed_by_table(self, tools: dict[str, Tool]) -> None:
        index = f"CREATE INDEX orders_invoice_idx ON {ORDERS}.orders USING btree (invoice_id)"
        assert lines_of(tools["compare_schema"], stand_a="dev1", stand_b="ift1", schema=ORDERS) == [
            f"dev1 / shop / {ORDERS}  vs  ift1 / shop / {ORDERS}",
            "only on dev1:",
            f"  {ORDERS}.invoices | table",
            "only on ift1:",
            f"  {ORDERS}.shipments | table",
            "differ:",
            f"  {ORDERS}.orders.invoice_id: dev1 integer NOT NULL | ift1 integer",
            f"  {ORDERS}.orders.note only on ift1: text DEFAULT ''::text",
            f"  {ORDERS}.orders: constraint orders_invoice_id_check only on ift1: CHECK (invoice_id > 0)",
            f"  {ORDERS}.orders: index orders_invoice_idx only on ift1: {index}",
        ]

    def test_many_stands_against_a_reference(self, tools: dict[str, Tool]) -> None:
        lines = lines_of(tools["compare_schema"], stand_a="dev1", stands="dev*", schema=BILLING)
        assert lines[0] == f"dev1 / shop / {BILLING}  vs  4 stands:"
        assert f"dev2 / shop / {BILLING}: same as dev1" in lines
        assert f"dev4 / shop / {BILLING}: same as dev1" in lines
        assert "no schema" in lines[lines.index("dev3") + 1]  # the registry has no login for billing there
        assert any(line.startswith(f"  dev9 / shop / {BILLING}: ") for line in lines)
        assert lines[-1] == "same as dev1: 2; different: 0; not checked: 2"

    def test_a_few_stands_get_the_full_differences(self, tools: dict[str, Tool]) -> None:
        lines = lines_of(tools["compare_schema"], stand_a="dev1", stands="ift1", schema=ORDERS)
        assert lines[0] == f"dev1 / shop / {ORDERS}  vs  1 stands:"
        assert lines[1] == f"ift1 / shop / {ORDERS}"
        assert f"  {ORDERS}.orders.invoice_id: dev1 integer NOT NULL | ift1 integer" in lines
        assert lines[-1] == "same as dev1: 0; different: 1; not checked: 0"

    @pytest.mark.parametrize("stand_b", ["dev2", "dev4"])  # the same database, and the other one with other rows
    def test_the_same_structure_is_one_line_whatever_the_rows(self, tools: dict[str, Tool], stand_b: str) -> None:
        assert lines_of(tools["compare_schema"], stand_a="dev1", stand_b=stand_b, schema=BILLING) == [
            f"dev1 / shop / {BILLING}  vs  {stand_b} / shop / {BILLING}",
            "identical: 1 table, 2 columns",
        ]

    @pytest.mark.parametrize(("stand_a", "stand_b"), [("dev3", "ift1"), ("ift1", "dev3")])
    def test_a_schema_the_database_lacks_is_an_error(self, tools: dict[str, Tool], stand_a: str, stand_b: str) -> None:
        # ift1 has the schema, empty; the database of dev3 has no such schema at all
        with pytest.raises(ToolError) as caught:
            tools["compare_schema"].call({"stand_a": stand_a, "stand_b": stand_b, "schema": "mcp_it_ghost"})
        assert str(caught.value) == (
            "Stand dev3 has no schema 'mcp_it_ghost' in database shop: the registry lists it, but the database "
            "does not have it. Nothing to compare."
        )

    def test_an_unreachable_stand_is_named(self, tools: dict[str, Tool]) -> None:
        with pytest.raises(ToolError) as caught:
            tools["compare_schema"].call({"stand_a": "dev1", "stand_b": "dev9", "schema": BILLING})
        # Windows answers a closed local port with a timeout rather than a refusal; the stand is named either way.
        assert str(caught.value).startswith(f"dev9 / shop / {BILLING}: ")
        assert "127.0.0.1:" in str(caught.value)


class TestData:
    def test_an_answer_without_rows_names_the_other_stands_with_the_schema(self, tools: dict[str, Tool]) -> None:
        query = "SELECT * FROM invoices WHERE id = 99"
        assert lines_of(tools["run_select"], stand="dev1", schema=BILLING, query=query)[-2:] == [
            "(0 rows)",
            f"Schema {BILLING} is on dev2, dev4, dev9 as well; pass stands to ask up to 100 stands in one call.",
        ]

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

    def test_a_guessed_column_and_a_guessed_schema_get_the_names_there_are(self, tools: dict[str, Tool]) -> None:
        with pytest.raises(ToolError, match=f"42703.* {ORDERS}\\.orders: id, invoice_id\\."):
            tools["run_select"].call({"stand": "dev1", "schema": ORDERS, "query": "SELECT order_id FROM orders"})
        with pytest.raises(ToolError, match=f"no schema 'public' .* Its schemas: {BILLING}, {ORDERS}\\."):
            tools["run_select"].call({"stand": "dev1", "schema": "public", "query": "SELECT 1"})

    def test_a_table_of_another_schema_is_pointed_to(self, tools: dict[str, Tool]) -> None:
        with pytest.raises(ToolError, match=f"42P01.* in schema {ORDERS}: call again with that schema"):
            tools["run_select"].call({"stand": "dev1", "schema": BILLING, "query": "SELECT count(*) FROM orders"})

    def test_sample_rows_reads_with_the_login_of_the_table_schema(self, tools: dict[str, Tool]) -> None:
        lines = lines_of(tools["sample_rows"], stand="dev1", table="orders", limit=2)
        assert lines[:3] == [f"dev1 / shop / {ORDERS}", f"{ORDERS}.orders:", "id | invoice_id"]

    def test_explain_runs_with_the_schema_login(self, tools: dict[str, Tool]) -> None:
        query = "SELECT * FROM invoices"
        lines = lines_of(tools["explain_query"], stand="dev1", schema=BILLING, query=query, analyze=True)
        assert lines[0] == f"dev1 / shop / {BILLING}"
        assert any("Seq Scan on invoices" in line for line in lines)


class TestManyStands:
    """stands: the same request on several stands, each answering under its own label."""

    def test_the_same_query_answers_stand_by_stand(self, tools: dict[str, Tool]) -> None:
        query = "SELECT count(*) FROM invoices"
        assert lines_of(tools["run_select"], stands="dev4,dev1", schema=BILLING, query=query) == [
            f"dev1 / shop / {BILLING}",
            "count",
            "2",
            "(1 row)",
            f"dev4 / shop / {BILLING}",
            "count",
            "3",
            "(1 row)",
        ]

    def test_a_pattern_asks_every_stand_and_a_failure_is_one_line(self, tools: dict[str, Tool]) -> None:
        query = "SELECT count(*) FROM invoices"
        lines = lines_of(tools["run_select"], stands="dev*", schema=BILLING, query=query)
        first, second = lines[:4], lines[4:8]
        assert first == [f"dev1 / shop / {BILLING}", "count", "2", "(1 row)"]
        assert second == [f"dev2 / shop / {BILLING}", "count", "2", "(1 row)"]
        assert lines[8] == "dev3"
        assert lines[9].startswith(f"  Stand dev3 has no schema '{BILLING}' that the server has a login for.")
        assert lines[10:15] == [f"dev4 / shop / {BILLING}", "count", "3", "(1 row)", f"dev9 / shop / {BILLING}"]
        assert lines[15].startswith("  ")
        assert len(lines) == 16

    def test_the_rows_are_shared_among_the_stands(self, tools: dict[str, Tool]) -> None:
        query = "SELECT n FROM generate_series(1, 100) AS n"
        lines = lines_of(tools["run_select"], stands="dev1,dev2,dev4", schema=BILLING, query=query)
        more = "(66 rows shown, there are more: narrow the query with WHERE, ORDER BY or LIMIT)"
        assert lines.count(more) == 3
        assert len(lines) == 3 * (2 + 66 + 1)

    def test_a_query_that_fails_on_a_stand_is_one_line_there(self, pool: Pool, tools: dict[str, Tool]) -> None:
        query = f"SELECT count(*) FROM {ORDERS}.orders"  # billing may not read orders on either stand
        lines = lines_of(tools["run_select"], stands="dev1,dev4", schema=BILLING, query=query)
        assert (lines[0], lines[2], len(lines)) == (f"dev1 / shop / {BILLING}", f"dev4 / shop / {BILLING}", 4)
        assert all(f"permission denied for schema {ORDERS}" in line for line in (lines[1], lines[3]))
        assert pool.outcomes == {"dev1": "error", "dev4": "error"}

    def test_search_covers_each_stand_database_by_database(self, tools: dict[str, Tool]) -> None:
        found = names_of(lines_of(tools["search_schema"], stands="dev1,dev4", text="invoice"))
        split = found.index("dev4 / shop")
        assert found[0] == "dev1 / shop"
        assert {f"{BILLING}.invoices", f"{ORDERS}.invoices"} <= set(found[1:split])
        assert found[split + 1 :] == [f"{BILLING}.invoices"]

    def test_search_in_one_schema_uses_the_login_of_each_stand(self, tools: dict[str, Tool]) -> None:
        found = names_of(lines_of(tools["search_schema"], stands="dev1,dev4", text="invoice", schema=BILLING))
        assert found == [
            f"dev1 / shop / {BILLING}",
            f"{BILLING}.invoices",
            f"dev4 / shop / {BILLING}",
            f"{BILLING}.invoices",
        ]

    def test_stands_without_rows_are_folded_into_one_line(self, tools: dict[str, Tool]) -> None:
        """Asked which stand has a row, the model reads the stand that has it and one line naming the rest."""
        arguments = {"stands": "dev1,dev4", "schema": BILLING}
        assert lines_of(tools["run_select"], query="SELECT id FROM invoices WHERE id = 3", **arguments) == [
            f"dev4 / shop / {BILLING}",
            "id",
            "3",
            "(1 row)",
            "no rows on dev1",
        ]
        assert lines_of(tools["run_select"], query="SELECT id FROM invoices WHERE id = 99", **arguments) == [
            "no rows on dev1, dev4"
        ]
        assert lines_of(tools["search_schema"], text="zzz", **arguments) == ["nothing matches on dev1, dev4"]
        assert lines_of(tools["search_schema"], text="invoice", **arguments)[0] == f"dev1 / shop / {BILLING}"
        assert lines_of(tools["run_select"], stands="dev1", schema=BILLING, query="SELECT 1 WHERE false")[-1] == (
            "(0 rows)"  # one stand keeps its header: there is nothing to fold it into
        )

    def test_twenty_stands_are_asked_at_once(self, pool: Pool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Twenty stands that each take a second answer in about a second, not twenty."""
        monkeypatch.delitem(sqlguard.FORBIDDEN_FUNCTIONS, "pg_sleep")  # here it stands in for a slow stand
        settings = Settings.from_env(os.environ)
        live = f"host={settings.host}\nport={settings.port}\ndbname={settings.database}\n"
        stands_file = tmp_path / "many.conf"
        stands_file.write_text("".join(f"[dev{n}.shop.{BILLING}]\n{live}\n" for n in range(1, 21)), encoding="utf-8")
        registry = load_registry({"PG_EXPLORER_MCP_STANDS": "*"}, [stands_file])
        assert registry is not None
        passfile = tmp_path / "pgpass"
        passfile.write_text(f"*:*:*:{BILLING}:{PASSWORDS[BILLING]}\n", encoding="utf-8")
        passfile.chmod(0o600)
        many = Pool(registry, dict(os.environ, PGPASSFILE=str(passfile)), limit=2)
        try:
            tools = by_name(build_stand_tools(many, str(ROOT), profile="analyst"))
            started = time.monotonic()
            lines = lines_of(tools["run_select"], stands="dev*", schema=BILLING, query="SELECT pg_sleep(1), 1")
            elapsed = time.monotonic() - started
            assert elapsed < 8, elapsed  # twenty seconds one after another; here all twenty wait together
            assert [line for line in lines if line.startswith("dev")] == [
                f"dev{n} / shop / {BILLING}" for n in range(1, 21)
            ]
            assert set(many.outcomes.values()) == {"ok"}
            assert many.open_count() == 2  # the twenty connections of the call, trimmed to the limit after it
        finally:
            many.close()
        assert pool.open_count() <= pool.limit

    def test_a_list_in_stand_asks_those_stands(self, tools: dict[str, Tool]) -> None:
        arguments = {"schema": BILLING, "query": "SELECT count(*) FROM invoices"}
        lines = lines_of(tools["run_select"], stand="dev1,dev4", **arguments)
        assert lines == lines_of(tools["run_select"], stands="dev1,dev4", **arguments)
        assert lines == [
            f"dev1 / shop / {BILLING}",
            "count",
            "2",
            "(1 row)",
            f"dev4 / shop / {BILLING}",
            "count",
            "3",
            "(1 row)",
        ]

    def test_an_unreachable_stand_does_not_hide_the_others(self, tools: dict[str, Tool]) -> None:
        found = names_of(lines_of(tools["search_schema"], stands="dev9,dev4", text="invoice", schema=BILLING))
        assert found[:3] == [f"dev4 / shop / {BILLING}", f"{BILLING}.invoices", f"dev9 / shop / {BILLING}"]
        assert found[3].startswith("  ")
        assert len(found) == 4

    def test_the_query_log_has_one_line_with_the_outcome_of_each_stand(self, pool: Pool, tmp_path: Path) -> None:
        log = tmp_path / "queries.jsonl"
        tools = by_name(build_stand_tools(pool, str(ROOT), profile="analyst", query_log=QueryLog(str(log), pool)))
        tools["run_select"].call({"stands": "dev1,dev4,dev9", "schema": BILLING, "query": "SELECT * FROM invoices"})
        (entry,) = (json.loads(line) for line in log.read_text(encoding="utf-8").splitlines())
        assert entry["stands"] == {"dev1": "ok", "dev4": "ok", "dev9": "unavailable"}
        assert (entry["outcome"], entry["rows"], entry["role"], entry["database"]) == ("ok", 5, None, None)
        assert "stand" not in entry


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
