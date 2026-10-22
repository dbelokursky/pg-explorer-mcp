"""The tools the agent sees.

All of them only read, and their limits live here in code: a prompt can ask
the model to behave, only the server can make sure it does. Every answer passes
through the response budget (budget.py) on its way out, free SQL through the
read-only check (sqlguard.py), and the group profile decides which tools a
server offers at all.
"""
from __future__ import annotations

import json
import platform
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from . import __version__
from .budget import DEFAULT_BUDGET, fit_to_budget, shorten
from .db import Database, DatabaseUnavailable, QueryFailed
from .sqlguard import ROW_SOURCES, SqlRejected, check_read_only

LIST_TABLES_LIMIT = 200
REFERENCED_BY_LIMIT = 50
SAMPLE_ROWS_DEFAULT = 10
SAMPLE_ROWS_LIMIT = 50
RUN_SELECT_LIMIT = 200
PLAN_LINES_LIMIT = 500

# Tools per group profile, from structure only to free read-only SQL (ADR 0002).
PROFILES: Dict[str, Tuple[str, ...]] = {
    "schema": ("status", "list_tables", "describe_table"),
    "explore": ("status", "list_tables", "describe_table", "sample_rows", "explain_query"),
    "analyst": ("status", "list_tables", "describe_table", "sample_rows", "explain_query", "run_select"),
}
DEFAULT_PROFILE = "explore"
STRICTEST_PROFILE = "schema"

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
    + 'ORDER BY n.nspname COLLATE "C", c.relname COLLATE "C"\nLIMIT :limit'
)

SCHEMAS_SQL = "SELECT DISTINCT n.nspname" + _READABLE_RELATIONS + "ORDER BY 1\nLIMIT 50"

FIND_RELATION_SQL = (
    "SELECT c.oid, n.nspname, c.relname, c.relkind, c.reltuples::bigint, obj_description(c.oid, 'pg_class')"
    + _READABLE_RELATIONS
    + "  AND c.relname = :name\n"
    + "  AND (CAST(:schema AS text) IS NULL OR n.nspname = CAST(:schema AS text))\n"
    + 'ORDER BY n.nspname COLLATE "C"'
)

# Names are ordered with COLLATE "C" so answers do not depend on the server's locale.
SIMILAR_RELATIONS_SQL = (
    "SELECT n.nspname || '.' || c.relname"
    + _READABLE_RELATIONS
    + "  AND c.relname ILIKE '%' || :fragment || '%'\n"
    + 'ORDER BY n.nspname COLLATE "C", c.relname COLLATE "C"\nLIMIT 10'
)

COLUMNS_SQL = """
SELECT a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull,
       pg_get_expr(d.adbin, d.adrelid), col_description(a.attrelid, a.attnum)
FROM pg_catalog.pg_attribute a
LEFT JOIN pg_catalog.pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
WHERE a.attrelid = CAST(:oid AS oid) AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum
"""

# NOT NULL constraints (contype 'n', PostgreSQL 18+) are left out: the column list shows them.
CONSTRAINTS_SQL = """
SELECT con.conname, pg_get_constraintdef(con.oid, true)
FROM pg_catalog.pg_constraint con
WHERE con.conrelid = CAST(:oid AS oid) AND con.contype IN ('p', 'u', 'f', 'c', 'x')
ORDER BY CASE con.contype WHEN 'p' THEN 0 WHEN 'u' THEN 1 WHEN 'f' THEN 2 ELSE 3 END, con.conname
"""

REFERENCED_BY_SQL = """
SELECT con.conrelid::regclass::text, pg_get_constraintdef(con.oid, true)
FROM pg_catalog.pg_constraint con
WHERE con.confrelid = CAST(:oid AS oid) AND con.contype = 'f'
  AND has_table_privilege(con.conrelid, 'SELECT')
ORDER BY con.conrelid::regclass::text COLLATE "C", con.conname COLLATE "C"
LIMIT :limit
"""

