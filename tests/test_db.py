"""Integration tests against a real PostgreSQL.

Run them with PG_MCP_IT=1 and the usual PG* variables pointing at a database
where the tests may create and drop the schemas mcp_it, mcp_it_archive and
mcp_it_role and the roles mcp_it_agent and mcp_it_login.
"""
import os
import shutil
import subprocess
import unittest

import support
import pg8000.native
from pg_mcp.db import Database, DatabaseUnavailable, QueryFailed, Settings
from pg_mcp.tools import ToolError, build_tools

SCHEMA = "mcp_it"  # PostgreSQL reserves the pg_ prefix for system schemas
ARCHIVE = "mcp_it_archive"
INTEGRATION = os.environ.get("PG_MCP_IT") == "1"
SKIP_REASON = "set PG_MCP_IT=1 and PG* variables to run"

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
)


def admin_connection() -> pg8000.native.Connection:
    s = Settings.from_env(os.environ)
    return pg8000.native.Connection(user=s.user, password=s.password, host=s.host, port=s.port, database=s.database)


def env_with(**overrides) -> dict:
    env = dict(os.environ)
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


@unittest.skipUnless(INTEGRATION, SKIP_REASON)
class DatabaseIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.admin = admin_connection()
        for statement in FIXTURES:
            cls.admin.run(statement)
        cls.db = Database()
        cls.tools = {tool.name: tool for tool in build_tools(cls.db, support.ROOT)}
        cls.analyst = {tool.name: tool for tool in build_tools(cls.db, support.ROOT, profile="analyst")}

    @classmethod
    def tearDownClass(cls):
        cls.db.close()
        cls.admin.run(f"DROP SCHEMA {SCHEMA}, {ARCHIVE} CASCADE")
        cls.admin.close()

    def describe(self, **arguments):
        return self.tools["describe_table"].call(arguments).splitlines()

    def select(self, query):
        return self.analyst["run_select"].call({"query": query}).splitlines()

    def test_status_reports_a_read_only_session(self):
        text = self.tools["status"].call({})
        self.assertIn("database: connected", text)
        self.assertIn("read only = on", text)
        self.assertIn("profile: explore (status, list_tables, describe_table, sample_rows, explain_query)", text)

    def test_list_tables_shows_tables_views_estimates_and_comments(self):
        lines = self.tools["list_tables"].call({"schema": SCHEMA}).splitlines()
        self.assertEqual(
            lines,
            [
                f"{SCHEMA}.big_orders | view",
                f"{SCHEMA}.customers | table | ~10 rows",
                f"{SCHEMA}.orders | table | ~1k rows | Заказы клиентов",
            ],
        )

    def test_unknown_schema_points_to_the_schemas_that_exist(self):
        with self.assertRaises(ToolError) as caught:
            self.tools["list_tables"].call({"schema": "no_such_schema"})
        self.assertIn(SCHEMA, str(caught.exception))

    def test_describe_table_shows_columns_keys_and_indexes(self):
        lines = self.describe(table=f"{SCHEMA}.orders")
        self.assertEqual(lines[0], f"{SCHEMA}.orders | table | ~1k rows | Заказы клиентов")
        self.assertEqual(lines[1], "columns:")
        self.assertTrue(lines[2].startswith("  id integer NOT NULL DEFAULT nextval("), lines[2])
        self.assertEqual(lines[3:6], ["  customer_id integer NOT NULL  -- Покупатель", "  total numeric(12,2) DEFAULT 0", "  note text"])
        self.assertEqual(
            lines[6:9],
            [
                "constraints:",
                "  orders_pkey: PRIMARY KEY (id)",
                f"  orders_customer_id_fkey: FOREIGN KEY (customer_id) REFERENCES {SCHEMA}.customers(id)",
            ],
        )
        self.assertEqual(lines[9], "indexes:")
        self.assertIn(f"  CREATE INDEX orders_customer_idx ON {SCHEMA}.orders USING btree (customer_id)", lines[10:])

    def test_describe_table_lists_tables_that_reference_it(self):
        lines = self.describe(table="customers", schema=SCHEMA)
        self.assertIn("  customers_email_key: UNIQUE (email)", lines)
        position = lines.index("referenced by:")
        self.assertEqual(
            lines[position + 1], f"  {SCHEMA}.orders: FOREIGN KEY (customer_id) REFERENCES {SCHEMA}.customers(id)"
        )

    def test_describe_view_has_columns_and_nothing_else(self):
        lines = self.describe(table=f"{SCHEMA}.big_orders")
        self.assertEqual(lines[0], f"{SCHEMA}.big_orders | view")
        self.assertEqual(len(lines), 2 + 4)  # header, "columns:", four columns

    def test_upper_case_name_finds_the_table(self):
        self.assertEqual(self.describe(table="Customers", schema=SCHEMA)[0], f"{SCHEMA}.customers | table | ~10 rows")

    def test_name_in_several_schemas_asks_to_pick_one(self):
        with self.assertRaises(ToolError) as caught:
            self.describe(table="orders")
        self.assertIn(f"{SCHEMA}.orders, {ARCHIVE}.orders", str(caught.exception))

    def test_unknown_table_suggests_similar_names(self):
        with self.assertRaises(ToolError) as caught:
            self.describe(table="order", schema=SCHEMA)
        self.assertIn(f"Similar names: {SCHEMA}.big_orders, {SCHEMA}.orders, {ARCHIVE}.orders", str(caught.exception))

    def test_sample_rows_returns_a_few_rows_of_the_chosen_columns(self):
        lines = self.tools["sample_rows"].call({"table": f"{SCHEMA}.orders", "columns": "id, TOTAL", "limit": 3}).splitlines()
        self.assertEqual(lines[:2], [f"{SCHEMA}.orders:", "id | total"])
        self.assertEqual(len(lines), 2 + 3 + 1)
        self.assertTrue(lines[-1].startswith("(3 rows shown, there are more"), lines[-1])

    def test_sample_rows_names_the_columns_when_one_is_wrong(self):
        with self.assertRaises(ToolError) as caught:
            self.tools["sample_rows"].call({"table": f"{SCHEMA}.orders", "columns": "id, amount"})
        self.assertIn("No column 'amount'", str(caught.exception))
        self.assertIn("Columns: id, customer_id, total, note", str(caught.exception))

    def test_sample_rows_keeps_the_limit(self):
        with self.assertRaises(ToolError) as caught:
            self.tools["sample_rows"].call({"table": f"{SCHEMA}.orders", "limit": 500})
        self.assertIn("limit must be from 1 to 50", str(caught.exception))

    def test_explain_query_shows_a_plan_without_running_the_query(self):
        text = self.tools["explain_query"].call({"query": f"SELECT * FROM {SCHEMA}.orders WHERE customer_id = 3"})
        self.assertIn("Scan", text)
        self.assertNotIn("actual time", text)

    def test_explain_analyze_is_offered_only_to_analysts(self):
        query = f"SELECT count(*) FROM {SCHEMA}.orders"
        with self.assertRaises(ToolError) as caught:
            self.tools["explain_query"].call({"query": query, "analyze": True})
        self.assertIn("Unknown argument(s): analyze", str(caught.exception))
        self.assertIn("actual time", self.analyst["explain_query"].call({"query": query, "analyze": True}))

    def test_run_select_returns_rows_with_a_count(self):
        self.assertEqual(
            self.select(f"SELECT id, total FROM {SCHEMA}.orders ORDER BY id LIMIT 2"),
            ["id | total", "1 | 10.00", "2 | 20.00", "(2 rows)"],
        )

    def test_run_select_returns_at_most_200_rows(self):
        lines = self.select("SELECT g FROM generate_series(1, 1000) AS g")
        self.assertEqual(len(lines), 1 + 200 + 1)
        self.assertTrue(lines[-1].startswith("(200 rows shown, there are more"), lines[-1])

    def test_run_select_answers_show_and_explain(self):
        self.assertEqual(self.select("SHOW statement_timeout"), ["statement_timeout", "5s", "(1 row)"])
        self.assertIn("Result", "\n".join(self.select("EXPLAIN SELECT 1")))

    def test_run_select_refuses_every_kind_of_write_and_the_data_survives(self):
        for query in (
            f"DROP TABLE {SCHEMA}.orders",
            f"DELETE FROM {SCHEMA}.orders",
            f"WITH gone AS (DELETE FROM {SCHEMA}.orders RETURNING *) SELECT count(*) FROM gone",
            "SELECT set_config('default_transaction_read_only', 'off', false)",
            f"SELECT 1; DROP TABLE {SCHEMA}.orders",
            f"SELECT * FROM {SCHEMA}.orders FOR UPDATE",
        ):
            with self.subTest(query=query):
                with self.assertRaises(ToolError):
                    self.select(query)
        self.assertEqual(self.select(f"SELECT count(*) FROM {SCHEMA}.orders")[1], "1000")

    def test_rollback_undoes_setting_changes_that_slip_past_the_check(self):
        self.db.read_only("SELECT set_config('statement_timeout', '1min', false)", 1, cursor=True)
        self.assertEqual(self.db.server_info()["statement_timeout"], "5s")

    def test_cursor_refuses_a_data_modifying_cte_even_without_the_check(self):
        with self.assertRaises(QueryFailed):
            self.db.read_only(f"WITH gone AS (DELETE FROM {SCHEMA}.orders RETURNING *) SELECT * FROM gone", 5, cursor=True)

    def test_session_refuses_to_write(self):
        with self.assertRaises(QueryFailed) as caught:
            self.db.query(f"CREATE TABLE {SCHEMA}.forbidden (id int)")
        self.assertIn("read-only transaction", str(caught.exception))

    def test_query_never_runs_a_second_statement(self):
        # The hole in the archived reference server: "SELECT 1; COMMIT; DROP ..." in one string.
        with self.assertRaises(QueryFailed) as caught:
            self.db.query("SELECT 1; COMMIT; SELECT 2")
        self.assertIn("cannot insert multiple commands into a prepared statement", str(caught.exception))


