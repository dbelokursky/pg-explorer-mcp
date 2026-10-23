from __future__ import annotations

import contextlib
import json
from typing import Any

import pytest

from pg_mcp import protocol
from pg_mcp.budget import DEFAULT_BUDGET
from pg_mcp.protocol import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    LIST_TTL_MS,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    SUPPORTED_PROTOCOL_VERSIONS,
    UNSUPPORTED_PROTOCOL_VERSION,
    InvalidParamsError,
    Server,
    negotiate_protocol_version,
)
from pg_mcp.tools import Tool, ToolError
from tests.helpers import MODERN_META, rpc


def _fail(args: dict[str, Any]) -> str:  # noqa: ARG001
    raise ToolError('relation "order" does not exist; did you mean "orders"?')


def _crash(args: dict[str, Any]) -> str:  # noqa: ARG001
    raise RuntimeError("bug")


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> Server:
    """A server with fake tools; version negotiation and the response budget are out of the way."""
    monkeypatch.setattr(protocol, "negotiate_protocol_version", lambda requested: "2025-06-18")  # noqa: ARG005
    monkeypatch.setattr("pg_mcp.tools.fit_to_budget", lambda lines, budget: lines)  # noqa: ARG005
    return Server(
        [
            Tool(
                "echo",
                "Echo",
                "Echo the text back",
                {"text": {"type": "string"}},
                lambda args: "echo: " + args.get("text", ""),
            ),
            Tool("fail", "Fail", "Always fails", {}, _fail),
            Tool(
                "need", "Need", "Needs x", {"x": {"type": "string"}}, lambda args: ["got " + args["x"]], required=("x",)
            ),
            Tool("crash", "Crash", "Has a bug", {}, _crash),
        ]
    )


def roundtrip(server: Server, message: Any) -> Any:
    reply = server.handle_line(json.dumps(message, ensure_ascii=False).encode("utf-8"))
    assert reply is not None
    return json.loads(reply)


def call(server: Server, method: str, params: dict[str, Any] | None = None) -> Any:
    return roundtrip(server, rpc(1, method, params))


