import os
import sys
import tempfile
import unittest

import support  # noqa: F401  (sets up sys.path)
from pg_mcp.db import Database, Settings, read_pgpass


class SettingsTests(unittest.TestCase):
    def test_libpq_defaults(self):
        settings = Settings.from_env({"PGUSER": "app", "PGPASSFILE": os.devnull})
        self.assertEqual((settings.host, settings.port, settings.database), ("localhost", 5432, "app"))
        self.assertEqual(settings.sslmode, "prefer")

    def test_bad_value_is_reported_through_the_database_not_at_startup(self):
        db = Database({"PGPORT": "five", "PGUSER": "app"})
        self.assertIn("PGPORT='five' is not a number", db.describe_target())


class PgpassTests(unittest.TestCase):
    def write_pgpass(self, text: str) -> str:
        handle = tempfile.NamedTemporaryFile("w", suffix=".pgpass", delete=False, encoding="utf-8")
        with handle:
            handle.write(text)
        os.chmod(handle.name, 0o600)
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def test_first_matching_line_wins_and_star_matches_anything(self):
        path = self.write_pgpass("# comment\ndb.local:5432:sales:app:first\n*:*:*:app:second\n")
        self.assertEqual(read_pgpass(path, "db.local", 5432, "sales", "app"), "first")
        self.assertEqual(read_pgpass(path, "other", 6432, "hr", "app"), "second")
        self.assertIsNone(read_pgpass(path, "db.local", 5432, "sales", "someone"))

    def test_escaped_colon_and_backslash(self):
        path = self.write_pgpass(r"db:5432:sales:app:pa\:ss\\word" + "\n")
        self.assertEqual(read_pgpass(path, "db", 5432, "sales", "app"), "pa:ss\\word")

    def test_socket_directory_matches_localhost(self):
        path = self.write_pgpass("localhost:5432:sales:app:secret\n")
        self.assertEqual(read_pgpass(path, "/tmp", 5432, "sales", "app"), "secret")

    @unittest.skipIf(sys.platform == "win32", "file modes are a Unix rule")
    def test_file_readable_by_others_is_ignored_like_libpq_does(self):
        path = self.write_pgpass("*:*:*:*:secret\n")
        os.chmod(path, 0o644)
        self.assertIsNone(read_pgpass(path, "db", 5432, "sales", "app"))


if __name__ == "__main__":
    unittest.main()
