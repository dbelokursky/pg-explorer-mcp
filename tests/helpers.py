"""Helpers shared by the tests; fixtures live in conftest.py."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RUN_PY = ROOT / "run.py"

MODERN_META: dict[str, Any] = {
    "_meta": {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientInfo": {"name": "tests", "version": "0"},
        "io.modelcontextprotocol/clientCapabilities": {},
    }
}


def rpc(msg_id: int, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}}


def run_stdio_session(
    messages: list[dict[str, Any]], env: Mapping[str, str], timeout: float = 30
) -> tuple[list[bytes], subprocess.CompletedProcess[bytes]]:
    """Send every message to a fresh `python -I run.py`, close stdin and return its stdout lines."""
    payload = b"".join(json.dumps(message, ensure_ascii=False).encode("utf-8") + b"\n" for message in messages)
    proc = subprocess.run(
        [sys.executable, "-I", str(RUN_PY)],
        input=payload,
        capture_output=True,
        timeout=timeout,
        env=dict(env),
        check=False,
    )
    return [line for line in proc.stdout.split(b"\n") if line.strip()], proc


def unreachable_database_env() -> dict[str, str]:
    """Environment that points the server at a local port where nothing listens."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    env = {key: value for key, value in os.environ.items() if not key.startswith("PG")}
    env.update(
        PGHOST="127.0.0.1",
        PGPORT=str(port),
        PGUSER="pg_mcp_test",
        PGDATABASE="pg_mcp_test",
        PGSSLMODE="disable",
        PGPASSFILE=str(ROOT / "no-such-pgpass"),
    )
    return env
