import unittest

import support  # noqa: F401  (sets up sys.path)
from pg_mcp.sqlguard import SqlRejected, check_read_only, forbidden_function


class AllowedTests(unittest.TestCase):
    def test_read_only_statements_pass_and_report_their_kind(self):
        cases = {
            "SELECT 1": "select",
            "  select * from sales.orders where total > 100 order by id limit 5;  ": "select",
            "WITH recent AS (SELECT * FROM orders) SELECT count(*) FROM recent": "with",
            "WITH RECURSIVE t(n) AS (VALUES (1) UNION ALL SELECT n + 1 FROM t WHERE n < 5) SELECT * FROM t": "with",
            "VALUES (1, 'a'), (2, 'b')": "values",
            "TABLE sales.orders": "table",
            "EXPLAIN SELECT * FROM orders": "explain",
            "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) SELECT * FROM orders": "explain",
            "EXPLAIN ANALYZE VERBOSE SELECT 1": "explain",
            "SHOW search_path": "show",
            "-- leading comment\n/* block /* nested */ comment */ SELECT 1": "select",
        }
        for sql, kind in cases.items():
            with self.subTest(sql=sql):
                self.assertEqual(check_read_only(sql), kind)

    def test_forbidden_words_inside_literals_comments_and_quoted_names_do_not_count(self):
        for sql in (
            "SELECT 'DELETE FROM orders' AS note",
            "SELECT E'it\\'s an UPDATE' AS note",
            "SELECT $$INSERT INTO t VALUES (1)$$, $tag$DROP TABLE t$tag$",
            'SELECT "update", "delete" FROM audit',
            "SELECT 1 -- then DELETE everything",
            "SELECT /* INSERT INTO */ 1",
            "SELECT substring('abc' FROM 1 FOR 2)",
            "SELECT pg_size_pretty(pg_total_relation_size('orders')), current_setting('work_mem')",
            "SELECT * FROM pg_stat_user_tables JOIN pg_replication_slots ON true",
        ):
            with self.subTest(sql=sql):
                check_read_only(sql)


class RejectedTests(unittest.TestCase):
    def assertRejected(self, sql, *fragments):
        with self.assertRaises(SqlRejected) as caught:
            check_read_only(sql)
        for fragment in fragments:
            self.assertIn(fragment, str(caught.exception))

    def test_statements_that_change_anything_are_refused_by_their_first_word(self):
        for sql, word in (
            ("DROP DATABASE app", "DROP"),
            ("drop table orders", "DROP"),
            ("TRUNCATE orders", "TRUNCATE"),
            ("CREATE TABLE t (id int)", "CREATE"),
            ("ALTER TABLE orders ADD COLUMN x int", "ALTER"),
            ("GRANT SELECT ON orders TO public", "GRANT"),
            ("VACUUM orders", "VACUUM"),
            ("ANALYZE orders", "ANALYZE"),
            ("SET default_transaction_read_only = off", "SET"),
            ("RESET ALL", "RESET"),
            ("BEGIN", "BEGIN"),
            ("COMMIT", "COMMIT"),
            ("DO $$ BEGIN PERFORM 1; END $$", "DO"),
            ("CALL refresh_all()", "CALL"),
            ("COPY orders TO PROGRAM 'curl evil'", "COPY"),
            ("LOCK TABLE orders", "LOCK"),
            ("NOTIFY channel", "NOTIFY"),
            ("PREPARE p AS SELECT 1", "PREPARE"),
            ("COMMENT ON TABLE orders IS 'x'", "COMMENT"),
        ):
            with self.subTest(sql=sql):
                self.assertRejected(sql, word, "Only read-only statements")

    def test_writes_hidden_behind_an_allowed_first_word_are_refused(self):
        for sql, fragment in (
            ("WITH gone AS (DELETE FROM orders RETURNING *) SELECT * FROM gone", "DELETE"),
            ("WITH x AS (UPDATE orders SET total = 0 RETURNING id) SELECT 1", "UPDATE"),
            ("WITH x AS (INSERT INTO t VALUES (1) RETURNING *) SELECT 1", "INSERT"),
            ("SELECT * INTO backup FROM orders", "INTO"),
            ("SELECT * FROM orders FOR UPDATE", "UPDATE"),
            ("SELECT * FROM orders FOR NO KEY UPDATE", "UPDATE"),
            ("SELECT * FROM orders FOR SHARE", "Row locks"),
            ("SELECT * FROM orders FOR KEY SHARE", "Row locks"),
            ("EXPLAIN ANALYZE DELETE FROM orders", "EXPLAIN is allowed only"),
            ("EXPLAIN CREATE TABLE t AS SELECT 1", "EXPLAIN is allowed only"),
        ):
            with self.subTest(sql=sql):
                self.assertRejected(sql, fragment)

    def test_functions_with_side_effects_are_refused_however_they_are_spelled(self):
        for sql, name in (
            ("SELECT pg_terminate_backend(123)", "pg_terminate_backend"),
            ("SELECT pg_catalog.pg_sleep (10)", "pg_sleep"),
            ("SELECT pg_sleep/* comment */(1)", "pg_sleep"),
            ('SELECT "pg_sleep"(1)', "pg_sleep"),
            ("SELECT set_config('default_transaction_read_only', 'off', false)", "set_config"),
            ("SELECT pg_advisory_lock(1)", "pg_advisory_lock"),
            ("SELECT pg_try_advisory_xact_lock(1)", "pg_try_advisory_xact_lock"),
            ("SELECT * FROM dblink('host=x', 'DELETE FROM t') AS t(x int)", "dblink"),
            ("SELECT query_to_xml('DELETE FROM t', true, false, '')", "query_to_xml"),
            ("SELECT pg_read_file('/etc/passwd')", "pg_read_file"),
            ("SELECT * FROM pg_ls_dir('.')", "pg_ls_dir"),
            ("SELECT nextval('orders_id_seq')", "nextval"),
            ("SELECT pg_reload_conf()", "pg_reload_conf"),
            ("SELECT pg_create_physical_replication_slot('s')", "pg_create_physical_replication_slot"),
            ("SELECT pg_stat_reset()", "pg_stat_reset"),
            ("SELECT pg_notify('ch', 'x')", "pg_notify"),
            ("SELECT lo_export(123, '/tmp/x')", "lo_export"),
        ):
            with self.subTest(sql=sql):
                self.assertRejected(sql, f"Function {name}()")

    def test_more_than_one_statement_is_refused(self):
        self.assertRejected("SELECT 1; COMMIT; DROP SCHEMA public CASCADE", "one statement per call")
        self.assertRejected("SELECT 1;;", "one statement per call")

    def test_empty_and_broken_input_is_refused(self):
        self.assertRejected("", "non-empty string")
        self.assertRejected("   -- only a comment", "only comments")
        self.assertRejected("SELECT 'unterminated", "Unterminated")
        self.assertRejected("SELECT /* unterminated", "Unterminated /*")
        self.assertRejected("SELECT $q$ unterminated", "Unterminated dollar")

    def test_keywords_used_as_unquoted_column_names_are_refused(self):
        # Accepted false positive (ADR 0002, question 5): quote such names instead.
        self.assertRejected("SELECT update FROM audit", "UPDATE")


class ForbiddenFunctionTests(unittest.TestCase):
    def test_reads_are_not_forbidden(self):
        for name in ("count", "pg_size_pretty", "pg_total_relation_size", "current_setting", "lo_get", "pg_stat_get_live_tuples"):
            with self.subTest(name=name):
                self.assertIsNone(forbidden_function(name))


if __name__ == "__main__":
    unittest.main()