@unittest.skipUnless(INTEGRATION and shutil.which("psql"), "needs PG_MCP_IT=1 and psql")
class RoleScriptTests(unittest.TestCase):
    """docs/role.sql gives a role that reads the granted schema and cannot write anywhere."""

    ROLE, PASSWORD, ROLE_SCHEMA = "mcp_it_agent", "agent-secret-1", "mcp_it_role"

    @classmethod
    def setUpClass(cls):
        cls.admin = admin_connection()
        cls.admin.run(f"DROP SCHEMA IF EXISTS {cls.ROLE_SCHEMA} CASCADE")
        cls.admin.run(f"CREATE SCHEMA {cls.ROLE_SCHEMA}")
        cls.admin.run(f"CREATE TABLE {cls.ROLE_SCHEMA}.items (id int PRIMARY KEY)")
        cls.admin.run(f"INSERT INTO {cls.ROLE_SCHEMA}.items VALUES (1), (2)")
        cls._drop_role()
        owner = Settings.from_env(os.environ).user
        subprocess.run(
            ["psql", "-X", "-q", "-v", f"role={cls.ROLE}", "-v", f"password={cls.PASSWORD}",
             "-v", f"schemas={cls.ROLE_SCHEMA}", "-v", f"owner={owner}", "-f", os.path.join(support.ROOT, "docs", "role.sql")],
            check=True,
            capture_output=True,
        )
        cls.db = Database(env_with(PGUSER=cls.ROLE, PGPASSWORD=cls.PASSWORD))
        cls.tools = {tool.name: tool for tool in build_tools(cls.db, support.ROOT, profile="analyst")}

    @classmethod
    def tearDownClass(cls):
        cls.db.close()
        cls.admin.run(f"DROP SCHEMA {cls.ROLE_SCHEMA} CASCADE")
        cls._drop_role()
        cls.admin.close()

    @classmethod
    def _drop_role(cls):
        if cls.admin.run("SELECT 1 FROM pg_roles WHERE rolname = :name", name=cls.ROLE):
            cls.admin.run(f"DROP OWNED BY {cls.ROLE}")
            cls.admin.run(f"DROP ROLE {cls.ROLE}")

    def test_the_role_reads_only_the_granted_schema(self):
        self.assertIn(f"as {self.ROLE}", self.tools["status"].call({}))
        self.assertEqual(self.tools["list_tables"].call({}).splitlines(), [f"{self.ROLE_SCHEMA}.items | table | rows unknown (never analyzed)"])
        self.assertEqual(self.tools["run_select"].call({"query": f"SELECT count(*) FROM {self.ROLE_SCHEMA}.items"}).splitlines()[1], "2")

    def test_the_role_cannot_write_even_with_read_only_mode_switched_off(self):
        agent = pg8000.native.Connection(
            user=self.ROLE, password=self.PASSWORD, host=self.db.settings.host, port=self.db.settings.port,
            database=self.db.settings.database,
        )
        try:
            agent.run("SET default_transaction_read_only = off")
            for statement in (f"INSERT INTO {self.ROLE_SCHEMA}.items VALUES (3)", f"CREATE TABLE {self.ROLE_SCHEMA}.x (id int)"):
                with self.subTest(statement=statement):
                    with self.assertRaises(pg8000.exceptions.DatabaseError) as caught:
                        agent.run(statement)
                    self.assertIn("permission denied", str(caught.exception))
        finally:
            agent.close()


