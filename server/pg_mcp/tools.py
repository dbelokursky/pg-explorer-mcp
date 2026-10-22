"""The tools the agent sees.

All of them only read, and their limits live here in code: a prompt can ask
the model to behave, only the server can make sure it does.
"""
from __future__ import annotations

import platform
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from . import __version__
from .db import Database, DatabaseUnavailable, QueryFailed

LIST_TABLES_LIMIT = 200

# MCP tool annotations: clients may run read-only tools without asking the user.
READ_ONLY_HINTS = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}

RELATION_KINDS = {"r": "table", "p": "partitioned table", "v": "view", "m": "materialized view", "f": "foreign table"}

_READABLE_RELATIONS = """
FROM pg_catalog.pg_class c
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f')
  AND n.nspname <> 'information_schema'
  AND n.nspname NOT LIKE 'pg\\_%'
  AND has_table_privilege(c.oid, 'SELECT')
"""

LIST_TABLES_SQL = (
    "SELECT n.nspname, c.relname, c.relkind, c.reltuples::bigint, obj_description(c.oid, 'pg_class')"
    + _READABLE_RELATIONS
    + "  AND (CAST(:schema AS text) IS NULL OR n.nspname = CAST(:schema AS text))\n"
    + "ORDER BY n.nspname, c.relname\nLIMIT :limit"
)

SCHEMAS_SQL = "SELECT DISTINCT n.nspname" + _READABLE_RELATIONS + "ORDER BY 1\nLIMIT 50"


class ToolError(Exception):
    """A failure the model should read and act on; sent back as an isError result."""


@dataclass(frozen=True)
class Tool:
    name: str
    title: str
    description: str
    properties: Dict[str, Any]
    handler: Callable[[Dict[str, Any]], str]

    def describe(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "inputSchema": {"type": "object", "properties": self.properties, "additionalProperties": False},
            "annotations": dict(READ_ONLY_HINTS, title=self.title),
        }

    def call(self, arguments: Any) -> str:
        """Run the tool; bad arguments and database failures become ToolError."""
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise ToolError("arguments must be a JSON object")
        unknown = sorted(set(arguments) - set(self.properties))
        if unknown:
            allowed = ", ".join(sorted(self.properties)) or "none"
            raise ToolError(f"Unknown argument(s): {', '.join(unknown)}. Allowed: {allowed}.")
        try:
            return self.handler(arguments)
        except (DatabaseUnavailable, QueryFailed) as exc:
            raise ToolError(str(exc)) from exc


def build_tools(db: Database, install_dir: str) -> List[Tool]:
    return [
        Tool(
            name="status",
            title="Database connection status",
            description=(
                "Show which PostgreSQL database this server uses, as which role, the server "
                "version and the limits in force. Call it first, and again whenever another "
                "tool reports a connection problem: its output says what to fix."
            ),
            properties={},
            handler=lambda args: status(db, install_dir),
        ),
        Tool(
            name="list_tables",
            title="List tables",
            description=(
                "List the tables, views, materialized views and foreign tables the current role "
                f"can read, with estimated row counts and comments. At most {LIST_TABLES_LIMIT} "
                "entries; pass schema to narrow the list."
            ),
            properties={
                "schema": {
                    "type": "string",
                    "description": 'Only list objects in this schema, e.g. "public". Omit to list all schemas.',
                },
            },
            handler=lambda args: list_tables(db, args.get("schema")),
        ),
    ]


def status(db: Database, install_dir: str) -> str:
    lines = [
        f"pg-mcp {__version__} on Python {platform.python_version()}, installed at {install_dir}",
        f"target: {db.describe_target()}",
    ]
    try:
        info = db.server_info()
    except DatabaseUnavailable as exc:
        lines.append(f"database: unavailable. {exc}")
        return "\n".join(lines)
    lines.append(f"database: connected to {info['database']} as {info['user']}, PostgreSQL {info['server_version']}")
    lines.append(
        f"limits: read only = {info['read_only']}, statement timeout = {info['statement_timeout']}, "
        f"list_tables returns at most {LIST_TABLES_LIMIT} entries"
    )
    return "\n".join(lines)


def list_tables(db: Database, schema: Optional[Any]) -> str:
    if schema is not None and (not isinstance(schema, str) or not schema.strip()):
        raise ToolError('schema must be a non-empty string such as "public"; omit it to list all schemas')
    rows = db.query(LIST_TABLES_SQL, schema=schema, limit=LIST_TABLES_LIMIT + 1)
    if not rows:
        if schema is None:
            raise ToolError("The current role cannot read any table in this database. Ask the DBA to grant SELECT.")
        visible = ", ".join(row[0] for row in db.query(SCHEMAS_SQL)) or "none"
        raise ToolError(f"No readable tables in schema {schema!r}. Schemas with readable tables: {visible}.")
    lines = [_describe_relation(*row) for row in rows[:LIST_TABLES_LIMIT]]
    if len(rows) > LIST_TABLES_LIMIT:
        lines.append(f"... more than {LIST_TABLES_LIMIT} entries; pass schema to narrow the list")
    return "\n".join(lines)


def _describe_relation(schema: str, name: str, kind: str, estimate: Optional[int], comment: Optional[str]) -> str:
    parts = [f"{schema}.{name}", RELATION_KINDS.get(kind, kind)]
    if kind in ("r", "p", "m"):
        parts.append(_estimate(estimate))
    if comment:
        parts.append(" ".join(comment.split()))  # keep each entry on one line
    return " | ".join(parts)


def _estimate(rows: Optional[int]) -> str:
    if rows is None or rows < 0:
        return "rows unknown (never analyzed)"
    for unit, suffix in ((10**9, "B"), (10**6, "M"), (10**3, "k")):
        if rows >= unit:
            return f"~{rows / unit:.1f}".rstrip("0").rstrip(".") + f"{suffix} rows"
    return f"~{rows} rows"
