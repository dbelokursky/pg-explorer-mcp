"""The server as an MCP client runs it: a separate `python -I run.py` speaking over stdin/stdout."""

from __future__ import annotations

import json

from tests.helpers import MODERN_META, rpc, run_stdio_session


def test_legacy_session_without_a_reachable_database(unreachable_env: dict[str, str]) -> None:
    lines, proc = run_stdio_session(
        [
            rpc(
                1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "tests"}}
            ),
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            rpc(2, "tools/list"),
            rpc(3, "tools/call", {"name": "status", "arguments": {}}),
            rpc(4, "tools/call", {"name": "list_tables", "arguments": {"schema": "заказы"}}),
        ],
        env=unreachable_env,
    )
    stderr = proc.stderr.decode("utf-8", "replace")
    assert proc.returncode == 0, stderr
    assert len(lines) == 4, stderr
    assert all(line.isascii() for line in lines)

    initialize, tools, status, tables = (json.loads(line) for line in lines)
    assert initialize["result"]["protocolVersion"] == "2025-06-18"
    assert [tool["name"] for tool in tools["result"]["tools"]] == [
        "status",
        "list_tables",
        "describe_table",
        "sample_rows",
        "explain_query",
    ]
    status_text = status["result"]["content"][0]["text"]
    assert status["result"]["isError"] is False
    assert "database: unavailable" in status_text
    assert "Check PGHOST and PGPORT" in status_text
    assert tables["result"]["isError"] is True
    assert "Nothing accepts connections" in tables["result"]["content"][0]["text"]


def test_modern_session_needs_no_initialize(unreachable_env: dict[str, str]) -> None:
    lines, proc = run_stdio_session(
        [
            rpc(1, "server/discover", MODERN_META),
            rpc(2, "tools/list", MODERN_META),
            rpc(3, "tools/call", dict(MODERN_META, name="status", arguments={})),
        ],
        env=unreachable_env,
    )
    assert len(lines) == 3, proc.stderr.decode("utf-8", "replace")
    discover, tools, status = (json.loads(line) for line in lines)
    assert discover["result"]["supportedVersions"] == ["2026-07-28"]
    assert len(tools["result"]["tools"]) == 5
    assert status["result"]["resultType"] == "complete"
    assert "database: unavailable" in status["result"]["content"][0]["text"]


def test_russian_session_stays_ascii_on_the_wire(unreachable_env: dict[str, str]) -> None:
    lines, proc = run_stdio_session(
        [
            rpc(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}}),
            rpc(2, "tools/list"),
            rpc(3, "tools/call", {"name": "status", "arguments": {}}),
        ],
        env=dict(unreachable_env, PG_MCP_LANG="ru"),
    )
    assert len(lines) == 3, proc.stderr.decode("utf-8", "replace")
    assert all(line.isascii() for line in lines)
    initialize, tools, status = (json.loads(line) for line in lines)
    assert "Отвечайте пользователю" in initialize["result"]["instructions"]
    assert tools["result"]["tools"][0]["title"] == "Состояние подключения к базе"
    assert "база: недоступна" in status["result"]["content"][0]["text"]