INDEXES_SQL = """
SELECT pg_get_indexdef(ix.indexrelid)
FROM pg_catalog.pg_index ix
JOIN pg_catalog.pg_class i ON i.oid = ix.indexrelid
WHERE ix.indrelid = CAST(:oid AS oid)
ORDER BY i.relname COLLATE "C"
"""

TABLE_ARGUMENT = {"type": "string", "description": 'Table or view name, optionally with its schema: "orders" or "sales.orders".'}
SCHEMA_ARGUMENT = {"type": "string", "description": "Schema to look in. Omit it to search every schema the role can read."}
QUERY_ARGUMENT = {"type": "string", "description": "One SQL statement in the PostgreSQL dialect."}

Answer = Union[str, List[str]]


class ToolError(Exception):
    """A failure the model should read and act on; sent back as an isError result."""


@dataclass(frozen=True)
class Tool:
    name: str
    title: str
    description: str
    properties: Dict[str, Any]
    handler: Callable[[Dict[str, Any]], Answer]
    required: Tuple[str, ...] = ()
    budget: int = DEFAULT_BUDGET

    def describe(self) -> Dict[str, Any]:
        schema: Dict[str, Any] = {"type": "object", "properties": self.properties, "additionalProperties": False}
        if self.required:
            schema["required"] = list(self.required)
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "inputSchema": schema,
            "annotations": dict(READ_ONLY_HINTS, title=self.title),
        }

    def call(self, arguments: Any) -> str:
        """Run the tool; bad arguments, refused SQL and database failures become ToolError."""
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise ToolError("arguments must be a JSON object")
        unknown = sorted(set(arguments) - set(self.properties))
        if unknown:
            allowed = ", ".join(sorted(self.properties)) or "none"
            raise ToolError(f"Unknown argument(s): {', '.join(unknown)}. Allowed: {allowed}.")
        missing = [name for name in self.required if arguments.get(name) in (None, "")]
        if missing:
            raise ToolError(f"Missing argument(s): {', '.join(missing)}.")
        try:
            answer = self.handler(arguments)
        except (DatabaseUnavailable, QueryFailed, SqlRejected) as exc:
            raise ToolError(str(exc)) from exc
        lines = answer.splitlines() if isinstance(answer, str) else answer
        return "\n".join(fit_to_budget(lines, self.budget))


def profile_from_env(env: Mapping[str, str]) -> Tuple[str, Optional[str]]:
    """The profile PG_MCP_PROFILE names, and a warning when the value is unusable."""
    raw = env.get("PG_MCP_PROFILE") or DEFAULT_PROFILE
    if raw.strip().lower() in PROFILES:
        return raw.strip().lower(), None
    return STRICTEST_PROFILE, f"PG_MCP_PROFILE={raw!r} is not one of {', '.join(PROFILES)}; using {STRICTEST_PROFILE!r}"


