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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Callable, Union

from . import __version__
from .budget import DEFAULT_BUDGET, fit_to_budget, shorten
from .db import Database, DatabaseUnavailableError, QueryFailedError
from .i18n import current, number, plural, t
from .sqlguard import ROW_SOURCES, SqlRejectedError, check_read_only

LIST_TABLES_LIMIT = 200
REFERENCED_BY_LIMIT = 50
SAMPLE_ROWS_DEFAULT = 10
SAMPLE_ROWS_LIMIT = 50
RUN_SELECT_LIMIT = 200
PLAN_LINES_LIMIT = 500

# Tools per group profile, from structure only to free read-only SQL (ADR 0002).
PROFILES: dict[str, tuple[str, ...]] = {
    "schema": ("status", "list_tables", "describe_table"),
    "explore": ("status", "list_tables", "describe_table", "sample_rows", "explain_query"),
    "analyst": ("status", "list_tables", "describe_table", "sample_rows", "explain_query", "run_select"),
}
DEFAULT_PROFILE = "explore"
STRICTEST_PROFILE = "schema"

# MCP tool annotations: clients may run read-only tools without asking the user.
READ_ONLY_HINTS = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}

# pg_class.relkind values the tools show; each is named by the text kind.<relkind>.
RELATION_KINDS = ("r", "p", "v", "m", "f")

_READABLE_RELATIONS = """
FROM pg_catalog.pg_class c
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f')
  AND n.nspname <> 'information_schema'
  AND n.nspname NOT LIKE 'pg\\_%'
  AND has_table_privilege(c.oid, 'SELECT')
"""

# Estimated rows. PostgreSQL 14+ keeps -1 until a table is first analyzed; before 14 such a
# table shows 0 rows in 0 pages, exactly like an empty one, so that case becomes NULL.
_ESTIMATE = (
    "CASE WHEN c.reltuples = 0 AND c.relpages = 0 AND current_setting('server_version_num')::int < 140000"
    " THEN NULL ELSE c.reltuples::bigint END"
)

LIST_TABLES_SQL = (
    f"SELECT n.nspname, c.relname, c.relkind, {_ESTIMATE}, obj_description(c.oid, 'pg_class')"
    + _READABLE_RELATIONS
    + "  AND (CAST(:schema AS text) IS NULL OR n.nspname = CAST(:schema AS text))\n"
    + 'ORDER BY n.nspname COLLATE "C", c.relname COLLATE "C"\nLIMIT :limit'
)

SCHEMAS_SQL = "SELECT DISTINCT n.nspname" + _READABLE_RELATIONS + "ORDER BY 1\nLIMIT 50"

