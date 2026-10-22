import contextlib
import glob
import io
import json
import os
import sys
import tempfile
import unittest

import support

sys.path.insert(0, support.ROOT)
import install  # noqa: E402


class RegisterTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = os.path.join(directory.name, ".gigacode", "settings.json")
        self.entry = {"command": "/usr/bin/python3", "args": ["-I", "/opt/pg-mcp/server/run.py"]}

    def read(self):
        with open(self.path, encoding="utf-8") as handle:
            return json.load(handle)

    def register(self):
        with contextlib.redirect_stdout(io.StringIO()):
            install.register(self.path, self.entry)

    def test_creates_the_settings_file_when_there_is_none(self):
        self.register()
        self.assertEqual(self.read(), {"mcpServers": {"pg": self.entry}})

    def test_keeps_other_settings_and_servers_and_backs_up_the_old_file(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump({"theme": "dark", "mcpServers": {"playwright": {"command": "npx"}}}, handle)
        self.register()
        settings = self.read()
        self.assertEqual(settings["theme"], "dark")
        self.assertEqual(set(settings["mcpServers"]), {"playwright", "pg"})
        self.assertEqual(len(glob.glob(self.path + ".bak-*")), 1)

    def test_refuses_to_rewrite_settings_it_cannot_parse(self):
        os.makedirs(os.path.dirname(self.path))
        original = '{\n  // comments are allowed by the agent, not by json\n  "theme": "dark"\n}\n'
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(original)
        with self.assertRaises(SystemExit) as caught:
            self.register()
        self.assertIn('"pg"', str(caught.exception))
        with open(self.path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), original)

    def test_entry_runs_this_interpreter_isolated_and_never_copies_the_password(self):
        os.environ["PGPASSWORD"] = "secret"
        os.environ["PGHOST"] = "db.example"
        self.addCleanup(os.environ.pop, "PGPASSWORD", None)
        self.addCleanup(os.environ.pop, "PGHOST", None)
        entry = install.server_entry()
        self.assertEqual(entry["command"], sys.executable)
        self.assertEqual(entry["args"], ["-I", install.RUN_PY])
        self.assertEqual(entry["env"]["PGHOST"], "db.example")
        self.assertNotIn("PGPASSWORD", entry["env"])


if __name__ == "__main__":
    unittest.main()