def build_tools(
    db: Database,
    install_dir: str,
    budget: int = DEFAULT_BUDGET,
    profile: str = DEFAULT_PROFILE,
    profile_warning: Optional[str] = None,
) -> List[Tool]:
    names = PROFILES[profile]
    analyze = profile == "analyst"
    explain_description = (
        "Show PostgreSQL's plan for one read-only query: SELECT, WITH ... SELECT, VALUES or TABLE, "
        "passed without EXPLAIN. The query is not run"
    )
    explain_properties: Dict[str, Any] = {"query": QUERY_ARGUMENT}
    if analyze:
        explain_description += (
            " unless analyze is true: then it runs inside a read-only transaction and the plan shows real timings"
        )
        explain_properties["analyze"] = {"type": "boolean", "description": "Run the query to get real timings. Default false."}
    tools = [
        Tool(
            name="status",
            title="Database connection status",
            description=(
                "Show which PostgreSQL database this server uses, as which role, the profile with its tools, "
                "and the limits in force. Call it first, and again whenever another tool reports a connection "
                "problem: its output says what to fix."
            ),
            properties={},
            handler=lambda args: status(db, install_dir, budget, profile, names, profile_warning),
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
        Tool(
            name="describe_table",
            title="Describe a table",
            description=(
                "Describe one table or view the current role can read: columns with types, NOT NULL, "
                "defaults and comments, then primary, unique and foreign keys, the tables that "
                'reference it, and its indexes. Accepts "orders" or "sales.orders".'
            ),
            properties={"table": TABLE_ARGUMENT, "schema": SCHEMA_ARGUMENT},
            required=("table",),
            handler=lambda args: describe_table(db, args.get("table"), args.get("schema")),
        ),
        Tool(
            name="sample_rows",
            title="Sample rows",
            description=(
                "Show a few rows of one table or view the current role can read, optionally only some columns. "
                f"Default {SAMPLE_ROWS_DEFAULT} rows, at most {SAMPLE_ROWS_LIMIT}, in no particular order."
            ),
            properties={
                "table": TABLE_ARGUMENT,
                "schema": SCHEMA_ARGUMENT,
                "columns": {"type": "string", "description": 'Comma-separated column names, e.g. "id, total". Omit for all.'},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": SAMPLE_ROWS_LIMIT,
                    "description": f"How many rows, 1 to {SAMPLE_ROWS_LIMIT}. Default {SAMPLE_ROWS_DEFAULT}.",
                },
            },
            required=("table",),
            handler=lambda args: sample_rows(db, args.get("table"), args.get("schema"), args.get("columns"), args.get("limit")),
        ),
        Tool(
            name="explain_query",
            title="Explain a query",
            description=explain_description + ".",
            properties=explain_properties,
            required=("query",),
            handler=lambda args: explain_query(db, args.get("query"), args.get("analyze") if analyze else None),
        ),
        Tool(
            name="run_select",
            title="Run a read-only query",
            description=(
                "Run one read-only SQL statement in the PostgreSQL dialect: SELECT, WITH ... SELECT, VALUES, "
                f"TABLE, EXPLAIN or SHOW. Returns at most {RUN_SELECT_LIMIT} rows; use WHERE, ORDER BY and "
                "LIMIT to get the rows you need. Statements that change data, structure, settings or other "
                "sessions are refused."
            ),
            properties={"query": QUERY_ARGUMENT},
            required=("query",),
            handler=lambda args: run_select(db, args.get("query")),
        ),
    ]
    offered = [tool for tool in tools if tool.name in names]
    return [Tool(t.name, t.title, t.description, t.properties, t.handler, t.required, budget) for t in offered]


def status(
    db: Database, install_dir: str, budget: int, profile: str, names: Sequence[str], warning: Optional[str]
) -> List[str]:
    lines = [
        f"pg-mcp {__version__} on Python {platform.python_version()}, installed at {install_dir}",
        f"target: {db.describe_target()}",
        f"profile: {profile} ({', '.join(names)})",
    ]
    if warning:
        lines.append(f"warning: {warning}")
    try:
        info = db.server_info()
    except DatabaseUnavailable as exc:
        lines.append(f"database: unavailable. {exc}")
        return lines
    lines.append(f"database: connected to {info['database']} as {info['user']}, PostgreSQL {info['server_version']}")
    lines.append(
        f"limits: read only = {info['read_only']}, statement timeout = {info['statement_timeout']}, "
        f"list_tables returns at most {LIST_TABLES_LIMIT} entries, answers are cut at {budget} characters"
    )
    return lines


def list_tables(db: Database, schema: Optional[Any]) -> List[str]:
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
    return lines


