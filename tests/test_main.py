"""main() in-process, with byte streams instead of stdin and stdout."""

from __future__ import annotations

import io
import json

from pg_mcp import __main__ as entry
from tests.helpers import ROOT, rpc


def test_main_answers_every_request_until_input_ends(unreachable_env: dict[str, str]) -> None:
    requests = [
        rpc(1, "initialize", {"protocolVersion": "2025-11-25", "capabilities": {}}),
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        rpc(2, "tools/call", {"name": "status", "arguments": {}}),
    ]
    stdin = io.BytesIO(b"\n".join(json.dumps(request).encode() for request in requests) + b"\n\n")
    stdout = io.BytesIO()
    assert entry.main(stdin, stdout, env=unreachable_env) == 0
    replies = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert [reply["id"] for reply in replies] == [1, 2]
    assert "database: unavailable" in replies[1]["result"]["content"][0]["text"]


def test_checkout_is_reported_as_the_install_dir() -> None:
    assert str(ROOT) == entry.INSTALL_DIR
