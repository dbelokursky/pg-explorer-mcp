import json
import unittest
from unittest import mock

import support  # noqa: F401  (sets up sys.path)
from pg_mcp import protocol
from pg_mcp.protocol import (
    INVALID_PARAMS,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    SUPPORTED_PROTOCOL_VERSIONS,
    InvalidParams,
    Server,
    negotiate_protocol_version,
)
from pg_mcp.budget import DEFAULT_BUDGET
from pg_mcp.tools import Tool, ToolError


def make_server() -> Server:
    def echo(args):
        return "echo: " + args.get("text", "")

    def fail(args):
        raise ToolError('relation "order" does not exist; did you mean "orders"?')

    return Server(
        [
            Tool("echo", "Echo", "Echo the text back", {"text": {"type": "string"}}, echo),
            Tool("fail", "Fail", "Always fails", {}, fail),
            Tool("need", "Need", "Needs x", {"x": {"type": "string"}}, lambda args: ["got " + args["x"]], required=("x",)),
        ]
    )


def roundtrip(server: Server, message) -> dict:
    reply = server.handle_line(json.dumps(message, ensure_ascii=False).encode("utf-8"))
    return json.loads(reply)


def call(server: Server, method: str, params=None, msg_id=1) -> dict:
    return roundtrip(server, support.request(msg_id, method, params))


class ProtocolTests(unittest.TestCase):
    """The JSON-RPC layer, with version negotiation and the response budget stubbed out."""

    def setUp(self):
        for patcher in (
            mock.patch.object(protocol, "negotiate_protocol_version", return_value="2025-06-18"),
            mock.patch("pg_mcp.tools.fit_to_budget", side_effect=lambda lines, budget: lines),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.server = make_server()

    def test_initialize_announces_tools_and_instructions(self):
        result = call(self.server, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})["result"]
        self.assertEqual(result["protocolVersion"], "2025-06-18")
        self.assertEqual(result["capabilities"], {"tools": {"listChanged": False}})
        self.assertEqual(result["serverInfo"]["name"], "pg-mcp")
        self.assertIn("status", result["instructions"])

    def test_tools_list_keeps_registration_order_and_marks_tools_read_only(self):
        tools = call(self.server, "tools/list")["result"]["tools"]
        self.assertEqual([tool["name"] for tool in tools], ["echo", "fail", "need"])
        for tool in tools:
            self.assertTrue(tool["annotations"]["readOnlyHint"])
            self.assertFalse(tool["inputSchema"]["additionalProperties"])
        self.assertEqual(tools[2]["inputSchema"]["required"], ["x"])
        self.assertNotIn("required", tools[0]["inputSchema"])

    def test_missing_required_argument_is_a_tool_error(self):
        result = call(self.server, "tools/call", {"name": "need", "arguments": {}})["result"]
        self.assertTrue(result["isError"])
        self.assertIn("Missing argument(s): x", result["content"][0]["text"])

    def test_tools_call_returns_text_content(self):
        result = call(self.server, "tools/call", {"name": "echo", "arguments": {"text": "привет"}})["result"]
        self.assertEqual(result, {"content": [{"type": "text", "text": "echo: привет"}], "isError": False})

    def test_tool_failure_reaches_the_model_as_is_error_result(self):
        result = call(self.server, "tools/call", {"name": "fail", "arguments": {}})["result"]
        self.assertTrue(result["isError"])
        self.assertIn('did you mean "orders"', result["content"][0]["text"])

    def test_unknown_argument_is_reported_with_the_allowed_ones(self):
        result = call(self.server, "tools/call", {"name": "echo", "arguments": {"txt": "x"}})["result"]
        self.assertTrue(result["isError"])
        self.assertIn("Allowed: text", result["content"][0]["text"])

    def test_unknown_tool_is_invalid_params(self):
        error = call(self.server, "tools/call", {"name": "drop_table", "arguments": {}})["error"]
        self.assertEqual(error["code"], INVALID_PARAMS)
        self.assertIn("echo, fail, need", error["message"])

    def test_unknown_method_is_method_not_found(self):
        # Clients of revision 2026-07-28 probe with server/discover and fall back on this error.
        self.assertEqual(call(self.server, "server/discover")["error"]["code"], METHOD_NOT_FOUND)

    def test_notifications_get_no_reply(self):
        line = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}).encode()
        self.assertIsNone(self.server.handle_line(line))

    def test_garbage_is_a_parse_error(self):
        self.assertEqual(json.loads(self.server.handle_line(b"{not json"))["error"]["code"], PARSE_ERROR)

    def test_ping_returns_an_empty_result(self):
        self.assertEqual(call(self.server, "ping")["result"], {})

    def test_batch_gets_a_batch_reply(self):
        replies = roundtrip(self.server, [support.request(1, "ping"), support.request(2, "tools/list")])
        self.assertEqual([reply["id"] for reply in replies], [1, 2])

    def test_reply_is_one_ascii_line_whatever_the_tool_returns(self):
        line = json.dumps(support.request(7, "tools/call", {"name": "echo", "arguments": {"text": "таблица\nзаказы"}}))
        reply = self.server.handle_line(line.encode("utf-8"))
        self.assertTrue(reply.isascii())
        self.assertNotIn(b"\n", reply)
        self.assertEqual(json.loads(reply)["result"]["content"][0]["text"], "echo: таблица\nзаказы")


class BudgetWiringTests(unittest.TestCase):
    def test_every_tool_answer_goes_through_the_budget(self):
        tool = Tool("echo", "Echo", "Echo", {"text": {"type": "string"}}, lambda args: "echo: " + args["text"])
        with mock.patch("pg_mcp.tools.fit_to_budget", return_value=["… cut"]) as fit:
            self.assertEqual(tool.call({"text": "hi"}), "… cut")
        fit.assert_called_once_with(["echo: hi"], DEFAULT_BUDGET)


class NegotiationTests(unittest.TestCase):
    """The real negotiate_protocol_version, per the MCP lifecycle rules."""

    def test_supported_revision_is_echoed(self):
        for version in SUPPORTED_PROTOCOL_VERSIONS:
            self.assertEqual(negotiate_protocol_version(version), version)

    def test_unknown_revision_gets_the_latest_supported(self):
        for version in ("2099-01-01", "2024-01-01", ""):
            self.assertEqual(negotiate_protocol_version(version), SUPPORTED_PROTOCOL_VERSIONS[0])

    def test_value_that_is_not_a_revision_is_answered_or_rejected(self):
        # Your call: fall back to a supported revision, or reject with InvalidParams.
        for value in (None, 20250618, ["2025-06-18"]):
            try:
                self.assertIn(negotiate_protocol_version(value), SUPPORTED_PROTOCOL_VERSIONS)
            except InvalidParams:
                pass


if __name__ == "__main__":
    unittest.main()