def describe_table(db: Database, table: Any, schema: Any) -> List[str]:
    oid, schema_name, relation, kind, estimate, comment = _resolve_relation(db, table, schema)
    lines = [_describe_relation(schema_name, relation, kind, estimate, comment), "columns:"]
    for column, data_type, not_null, default, column_comment in db.query(COLUMNS_SQL, oid=oid):
        line = f"  {column} {data_type}"
        if not_null:
            line += " NOT NULL"
        if default:
            line += f" DEFAULT {shorten(default)}"
        if column_comment:
            line += f"  -- {shorten(column_comment)}"
        lines.append(line)
    constraints = db.query(CONSTRAINTS_SQL, oid=oid)
    _section(lines, "constraints:", (f"{name}: {shorten(definition)}" for name, definition in constraints))
    referenced_by = db.query(REFERENCED_BY_SQL, oid=oid, limit=REFERENCED_BY_LIMIT)
    _section(lines, "referenced by:", (f"{source}: {shorten(definition)}" for source, definition in referenced_by))
    _section(lines, "indexes:", (shorten(definition) for (definition,) in db.query(INDEXES_SQL, oid=oid)))
    return lines


def sample_rows(db: Database, table: Any, schema: Any, columns: Any, limit: Any) -> List[str]:
    oid, schema_name, relation = _resolve_relation(db, table, schema)[:3]
    available = [row[0] for row in db.query(COLUMNS_SQL, oid=oid)]
    chosen = _choose_columns(available, columns, f"{schema_name}.{relation}")
    count = _whole_number(limit, "limit", SAMPLE_ROWS_DEFAULT, 1, SAMPLE_ROWS_LIMIT)
    sql = f"SELECT {', '.join(map(_quote_ident, chosen))} FROM {_quote_ident(schema_name)}.{_quote_ident(relation)}"
    names, rows, more = db.read_only(sql, count, cursor=True)
    return [f"{schema_name}.{relation}:"] + _format_rows(names, rows, more)


def explain_query(db: Database, query: Any, analyze: Any) -> List[str]:
    if check_read_only(query) not in ROW_SOURCES:
        raise ToolError("explain_query takes a SELECT, WITH ... SELECT, VALUES or TABLE statement, without EXPLAIN.")
    options = "ANALYZE, BUFFERS, FORMAT TEXT" if _flag(analyze, "analyze") else "FORMAT TEXT"
    _, rows, more = db.read_only(f"EXPLAIN ({options}) {query}", PLAN_LINES_LIMIT, cursor=False)
    return _plan_lines(rows, more)


def run_select(db: Database, query: Any) -> List[str]:
    kind = check_read_only(query)
    if kind in ROW_SOURCES:
        names, rows, more = db.read_only(query, RUN_SELECT_LIMIT, cursor=True)
        return _format_rows(names, rows, more)
    names, rows, more = db.read_only(query, PLAN_LINES_LIMIT, cursor=False)
    return _plan_lines(rows, more) if kind == "explain" else _format_rows(names, rows, more)


def _resolve_relation(db: Database, table: Any, schema: Any) -> Tuple[Any, ...]:
    """The pg_class row of the one readable relation the arguments name, or a ToolError with a hint."""
    name, schema = _split_name(table, schema)
    matches = db.query(FIND_RELATION_SQL, name=name, schema=schema)
    if not matches and name != name.lower():
        # Unquoted identifiers are folded to lower case, so "Orders" usually means orders.
        name = name.lower()
        matches = db.query(FIND_RELATION_SQL, name=name, schema=schema)
    if not matches:
        raise ToolError(_not_found(db, name, schema))
    if len(matches) > 1:
        options = ", ".join(f"{row[1]}.{row[2]}" for row in matches)
        raise ToolError(f"{name!r} exists in several schemas: {options}. Pass schema to pick one.")
    return tuple(matches[0])


def _split_name(table: Any, schema: Any) -> Tuple[str, Optional[str]]:
    if not isinstance(table, str) or not table.strip():
        raise ToolError('table must be a non-empty string such as "orders" or "sales.orders"')
    if schema is not None and (not isinstance(schema, str) or not schema.strip()):
        raise ToolError('schema must be a non-empty string such as "public"; omit it to search every schema')
    name = table.strip()
    if schema is None and "." in name:
        schema, name = name.split(".", 1)
    return name.strip().strip('"'), schema.strip().strip('"') if schema else None


