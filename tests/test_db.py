"""Integration tests against a real PostgreSQL.

Run them with PG_MCP_IT=1 and the usual PG* variables pointing at a database
where the test may create and drop the schema mcp_it.
"""
import os
import unittest

import support
import pg8000.native
from pg_mcp.db import Database, QueryFailed, Settings
from pg_mcp.tools import ToolError, build_tools

SCHEMA = "mcp_it"  # PostgreSQL reserves the pg_ prefix for system schemas


@unittest.skipUnless(os.environ.get("PG_MCP_IT") == "1", "set PG_MCP_IT=1 and PG* variables to run")
class DatabaseIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        s = Settings.from_env(os.environ)
        cls.admin = pg8000.native.Connection(user=s.user, password=s.password, host=s.host, port=s.port, database=s.database)
        for statement in (
            f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE",
            f"CREATE SCHEMA {SCHEMA}",
            f"CREATE TABLE {SCHEMA}.orders (id int PRIMARY KEY, total numeric)",
            f"COMMENT ON TABLE {SCHEMA}.orders IS 'Заказы клиентов'",
            f"INSERT INTO {SCHEMA}.orders SELECT g, g * 10 FROM generate_series(1, 1000) g",
            f"ANALYZE {SCHEMA}.orders",
            f"CREATE VIEW {SCHEMA}.big_orders AS SELECT * FROM {SCHEMA}.orders WHERE total > 5000",
        ):
            cls.admin.run(statement)
        cls.db = Database()
        cls.tools = {tool.name: tool for tool in build_tools(cls.db, support.ROOT)}

    @classmethod
    def tearDownClass(cls):
        cls.db.close()
        cls.admin.run(f"DROP SCHEMA {SCHEMA} CASCADE")
        cls.admin.close()

    def test_status_reports_a_read_only_session(self):
        text = self.tools["status"].call({})
        self.assertIn("database: connected", text)
        self.assertIn("read only = on", text)

    def test_list_tables_shows_tables_views_estimates_and_comments(self):
        lines = self.tools["list_tables"].call({"schema": SCHEMA}).splitlines()
        self.assertEqual(lines, [f"{SCHEMA}.big_orders | view", f"{SCHEMA}.orders | table | ~1k rows | Заказы клиентов"])

    def test_unknown_schema_points_to_the_schemas_that_exist(self):
        with self.assertRaises(ToolError) as caught:
            self.tools["list_tables"].call({"schema": "no_such_schema"})
        self.assertIn(SCHEMA, str(caught.exception))

    def test_session_refuses_to_write(self):
        with self.assertRaises(QueryFailed) as caught:
            self.db.query(f"CREATE TABLE {SCHEMA}.forbidden (id int)")
        self.assertIn("read-only transaction", str(caught.exception))

    def test_query_never_runs_a_second_statement(self):
        # The hole in the archived reference server: "SELECT 1; COMMIT; DROP ..." in one string.
        with self.assertRaises(QueryFailed) as caught:
            self.db.query("SELECT 1; COMMIT; SELECT 2")
        self.assertIn("cannot insert multiple commands into a prepared statement", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