@unittest.skipUnless(INTEGRATION, SKIP_REASON)
class ConnectionHintTests(unittest.TestCase):
    """Connection failures come back as hints that say what to fix."""

    LOGIN = "mcp_it_login"

    @classmethod
    def setUpClass(cls):
        cls.admin = admin_connection()
        cls.admin.run(f"DROP ROLE IF EXISTS {cls.LOGIN}")
        cls.admin.run(f"CREATE ROLE {cls.LOGIN} LOGIN PASSWORD 'right-password'")

    @classmethod
    def tearDownClass(cls):
        cls.admin.run(f"DROP ROLE IF EXISTS {cls.LOGIN}")
        cls.admin.close()

    def status_with(self, **env):
        db = Database(env_with(**env))
        try:
            return build_tools(db, support.ROOT)[0].call({})
        finally:
            db.close()

    def connects(self, **env) -> bool:
        db = Database(env_with(**env))
        try:
            db.server_info()
            return True
        except DatabaseUnavailable:
            return False
        finally:
            db.close()

    def test_missing_database(self):
        self.assertIn("Database 'mcp_it_no_such_db' does not exist", self.status_with(PGDATABASE="mcp_it_no_such_db"))

    def test_wrong_password(self):
        env = dict(PGUSER=self.LOGIN, PGPASSWORD="wrong-password")
        if self.connects(**env):
            self.skipTest("the server trusts these connections, passwords are not checked")
        text = self.status_with(**env)
        self.assertIn(f"Login as '{self.LOGIN}' failed", text)
        self.assertIn("Never paste it into the chat", text)

    def test_missing_password_points_to_the_password_file(self):
        env = dict(PGUSER=self.LOGIN, PGPASSWORD=None, PGPASSFILE=os.path.join(support.ROOT, "no-such-pgpass"))
        if self.connects(**env):
            self.skipTest("the server trusts these connections, passwords are not checked")
        self.assertIn("Put the password into", self.status_with(**env))

    def test_tls_required_but_not_offered(self):
        if self.connects(PGSSLMODE="require"):
            self.skipTest("the server offers TLS")
        text = self.status_with(PGSSLMODE="require")
        self.assertIn("TLS with", text)
        self.assertIn("PGSSLMODE", text)


if __name__ == "__main__":
    unittest.main()