FIND_RELATION_SQL = (
    f"SELECT c.oid, n.nspname, c.relname, c.relkind, {_ESTIMATE}, obj_description(c.oid, 'pg_class')"
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

Answer = Union[str, list[str]]


class ToolError(Exception):
    """A failure the model should read and act on; sent back as an isError result."""


@dataclass(frozen=True)
class Tool:
    name: str
    title: str
    description: str
    properties: dict[str, Any]
    handler: Callable[[dict[str, Any]], Answer]
    required: tuple[str, ...] = ()
    budget: int = DEFAULT_BUDGET

    def describe(self) -> dict[str, Any]:
        schema: dict[str, Any] = {"type": "object", "properties": self.properties, "additionalProperties": False}
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
            raise ToolError(t("error.arguments"))
        unknown = sorted(set(arguments) - set(self.properties))
        if unknown:
            allowed = ", ".join(sorted(self.properties)) or t("error.none")
            raise ToolError(t("error.unknown_arguments", unknown=", ".join(unknown), allowed=allowed))
        missing = [name for name in self.required if arguments.get(name) in (None, "")]
        if missing:
            raise ToolError(t("error.missing_arguments", missing=", ".join(missing)))
        try:
            answer = self.handler(arguments)
        except (DatabaseUnavailableError, QueryFailedError, SqlRejectedError) as exc:
            raise ToolError(str(exc)) from exc
        lines = answer.splitlines() if isinstance(answer, str) else answer
        return "\n".join(fit_to_budget(lines, self.budget))


def profile_from_env(env: Mapping[str, str]) -> tuple[str, str | None]:
    """The profile PG_MCP_PROFILE names, and a warning when the value is unusable."""
    raw = env.get("PG_MCP_PROFILE") or DEFAULT_PROFILE
    if raw.strip().lower() in PROFILES:
        return raw.strip().lower(), None
    return STRICTEST_PROFILE, t("warning.profile", value=raw, choices=", ".join(PROFILES), fallback=STRICTEST_PROFILE)


def build_tools(
    db: Database,
    install_dir: str,
    budget: int = DEFAULT_BUDGET,
    profile: str = DEFAULT_PROFILE,
    warnings: Sequence[str] = (),
) -> list[Tool]:
    """The tools of this profile, described in the current language."""
    names = PROFILES[profile]
    analyze = profile == "analyst"
    table_argument = {"type": "string", "description": t("argument.table")}
    schema_argument = {"type": "string", "description": t("argument.schema")}
    query_argument = {"type": "string", "description": t("argument.query")}
    explain_properties: dict[str, Any] = {"query": query_argument}
    if analyze:
        explain_properties["analyze"] = {"type": "boolean", "description": t("argument.analyze")}
    tools = [
        Tool(
            name="status",
            title=t("tool.status.title"),
            description=t("tool.status.description"),
            properties={},
            handler=lambda _args: status(
                db, install_dir, budget=budget, profile=profile, names=names, warnings=warnings
            ),
        ),
        Tool(
            name="list_tables",
            title=t("tool.list_tables.title"),
            description=t("tool.list_tables.description", limit=LIST_TABLES_LIMIT),
            properties={"schema": {"type": "string", "description": t("tool.list_tables.schema")}},
            handler=lambda args: list_tables(db, args.get("schema")),
        ),
        Tool(
            name="describe_table",
            title=t("tool.describe_table.title"),
            description=t("tool.describe_table.description"),
            properties={"table": table_argument, "schema": schema_argument},
            required=("table",),
            handler=lambda args: describe_table(db, args.get("table"), args.get("schema")),
        ),
        Tool(
            name="sample_rows",
            title=t("tool.sample_rows.title"),
            description=t("tool.sample_rows.description", default=SAMPLE_ROWS_DEFAULT, limit=SAMPLE_ROWS_LIMIT),
            properties={
                "table": table_argument,
                "schema": schema_argument,
                "columns": {"type": "string", "description": t("argument.columns")},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": SAMPLE_ROWS_LIMIT,
                    "description": t("argument.limit", limit=SAMPLE_ROWS_LIMIT, default=SAMPLE_ROWS_DEFAULT),
                },
            },
            required=("table",),
            handler=lambda args: sample_rows(
                db, args.get("table"), args.get("schema"), args.get("columns"), args.get("limit")
            ),
        ),
        Tool(
            name="explain_query",
            title=t("tool.explain_query.title"),
            description=t("tool.explain_query.description_analyze" if analyze else "tool.explain_query.description"),
            properties=explain_properties,
            required=("query",),
            handler=lambda args: explain_query(db, args.get("query"), args.get("analyze") if analyze else None),
        ),
        Tool(
            name="run_select",
            title=t("tool.run_select.title"),
            description=t("tool.run_select.description", limit=RUN_SELECT_LIMIT),
            properties={"query": query_argument},
            required=("query",),
            handler=lambda args: run_select(db, args.get("query")),
        ),
    ]
    offered = [tool for tool in tools if tool.name in names]
    return [
        Tool(tool.name, tool.title, tool.description, tool.properties, tool.handler, tool.required, budget)
        for tool in offered
    ]


def status(
    db: Database, install_dir: str, *, budget: int, profile: str, names: Sequence[str], warnings: Sequence[str]
) -> list[str]:
    lines = [
        t("status.version", version=__version__, python=platform.python_version(), path=install_dir),
        t("status.target", target=db.describe_target()),
        t("status.profile", profile=profile, tools=", ".join(names)),
        t("status.language", language=current()),
    ]
    lines.extend(t("status.warning", warning=warning) for warning in warnings)
    try:
        info = db.server_info()
    except DatabaseUnavailableError as exc:
        lines.append(t("status.unavailable", reason=exc))
        return lines
    lines.append(t("status.connected", database=info["database"], user=info["user"], version=info["server_version"]))
    lines.append(
        t(
            "status.limits",
            read_only=info["read_only"],
            timeout=info["statement_timeout"],
            tables=LIST_TABLES_LIMIT,
            budget=budget,
        )
    )
    return lines