class TestLegacyProtocol:
    """Revisions 2024-11-05 to 2025-11-25: initialize first, then tools."""

    def test_initialize_announces_tools_and_instructions(self, server: Server) -> None:
        result = call(server, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})["result"]
        assert result["protocolVersion"] == "2025-06-18"
        assert result["capabilities"] == {"tools": {"listChanged": False}}
        assert result["serverInfo"]["name"] == "pg-mcp"
        assert "status" in result["instructions"]

    def test_tools_list_keeps_registration_order_and_marks_tools_read_only(self, server: Server) -> None:
        tools = call(server, "tools/list")["result"]["tools"]
        assert [tool["name"] for tool in tools] == ["echo", "fail", "need", "crash"]
        assert all(tool["annotations"]["readOnlyHint"] for tool in tools)
        # Only what a client acts on: the other hints mean something for tools that write, and the title
        # sits where clients of every protocol version find it.
        assert {key for tool in tools for key in tool} == {"name", "description", "inputSchema", "annotations"}
        assert {key for tool in tools for key in tool["annotations"]} == {"title", "readOnlyHint", "openWorldHint"}
        assert all(tool["inputSchema"]["additionalProperties"] is False for tool in tools)
        assert tools[2]["inputSchema"]["required"] == ["x"]
        assert "required" not in tools[0]["inputSchema"]

    def test_tools_call_returns_text_content(self, server: Server) -> None:
        result = call(server, "tools/call", {"name": "echo", "arguments": {"text": "привет"}})["result"]
        assert result == {"content": [{"type": "text", "text": "echo: привет"}], "isError": False}

    @pytest.mark.parametrize(
        ("arguments", "fragment"),
        [
            ({"txt": "x"}, "Allowed: text"),
            ("not an object", "arguments must be a JSON object"),
        ],
    )
    def test_bad_arguments_reach_the_model_as_is_error_results(
        self, server: Server, arguments: Any, fragment: str
    ) -> None:
        result = call(server, "tools/call", {"name": "echo", "arguments": arguments})["result"]
        assert result["isError"] is True
        assert fragment in result["content"][0]["text"]

    def test_missing_required_argument_is_a_tool_error(self, server: Server) -> None:
        result = call(server, "tools/call", {"name": "need", "arguments": {}})["result"]
        assert result["isError"] is True
        assert "Missing argument(s): x" in result["content"][0]["text"]

    def test_tool_failure_reaches_the_model_as_is_error_result(self, server: Server) -> None:
        result = call(server, "tools/call", {"name": "fail", "arguments": {}})["result"]
        assert result["isError"] is True
        assert 'did you mean "orders"' in result["content"][0]["text"]

    def test_bug_in_a_tool_is_an_internal_error_without_details(self, server: Server) -> None:
        error = call(server, "tools/call", {"name": "crash", "arguments": {}})["error"]
        assert error == {
            "code": INTERNAL_ERROR,
            "message": "Internal error in tools/call; details are in the server log",
        }

    def test_unknown_tool_is_invalid_params(self, server: Server) -> None:
        error = call(server, "tools/call", {"name": "drop_table", "arguments": {}})["error"]
        assert error["code"] == INVALID_PARAMS
        assert "echo, fail, need, crash" in error["message"]

    def test_unknown_method_is_method_not_found(self, server: Server) -> None:
        assert call(server, "resources/list")["error"]["code"] == METHOD_NOT_FOUND

    def test_ping_returns_an_empty_result(self, server: Server) -> None:
        assert call(server, "ping")["result"] == {}

    @pytest.mark.parametrize(
        ("message", "code"),
        [
            ([1, 2], INVALID_REQUEST),
            ({"id": 1, "method": "ping"}, INVALID_REQUEST),
            ({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": [1]}, INVALID_PARAMS),
        ],
        ids=["not-an-object", "no-jsonrpc", "params-not-an-object"],
    )
    def test_malformed_requests_get_json_rpc_errors(self, server: Server, message: Any, code: int) -> None:
        reply = roundtrip(server, message)
        errors = reply if isinstance(reply, list) else [reply]
        assert {error["error"]["code"] for error in errors} == {code}

    def test_garbage_is_a_parse_error(self, server: Server) -> None:
        reply = server.handle_line(b"{not json")
        assert reply is not None
        assert json.loads(reply)["error"]["code"] == PARSE_ERROR

    @pytest.mark.parametrize(
        "message",
        [
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 9, "result": {}},
            [{"jsonrpc": "2.0", "method": "notifications/cancelled"}],
        ],
        ids=["notification", "response", "batch-of-notifications"],
    )
    def test_messages_that_need_no_reply_get_none(self, server: Server, message: Any) -> None:
        assert server.handle_line(json.dumps(message).encode()) is None

    def test_batch_gets_a_batch_reply(self, server: Server) -> None:
        replies = roundtrip(server, [rpc(1, "ping"), rpc(2, "tools/list")])
        assert [reply["id"] for reply in replies] == [1, 2]

    def test_reply_is_one_ascii_line_whatever_the_tool_returns(self, server: Server) -> None:
        message = rpc(7, "tools/call", {"name": "echo", "arguments": {"text": "таблица\nзаказы"}})
        reply = server.handle_line(json.dumps(message).encode("utf-8"))
        assert reply is not None
        assert reply.isascii()
        assert b"\n" not in reply
        assert json.loads(reply)["result"]["content"][0]["text"] == "echo: таблица\nзаказы"


class TestModernProtocol:
    """Revision 2026-07-28: no handshake, the protocol version rides on every request."""

    def modern(self, server: Server, method: str, **params: Any) -> Any:
        return call(server, method, dict(MODERN_META, **params))

    def test_discover_names_the_modern_revision_and_the_server(self, server: Server) -> None:
        result = self.modern(server, "server/discover")["result"]
        assert result["resultType"] == "complete"
        assert result["supportedVersions"] == ["2026-07-28"]
        assert result["capabilities"] == {"tools": {"listChanged": False}}
        assert result["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "pg-mcp"
        assert "status" in result["instructions"]
        assert (result["ttlMs"], result["cacheScope"]) == (LIST_TTL_MS, "private")

    def test_tools_work_without_initialize(self, server: Server) -> None:
        listing = self.modern(server, "tools/list")["result"]
        assert listing["resultType"] == "complete"
        assert [tool["name"] for tool in listing["tools"]] == ["echo", "fail", "need", "crash"]
        assert (listing["ttlMs"], listing["cacheScope"]) == (LIST_TTL_MS, "private")
        called = self.modern(server, "tools/call", name="echo", arguments={"text": "hi"})["result"]
        assert called["resultType"] == "complete"
        assert called["content"] == [{"type": "text", "text": "echo: hi"}]
        assert "ttlMs" not in called

    def test_unsupported_revision_lists_the_supported_ones(self, server: Server) -> None:
        error = call(server, "tools/list", {"_meta": {"io.modelcontextprotocol/protocolVersion": "1900-01-01"}})[
            "error"
        ]
        assert error["code"] == UNSUPPORTED_PROTOCOL_VERSION
        assert error["data"] == {"supported": ["2026-07-28"], "requested": "1900-01-01"}

    def test_ping_is_gone_in_the_modern_revision(self, server: Server) -> None:
        assert self.modern(server, "ping")["error"]["code"] == METHOD_NOT_FOUND

    def test_legacy_requests_keep_their_old_shape(self, server: Server) -> None:
        listing = call(server, "tools/list")["result"]
        assert "resultType" not in listing
        assert "ttlMs" not in listing

    def test_discover_without_meta_is_still_answered(self, server: Server) -> None:
        assert call(server, "server/discover")["result"]["supportedVersions"] == ["2026-07-28"]


def test_every_tool_answer_goes_through_the_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[list[str], int]] = []

    def fake_budget(lines: list[str], budget: int) -> list[str]:
        seen.append((lines, budget))
        return ["… cut"]

    monkeypatch.setattr("pg_mcp.tools.fit_to_budget", fake_budget)
    tool = Tool("echo", "Echo", "Echo", {"text": {"type": "string"}}, lambda args: "echo: " + args["text"])
    assert tool.call({"text": "hi"}) == "… cut"
    assert seen == [(["echo: hi"], DEFAULT_BUDGET)]


class TestNegotiation:
    """The real negotiate_protocol_version, per the MCP lifecycle rules."""

    @pytest.mark.parametrize("version", SUPPORTED_PROTOCOL_VERSIONS)
    def test_supported_revision_is_echoed(self, version: str) -> None:
        assert negotiate_protocol_version(version) == version

    @pytest.mark.parametrize("version", ["2099-01-01", "2024-01-01", "", "2026-07-28"])
    def test_unknown_revision_gets_the_latest_supported(self, version: str) -> None:
        assert negotiate_protocol_version(version) == SUPPORTED_PROTOCOL_VERSIONS[0]

    @pytest.mark.parametrize("value", [None, 20250618, ["2025-06-18"]])
    def test_value_that_is_not_a_revision_is_answered(self, value: object) -> None:
        with contextlib.suppress(InvalidParamsError):  # rejecting would also follow the rules
            assert negotiate_protocol_version(value) in SUPPORTED_PROTOCOL_VERSIONS
