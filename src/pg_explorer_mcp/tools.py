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
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Callable, Union

from . import __version__, diagram
from .budget import DEFAULT_BUDGET, fit_to_budget, shorten
from .db import Database, DatabaseUnavailableError, QueryFailedError
from .i18n import current, number, plural, t
from .querylog import ERROR, FAILED, OK, REFUSED, UNAVAILABLE, Mark, QueryLog
from .sqlguard import ROW_SOURCES, SqlRejectedError, check_read_only
from .stands import StandError

LIST_TABLES_LIMIT = 200
REFERENCED_BY_LIMIT = 50
SAMPLE_ROWS_DEFAULT = 10
SAMPLE_ROWS_LIMIT = 50
RUN_SELECT_LIMIT = 200
PLAN_LINES_LIMIT = 500
SEARCH_LIMIT = 100

# Tools per group profile, from structure only to free read-only SQL (ADR 0002).
PROFILES: dict[str, tuple[str, ...]] = {
    "schema": ("status", "list_tables", "search_schema", "describe_table", "schema_diagram"),
    "explore": (
        "status",
        "list_tables",
        "search_schema",
        "describe_table",
        "schema_diagram",
        "sample_rows",
        "explain_query",
    ),
    "analyst": (
        "status",
        "list_tables",
        "search_schema",
        "describe_table",
        "schema_diagram",
        "sample_rows",
        "explain_query",
        "run_select",
    ),
}
DEFAULT_PROFILE = "explore"
DEFAULT_NAME = "pg"  # the entry install.py writes; further databases are pg-<name>
STRICTEST_PROFILE = "schema"

# MCP tool annotations: clients may run read-only tools without asking the user. destructiveHint and
# idempotentHint mean something only when readOnlyHint is false, so they are left out; the title goes
# here rather than beside the name, where clients of protocol 2025-06-18 look first and older ones never
# look: clients of every version fall back to this one.
READ_ONLY_HINTS = {"readOnlyHint": True, "openWorldHint": False}

# The SQLSTATE of a table PostgreSQL could not find, and its name in the message in any language:
# 'relation "orders" does not exist', 'отношение "orders" не существует', 'Relation »orders« ...'.
UNDEFINED_TABLE = "42P01"
UNDEFINED_COLUMN = "42703"
# The relations a statement reads: the name after FROM or JOIN, its schema and quotes as written.
_READ_RELATION = re.compile(
    r'\b(?:from|join)\s+("[^"]+"|[^\s,;()".]+)(?:\s*\.\s*("[^"]+"|[^\s,;()".]+))?', re.IGNORECASE
)
TABLES_LISTED = 5  # the tables of a statement whose columns a column error lists
COLUMNS_LISTED = 40
_QUOTED_NAME = re.compile(r'["«»„“]\s*([^"«»„“”]+?)\s*["«»“”]')

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

# Whether a schema holds a table or view at all, readable or not. One that holds none was guessed,
# "public" most often; one whose tables the role cannot read was not, and it is refused as before.
SCHEMA_HOLDS_RELATIONS_SQL = """
SELECT 1
FROM pg_catalog.pg_class c
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = CAST(:schema AS text) AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
LIMIT 1
"""

# A relation as the statement found it: to_regclass reads the name with its schema and quotes, or on
# the search path of the session, as the statement did. Only one the role reads, as everywhere else:
# PostgreSQL reports a missing column before it checks the privilege to read the table.
RELATION_BY_NAME_SQL = """
SELECT c.oid, n.nspname, c.relname
FROM pg_catalog.pg_class c
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.oid = to_regclass(CAST(:name AS text))
  AND has_table_privilege(c.oid, 'SELECT')
"""

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

# Tables and views whose name or comment matches, then columns whose name or comment matches;
# a table's own line comes before its columns. A UNION may only be ordered by its output columns,
# so the collation is applied outside it.
_SEARCH_MATCHES = """
SELECT nspname, relname, relkind, estimate, comment, column_name, data_type, not_null, column_comment
FROM (
    SELECT r.nspname, r.relname, r.relkind, r.estimate, r.comment, NULL::name AS column_name,
           NULL::text AS data_type, NULL::boolean AS not_null, NULL::text AS column_comment, 0 AS position
    FROM readable r
    WHERE r.relname ILIKE ANY (CAST(:patterns AS text[])) OR r.comment ILIKE ANY (CAST(:patterns AS text[]))
    UNION ALL
    SELECT r.nspname, r.relname, r.relkind, NULL::bigint, NULL::text, a.attname,
           format_type(a.atttypid, a.atttypmod), a.attnotnull, col_description(a.attrelid, a.attnum), a.attnum
    FROM readable r
    JOIN pg_catalog.pg_attribute a ON a.attrelid = r.oid AND a.attnum > 0 AND NOT a.attisdropped
    WHERE a.attname ILIKE ANY (CAST(:patterns AS text[]))
       OR col_description(a.attrelid, a.attnum) ILIKE ANY (CAST(:patterns AS text[]))
) AS found
ORDER BY nspname COLLATE "C", relname COLLATE "C", position
LIMIT :limit
"""