def list_tables(db: Database, schema: Any | None) -> list[str]:
    if schema is not None and (not isinstance(schema, str) or not schema.strip()):
        raise ToolError(t("error.schema"))
    rows = db.query(LIST_TABLES_SQL, schema=schema, limit=LIST_TABLES_LIMIT + 1)
    if not rows:
        if schema is None:
            raise ToolError(t("error.no_tables"))
        visible = ", ".join(row[0] for row in db.query(SCHEMAS_SQL)) or t("error.none")
        raise ToolError(t("error.no_tables_in_schema", schema=schema, visible=visible))
    lines = [_describe_relation(*row) for row in rows[:LIST_TABLES_LIMIT]]
    if len(rows) > LIST_TABLES_LIMIT:
        lines.append(t("list.more", limit=LIST_TABLES_LIMIT))
    return lines


def describe_table(db: Database, table: Any, schema: Any) -> list[str]:
    oid, schema_name, relation, kind, estimate, comment = _resolve_relation(db, table, schema)
    lines = [_describe_relation(schema_name, relation, kind, estimate, comment), t("describe.columns")]
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
    _section(lines, t("describe.constraints"), (f"{name}: {shorten(definition)}" for name, definition in constraints))
    referenced_by = db.query(REFERENCED_BY_SQL, oid=oid, limit=REFERENCED_BY_LIMIT)
    _section(
        lines,
        t("describe.referenced_by"),
        (f"{source}: {shorten(definition)}" for source, definition in referenced_by),
    )
    indexes = db.query(INDEXES_SQL, oid=oid)
    _section(lines, t("describe.indexes"), (shorten(definition) for (definition,) in indexes))
    return lines


def sample_rows(db: Database, table: Any, schema: Any, columns: Any, limit: Any) -> list[str]:
    oid, schema_name, relation = _resolve_relation(db, table, schema)[:3]
    available = [row[0] for row in db.query(COLUMNS_SQL, oid=oid)]
    chosen = _choose_columns(available, columns, f"{schema_name}.{relation}")
    count = _whole_number(limit, "limit", SAMPLE_ROWS_DEFAULT, 1, SAMPLE_ROWS_LIMIT)
    # Every name comes from the catalog and is quoted, so this string cannot smuggle SQL in.
    columns_sql = ", ".join(map(_quote_ident, chosen))
    sql = f"SELECT {columns_sql} FROM {_quote_ident(schema_name)}.{_quote_ident(relation)}"  # noqa: S608
    names, rows, more = db.read_only(sql, count, cursor=True)
    return [f"{schema_name}.{relation}:", *_format_rows(names, rows, more=more)]


def explain_query(db: Database, query: Any, analyze: Any) -> list[str]:
    if check_read_only(query) not in ROW_SOURCES:
        raise ToolError(t("error.explain_row_sources"))
    options = "ANALYZE, BUFFERS, FORMAT TEXT" if _flag(analyze, "analyze") else "FORMAT TEXT"
    _, rows, more = db.read_only(f"EXPLAIN ({options}) {query}", PLAN_LINES_LIMIT, cursor=False)
    return _plan_lines(rows, more=more)


def run_select(db: Database, query: Any) -> list[str]:
    kind = check_read_only(query)
    if kind in ROW_SOURCES:
        names, rows, more = db.read_only(query, RUN_SELECT_LIMIT, cursor=True)
        return _format_rows(names, rows, more=more)
    names, rows, more = db.read_only(query, PLAN_LINES_LIMIT, cursor=False)
    return _plan_lines(rows, more=more) if kind == "explain" else _format_rows(names, rows, more=more)


def _resolve_relation(db: Database, table: Any, schema: Any) -> tuple[Any, ...]:
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
        raise ToolError(t("error.ambiguous", name=name, options=options))
    return tuple(matches[0])