def _not_found(db: Database, name: str, schema: Optional[str]) -> str:
    where = f" in schema {schema!r}" if schema else ""
    fragment = name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    similar = [row[0] for row in db.query(SIMILAR_RELATIONS_SQL, fragment=fragment)]
    if similar:
        return f"No readable table or view named {name!r}{where}. Similar names: {', '.join(similar)}."
    return f"No readable table or view named {name!r}{where}. Call list_tables to see what exists."


def _choose_columns(available: List[str], columns: Any, relation: str) -> List[str]:
    if columns is None or columns == "":
        return available
    if isinstance(columns, str):
        wanted = [part.strip().strip('"') for part in columns.split(",") if part.strip()]
    elif isinstance(columns, list) and all(isinstance(part, str) for part in columns):
        wanted = [part.strip().strip('"') for part in columns if part.strip()]
    else:
        raise ToolError('columns must be a comma-separated string such as "id, total"')
    by_name = {name: name for name in available}
    by_lower = {name.lower(): name for name in available}
    chosen = []
    for name in wanted:
        match = by_name.get(name) or by_lower.get(name.lower())
        if match is None:
            raise ToolError(f"No column {name!r} in {relation}. Columns: {', '.join(available)}.")
        chosen.append(match)
    return chosen or available


def _whole_number(value: Any, name: str, default: int, low: int, high: int) -> int:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        raise ToolError(f"{name} must be a whole number from {low} to {high}")
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ToolError(f"{name} must be a whole number from {low} to {high}") from None
    if number != value and str(number) != str(value).strip():
        raise ToolError(f"{name} must be a whole number from {low} to {high}")
    if not low <= number <= high:
        raise ToolError(f"{name} must be from {low} to {high}, got {number}")
    return number


def _flag(value: Any, name: str) -> bool:
    if value is None or isinstance(value, bool):
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in ("true", "false", "yes", "no", "1", "0"):
        return value.strip().lower() in ("true", "yes", "1")
    raise ToolError(f"{name} must be true or false")


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _format_rows(names: List[str], rows: List[Sequence[Any]], more: bool) -> List[str]:
    lines = [" | ".join(names)] if names else []
    lines.extend(" | ".join(_cell(value) for value in row) for row in rows)
    if more:
        lines.append(f"({len(rows)} rows shown, there are more: narrow the query with WHERE, ORDER BY or LIMIT)")
    else:
        lines.append(f"({len(rows)} row{'' if len(rows) == 1 else 's'})")
    return lines


def _plan_lines(rows: List[Sequence[Any]], more: bool) -> List[str]:
    lines = [str(row[0]) for row in rows]
    if more:
        lines.append(f"... plan longer than {PLAN_LINES_LIMIT} lines")
    return lines


def _cell(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (bytes, bytearray, memoryview)):
        return shorten("\\x" + bytes(value).hex())
    if isinstance(value, (dict, list)):
        return shorten(json.dumps(value, ensure_ascii=False, default=str))
    return shorten(str(value))


def _section(lines: List[str], title: str, entries: Any) -> None:
    entries = list(entries)
    if entries:
        lines.append(title)
        lines.extend(f"  {entry}" for entry in entries)


def _describe_relation(schema: str, name: str, kind: str, estimate: Optional[int], comment: Optional[str]) -> str:
    parts = [f"{schema}.{name}", RELATION_KINDS.get(kind, kind)]
    if kind in ("r", "p", "m"):
        parts.append(_estimate(estimate))
    if comment:
        parts.append(shorten(comment))
    return " | ".join(parts)


def _estimate(rows: Optional[int]) -> str:
    if rows is None or rows < 0:
        return "rows unknown (never analyzed)"
    for unit, suffix in ((10**9, "B"), (10**6, "M"), (10**3, "k")):
        if rows >= unit:
            return f"~{rows / unit:.1f}".rstrip("0").rstrip(".") + f"{suffix} rows"
    return f"~{rows} rows"
