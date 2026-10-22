"""Integration tests against a real PostgreSQL.

Run them with PG_MCP_IT=1 and the usual PG* variables pointing at a database
where the test may create and drop the schemas mcp_it and mcp_it_archive.
"""
import os
import unittest

import support
import pg8000.native
from pg_mcp.db import Database, QueryFailed, Settings
from pg_mcp.tools import ToolError, build_tools

SCHEMA = "mcp_it"  # PostgreSQL reserves the pg_ prefix for system schemas
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
)


@unittest.skipUnless(os.environ.get("PG_MCP_IT") == "1", "set PG_MCP_IT=1 and PG* variables to run")
class DatabaseIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        s = Settings.from_env(os.environ)
        cls.admin = pg8000.native.Connection(user=s.user, password=s.password, host=s.host, port=s.port, database=s.database)
        for statement in FIXTURES:
            cls.admin.run(statement)
        cls.db = Database()
        cls.tools = {tool.name: tool for tool in build_tools(cls.db, support.ROOT)}

    @classmethod
    def tearDownClass(cls):
        cls.db.close()
        cls.admin.run(f"DROP SCHEMA {SCHEMA}, {ARCHIVE} CASCADE")
        cls.admin.close()

    def describe(self, **arguments):
        return self.tools["describe_table"].call(arguments).splitlines()

    def test_status_reports_a_read_only_session(self):
        text = self.tools["status"].call({})
        self.assertIn("database: connected", text)
        self.assertIn("read only = on", text)

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