def _split_name(table: Any, schema: Any) -> tuple[str, str | None]:
    if not isinstance(table, str) or not table.strip():
        raise ToolError(t("error.table"))
    if schema is not None and (not isinstance(schema, str) or not schema.strip()):
        raise ToolError(t("error.schema_search"))
    name = table.strip()
    if schema is None and "." in name:
        schema, name = name.split(".", 1)
    return name.strip().strip('"'), schema.strip().strip('"') if schema else None


def _not_found(db: Database, name: str, schema: str | None) -> str:
    where = t("error.in_schema", schema=schema) if schema else ""
    fragment = name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    similar = [row[0] for row in db.query(SIMILAR_RELATIONS_SQL, fragment=fragment)]
    if similar:
        return t("error.not_found_similar", name=name, where=where, similar=", ".join(similar))
    return t("error.not_found", name=name, where=where)


def _choose_columns(available: list[str], columns: Any, relation: str) -> list[str]:
    if columns is None or columns == "":
        return available
    if isinstance(columns, str):
        wanted = [part.strip().strip('"') for part in columns.split(",") if part.strip()]
    elif isinstance(columns, list) and all(isinstance(part, str) for part in columns):
        wanted = [part.strip().strip('"') for part in columns if part.strip()]
    else:
        raise ToolError(t("error.columns"))
    by_name = {name: name for name in available}
    by_lower = {name.lower(): name for name in available}
    chosen = []
    for name in wanted:
        match = by_name.get(name) or by_lower.get(name.lower())
        if match is None:
            raise ToolError(t("error.no_column", name=name, relation=relation, columns=", ".join(available)))
        chosen.append(match)
    return chosen or available


def _whole_number(value: Any, name: str, default: int, low: int, high: int) -> int:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        raise ToolError(t("error.whole_number", name=name, low=low, high=high))
    try:
        whole = int(value)
    except (TypeError, ValueError):
        raise ToolError(t("error.whole_number", name=name, low=low, high=high)) from None
    if whole != value and str(whole) != str(value).strip():
        raise ToolError(t("error.whole_number", name=name, low=low, high=high))
    if not low <= whole <= high:
        raise ToolError(t("error.out_of_range", name=name, low=low, high=high, number=whole))
    return whole


def _flag(value: Any, name: str) -> bool:
    if value is None or isinstance(value, bool):
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in ("true", "false", "yes", "no", "1", "0"):
        return value.strip().lower() in ("true", "yes", "1")
    raise ToolError(t("error.flag", name=name))


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _format_rows(names: list[str], rows: list[Sequence[Any]], *, more: bool) -> list[str]:
    lines = [" | ".join(names)] if names else []
    lines.extend(" | ".join(_cell(value) for value in row) for row in rows)
    lines.append(plural("rows.more" if more else "rows.count", len(rows)))
    return lines


def _plan_lines(rows: list[Sequence[Any]], *, more: bool) -> list[str]:
    lines = [str(row[0]) for row in rows]
    if more:
        lines.append(t("plan.more", limit=PLAN_LINES_LIMIT))
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


def _section(lines: list[str], title: str, entries: Any) -> None:
    entries = list(entries)
    if entries:
        lines.append(title)
        lines.extend(f"  {entry}" for entry in entries)


def _describe_relation(schema: str, name: str, kind: str, estimate: int | None, comment: str | None) -> str:
    parts = [f"{schema}.{name}", t(f"kind.{kind}") if kind in RELATION_KINDS else kind]
    if kind in ("r", "p", "m"):
        parts.append(_estimate(estimate))
    if comment:
        parts.append(shorten(comment))
    return " | ".join(parts)


def _estimate(rows: int | None) -> str:
    """A row estimate from _ESTIMATE: None is "empty or never analyzed", a negative count "never analyzed"."""
    if rows is None:
        return t("estimate.empty_or_unknown")
    if rows < 0:
        return t("estimate.unknown")
    for unit, scale in ((10**9, "estimate.billions"), (10**6, "estimate.millions"), (10**3, "estimate.thousands")):
        if rows >= unit:
            return t("estimate.scaled", amount=t(scale, value=number(rows / unit)))
    return plural("estimate.rows", rows)
