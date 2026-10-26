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

from . import environment, i18n
from .budget import budget_from_env
from .db import Database
from .protocol import Server
from .querylog import QueryLog
from .stand_tools import build_stand_tools, registry_warnings
from .stands import DEFAULT_MAX_CONNECTIONS, Pool, load_registry
from .tools import DEFAULT_NAME, build_tools, profile_from_env

_PACKAGE_DIR = Path(__file__).resolve().parent
# A checkout keeps run.py two levels up; an installed copy has only the package.
INSTALL_DIR = str(_PACKAGE_DIR.parents[1] if (_PACKAGE_DIR.parents[1] / "run.py").exists() else _PACKAGE_DIR)


def main(
    stdin: BinaryIO | None = None,
    stdout: BinaryIO | None = None,
    env: Mapping[str, str] | None = None,
) -> int:
    """Serve MCP over the given byte streams (stdin and stdout by default) until input ends."""
    given = os.environ if env is None else env
    env = environment.with_legacy_names(given)
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(errors="backslashreplace")
    level = getattr(logging, env.get("PG_EXPLORER_MCP_LOG_LEVEL", "INFO").upper(), logging.INFO)
    logging.basicConfig(stream=sys.stderr, level=level, format="pg-mcp %(levelname)s %(message)s")
    old = environment.legacy_names(given)
    if old:
        logging.getLogger(__name__).info("old setting names %s; install.py writes the new ones", ", ".join(old))

    source = stdin if stdin is not None else sys.stdin.buffer
    sink = stdout if stdout is not None else sys.stdout.buffer
    # The language comes first: every text built below is in it.
    language, ignored = i18n.language_from_env(env, i18n.system_locale())
    i18n.use(language)
    warnings = []
    if ignored is not None:
        choices = ", ".join(i18n.LANGUAGES)
        warnings.append(i18n.t("warning.language", value=ignored, choices=choices, fallback=language))
    profile, profile_warning = profile_from_env(env)
    if profile_warning is not None:
        warnings.append(profile_warning)
    name = env.get("PG_EXPLORER_MCP_NAME", "").strip() or DEFAULT_NAME
    # Many stands only when PG_EXPLORER_MCP_STANDS asks and the registry has some; otherwise the PG* settings.
    registry = load_registry(env)
    if registry is not None and not registry.targets:
        warnings.extend(registry_warnings(registry))  # the single-database tools show why the stands are off
    connections: Database | Pool
    if registry is not None and registry.targets:
        connections = Pool(registry, env, _whole(env, "PG_EXPLORER_MCP_MAX_CONNECTIONS", DEFAULT_MAX_CONNECTIONS))
        query_log = QueryLog.from_env(env, connections)
        tools = build_stand_tools(
            connections,
            INSTALL_DIR,
            budget=budget_from_env(env),
            profile=profile,
            warnings=warnings,
            query_log=query_log,
            name=name,
        )
        server = Server(tools, instructions=i18n.t("server.instructions") + " " + i18n.t("stands.instructions"))
    else:
        connections = Database(env)
        query_log = QueryLog.from_env(env, connections)
        tools = build_tools(
            connections,
            INSTALL_DIR,
            budget=budget_from_env(env),
            profile=profile,
            warnings=warnings,
            query_log=query_log,
            name=name,
        )
        server = Server(tools)
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
        connections.close()
    return 0


def _whole(env: Mapping[str, str], variable: str, default: int) -> int:
    raw = env.get(variable, "").strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else default


if __name__ == "__main__":
    sys.exit(main())
