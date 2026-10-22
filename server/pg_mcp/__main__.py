"""Stdio transport: one JSON-RPC message per line in, one reply per line out.

stdin is read as raw bytes and replies are ASCII-only JSON, so neither the
console code page (cp866/cp1251 on Russian Windows) nor newline translation can
corrupt the protocol. Logs go to stderr; stdout carries nothing but replies.
"""
from __future__ import annotations

import logging
import os
import sys

from .budget import budget_from_env
from .db import Database
from .protocol import Server
from .tools import build_tools

INSTALL_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(errors="backslashreplace")
    level = getattr(logging, os.environ.get("PG_MCP_LOG_LEVEL", "INFO").upper(), logging.INFO)
    logging.basicConfig(stream=sys.stderr, level=level, format="pg-mcp %(levelname)s %(message)s")

    db = Database()
    server = Server(build_tools(db, INSTALL_DIR, budget_from_env(os.environ)))
    stdin, stdout = sys.stdin.buffer, sys.stdout.buffer
    try:
        for line in iter(stdin.readline, b""):
            if not line.strip():
                continue
            reply = server.handle_line(line)
            if reply is not None:
                stdout.write(reply + b"\n")
                stdout.flush()
    except KeyboardInterrupt:
        pass
    finally:
        db.close()
    return 0