SEARCH_SQL = (
    "WITH readable AS (\nSELECT c.oid, n.nspname, c.relname, c.relkind, "
    + _ESTIMATE
    + " AS estimate, obj_description(c.oid, 'pg_class') AS comment"
    + _READABLE_RELATIONS
    + "  AND (CAST(:schema AS text) IS NULL OR n.nspname = CAST(:schema AS text))\n)"
    + _SEARCH_MATCHES
)

# On a stand the catalog is read through any login of the database, so instead of that
# login's privileges the queries keep to the schemas the registry has logins for.
_PRIVILEGE = "  AND has_table_privilege(c.oid, 'SELECT')\n"
_REGISTRY_RELATIONS = _READABLE_RELATIONS.replace(_PRIVILEGE, "  AND n.nspname = ANY (CAST(:schemas AS text[]))\n")
LIST_TABLES_IN_SCHEMAS_SQL = LIST_TABLES_SQL.replace(_READABLE_RELATIONS, _REGISTRY_RELATIONS)
FIND_RELATION_IN_SCHEMAS_SQL = FIND_RELATION_SQL.replace(_READABLE_RELATIONS, _REGISTRY_RELATIONS)
SEARCH_IN_SCHEMAS_SQL = SEARCH_SQL.replace(_READABLE_RELATIONS, _REGISTRY_RELATIONS)

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
    query_log: QueryLog | None = None
    # A better answer than "Missing argument(s)" for the arguments and what is missing, or None.
    missing_hint: Callable[[dict[str, Any], list[str]], str | None] | None = None

    def describe(self) -> dict[str, Any]:
        schema: dict[str, Any] = {"type": "object", "properties": self.properties, "additionalProperties": False}
        if self.required:
            schema["required"] = list(self.required)
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": schema,
            "annotations": {"title": self.title, **READ_ONLY_HINTS},
        }

    def call(self, arguments: Any) -> str:
        """Run the tool; bad arguments, refused SQL and database failures become ToolError.

        Every call goes into the query log, however it ends.
        """
        mark = self.query_log.start() if self.query_log else None
        try:
            lines = self._run(arguments)
        except SqlRejectedError as exc:
            self._log(mark, arguments, REFUSED, str(exc))
            raise ToolError(str(exc)) from exc
        except DatabaseUnavailableError as exc:
            self._log(mark, arguments, UNAVAILABLE, str(exc))
            raise ToolError(str(exc)) from exc
        except (QueryFailedError, StandError) as exc:
            self._log(mark, arguments, ERROR, str(exc))
            raise ToolError(str(exc)) from exc
        except ToolError as exc:
            self._log(mark, arguments, ERROR, str(exc))
            raise
        except Exception as exc:
            self._log(mark, arguments, FAILED, f"{type(exc).__name__}: {exc}")
            raise
        self._log(mark, arguments, OK, None)
        return "\n".join(fit_to_budget(lines, self.budget))

    def _run(self, arguments: Any) -> list[str]:
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise ToolError(t("error.arguments"))
        unknown = sorted(set(arguments) - set(self.properties))
        if unknown:
            allowed = ", ".join(sorted(self.properties)) or t("error.none")
            raise ToolError(t("error.unknown_arguments", unknown=", ".join(unknown), allowed=allowed))
        # Models often send "" for an argument they mean to leave out; take it as left out.
        arguments = {
            name: value
            for name, value in arguments.items()
            if name in self.required or not (isinstance(value, str) and not value.strip())
        }
        missing = [name for name in self.required if arguments.get(name) in (None, "")]
        if missing:
            hint = self.missing_hint(arguments, missing) if self.missing_hint else None
            raise ToolError(hint or t("error.missing_arguments", missing=", ".join(missing)))
        answer = self.handler(arguments)
        return answer.splitlines() if isinstance(answer, str) else answer

    def _log(self, mark: Mark | None, arguments: Any, outcome: str, message: str | None) -> None:
        if self.query_log is not None and mark is not None:
            self.query_log.write(mark, self.name, arguments, outcome, message)


