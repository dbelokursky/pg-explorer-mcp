"""The server's environment variables, and the names they had before the server was pg-explorer-mcp.

Agent settings written by pg-mcp 0.7.0 and earlier name the variables PG_MCP_*. They keep working: an old
name stands in for its PG_EXPLORER_MCP_* twin, and the new name wins when both are set (ADR 0007).
"""

from __future__ import annotations

from collections.abc import Mapping

PREFIX = "PG_EXPLORER_MCP_"
LEGACY_PREFIX = "PG_MCP_"


def legacy_names(env: Mapping[str, str]) -> list[str]:
    """The old PG_MCP_* variables that stand in for a missing new one, sorted."""
    return sorted(name for name in env if name.startswith(LEGACY_PREFIX) and _new(name) not in env)


def with_legacy_names(env: Mapping[str, str]) -> dict[str, str]:
    """The environment with each old PG_MCP_X copied to PG_EXPLORER_MCP_X where that one is missing."""
    found = dict(env)
    for name in legacy_names(env):
        found[_new(name)] = env[name]
    return found


def _new(name: str) -> str:
    return PREFIX + name[len(LEGACY_PREFIX) :]
