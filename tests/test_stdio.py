import json
import unittest

from support import request, run_stdio_session, unreachable_database_env


class StdioSessionTests(unittest.TestCase):
    """The server as an MCP client runs it: a separate process speaking over stdin/stdout."""

    def test_session_without_a_reachable_database(self):
        lines, proc = run_stdio_session(
            [
                request(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "tests"}}),
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                request(2, "tools/list"),
                request(3, "tools/call", {"name": "status", "arguments": {}}),
                request(4, "tools/call", {"name": "list_tables", "arguments": {"schema": "заказы"}}),
            ],
            env=unreachable_database_env(),
        )
        stderr = proc.stderr.decode("utf-8", "replace")
        self.assertEqual(proc.returncode, 0, stderr)
        self.assertEqual(len(lines), 4, stderr)
        self.assertTrue(all(line.isascii() for line in lines))

        initialize, tools, status, tables = (json.loads(line) for line in lines)
        self.assertEqual(initialize["result"]["protocolVersion"], "2025-06-18", stderr)
        self.assertEqual([tool["name"] for tool in tools["result"]["tools"]], ["status", "list_tables"])

        status_text = status["result"]["content"][0]["text"]
        self.assertFalse(status["result"]["isError"])
        self.assertIn("database: unavailable", status_text)
        self.assertIn("Check PGHOST and PGPORT", status_text)

        self.assertTrue(tables["result"]["isError"])
        self.assertIn("Nothing accepts connections", tables["result"]["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