def profile_from_env(env: Mapping[str, str]) -> tuple[str, str | None]:
    """The profile PG_EXPLORER_MCP_PROFILE names, and a warning when the value is unusable."""
    raw = env.get("PG_EXPLORER_MCP_PROFILE") or DEFAULT_PROFILE
    if raw.strip().lower() in PROFILES:
        return raw.strip().lower(), None
    return STRICTEST_PROFILE, t("warning.profile", value=raw, choices=", ".join(PROFILES), fallback=STRICTEST_PROFILE)


def build_tools(
    db: Database,
    install_dir: str,
    *,
    budget: int = DEFAULT_BUDGET,
    profile: str = DEFAULT_PROFILE,
    warnings: Sequence[str] = (),
    query_log: QueryLog | None = None,
    name: str = DEFAULT_NAME,
) -> list[Tool]:
    """The tools of this profile, described in the current language, all writing to one query log."""
    query_log = query_log or QueryLog(None)
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
                db,
                install_dir,
                budget=budget,
                profile=profile,
                names=names,
                warnings=warnings,
                query_log=query_log,
                name=name,
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
            name="search_schema",
            title=t("tool.search_schema.title"),
            description=t("tool.search_schema.description", limit=SEARCH_LIMIT),
            properties={"text": {"type": "string", "description": t("argument.text")}, "schema": schema_argument},
            required=("text",),
            handler=lambda args: search_schema(db, args.get("text"), args.get("schema")),
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
            name="schema_diagram",
            title=t("tool.schema_diagram.title"),
            description=t("tool.schema_diagram.description"),
            properties={
                "schema": {"type": "string", "description": t("argument.schema_diagram")},
                **diagram_properties(),
            },
            handler=lambda args: schema_diagram(
                db, args.get("schema"), args.get("table"), args.get("depth"), args.get("columns"), budget=budget
            ),
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
    return [replace(tool, budget=budget, query_log=query_log) for tool in offered]


