"""Stdio transport: one JSON-RPC message per line in, one reply per line out.

stdin is read as raw bytes and replies are ASCII-only JSON, so neither the
console code page (cp866/cp1251 on Russian Windows) nor newline translation can
corrupt the protocol. Logs go to stderr; stdout carries nothing but replies.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import BinaryIO

from .budget import budget_from_env
from .db import Database
from .protocol import Server
from .tools import build_tools, profile_from_env

_PACKAGE_DIR = Path(__file__).resolve().parent
# A checkout keeps run.py two levels up; an installed copy has only the package.
INSTALL_DIR = str(_PACKAGE_DIR.parents[1] if (_PACKAGE_DIR.parents[1] / "run.py").exists() else _PACKAGE_DIR)


def main(
    stdin: BinaryIO | None = None,
    stdout: BinaryIO | None = None,
    env: Mapping[str, str] | None = None,
) -> int:
    """Serve MCP over the given byte streams (stdin and stdout by default) until input ends."""
    env = os.environ if env is None else env
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(errors="backslashreplace")
    level = getattr(logging, env.get("PG_MCP_LOG_LEVEL", "INFO").upper(), logging.INFO)
    logging.basicConfig(stream=sys.stderr, level=level, format="pg-mcp %(levelname)s %(message)s")

    source = stdin if stdin is not None else sys.stdin.buffer
    sink = stdout if stdout is not None else sys.stdout.buffer
    db = Database(env)
    profile, warning = profile_from_env(env)
    server = Server(build_tools(db, INSTALL_DIR, budget_from_env(env), profile, warning))
    try:
        for line in iter(source.readline, b""):
            if not line.strip():
                continue
            reply = server.handle_line(line)
            if reply is not None:
                sink.write(reply + b"\n")
                sink.flush()
    except KeyboardInterrupt:
        pass
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
