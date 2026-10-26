"""Integration tests against a real PostgreSQL.

Run them with PG_EXPLORER_MCP_IT=1 and the usual PG* variables pointing at a database
where the tests may create and drop the schemas mcp_it, mcp_it_archive and
mcp_it_role and the roles mcp_it_agent and mcp_it_login.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pg8000.exceptions
import pg8000.native
import pytest

from pg_explorer_mcp.db import Database, DatabaseUnavailableError, QueryFailedError, Settings
from pg_explorer_mcp.querylog import QueryLog
from pg_explorer_mcp.tools import Tool, ToolError, build_tools
from tests.helpers import ROOT

pytestmark = pytest.mark.integration

SCHEMA = "mcp_it"  # PostgreSQL reserves the pg_ prefix for system schemas
SLOW_STATEMENT = "SELECT count(*) FROM generate_series(1, 1000000000)"  # minutes of work, no pg_sleep needed
ARCHIVE = "mcp_it_archive"

FIXTURES = (
    f"DROP SCHEMA IF EXISTS {SCHEMA}, {ARCHIVE} CASCADE",
    f"CREATE SCHEMA {SCHEMA}",
    f"CREATE SCHEMA {ARCHIVE}",
    f"CREATE TABLE {SCHEMA}.customers (id int PRIMARY KEY, email text NOT NULL UNIQUE)",
    f"""CREATE TABLE {SCHEMA}.orders (
        id serial PRIMARY KEY,
        customer_id int NOT NULL REFERENCES {SCHEMA}.customers (id),
        total numeric(12,2) DEFAULT 0,
        note text)""",
    f"COMMENT ON TABLE {SCHEMA}.orders IS 'Заказы клиентов'",
    f"COMMENT ON COLUMN {SCHEMA}.orders.customer_id IS 'Покупатель'",
    f"CREATE INDEX orders_customer_idx ON {SCHEMA}.orders (customer_id)",
    f"INSERT INTO {SCHEMA}.customers SELECT g, 'c' || g || '@example.com' FROM generate_series(1, 10) g",
    f"INSERT INTO {SCHEMA}.orders (customer_id, total) SELECT 1 + g % 10, g * 10 FROM generate_series(1, 1000) g",
    f"ANALYZE {SCHEMA}.customers",
    f"ANALYZE {SCHEMA}.orders",
    f"CREATE VIEW {SCHEMA}.big_orders AS SELECT * FROM {SCHEMA}.orders WHERE total > 5000",
    f"CREATE TABLE {ARCHIVE}.orders (id int)",
    # Cyrillic names, unquoted as people write them: PostgreSQL folds only ASCII letters to lower case.
    f"CREATE TABLE {SCHEMA}.заказы (номер int PRIMARY KEY, сумма numeric(10,2) NOT NULL)",
    f"COMMENT ON TABLE {SCHEMA}.заказы IS 'Заказы по-русски'",
    f"COMMENT ON COLUMN {SCHEMA}.заказы.сумма IS 'Сумма в рублях'",
    f"INSERT INTO {SCHEMA}.заказы VALUES (1, 100), (2, 250.5), (3, 1000)",
    f"ANALYZE {SCHEMA}.заказы",
)


def connect(**overrides: str) -> pg8000.native.Connection:
    s = Settings.from_env({**os.environ, **overrides})
    return pg8000.native.Connection(user=s.user, password=s.password, host=s.host, port=s.port, database=s.database)


def env_with(**overrides: str | None) -> dict[str, str]:
    env = dict(os.environ)
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


def by_name(tools: list[Tool]) -> dict[str, Tool]:
    return {tool.name: tool for tool in tools}


@pytest.fixture(scope="module")
def admin() -> Iterator[pg8000.native.Connection]:
    connection = connect()
    yield connection
    connection.close()


@pytest.fixture(scope="module")
def db(admin: pg8000.native.Connection) -> Iterator[Database]:
    for statement in FIXTURES:
        admin.run(statement)
    database = Database()
    yield database
    database.close()
    admin.run(f"DROP SCHEMA {SCHEMA}, {ARCHIVE} CASCADE")


@pytest.fixture(scope="module")
def tools(db: Database) -> dict[str, Tool]:
    return by_name(build_tools(db, str(ROOT)))


@pytest.fixture(scope="module")
def analyst(db: Database) -> dict[str, Tool]:
    return by_name(build_tools(db, str(ROOT), profile="analyst"))


def lines_of(tool: Tool, **arguments: object) -> list[str]:
    return tool.call(arguments).splitlines()


def never_analyzed(db: Database) -> str:
    """How list_tables describes a table without statistics on this server."""
    version = int(db.query("SELECT current_setting('server_version_num')")[0][0])
    return "rows unknown (never analyzed)" if version >= 140000 else "empty or never analyzed"


class TestStructure:
    def test_status_reports_a_read_only_session(self, tools: dict[str, Tool]) -> None:
        text = tools["status"].call({})
        assert "database: connected" in text
        assert "read only = on" in text
        assert (
            "profile: explore (status, list_tables, search_schema, describe_table, sample_rows, explain_query)" in text
        )

    def test_list_tables_shows_tables_views_estimates_and_comments(self, tools: dict[str, Tool]) -> None:
        assert lines_of(tools["list_tables"], schema=SCHEMA) == [
            f"{SCHEMA}.big_orders | view",
            f"{SCHEMA}.customers | table | ~10 rows",
            f"{SCHEMA}.orders | table | ~1k rows | Заказы клиентов",
            f"{SCHEMA}.заказы | table | ~3 rows | Заказы по-русски",
        ]

    def test_a_table_without_statistics_says_so(self, db: Database, tools: dict[str, Tool]) -> None:
        # Before PostgreSQL 14 an empty table and a never analyzed one look the same.
        assert lines_of(tools["list_tables"], schema=ARCHIVE) == [f"{ARCHIVE}.orders | table | {never_analyzed(db)}"]

    def test_a_schema_without_tables_brings_the_tables_of_every_schema(self, tools: dict[str, Tool]) -> None:
        lines = lines_of(tools["list_tables"], schema="no_such_schema")
        assert lines[0] == "Schema 'no_such_schema' has no tables, so these are the readable tables of all schemas:"
        assert f"{SCHEMA}.orders | table | ~1k rows | Заказы клиентов" in lines

    def test_a_schema_without_tables_does_not_hide_the_table_a_name_means(self, tools: dict[str, Tool]) -> None:
        first = f"{SCHEMA}.orders | table | ~1k rows | Заказы клиентов"
        assert lines_of(tools["describe_table"], table=f"{SCHEMA}.orders", schema="no_such_schema")[0] == first
        assert lines_of(tools["sample_rows"], table=f"{SCHEMA}.customers", schema="public")[0] == f"{SCHEMA}.customers:"
        with pytest.raises(ToolError, match=f"orders.*{SCHEMA}.orders, {ARCHIVE}.orders"):  # two tables of that name
            tools["describe_table"].call({"table": "orders", "schema": "no_such_schema"})

    def test_a_system_schema_is_left_out_and_not_called_closed(self, tools: dict[str, Tool]) -> None:
        with pytest.raises(ToolError, match="Schema 'pg_catalog' is a system one"):
            tools["list_tables"].call({"schema": "pg_catalog"})

    def test_describe_table_shows_columns_keys_and_indexes(self, tools: dict[str, Tool]) -> None:
        lines = lines_of(tools["describe_table"], table=f"{SCHEMA}.orders")
        assert lines[0] == f"{SCHEMA}.orders | table | ~1k rows | Заказы клиентов"
        assert lines[1] == "columns:"
        assert lines[2].startswith("  id integer NOT NULL DEFAULT nextval(")
        assert lines[3:6] == [
            "  customer_id integer NOT NULL  -- Покупатель",
            "  total numeric(12,2) DEFAULT 0",
            "  note text",
        ]
        assert lines[6:9] == [
            "constraints:",
            "  orders_pkey: PRIMARY KEY (id)",
            f"  orders_customer_id_fkey: FOREIGN KEY (customer_id) REFERENCES {SCHEMA}.customers(id)",
        ]
        assert lines[9] == "indexes:"
        assert f"  CREATE INDEX orders_customer_idx ON {SCHEMA}.orders USING btree (customer_id)" in lines[10:]

    def test_describe_table_lists_tables_that_reference_it(self, tools: dict[str, Tool]) -> None:
        lines = lines_of(tools["describe_table"], table="customers", schema=SCHEMA)
        assert "  customers_email_key: UNIQUE (email)" in lines
        position = lines.index("referenced by:")
        assert lines[position + 1] == f"  {SCHEMA}.orders: FOREIGN KEY (customer_id) REFERENCES {SCHEMA}.customers(id)"

    def test_describe_view_has_columns_and_nothing_else(self, tools: dict[str, Tool]) -> None:
        lines = lines_of(tools["describe_table"], table=f"{SCHEMA}.big_orders")
        assert lines[0] == f"{SCHEMA}.big_orders | view"
        assert len(lines) == 2 + 4  # header, "columns:", four columns

    def test_upper_case_name_finds_the_table(self, tools: dict[str, Tool]) -> None:
        assert (
            lines_of(tools["describe_table"], table="Customers", schema=SCHEMA)[0]
            == f"{SCHEMA}.customers | table | ~10 rows"
        )

    @pytest.mark.parametrize(
        ("arguments", "message"),
        [
            ({"table": "orders"}, f"{SCHEMA}.orders, {ARCHIVE}.orders"),
            (
                {"table": "order", "schema": SCHEMA},
                f"Similar names: {SCHEMA}.big_orders, {SCHEMA}.orders, {ARCHIVE}.orders",
            ),
            ({"table": "zzz"}, "Call list_tables"),
        ],
        ids=["ambiguous", "similar", "nothing-similar"],
    )
    def test_names_that_do_not_pick_one_table_get_a_hint(
        self, tools: dict[str, Tool], arguments: dict[str, str], message: str
    ) -> None:
        with pytest.raises(ToolError, match=message):
            tools["describe_table"].call(arguments)


class TestSearch:
    def test_a_schema_without_tables_is_searched_as_every_schema(self, tools: dict[str, Tool]) -> None:
        lines = lines_of(tools["search_schema"], text="заказ", schema="no_such_schema")
        assert lines[0] == "Schema 'no_such_schema' has no tables, so the search went through all schemas:"
        assert f"{SCHEMA}.orders | table | ~1k rows | Заказы клиентов" in lines

    def test_finds_tables_by_name_or_comment_in_either_alphabet(self, tools: dict[str, Tool]) -> None:
        assert lines_of(tools["search_schema"], text="заказ", schema=SCHEMA) == [
            f"{SCHEMA}.orders | table | ~1k rows | Заказы клиентов",
            f"{SCHEMA}.заказы | table | ~3 rows | Заказы по-русски",
        ]

    def test_finds_columns_by_name_or_comment(self, tools: dict[str, Tool]) -> None:
        assert lines_of(tools["search_schema"], text="TOTAL", schema=SCHEMA) == [
            f"{SCHEMA}.big_orders.total numeric(12,2)",
            f"{SCHEMA}.orders.total numeric(12,2)",
        ]
        assert lines_of(tools["search_schema"], text="покупател", schema=SCHEMA) == [
            f"{SCHEMA}.orders.customer_id integer NOT NULL  -- Покупатель"
        ]

    def test_wildcards_in_the_text_match_literally(self, tools: dict[str, Tool]) -> None:
        assert lines_of(tools["search_schema"], text="r_d", schema=SCHEMA)[0].startswith("Nothing matches 'r_d'.")


class TestData:
    def test_sample_rows_returns_a_few_rows_of_the_chosen_columns(self, tools: dict[str, Tool]) -> None:
        lines = lines_of(tools["sample_rows"], table=f"{SCHEMA}.orders", columns="id, TOTAL", limit=3)
        assert lines[:2] == [f"{SCHEMA}.orders:", "id | total"]
        assert len(lines) == 2 + 3 + 1
        assert lines[-1].startswith("(3 rows shown, there are more")

    @pytest.mark.parametrize(
        ("arguments", "message"),
        [
            ({"columns": "id, amount"}, "No column 'amount'.*Columns: id, customer_id, total, note"),
            ({"limit": 500}, "limit must be from 1 to 50"),
            ({"limit": "lots"}, "limit must be a whole number"),
            ({"columns": 7}, "columns must be a comma-separated string"),
        ],
    )
    def test_sample_rows_explains_bad_arguments(
        self, tools: dict[str, Tool], arguments: dict[str, object], message: str
    ) -> None:
        with pytest.raises(ToolError, match=message):
            tools["sample_rows"].call({"table": f"{SCHEMA}.orders", **arguments})

    def test_explain_query_shows_a_plan_without_running_the_query(self, tools: dict[str, Tool]) -> None:
        text = tools["explain_query"].call({"query": f"SELECT * FROM {SCHEMA}.orders WHERE customer_id = 3"})
        assert "Scan" in text
        assert "actual time" not in text

    def test_explain_query_takes_only_row_sources(self, tools: dict[str, Tool]) -> None:
        with pytest.raises(ToolError, match="without EXPLAIN"):
            tools["explain_query"].call({"query": "SHOW work_mem"})

    def test_explain_analyze_is_offered_only_to_analysts(
        self, tools: dict[str, Tool], analyst: dict[str, Tool]
    ) -> None:
        query = f"SELECT count(*) FROM {SCHEMA}.orders"
        with pytest.raises(ToolError, match=r"Unknown argument\(s\): analyze"):
            tools["explain_query"].call({"query": query, "analyze": True})
        assert "actual time" in analyst["explain_query"].call({"query": query, "analyze": "yes"})
        with pytest.raises(ToolError, match="analyze must be true or false"):
            analyst["explain_query"].call({"query": query, "analyze": "maybe"})

    def test_run_select_returns_rows_with_a_count(self, analyst: dict[str, Tool]) -> None:
        assert lines_of(analyst["run_select"], query=f"SELECT id, total FROM {SCHEMA}.orders ORDER BY id LIMIT 2") == [
            "id | total",
            "1 | 10.00",
            "2 | 20.00",
            "(2 rows)",
        ]

    def test_run_select_shows_values_of_every_kind(self, analyst: dict[str, Tool]) -> None:
        query = "SELECT NULL AS n, true AS b, '\\xdead'::bytea AS raw, '{\"a\": [1]}'::jsonb AS doc"
        assert lines_of(analyst["run_select"], query=query)[1] == 'NULL | true | \\xdead | {"a": [1]}'

    def test_run_select_returns_at_most_200_rows(self, analyst: dict[str, Tool]) -> None:
        lines = lines_of(analyst["run_select"], query="SELECT g FROM generate_series(1, 1000) AS g")
        assert len(lines) == 1 + 200 + 1
        assert lines[-1].startswith("(200 rows shown, there are more")

    def test_run_select_answers_show_and_explain(self, analyst: dict[str, Tool]) -> None:
        assert lines_of(analyst["run_select"], query="SHOW statement_timeout") == ["statement_timeout", "5s", "(1 row)"]
        assert "Result" in analyst["run_select"].call({"query": "EXPLAIN SELECT 1"})

    @pytest.mark.parametrize(
        "query",
        [
            f"DROP TABLE {SCHEMA}.orders",
            f"DELETE FROM {SCHEMA}.orders",
            f"WITH gone AS (DELETE FROM {SCHEMA}.orders RETURNING *) SELECT count(*) FROM gone",
            "SELECT set_config('default_transaction_read_only', 'off', false)",
            f"SELECT 1; DROP TABLE {SCHEMA}.orders",
            f"SELECT * FROM {SCHEMA}.orders FOR UPDATE",
        ],
    )
    def test_run_select_refuses_every_kind_of_write(self, analyst: dict[str, Tool], query: str) -> None:
        with pytest.raises(ToolError):
            analyst["run_select"].call({"query": query})
        assert lines_of(analyst["run_select"], query=f"SELECT count(*) FROM {SCHEMA}.orders")[1] == "1000"

    def test_database_errors_reach_the_model_as_text(self, analyst: dict[str, Tool]) -> None:
        with pytest.raises(ToolError, match="SQLSTATE 42P01"):
            analyst["run_select"].call({"query": "SELECT * FROM no_such_table"})

    def test_a_table_without_its_schema_is_pointed_to(self, analyst: dict[str, Tool]) -> None:
        hint = rf"42P01.* readable as .*{SCHEMA}\.orders.*: write the name with its schema\."
        with pytest.raises(ToolError, match=hint):
            analyst["run_select"].call({"query": "SELECT count(*) FROM orders"})
        with pytest.raises(ToolError, match=hint):
            analyst["explain_query"].call({"query": "SELECT * FROM orders"})

    def test_a_guessed_column_brings_the_columns_of_the_table(self, analyst: dict[str, Tool]) -> None:
        hint = rf"42703.* Columns of the tables the query reads: {SCHEMA}\.orders: id, customer_id, total, note\."
        with pytest.raises(ToolError, match=hint):
            analyst["run_select"].call({"query": f"SELECT order_id FROM {SCHEMA}.orders"})


@pytest.mark.usefixtures("russian")
class TestRussian:
    """The same tools in Russian, on a table whose names are Cyrillic as well."""

    def test_status_is_in_russian(self, tools: dict[str, Tool]) -> None:
        text = tools["status"].call({})
        assert "база: подключено к " in text
        assert "лимиты: только чтение = on" in text
        assert "доступ: только чтение. Добавить, изменить или удалить данные через этот сервер нельзя" in text

    def test_list_tables_names_kinds_and_estimates_in_russian(self, tools: dict[str, Tool]) -> None:
        assert lines_of(tools["list_tables"], schema=SCHEMA) == [
            f"{SCHEMA}.big_orders | представление",
            f"{SCHEMA}.customers | таблица | ~10 строк",
            f"{SCHEMA}.orders | таблица | ~1 тыс. строк | Заказы клиентов",
            f"{SCHEMA}.заказы | таблица | ~3 строки | Заказы по-русски",
        ]

    def test_describe_finds_a_cyrillic_name_written_with_a_capital(self, tools: dict[str, Tool]) -> None:
        lines = lines_of(tools["describe_table"], table="Заказы", schema=SCHEMA)
        assert lines[:4] == [
            f"{SCHEMA}.заказы | таблица | ~3 строки | Заказы по-русски",
            "колонки:",
            "  номер integer NOT NULL",
            "  сумма numeric(10,2) NOT NULL  -- Сумма в рублях",
        ]
        assert lines[4] == "ограничения:"
        assert "индексы:" in lines

    def test_sample_rows_agree_with_their_count(self, tools: dict[str, Tool]) -> None:
        lines = lines_of(tools["sample_rows"], table=f"{SCHEMA}.заказы", columns="номер, СУММА", limit=2)
        assert lines[:2] == [f"{SCHEMA}.заказы:", "номер | сумма"]
        assert lines[-1] == "(показаны 2 строки, есть ещё: сузьте запрос через WHERE, ORDER BY или LIMIT)"

    def test_mistakes_are_explained_in_russian(self, tools: dict[str, Tool], analyst: dict[str, Tool]) -> None:
        with pytest.raises(ToolError, match=f"Похожие имена: {SCHEMA}.заказы"):
            tools["describe_table"].call({"table": "заказ", "schema": SCHEMA})
        with pytest.raises(ToolError, match="Команда DELETE запрещена"):
            analyst["run_select"].call({"query": f"DELETE FROM {SCHEMA}.заказы"})
        assert lines_of(tools["search_schema"], text="Сумм", schema=SCHEMA) == [
            f"{SCHEMA}.заказы.сумма numeric(10,2) NOT NULL  -- Сумма в рублях"
        ]
        assert lines_of(analyst["run_select"], query=f"SELECT sum(сумма) FROM {SCHEMA}.заказы") == [
            "sum",
            "1350.50",
            "(1 строка)",
        ]


class TestQueryLog:
    def test_rows_refusals_and_errors_reach_the_log(self, db: Database, tmp_path: Path) -> None:
        log_file = tmp_path / "queries.jsonl"
        tools = by_name(build_tools(db, str(ROOT), profile="analyst", query_log=QueryLog(str(log_file), db)))
        lines_of(tools["run_select"], query=f"SELECT id FROM {SCHEMA}.orders ORDER BY id LIMIT 3")
        lines_of(tools["sample_rows"], table=f"{SCHEMA}.orders", limit=2)
        lines_of(tools["describe_table"], table=f"{SCHEMA}.orders")
        for query in (f"DELETE FROM {SCHEMA}.orders", "SELECT * FROM no_such_table"):
            with pytest.raises(ToolError):
                tools["run_select"].call({"query": query})

        logged = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
        assert [(entry["tool"], entry["outcome"], entry.get("rows")) for entry in logged] == [
            ("run_select", "ok", 3),
            ("sample_rows", "ok", 2),
            ("describe_table", "ok", None),
            ("run_select", "refused", None),
            ("run_select", "error", None),
        ]
        assert "SQLSTATE 42P01" in logged[-1]["message"]
        assert logged[0]["role"] == Settings.from_env(os.environ).user


class TestDefences:
    """The layers of ADR 0002 below the SQL check."""

    def test_rollback_undoes_setting_changes_that_slip_past_the_check(self, db: Database) -> None:
        db.read_only("SELECT set_config('statement_timeout', '1min', false)", 1, cursor=True)
        assert db.server_info()["statement_timeout"] == "5s"

    def test_cursor_refuses_a_data_modifying_cte_even_without_the_check(self, db: Database) -> None:
        with pytest.raises(QueryFailedError):
            db.read_only(f"WITH gone AS (DELETE FROM {SCHEMA}.orders RETURNING *) SELECT * FROM gone", 5, cursor=True)

    def test_session_refuses_to_write(self, db: Database) -> None:
        with pytest.raises(QueryFailedError, match="read-only transaction"):
            db.query(f"CREATE TABLE {SCHEMA}.forbidden (id int)")

    def test_query_never_runs_a_second_statement(self, db: Database) -> None:
        # The hole in the archived reference server: "SELECT 1; COMMIT; DROP ..." in one string.
        with pytest.raises(QueryFailedError, match="cannot insert multiple commands into a prepared statement"):
            db.query("SELECT 1; COMMIT; SELECT 2")

    def test_a_slow_statement_is_cancelled_by_postgresql(self) -> None:
        database = Database(env_with(PG_EXPLORER_MCP_STATEMENT_TIMEOUT_MS="200"))
        try:
            with pytest.raises(QueryFailedError, match="statement timeout"):
                database.read_only(SLOW_STATEMENT, 1, cursor=True)
            assert database.server_info()["statement_timeout"] == "200ms"  # the session outlived the cancel
        finally:
            database.close()

    def test_the_socket_outwaits_the_statement_timeout(self) -> None:
        """A short socket timeout must not cut a running statement, or it would be run again."""
        database = Database(env_with(PG_EXPLORER_MCP_SOCKET_TIMEOUT_S="1", PG_EXPLORER_MCP_STATEMENT_TIMEOUT_MS="2500"))
        try:
            started = time.monotonic()
            with pytest.raises(QueryFailedError, match="statement timeout"):
                database.read_only(SLOW_STATEMENT, 1, cursor=True)
            assert time.monotonic() - started >= 2  # PostgreSQL's timeout spoke, not the socket's
        finally:
            database.close()

    def test_lost_connection_is_reopened(self, db: Database, admin: pg8000.native.Connection) -> None:
        db.server_info()
        admin.run("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE application_name = 'pg-explorer-mcp'")
        assert db.server_info()["read_only"] == "on"


ROLE, PASSWORD, ROLE_SCHEMA = "mcp_it_agent", "agent-secret-1", "mcp_it_role"


def drop_role(admin: pg8000.native.Connection, role: str) -> None:
    if admin.run("SELECT 1 FROM pg_roles WHERE rolname = :name", name=role):
        admin.run(f"DROP OWNED BY {role}")
        admin.run(f"DROP ROLE {role}")


@pytest.fixture(scope="module")
def agent(admin: pg8000.native.Connection) -> Iterator[Database]:
    """A role made by docs/role.sql, as a DBA would make it."""
    admin.run(f"DROP SCHEMA IF EXISTS {ROLE_SCHEMA} CASCADE")
    admin.run(f"CREATE SCHEMA {ROLE_SCHEMA}")
    admin.run(f"CREATE TABLE {ROLE_SCHEMA}.items (id int PRIMARY KEY)")
    admin.run(f"INSERT INTO {ROLE_SCHEMA}.items VALUES (1), (2)")
    drop_role(admin, ROLE)
    subprocess.run(
        [
            "psql", "-X", "-q",
            "-v", f"role={ROLE}", "-v", f"password={PASSWORD}", "-v", f"schemas={ROLE_SCHEMA}",
            "-v", f"owner={Settings.from_env(os.environ).user}",
            "-f", str(ROOT / "docs" / "role.sql"),
        ],
        check=True,
        capture_output=True,
    )  # fmt: skip
    database = Database(env_with(PGUSER=ROLE, PGPASSWORD=PASSWORD))
    yield database
    database.close()
    admin.run(f"DROP SCHEMA {ROLE_SCHEMA} CASCADE")
    drop_role(admin, ROLE)


@pytest.mark.skipif(shutil.which("psql") is None, reason="needs psql to run docs/role.sql")
class TestRoleScript:
    """docs/role.sql gives a role that reads the granted schema and cannot write anywhere."""

    def test_the_role_reads_only_the_granted_schema(self, agent: Database) -> None:
        tools = by_name(build_tools(agent, str(ROOT), profile="analyst"))
        assert f"as {ROLE}" in tools["status"].call({})
        assert lines_of(tools["list_tables"]) == [f"{ROLE_SCHEMA}.items | table | {never_analyzed(agent)}"]
        assert lines_of(tools["run_select"], query=f"SELECT count(*) FROM {ROLE_SCHEMA}.items")[1] == "2"

    def test_search_shows_only_what_the_role_can_read(self, agent: Database) -> None:
        tools = by_name(build_tools(agent, str(ROOT)))
        assert lines_of(tools["search_schema"], text="item") == [
            f"{ROLE_SCHEMA}.items | table | {never_analyzed(agent)}"
        ]
        assert lines_of(tools["search_schema"], text="orders")[0].startswith("Nothing matches 'orders'.")

    @pytest.mark.usefixtures("db")  # the tables of SCHEMA, which the role cannot read
    def test_a_schema_the_role_cannot_read_is_refused_and_one_without_tables_answered(self, agent: Database) -> None:
        tools = by_name(build_tools(agent, str(ROOT)))
        refusal = rf"cannot read any table in schema '{SCHEMA}'\. Schemas with readable tables: {ROLE_SCHEMA}\."
        with pytest.raises(ToolError, match=refusal):
            tools["list_tables"].call({"schema": SCHEMA})
        with pytest.raises(ToolError, match=refusal):
            tools["search_schema"].call({"text": "order", "schema": SCHEMA})
        assert lines_of(tools["list_tables"], schema="no_such_schema") == [
            "Schema 'no_such_schema' has no tables, so these are the readable tables of all schemas:",
            f"{ROLE_SCHEMA}.items | table | {never_analyzed(agent)}",
        ]
        assert lines_of(tools["describe_table"], table="items", schema="no_such_schema")[0].startswith(
            f"{ROLE_SCHEMA}.items | table"
        )
        with pytest.raises(ToolError, match=f"No readable table or view named 'orders' in schema '{SCHEMA}'"):
            tools["describe_table"].call({"table": "orders", "schema": SCHEMA})

    def test_a_column_error_lists_only_the_tables_the_role_reads(
        self, agent: Database, admin: pg8000.native.Connection
    ) -> None:
        # PostgreSQL reports the missing column first: the privilege is checked when the plan starts
        admin.run(f"CREATE TABLE {ROLE_SCHEMA}.salaries (id int, amount numeric)")
        admin.run(f"REVOKE ALL ON {ROLE_SCHEMA}.salaries FROM {ROLE}")
        try:
            tools = by_name(build_tools(agent, str(ROOT), profile="analyst"))
            query = f"SELECT nosuch FROM {ROLE_SCHEMA}.items JOIN {ROLE_SCHEMA}.salaries USING (id)"
            with pytest.raises(ToolError, match=rf"42703.* reads: {ROLE_SCHEMA}\.items: id\.$") as caught:
                tools["run_select"].call({"query": query})
            assert "amount" not in str(caught.value)
        finally:
            admin.run(f"DROP TABLE {ROLE_SCHEMA}.salaries")

    @pytest.mark.usefixtures("agent")
    def test_the_role_cannot_write_even_with_read_only_mode_switched_off(self) -> None:
        connection = connect(PGUSER=ROLE, PGPASSWORD=PASSWORD)
        try:
            connection.run("SET default_transaction_read_only = off")
            for statement in (f"INSERT INTO {ROLE_SCHEMA}.items VALUES (3)", f"CREATE TABLE {ROLE_SCHEMA}.x (id int)"):
                with pytest.raises(pg8000.exceptions.DatabaseError, match="permission denied"):
                    connection.run(statement)
        finally:
            connection.close()


LOGIN = "mcp_it_login"


@pytest.fixture(scope="module")
def login(admin: pg8000.native.Connection) -> Iterator[str]:
    admin.run(f"DROP ROLE IF EXISTS {LOGIN}")
    admin.run(f"CREATE ROLE {LOGIN} LOGIN PASSWORD 'right-password'")
    yield LOGIN
    admin.run(f"DROP ROLE IF EXISTS {LOGIN}")


def status_with(**env: str | None) -> str:
    database = Database(env_with(**env))
    try:
        return build_tools(database, str(ROOT))[0].call({})
    finally:
        database.close()


def connects(**env: str | None) -> bool:
    database = Database(env_with(**env))
    try:
        database.server_info()
    except DatabaseUnavailableError:
        return False
    finally:
        database.close()
    return True


def test_the_server_can_be_reached_through_its_unix_socket(admin: pg8000.native.Connection) -> None:
    directories = str(admin.run("SHOW unix_socket_directories")[0][0])
    directory = directories.split(",", maxsplit=1)[0].strip()
    port = Settings.from_env(os.environ).port
    if sys.platform == "win32" or not directory or not (Path(directory) / f".s.PGSQL.{port}").exists():
        pytest.skip("the server's Unix socket is not on this machine")
    text = status_with(PGHOST=directory)
    assert f"target: {Settings.from_env(os.environ).user}@{directory}:{port}/" in text
    assert "database: connected to" in text


class TestConnectionHints:
    """Connection failures come back as hints that say what to fix."""

    def test_missing_database(self) -> None:
        assert "Database 'mcp_it_no_such_db' does not exist" in status_with(PGDATABASE="mcp_it_no_such_db")

    def test_wrong_password(self, login: str) -> None:
        env: dict[str, str | None] = {"PGUSER": login, "PGPASSWORD": "wrong-password"}
        if connects(**env):
            pytest.skip("the server trusts these connections, passwords are not checked")
        text = status_with(**env)
        # Windows discards what the server sent before it reset the connection, so the refusal
        # sometimes arrives without the server's message; both answers point at the password.
        refused = f"Login as '{login}' failed" in text and "Never paste it into the chat" in text
        dropped = f"closed it during the login of '{login}'" in text and "check the password file" in text
        assert refused or dropped, text

    def test_missing_password_points_to_the_password_file(self, login: str) -> None:
        env: dict[str, str | None] = {"PGUSER": login, "PGPASSWORD": None, "PGPASSFILE": str(ROOT / "no-such-pgpass")}
        if connects(**env):
            pytest.skip("the server trusts these connections, passwords are not checked")
        assert "Put the password into" in status_with(**env)

    def test_tls_required_but_not_offered(self) -> None:
        if connects(PGSSLMODE="require"):
            pytest.skip("the server offers TLS")
        text = status_with(PGSSLMODE="require")
        assert "TLS with" in text
        assert "PGSSLMODE" in text