def status(
    db: Database,
    install_dir: str,
    *,
    budget: int,
    profile: str,
    names: Sequence[str],
    warnings: Sequence[str],
    query_log: QueryLog,
    name: str = DEFAULT_NAME,
) -> list[str]:
    lines = [
        t("status.version", version=__version__, python=platform.python_version(), path=install_dir),
        t("status.name", name=name),
        t("status.read_only"),
        t("status.target", target=db.describe_target()),
        t("status.profile", profile=profile, tools=", ".join(names)),
        t("status.language", language=current()),
        query_log.describe(),
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


def list_tables(db: Database, schema: Any | None, schemas: Sequence[str] | None = None) -> list[str]:
    """Readable tables, or with schemas (on a stand) the tables of those schemas."""
    if schema is not None and (not isinstance(schema, str) or not schema.strip()):
        raise ToolError(t("error.schema"))
    schema = schema.strip().strip('"') if schema else None
    if schemas is None:
        rows = db.query(LIST_TABLES_SQL, schema=schema, limit=LIST_TABLES_LIMIT + 1)
    else:
        rows = db.query(LIST_TABLES_IN_SCHEMAS_SQL, schema=schema, schemas=list(schemas), limit=LIST_TABLES_LIMIT + 1)
    if not rows:
        if schemas is not None:  # the catalog does not depend on privileges here: the schemas are empty or gone
            raise ToolError(t("stands.no_tables", schemas=", ".join(schemas)))
        if schema is None:
            raise ToolError(t("error.no_tables"))
        if _guessed(db, schema):  # the question was what tables there are: those of every schema answer it
            return [t("list.guessed_schema", schema=schema), *list_tables(db, None)]
        raise ToolError(_closed_schema(db, schema))
    lines = [_describe_relation(*row) for row in rows[:LIST_TABLES_LIMIT]]
    if len(rows) > LIST_TABLES_LIMIT:
        lines.append(t("list.more", limit=LIST_TABLES_LIMIT))
    return lines


def search_schema(
    db: Database, text: Any, schema: Any, schemas: Sequence[str] | None = None, limit: int = SEARCH_LIMIT
) -> list[str]:
    """Matches of text; on a stand, schemas keeps to the registry's, and limit shares the matches among stands."""
    if not isinstance(text, str) or not text.strip():
        raise ToolError(t("error.text"))
    if schema is not None and (not isinstance(schema, str) or not schema.strip()):
        raise ToolError(t("error.schema_search"))
    fragment = text.strip()
    params: dict[str, Any] = {
        "patterns": _like_patterns(fragment),
        "schema": schema.strip().strip('"') if schema else None,
        "limit": limit + 1,
    }
    if schemas is None:
        rows = db.query(SEARCH_SQL, **params)
    else:
        rows = db.query(SEARCH_IN_SCHEMAS_SQL, schemas=list(schemas), **params)
    if not rows:
        if schemas is None and params["schema"] is not None:
            visible = [row[0] for row in db.query(SCHEMAS_SQL)]
            if params["schema"] not in visible:
                if _guessed(db, params["schema"]):  # "public" most often: the other schemas answer the question
                    lead = t("search.guessed_schema", schema=params["schema"])
                    return [lead, *search_schema(db, fragment, None, limit=limit)]
                raise ToolError(_closed_schema(db, params["schema"], visible))
        return [t("search.nothing", text=fragment)]
    lines = []
    for row in rows[:limit]:
        schema_name, relation, kind, estimate, comment, column, data_type, not_null, column_comment = row
        if column is None:
            lines.append(_describe_relation(schema_name, relation, kind, estimate, comment))
        else:
            name = f"{schema_name}.{relation}.{column}"
            lines.append(_column_line(name, data_type, not_null=not_null, comment=column_comment))
    if len(rows) > limit:
        lines.append(t("search.more", limit=limit))
    return lines


def _system(schema: str) -> bool:
    """A schema the tools leave out, as _READABLE_RELATIONS does."""
    return schema == "information_schema" or schema.startswith("pg_")


def _guessed(db: Database, schema: str) -> bool:
    """A schema without a single table or view, readable or not: the model made it up."""
    return not _system(schema) and not db.query(SCHEMA_HOLDS_RELATIONS_SQL, schema=schema)


def _closed_schema(db: Database, schema: str, visible: Sequence[str] | None = None) -> str:
    """Why a schema that holds tables shows none: the tools leave system schemas out, or the role reads none.

    Said plainly, so that a model answers "no access to hr" rather than "hr has no tables".
    """
    names = [row[0] for row in db.query(SCHEMAS_SQL)] if visible is None else visible
    key = "error.system_schema" if _system(schema) else "error.no_tables_in_schema"
    return t(key, schema=schema, visible=", ".join(names) or t("error.none"))


def describe_table(db: Database, table: Any, schema: Any) -> list[str]:
    oid, schema_name, relation, kind, estimate, comment = _resolve_relation(db, table, schema)
    lines = [_describe_relation(schema_name, relation, kind, estimate, comment), t("describe.columns")]
    for column, data_type, not_null, default, column_comment in db.query(COLUMNS_SQL, oid=oid):
        line = _column_line(column, data_type, not_null=not_null, default=default, comment=column_comment)
        lines.append(f"  {line}")
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


def diagram_properties() -> dict[str, Any]:
    """The arguments of schema_diagram besides the schema, which a stand takes with its login."""
    return {
        "table": {"type": "string", "description": t("argument.table_diagram")},
        "depth": {
            "type": "integer",
            "minimum": 0,
            "maximum": diagram.DEPTH_LIMIT,
            "description": t("argument.depth", limit=diagram.DEPTH_LIMIT, default=diagram.DEPTH_DEFAULT),
        },
        "columns": {"type": "string", "enum": list(diagram.DETAILS), "description": t("argument.columns_diagram")},
    }


def schema_diagram(
    db: Database, schema: Any, table: Any, depth: Any, columns: Any, *, budget: int = DEFAULT_BUDGET
) -> list[str]:
    """Mermaid erDiagram of a schema, or of one table with its neighbours by foreign keys; the data is not read.

    The answer has to fit the budget whole, since a cut diagram does not render: when it would not,
    the columns go first, then the keys, and a diagram that does not fit even so is an error that
    says how to ask for a part of it.
    """
    detail = _detail(columns)
    hops = _whole_number(depth, "depth", diagram.DEPTH_DEFAULT, 0, diagram.DEPTH_LIMIT)
    if table not in (None, ""):
        oid, schema_name, relation = _resolve_relation(db, table, schema or None)[:3]
        model = diagram.around(db, oid, schema_name, relation, hops)
        name = f"{schema_name}.{relation}"
        if hops:
            what = t("diagram.of_table", table=name, hops=plural("diagram.hops", hops))
        else:
            what = t("diagram.of_table_alone", table=name)
    else:
        if not isinstance(schema, str) or not schema.strip():
            raise ToolError(t("diagram.need_schema", visible=_visible_schemas(db)))
        name = schema.strip().strip('"')
        model = diagram.of_schema(db, name)
        if not model.tables:
            raise ToolError(_closed_schema(db, name))
        what = t("diagram.of_schema", schema=name)
    tables = plural("diagram.tables", len(model.tables))
    relationships = plural("diagram.relationships", len(model.foreign_keys))
    summary = t("diagram.summary", what=what, tables=tables, relationships=relationships)
    for level in diagram.DETAILS[diagram.DETAILS.index(detail) :]:
        notes = [summary]
        if level != detail:
            notes.append(t("diagram.columns_cut" if level == diagram.KEYS else "diagram.keys_cut"))
        lines = diagram.render(model, level, notes)
        if len("\n".join(lines)) <= budget:
            return lines
    raise ToolError(t("diagram.too_big", budget=budget, tables=tables, relationships=relationships))


def _detail(columns: Any) -> str:
    """How much of the columns a diagram shows: the keys unless asked for every column or none."""
    if columns in (None, ""):
        return diagram.KEYS
    if isinstance(columns, str) and columns.strip().lower() in diagram.DETAILS:
        return columns.strip().lower()
    raise ToolError(t("diagram.columns_mode"))


def _visible_schemas(db: Database) -> str:
    return ", ".join(str(row[0]) for row in db.query(SCHEMAS_SQL)) or t("error.none")


def explain_query(db: Database, query: Any, analyze: Any, schemas: Sequence[str] | None = None) -> list[str]:
    if check_read_only(query) not in ROW_SOURCES:
        raise ToolError(t("error.explain_row_sources"))
    options = "ANALYZE, BUFFERS, FORMAT TEXT" if _flag(analyze, "analyze") else "FORMAT TEXT"
    try:
        _, rows, more = db.read_only(f"EXPLAIN ({options}) {query}", PLAN_LINES_LIMIT, cursor=False)
    except QueryFailedError as exc:
        raise QueryFailedError(query_hint(db, exc, query, schemas)) from exc
    return _plan_lines(rows, more=more)


def run_select(
    db: Database, query: Any, limit: int = RUN_SELECT_LIMIT, schemas: Sequence[str] | None = None
) -> list[str]:
    """Rows of one read-only statement; limit is smaller when a call shares the rows among several stands.

    schemas, on a stand, are the registry's schemas of the database: where to look for a missing table.
    """
    kind = check_read_only(query)
    try:
        if kind in ROW_SOURCES:
            names, rows, more = db.read_only(query, limit, cursor=True)
            return _format_rows(names, rows, more=more)
        names, rows, more = db.read_only(query, PLAN_LINES_LIMIT, cursor=False)
    except QueryFailedError as exc:
        raise QueryFailedError(query_hint(db, exc, query, schemas)) from exc
    return _plan_lines(rows, more=more) if kind == "explain" else _format_rows(names, rows, more=more)


def query_hint(db: Database, error: QueryFailedError, query: Any, schemas: Sequence[str] | None) -> str:
    """The error, and what spares the model a call: where a missing table is, the columns of the tables read."""
    if error.code == UNDEFINED_TABLE:
        return missing_table_hint(db, error, schemas)
    if error.code == UNDEFINED_COLUMN:
        return missing_column_hint(db, error, query)
    return str(error)


def missing_column_hint(db: Database, error: QueryFailedError, query: Any) -> str:
    """The error, and for a column PostgreSQL could not find, the columns of the tables the statement reads.

    A model tends to guess a column ("order_id" for "id"); without the list it calls describe_table next.
    """
    text = query if isinstance(query, str) else ""
    names = [f"{first}.{second}" if second else first for first, second in _READ_RELATION.findall(text)]
    tables: dict[str, str] = {}
    try:
        for name in list(dict.fromkeys(names))[:TABLES_LISTED]:
            tables.update(_columns_of(db, name))
    except DatabaseUnavailableError:
        return str(error)  # the hint is a courtesy: the error itself still has to reach the model
    if not tables:
        return str(error)
    listed = "; ".join(f"{name}: {columns}" for name, columns in tables.items())
    return t("error.columns_of", error=error, tables=listed)


def _columns_of(db: Database, name: str) -> dict[str, str]:
    """The relation a name means, as schema.name with its columns; nothing when it is not a relation."""
    try:
        found = db.query(RELATION_BY_NAME_SQL, name=name)
        tables = {}
        for oid, schema, relation in found:
            columns = [str(row[0]) for row in db.query(COLUMNS_SQL, oid=oid)]
            more = ", …" if len(columns) > COLUMNS_LISTED else ""
            tables[f"{schema}.{relation}"] = ", ".join(columns[:COLUMNS_LISTED]) + more
    except QueryFailedError:
        return {}  # not a relation name after all: "FROM 2" in substring(), say
    return tables


def missing_table_hint(db: Database, error: QueryFailedError, schemas: Sequence[str] | None) -> str:
    """The error, and for a table PostgreSQL could not find, where a table of that name can be read.

    A model tends to write a table without its schema, or with "public". On a stand the answer names
    the schemas of the registry, whose logins the next call takes.
    """
    found = _QUOTED_NAME.search(error.server_message or "") if error.code == UNDEFINED_TABLE else None
    if found is None:
        return str(error)
    name = found.group(1).split(".")[-1]
    try:
        rows = _relations_named(db, name, schemas)
        if not rows and name != name.lower():
            rows = _relations_named(db, name.lower(), schemas)
    except (QueryFailedError, DatabaseUnavailableError):
        return str(error)  # the hint is a courtesy: the error itself still has to reach the model
    if not rows:
        return str(error)
    if schemas is not None:
        return t("stands.table_elsewhere", error=error, schemas=", ".join(str(row[1]) for row in rows))
    return t("error.table_elsewhere", error=error, tables=", ".join(f"{row[1]}.{row[2]}" for row in rows))


def _relations_named(db: Database, name: str, schemas: Sequence[str] | None) -> list[Sequence[Any]]:
    if schemas is None:
        return db.query(FIND_RELATION_SQL, name=name, schema=None)
    return db.query(FIND_RELATION_IN_SCHEMAS_SQL, name=name, schema=None, schemas=list(schemas))


def _resolve_relation(db: Database, table: Any, schema: Any) -> tuple[Any, ...]:
    """The pg_class row of the one readable relation the arguments name, or a ToolError with a hint."""
    name, schema = split_name(table, schema)
    name, matches = _relations_by_name(db, name, schema)
    if not matches and schema is not None and _guessed(db, schema):
        # "public" most often: the name alone finds the table, or the schema it carries, as in shop.orders
        name, schema = split_name(table, None)
        name, matches = _relations_by_name(db, name, schema)
    if not matches:
        raise ToolError(_not_found(db, name, schema))
    if len(matches) > 1:
        options = ", ".join(f"{row[1]}.{row[2]}" for row in matches)
        raise ToolError(t("error.ambiguous", name=name, options=options))
    return tuple(matches[0])


def _relations_by_name(db: Database, name: str, schema: str | None) -> tuple[str, list[Sequence[Any]]]:
    matches = db.query(FIND_RELATION_SQL, name=name, schema=schema)
    if not matches and name != name.lower():
        # Unquoted identifiers are folded to lower case, so "Orders" usually means orders.
        name = name.lower()
        matches = db.query(FIND_RELATION_SQL, name=name, schema=schema)
    return name, matches


def split_name(table: Any, schema: Any) -> tuple[str, str | None]:
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
    similar = [row[0] for row in db.query(SIMILAR_RELATIONS_SQL, fragment=_like_escape(name))]
    if similar:
        return t("error.not_found_similar", name=name, where=where, similar=", ".join(similar))
    return t("error.not_found", name=name, where=where)


def _like_escape(text: str) -> str:
    """Text that LIKE matches literally: its wildcards and backslashes escaped."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _like_patterns(fragment: str) -> list[str]:
    """ILIKE patterns for the usual spellings of a fragment.

    Under LC_CTYPE=C, ILIKE folds only ASCII letters, so "заказ" would miss "Заказы";
    searching for the lower, upper and capitalized spellings as well covers the common cases.
    """
    escaped = _like_escape(fragment)
    spellings = dict.fromkeys((escaped, escaped.lower(), escaped.upper(), escaped.capitalize()))
    return [f"%{spelling}%" for spelling in spellings]


def _column_line(
    name: str, data_type: str, *, not_null: bool, default: str | None = None, comment: str | None = None
) -> str:
    line = f"{name} {data_type}"
    if not_null:
        line += " NOT NULL"
    if default:
        line += f" DEFAULT {shorten(default)}"
    if comment:
        line += f"  -- {shorten(comment)}"
    return line


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
