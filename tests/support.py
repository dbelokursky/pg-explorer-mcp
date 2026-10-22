"""Shared test helpers: import paths and a stdio round trip with server/run.py."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from typing import Any, Dict, List, Mapping, Optional, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER_DIR = os.path.join(ROOT, "server")
RUN_PY = os.path.join(SERVER_DIR, "run.py")
sys.path[:0] = [SERVER_DIR, os.path.join(SERVER_DIR, "vendor")]


def request(msg_id: int, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}}


def run_stdio_session(
    messages: List[Dict[str, Any]], env: Mapping[str, str], timeout: float = 30
) -> Tuple[List[bytes], "subprocess.CompletedProcess[bytes]"]:
    """Send every message to a fresh server process, close stdin, return its stdout lines."""
    payload = b"".join(json.dumps(m, ensure_ascii=False).encode("utf-8") + b"\n" for m in messages)
    proc = subprocess.run(
        [sys.executable, "-I", RUN_PY], input=payload, capture_output=True, timeout=timeout, env=dict(env)
    )
    return [line for line in proc.stdout.split(b"\n") if line.strip()], proc


def unreachable_database_env() -> Dict[str, str]:
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
        PGPASSFILE=os.path.join(ROOT, "no-such-pgpass"),
    )
    return env
