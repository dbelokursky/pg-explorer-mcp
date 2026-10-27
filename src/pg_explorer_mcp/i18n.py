"""English and Russian texts for everything the model and the user read.

The server picks a language once at start-up and every message goes through
t(), or plural() for counted things. JSON-RPC protocol errors and the log stay
in English: clients act on error codes, and logs are for whoever runs the server.
"""

from __future__ import annotations

import locale
import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar

LANGUAGES = ("en", "ru")
DEFAULT_LANGUAGE = "en"
# The order Python's gettext reads them in; the first one that is set decides.
LOCALE_VARIABLES = ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG")
# CLDR plural categories for whole numbers: "1 row, 2 rows" and "1 строка, 2 строки, 5 строк".
PLURAL_FORMS = {"en": ("one", "other"), "ru": ("one", "few", "many")}

_language: ContextVar[str] = ContextVar("pg_mcp_language", default=DEFAULT_LANGUAGE)


def language_from_env(env: Mapping[str, str], system_locale: str | None = None) -> tuple[str, str | None]:
    """The language to speak, and the PG_EXPLORER_MCP_LANG value it had to ignore, if any.

    PG_EXPLORER_MCP_LANG decides when it is set. Otherwise the locale variables do and,
    when none is set (as on Windows), the locale the system reports.
    """
    explicit = env.get("PG_EXPLORER_MCP_LANG", "").strip()
    if explicit:
        code = re.split(r"[-_.@]", explicit.lower(), maxsplit=1)[0]
        return (code, None) if code in LANGUAGES else (DEFAULT_LANGUAGE, explicit)
    name = next((env[name] for name in LOCALE_VARIABLES if env.get(name, "").strip()), system_locale)
    return ("ru" if name and name.strip().lower().startswith("ru") else DEFAULT_LANGUAGE), None


def system_locale() -> str | None:
    """The locale the system gives this process: "ru_RU" on Linux and macOS, "Russian_Russia" on Windows."""
    try:
        return locale.getlocale()[0]
    except ValueError:  # macOS may report LC_CTYPE=UTF-8, which the locale module cannot parse
        return None


def use(language: str) -> None:
    """Speak this language from now on, in the current context."""
    _check(language)
    _language.set(language)


@contextmanager
def speaking(language: str) -> Iterator[None]:
    """Speak this language inside the block, then return to the previous one."""
    _check(language)
    token = _language.set(language)
    try:
        yield
    finally:
        _language.reset(token)


def current() -> str:
    return _language.get()


def t(key: str, **params: object) -> str:
    """The text for key in the current language, with its {placeholders} filled in."""
    return CATALOG[_language.get()][key].format(**params)


def plural(key: str, count: int, **params: object) -> str:
    """t() for a counted thing: picks key.one, key.few, key.many or key.other by count; {count} is filled in."""
    return t(f"{key}.{_plural_form(_language.get(), count)}", count=count, **params)


def number(value: float) -> str:
    """A number with at most one decimal, written the local way: 1.2 in English, 1,2 in Russian."""
    text = f"{value:.1f}".rstrip("0").rstrip(".")
    return text.replace(".", t("number.decimal"))


def _check(language: str) -> None:
    if language not in LANGUAGES:
        raise ValueError(f"unsupported language {language!r}; expected one of {', '.join(LANGUAGES)}")


def _plural_form(language: str, count: int) -> str:
    if language == "ru":
        if count % 10 == 1 and count % 100 != 11:
            return "one"  # 1, 21, 101
        if 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
            return "few"  # 2-4, 22-24, but not 12-14
        return "many"  # 0, 5-20, 25-30, 111
    return "one" if count == 1 else "other"


EN: dict[str, str] = {
    "server.instructions": (
        "Read-only access to one PostgreSQL database: nothing can be inserted, changed or deleted through it, "
        "so decline such requests instead of asking the user to confirm them. Call status first: it shows "
        "the database, the role, "
        "the tools this profile offers and the limits in force. Then learn the structure before querying data: "
        "list_tables for an overview, search_schema to find tables and columns by name or comment in a large "
        "database, describe_table for the details. Reply to the user in the language they write in."
    ),
    "number.decimal": ".",
    # Tools as the model sees them in tools/list.
    "tool.status.title": "Connection status",
    "tool.status.description": (
        "Read-only server: database, role, profile and limits. Call it first, and when a tool reports a "
        "connection problem: it says what to fix."
    ),
    "tool.list_tables.title": "List tables",
    "tool.list_tables.description": (
        "Tables, views and foreign tables the role can read, with row estimates and comments. At most "
        "{limit}; pass schema to narrow."
    ),
    "tool.list_tables.schema": "Only list objects in this schema; omit it to list every schema.",
    "tool.search_schema.title": "Search the schema",
    "tool.search_schema.description": (
        "Find tables, views and columns by a piece of name or comment; use it instead of paging list_tables. "
        "At most {limit} matches."
    ),
    "tool.describe_table.title": "Describe a table",
    "tool.describe_table.description": (
        "Columns with types, NOT NULL, defaults and comments; keys, referencing tables, indexes."
    ),
    "tool.sample_rows.title": "Sample rows",
    "tool.sample_rows.description": (
        "A few rows of a table or view, optionally some columns. Default {default}, at most {limit}, in no order."
    ),
    "tool.explain_query.title": "Explain a query",
    "tool.explain_query.description": (
        "PostgreSQL's plan for one read-only query passed without EXPLAIN; the query is not run."
    ),
    "tool.explain_query.description_analyze": (
        "PostgreSQL's plan for one read-only query passed without EXPLAIN; with analyze it runs read-only and "
        "the plan shows real timings."
    ),
    "tool.run_select.title": "Run a read-only query",
    "tool.run_select.description": (
        "One read-only statement: SELECT, WITH, VALUES, TABLE, EXPLAIN or SHOW. At most {limit} rows, so use "
        "WHERE and LIMIT. Data cannot be changed: decline such requests."
    ),
    "argument.table": '"orders" or "sales.orders".',
    "argument.schema": "Schema to look in; omit for every readable schema.",
    "argument.columns": 'Columns, e.g. "id, total"; omit for all.',
    "argument.limit": "How many rows, 1 to {limit}. Default {default}.",
    "argument.query": "One SQL statement.",
    "argument.text": 'Part of a name or comment, e.g. "order".',
    "argument.analyze": "Run the query for real timings.",
    "status.version": "pg-explorer-mcp {version} on Python {python}, installed at {path}",
    "status.name": "server name: {name}",
    "status.read_only": (
        "access: read only. Nothing can be inserted, changed or deleted through this server: decline such "
        "requests instead of asking the user to confirm them."
    ),
    "status.target": "target: {target}",
    "status.profile": "profile: {profile} ({tools})",
    "status.language": "language: {language}",
    "status.query_log": "query log: {path}",
    "status.query_log_off": "query log: off",
    "status.query_log_problem": "query log: {path}, not written: {problem}",
    "querylog.relative": "the path must be absolute",
    "status.warning": "warning: {warning}",
    "status.unavailable": "database: unavailable. {reason}",
    "status.connected": "database: connected to {database} as {user}, PostgreSQL {version}",
    "status.limits": (
        "limits: read only = {read_only}, statement timeout = {timeout}, list_tables returns at most "
        "{tables} entries, answers are cut at {budget} characters"
    ),
    "warning.profile": "PG_EXPLORER_MCP_PROFILE={value!r} is not one of {choices}; using {fallback!r}",
    "warning.language": "PG_EXPLORER_MCP_LANG={value!r} is not one of {choices}; using {fallback!r}",
    # Arguments and answers of the tools.
    "error.arguments": "arguments must be a JSON object",
    "error.unknown_arguments": "Unknown argument(s): {unknown}. Allowed: {allowed}.",
    "error.missing_arguments": "Missing argument(s): {missing}.",
    "error.none": "none",
    "error.schema": "schema must be a string with the name of a schema; omit it to list all schemas",
    "error.schema_search": "schema must be a string with the name of a schema; omit it to search every schema",
    "error.table": 'table must be a non-empty string such as "orders" or "sales.orders"',
    "error.columns": 'columns must be a comma-separated string such as "id, total"',
    "error.text": 'text must be a non-empty string such as "order"',
    "error.whole_number": "{name} must be a whole number from {low} to {high}",
    "error.out_of_range": "{name} must be from {low} to {high}, got {number}",
    "error.flag": "{name} must be true or false",
    "error.no_tables": "The current role cannot read any table in this database. Ask the DBA to grant SELECT.",
    "error.no_tables_in_schema": (
        "The current role cannot read any table in schema {schema!r}. Schemas with readable tables: {visible}."
    ),
    "error.system_schema": (
        "Schema {schema!r} is a system one: list_tables and search_schema leave system schemas out. "
        "Schemas with readable tables: {visible}."
    ),
    "error.ambiguous": "{name!r} exists in several schemas: {options}. Pass schema to pick one.",
    "error.not_found": "No readable table or view named {name!r}{where}. Call list_tables to see what exists.",
    "error.not_found_similar": "No readable table or view named {name!r}{where}. Similar names: {similar}.",
    "error.table_elsewhere": "{error} A table of that name is readable as {tables}: write the name with its schema.",
    "error.columns_of": "{error} Columns of the tables the query reads: {tables}.",
    "error.in_schema": " in schema {schema!r}",
    "error.no_column": "No column {name!r} in {relation}. Columns: {columns}.",
    "error.explain_row_sources": (
        "explain_query takes a SELECT, WITH ... SELECT, VALUES or TABLE statement, without EXPLAIN."
    ),
    "list.more": "... more than {limit} entries; pass schema to narrow the list",
    "list.guessed_schema": "Schema {schema!r} has no tables, so these are the readable tables of all schemas:",
    "search.nothing": "Nothing matches {text!r}. Try a shorter piece of the name, another spelling, or list_tables.",
    "search.more": "... more than {limit} matches; use a longer piece of text or pass schema",
    "search.guessed_schema": "Schema {schema!r} has no tables, so the search went through all schemas:",
    "describe.columns": "columns:",
    "describe.constraints": "constraints:",
    "describe.referenced_by": "referenced by:",
    "describe.indexes": "indexes:",
    "kind.r": "table",
    "kind.p": "partitioned table",
    "kind.v": "view",
    "kind.m": "materialized view",
    "kind.f": "foreign table",
    "estimate.unknown": "rows unknown (never analyzed)",
    "estimate.empty_or_unknown": "empty or never analyzed",
    "estimate.rows.one": "~{count} row",
    "estimate.rows.other": "~{count} rows",
    "estimate.scaled": "~{amount} rows",
    "estimate.thousands": "{value}k",
    "estimate.millions": "{value}M",
    "estimate.billions": "{value}B",
    "rows.count.one": "({count} row)",
    "rows.count.other": "({count} rows)",
    "rows.more.one": "({count} row shown, there are more: narrow the query with WHERE, ORDER BY or LIMIT)",
    "rows.more.other": "({count} rows shown, there are more: narrow the query with WHERE, ORDER BY or LIMIT)",
    "plan.more": "... plan longer than {limit} lines",
    "budget.notice": (
        "… answer cut to {budget} characters: {shown} of {total} lines shown. "
        "Narrow the request (one schema or table, fewer columns or rows) to see the rest."
    ),
    # Connection settings and failures.
    "db.not_configured": "not configured: {error}",
    "db.settings_invalid": (
        "Connection settings are invalid: {error}. Fix the env block of the pg server entry in the agent settings."
    ),
    "db.bad_sslmode": "PGSSLMODE={value!r} is not one of {choices}",
    "db.not_a_number": "{name}={value!r} is not a number",
    "db.lost": "Lost the connection to {target}: {error}",
    "db.session_setup": "Connected, but could not set up a read-only session: {error}",
    "db.password_unset": "the server asks for a password and none is set",
    "db.password": (
        "Login as {user!r} failed: {detail}. Put the password into {passfile} as one line "
        "host:port:database:user:password (file mode 600). Never paste it into the chat."
    ),
    "db.refused": (
        "Nothing accepts connections at {where}. Check PGHOST and PGPORT, and that the database is reachable "
        "from this machine (VPN?)."
    ),
    "db.unresolved": "Host {host!r} cannot be resolved. Check PGHOST.",
    "db.timeout": "Connecting to {where} timed out. The host may be behind a firewall or a VPN.",
    "db.unreachable": "{where} is unreachable from this machine: {error}. Connect the VPN and check PGHOST.",
    "db.dropped": (
        "{where} accepted the connection, then closed it during the login of {user!r} without saying why. "
        "Usually a wrong password or pg_hba.conf: check the password file, then ask the DBA."
    ),
    "db.no_database": "Database {database!r} does not exist at {where}. Check PGDATABASE.",
    "db.login_refused": (
        "The server refused the login for {user!r}: {detail}. Check the role and pg_hba.conf with the DBA."
    ),
    "db.tls": "TLS with {where} failed: {detail}. Check PGSSLMODE and PGSSLROOTCERT.",
    "db.cannot_connect": "Cannot connect to {target}: {detail}",
    "db.server_error": "{message} (SQLSTATE {code})",
    "db.server_hint": "{text}. Hint: {hint}",
    # The read-only SQL check.
    "sql.summary": "SELECT, WITH ... SELECT, VALUES, TABLE, EXPLAIN and SHOW",
    "sql.empty": "query must be a non-empty string with one SQL statement",
    "sql.only_comments": "The query has no statement, only comments.",
    "sql.one_statement": "Send one statement per call: the query has ';' between statements.",
    "sql.not_allowed": (
        "{word} is not allowed: this server only reads, so tell the user the change cannot be made here. "
        "Read-only statements: {allowed}."
    ),
    "sql.not_allowed_other": "This statement is not allowed: this server only reads. Read-only statements: {allowed}.",
    "sql.explain": "EXPLAIN is allowed only for SELECT, WITH ... SELECT, VALUES and TABLE.",
    "sql.forbidden_word": (
        "{word} is not allowed: {reason}. This server only reads: tell the user the change cannot be made here."
    ),
    "sql.row_locks": "Row locks (FOR SHARE, FOR KEY SHARE) are not allowed: they block other sessions.",
    "sql.function": "Function {name}() is not allowed: {reason}.",
    "sql.unterminated_dollar": "Unterminated dollar-quoted string.",
    "sql.unterminated_comment": "Unterminated /* comment.",
    "sql.unterminated_quote": "Unterminated {quote} quote.",
    "sql.unicode_identifier": (
        'Identifiers written as U&"..." are not allowed: write the name as it is, in double quotes if it needs them.'
    ),
    "sql.word.insert": "INSERT changes data",
    "sql.word.update": "UPDATE changes data, and FOR UPDATE locks rows",
    "sql.word.delete": "DELETE changes data",
    "sql.word.merge": "MERGE changes data",
    "sql.word.into": "SELECT ... INTO creates a table",
    "sql.reason.ends_session": "it ends another session",
    "sql.reason.cancels_query": "it cancels another session's query",
    "sql.reason.settings": "it changes session settings",
    "sql.reason.unseen_sql": "it runs SQL text this check never saw",
    "sql.reason.unseen_cursor": "it reads a cursor this check never saw",
    "sql.reason.reads_files": "it reads files on the database server",
    "sql.reason.inspects_files": "it inspects files on the database server",
    "sql.reason.writes_files": "it writes files on the database server",
    "sql.reason.lists_files": "it lists files on the database server",
    "sql.reason.large_objects": "it changes large objects",
    "sql.reason.administers": "it administers the server",
    "sql.reason.signals": "it signals another session",
    "sql.reason.wal": "it writes to the WAL",
    "sql.reason.catalog": "it changes the catalog",
    "sql.reason.wakes": "it wakes other sessions",
    "sql.reason.sequence": "it changes a sequence",
    "sql.reason.sleeps": "it holds the connection doing nothing",
    "sql.reason.dblink": "it opens a separate connection that this server's read-only mode does not cover",
    "sql.reason.resets_stats": "it resets statistics",
    "sql.reason.replication": "it controls replication",
    "sql.reason.advisory": "advisory locks outlive the query and block other sessions",
    "sql.reason.slots": "it changes replication slots",
    # Many stands in one server entry (ADR 0006).
    "stands.instructions": (
        "This server reaches many stands. status lists them, list_schemas shows the schemas of one. Every "
        "tool takes stand; reading data also takes schema, whose login the server uses; database only when "
        "the schema is in several databases of the stand. search_schema and run_select take stands instead of "
        "stand for several stands in one call; compare_schema compares one schema on two stands."
    ),
    "stands.status_note": "With stand it also checks that stand.",
    "tool.list_schemas.title": "List schemas",
    "tool.list_schemas.description": (
        "Databases of a stand and the schemas with logins; call it first on a new stand."
    ),
    "argument.stand": "One of the stands: {stands}.",
    "argument.stands": 'Instead of stand: "dev*" or "dev1,dev2", at most {limit}.',
    "argument.stand_status": "Stand to check; omit for the list.",
    "argument.database": "Only when the schema is in several databases.",
    "argument.schema_data": "Schema whose login runs the query; names resolve in it.",
    "argument.schema_stand": "One schema; omit for the whole stand.",
    "status.stands": "stands: {count} ({stands}), connections in the registry: {connections}",
    "status.registry": "registry: {files}",
    "status.limits_stands": (
        "limits: list_tables returns at most {tables} entries per database, answers are cut at {budget} "
        "characters, at most {connections} connections stay open, stands covers at most {many} stands per call"
    ),
    "status.stand_hint": "pass stand to status to check that stand's connection",
    "status.checked_with": "checked with {label}",
    "stands.no_stand_argument": "Pass {argument}, one of: {stands}.",
    "stands.unknown": "No stand {stand!r} in the registry. Stands: {stands}.",
    "stands.unknown_similar": "No stand {stand!r} in the registry. Similar: {similar}.",
    "stands.no_stand_or_stands": (
        'Pass stand, one of: {stands}; or stands for several at once, e.g. "dev1,dev2" or "dev*".'
    ),
    "stands.bad_stands_argument": (
        'stands must be stand names separated by commas or a pattern, e.g. "dev1,dev2" or "dev*". Stands: {stands}.'
    ),
    "stands.one_or_many": "Pass either stand or stands, not both.",
    "stands.no_match": "No stand matches {pattern!r}. Stands: {stands}.",
    "stands.too_many.one": (
        "{patterns!r} means {count} stand ({stands}), and one call covers at most {limit}. "
        "Narrow the pattern or name up to {limit} stands separated by commas."
    ),
    "stands.too_many.other": (
        "{patterns!r} means {count} stands ({stands}), and one call covers at most {limit}. "
        "Narrow the pattern or name up to {limit} stands separated by commas."
    ),
    "stands.out_of_time": (
        "{stands}: no answer within the {budget} seconds of the call; ask about them in another call."
    ),
    "stands.no_rows": "no rows on {stands}",
    "stands.no_matches": "nothing matches on {stands}",
    "stands.no_schema_argument": (
        "Pass schema: on stand {stand} the server reads data with the login of the schema you name. "
        "Its schemas: {schemas}."
    ),
    "stands.unknown_schema": (
        "Stand {stand} has no schema {schema!r} that the server has a login for. Its schemas: {schemas}."
    ),
    "stands.more_names": "{names} and more, {count} in all: list_schemas shows them",
    "stands.more_stands": "{names} and more, {count} in all: status lists them",
    "stands.ask_every": 'To look on every stand, call again with stands: "*" in place of stand.',
    "stands.ask_several": (
        "To look on several stands in one call, pass stands in place of stand: a list or a pattern, "
        "at most {limit} stands."
    ),
    "stands.elsewhere": "Schema {schema} is on {stands} as well; pass stands to ask up to {limit} stands in one call.",
    "stands.unknown_schema_similar": (
        "Stand {stand} has no schema {schema!r} that the server has a login for. Similar: {similar}."
    ),
    "stands.schema_in_databases": (
        "Schema {schema!r} exists in several databases of {stand}: {databases}. Pass database to pick one."
    ),
    "stands.schema_elsewhere": (
        "Schema {schema!r} of {stand} is in {databases}, not in {database!r}. Pass one of those as database."
    ),
    "stands.unknown_database": "Stand {stand} has no database {database!r} in the registry. Databases: {databases}.",
    "stands.table_in_several": "{name!r} exists in several schemas of {stand}: {options}. Pass schema to pick one.",
    "stands.no_tables": "No tables or views in {schemas}: these schemas are empty or missing in this database.",
    "stands.table_not_found": (
        "No table or view {name!r} in the schemas of {stand} that the server has logins for. "
        "Call search_schema or list_schemas."
    ),
    "stands.table_elsewhere": (
        "{error} In this database a table of that name is in schema {schemas}: call again with that schema."
    ),
    "stands.unreadable": "cannot read the stands registry {path}: {error}",
    "stands.bad_line": "{path}: {problem}",
    "stands.line": "line {number} is neither [section] nor key=value",
    "stands.no_host": "stand connection {name} in {path} has no host, skipped",
    "stands.bad_number": "stand connection {name} in {path} has {option}={value!r}, not a number, skipped",
    "stands.none_found": (
        "PG_EXPLORER_MCP_STANDS is set, but no stands were found in {files}; working with the PG* settings"
    ),
    "stands.more_warnings": "... and {count} more problems in the stands registry",
    # compare_schema: one schema on two stands.
    "tool.compare_schema.title": "Compare a schema",
    "tool.compare_schema.description": (
        "How one schema differs between two stands, or between stand_a and several (stands): tables, columns, "
        "constraints, indexes. Data is not read."
    ),
    "argument.stand_a": "First stand.",
    "argument.stand_b": "Second stand.",
    "argument.schema_compare": "Schema to compare.",
    "compare.header": "{a}  vs  {b}",
    "compare.only_on": "only on {stand}:",
    "compare.differ": "differ:",
    "compare.same": "identical: {tables}, {columns}",
    "compare.tables.one": "{count} table",
    "compare.tables.other": "{count} tables",
    "compare.columns.one": "{count} column",
    "compare.columns.other": "{count} columns",
    "compare.constraint": "{table}: constraint {name}",
    "compare.index": "{table}: index {name}",
    "compare.only_here": "{what} only on {stand}: {definition}",
    "compare.same_stand": "stand_a and stand_b are both {stand!r}. Pass two different stands.",
    "argument.stands_compare": (
        'Instead of stand_b: several stands against stand_a, "dev*" or "dev1,dev2", at most {limit}.'
    ),
    "compare.many_header": "{a}  vs  {count} stands:",
    "compare.like": "same as {a}",
    "compare.summary": "tables only on {a}: {only_a}, only here: {only_b}, differences: {differ}",
    "compare.totals": "same as {a}: {same}; different: {differ}; not checked: {skipped}",
    "compare.b_or_stands": "Pass either stand_b or stands, not both.",
    "compare.need_b": "Pass stand_b, or stands for several stands at once.",
    "compare.no_others": "{pattern!r} names only {stand!r}, the reference itself. Name other stands.",
    "compare.no_schema": (
        "Stand {stand} has no schema {schema!r} in database {database}: the registry lists it, but the database "
        "does not have it. Nothing to compare."
    ),
    "stands.readable_by_others": "{path} holds a password, and other users can read it: chmod 600 {path}",
    "stands.reload_kept": (
        "the stands registry changed ({files}), but no stands could be read from it: keeping the stands read before"
    ),
    # install.py import
    "install.help.import": "bring stand connections from DBeaver or a pg_service.conf file into pg-explorer-mcp",
    "install.help.source": "where the connections come from: dbeaver or pg-service",
    "install.help.file": "the pg_service.conf file to read (for pg-service)",
    "install.help.workspace": "DBeaver's folder with data-sources.json (default: the usual place for this OS)",
    "install.help.write": "write the registry and the password file and register pg-stands; without it, only show",
    "install.help.prune": "with --write: replace the registry instead of adding to it",
    "install.help.stand_pattern": (
        "regular expression that finds the stand in a folder, connection name or host (default: {pattern})"
    ),
    "import.unreadable": "cannot read {path}: {error}",
    "import.bad_pattern": "the stand pattern {pattern!r} is not a regular expression: {error}",
    "import.duplicate": "the same stand, database and user as {other!r}: [{name}] is already taken",
    "import.no_host": "no host",
    "import.no_database": "no database",
    "import.no_user": "no user: pg-explorer-mcp reads a schema with the login of the same name",
    "import.no_stand": "no stand in the folder, the name or the host",
    "import.bad_name": "{part!r} cannot be a part of a section name (letters, digits, _ $ - only)",
    "import.reading": "reading {source}",
    "import.preview_header": "{count} connections go into {registry}:",
    "import.preview_no_password": "no saved password: add a line to the password file",
    "import.preview_passwords": "{count} passwords go into {passfile}",
    "import.preview_skipped": "left out: {count}",
    "import.nothing": "nothing to import",
    "import.dry_run": (
        "This was a preview, nothing was written. Add --write to write the files and register pg-stands."
    ),
    "import.cannot_write": "cannot write {path}: {error}. Nothing was changed.",
    "import.written": "written {path}",
    "import.written_backup": "written {path}; the old file is kept as {backup}",
    "import.no_dbeaver": (
        "DBeaver's connections were not found on this machine; pass --workspace with the folder that holds "
        "data-sources.json"
    ),
    "import.need_file": "pg-service needs the file to read: install.py import pg-service ~/.pg_service.conf",
    "import.offer": "DBeaver connections found in {path}. Show what an import would bring into pg-explorer-mcp? [y/N] ",
    "import.offer_hint": "To import them, run: {command}",
    # install.py export
    "export.help": "add the stand connections to a database tool: DBeaver, DataGrip, pgAdmin, psql, VS Code",
    "export.help.tool": (
        "the tool: dbeaver, datagrip (and the other JetBrains IDEs), pgadmin, pg-service (psql, pgcli), "
        "vscode-pgsql or sqltools"
    ),
    "export.help.stands": "the stands to export: names or patterns separated by commas (default: all)",
    "export.help.write": "write the files; without it, only show what would be written",
    "export.help.project": (
        "for datagrip: the project folder to add the data sources to; close the project in the IDE first "
        "(default: {path})"
    ),
    "export.help.editor": "for vscode-pgsql and sqltools: the editor whose user settings get the connections",
    "export.no_stands": (
        "no stands to export: import them first (install.py import) or check PG_EXPLORER_MCP_STANDS_FILE"
    ),
    "export.no_match": "no stands match {patterns}; the registry has {stands}",
    "export.no_dbeaver": (
        "DBeaver's connections were not found on this machine; pass --workspace with the folder that holds "
        "data-sources.json"
    ),
    "export.not_a_folder": "{path} is not a folder",
    "export.no_editor": "the settings of {editor} were not found in {path}: is it installed?",
    "export.unreadable": "cannot read {path}: {error}. Nothing was changed.",
    "export.not_json": (
        "{path} is not plain JSON (comments?), so it is left alone. Merge this into it by hand:\n{snippet}"
    ),
    "export.not_a_list": "{setting} in the editor's settings is not a list. Nothing was changed.",
    "export.preview.one": "{tool}: {count} connection ({stands}) goes into:",
    "export.preview.other": "{tool}: {count} connections ({stands}) go into:",
    "export.present_dbeaver": "left out, already in DBeaver with the same host, port, database and user: {count}",
    "export.present_service": "left out, already in {path}: {count}",
    "export.nothing_new": "{tool} has all of these connections already; nothing to write",
    "export.dry_run": "This was a preview, nothing was written. Add --write to write the files.",
    "export.cannot_place": "cannot replace {path}: {error}. It is left as it was.",
    "export.next_dbeaver": (
        "Restart DBeaver: the connections are in a folder per stand and log in with the password file."
    ),
    "export.next_datagrip": (
        "Open the folder {path} as a project in DataGrip or another JetBrains IDE (File | Open), or reopen the "
        "project if it is open: the data sources are grouped by stand and log in with the password file."
    ),
    "export.next_pgadmin": (
        "In pgAdmin: Tools | Import/Export Servers, Import, the file {path}. The servers are grouped by stand "
        "and read passwords from {passfile}."
    ),
    "export.next_pg_service": (
        'Connect with psql "service={service}" or pgcli "service={service}": the schema comes first on the search path.'
    ),
    "export.next_vscode_pgsql": (
        "Reload the editor window: the connections are in the PostgreSQL view, a group per stand. The extension "
        "cannot read the password file: it asks for each password once and keeps it in the system keychain."
    ),
    "export.next_sqltools": (
        "Reload the editor window: the connections are in SQLTools, a group per stand, and log in with the "
        "password file."
    ),
    "export.offer": "Found {tools}. Add the stand connections there ({count})? [y/N] ",
    "export.offer_hint": "To add them later:",
    # install.py
    "install.description": "Register the pg-explorer-mcp server in the agent's settings and check it.",
    "install.help.client": "the agent or editor to register the server with (default: gigacode)",
    "install.help.print": "print the entry and where it goes instead of registering it",
    "clients.where_file": "for {client}, add this to {path}:",
    "clients.where_paste": (
        "{client} keeps its servers in the IDE: Settings | Tools | AI Assistant | Model Context Protocol (MCP) | "
        "Add, then paste this into the JSON configuration field:"
    ),
    "clients.offer": "Also found {clients}. Register {name} there too? [y/N] ",
    "clients.offer_hint": "To register {name} there later: {command}",
    "install.help.check": "only start the server and call status; change nothing",
    "install.help.profile": "tools for this group: schema, explore (default) or analyst (adds run_select)",
    "install.help.lang": "language of the server and of this script: en or ru (default: from the system locale)",
    "install.help.name": "register a further database as pg-NAME next to pg (lowercase letters, digits, hyphens)",
    "install.bad_name": "{name!r} cannot be a server name: use lowercase letters, digits and hyphens, at most 30",
    "install.old_python": "pg-explorer-mcp needs Python {need} or newer, this is {have}",
    "install.backup": "backup of the old settings: {path}",
    "install.not_json": "{path} is not plain JSON (comments?). Merge this into it by hand:\n{snippet}",
    "install.registered": "registered MCP server {name!r} in {path}",
    "install.did_not_run": "the server did not run: {error}",
    "install.no_answer": "the server did not answer as expected:",
    "install.protocol": "protocol {version}",
    "install.next": "next: {client} extensions link {path}",
    "install.old_extension": (
        "{path} still holds the extension linked as pg-mcp before 0.8.0: if that command says it is installed"
        " already, delete the folder and run the command again"
    ),
    "install.restart": "then restart the agent so it starts the server with the new settings",
}

RU: dict[str, str] = {
    "server.instructions": (
        "Доступ только на чтение к одной базе PostgreSQL: добавить, изменить или удалить через него ничего "
        "нельзя, поэтому от таких просьб отказывайтесь, а не просите пользователя их подтвердить. Сначала "
        "вызовите status: он покажет базу, роль, "
        "инструменты этого профиля и действующие лимиты. Затем, прежде чем запрашивать данные, изучите "
        "структуру: list_tables — для обзора, search_schema — чтобы найти таблицы и колонки по имени или "
        "комментарию в большой базе, describe_table — для подробностей. Отвечайте пользователю на том языке, "
        "на котором он пишет."
    ),
    "number.decimal": ",",
    "tool.status.title": "Состояние подключения",
    "tool.status.description": (
        "Сервер только читает: база, роль, профиль и лимиты. Вызывайте первым и когда инструмент сообщает о "
        "проблеме с подключением: в ответе сказано, что исправить."
    ),
    "tool.list_tables.title": "Список таблиц",
    "tool.list_tables.description": (
        "Таблицы, представления и внешние таблицы, доступные роли, с оценкой числа строк и комментариями. Не "
        "больше {limit}; чтобы сузить, передайте schema."
    ),
    "tool.list_tables.schema": "Показать объекты только этой схемы; без него — все схемы.",
    "tool.search_schema.title": "Поиск по схеме",
    "tool.search_schema.description": (
        "Находит таблицы, представления и колонки по фрагменту имени или комментария; в большой базе — вместо "
        "листания list_tables. Не больше {limit} совпадений."
    ),
    "tool.describe_table.title": "Описание таблицы",
    "tool.describe_table.description": (
        "Колонки с типами, NOT NULL, значениями по умолчанию и комментариями; ключи, ссылающиеся таблицы, индексы."
    ),
    "tool.sample_rows.title": "Примеры строк",
    "tool.sample_rows.description": (
        "Несколько строк таблицы или представления, все колонки или выбранные. По умолчанию {default}, не "
        "больше {limit}, в произвольном порядке."
    ),
    "tool.explain_query.title": "План запроса",
    "tool.explain_query.description": (
        "План PostgreSQL для одного запроса на чтение, переданного без EXPLAIN; запрос не выполняется."
    ),
    "tool.explain_query.description_analyze": (
        "План PostgreSQL для одного запроса на чтение, переданного без EXPLAIN; с analyze запрос выполняется "
        "в транзакции только на чтение, и в плане видно реальное время."
    ),
    "tool.run_select.title": "Выполнить запрос на чтение",
    "tool.run_select.description": (
        "Одна команда только на чтение: SELECT, WITH, VALUES, TABLE, EXPLAIN или SHOW. Возвращает не больше "
        "{limit} строк, поэтому отбирайте их через WHERE и LIMIT. Данные изменить нельзя: от таких просьб "
        "отказывайтесь."
    ),
    "argument.table": '"orders" или "sales.orders".',
    "argument.schema": "Схема, в которой искать; без неё — все доступные схемы.",
    "argument.columns": 'Колонки, например "id, total"; без них — все.',
    "argument.limit": "Сколько строк, от 1 до {limit}. По умолчанию {default}.",
    "argument.query": "Одна SQL-команда.",
    "argument.text": 'Фрагмент имени или комментария, например "заказ".',
    "argument.analyze": "Выполнить запрос, чтобы увидеть реальное время.",
    "status.version": "pg-explorer-mcp {version} на Python {python}, установлен в {path}",
    "status.name": "имя сервера: {name}",
    "status.read_only": (
        "доступ: только чтение. Добавить, изменить или удалить данные через этот сервер нельзя: от таких "
        "просьб отказывайтесь, а не просите пользователя их подтвердить."
    ),
    "status.target": "адрес: {target}",
    "status.profile": "профиль: {profile} ({tools})",
    "status.language": "язык: {language}",
    "status.query_log": "журнал запросов: {path}",
    "status.query_log_off": "журнал запросов: выключен",
    "status.query_log_problem": "журнал запросов: {path}, запись не удалась: {problem}",
    "querylog.relative": "путь должен быть абсолютным",
    "status.warning": "предупреждение: {warning}",
    "status.unavailable": "база: недоступна. {reason}",
    "status.connected": "база: подключено к {database} под ролью {user}, PostgreSQL {version}",
    "status.limits": (
        "лимиты: только чтение = {read_only}, таймаут запроса = {timeout}, list_tables возвращает не больше "
        "{tables} записей, ответы обрезаются до {budget} символов"
    ),
    "warning.profile": "PG_EXPLORER_MCP_PROFILE={value!r} — нет такого профиля (есть {choices}); включён {fallback!r}",
    "warning.language": "PG_EXPLORER_MCP_LANG={value!r} — нет такого языка (есть {choices}); включён {fallback!r}",
    "error.arguments": "Аргументы должны быть JSON-объектом",
    "error.unknown_arguments": "Неизвестные аргументы: {unknown}. Допустимые: {allowed}.",
    "error.missing_arguments": "Не хватает аргументов: {missing}.",
    "error.none": "нет",
    "error.schema": ("Параметр schema должен быть строкой с именем схемы; чтобы увидеть все схемы, не передавайте его"),
    "error.schema_search": (
        "Параметр schema должен быть строкой с именем схемы; чтобы искать во всех схемах, не передавайте его"
    ),
    "error.table": 'Параметр table должен быть непустой строкой, например "orders" или "sales.orders"',
    "error.columns": 'Параметр columns должен быть строкой с именами через запятую, например "id, total"',
    "error.text": 'Параметр text должен быть непустой строкой, например "заказ"',
    "error.whole_number": "Параметр {name} должен быть целым числом от {low} до {high}",
    "error.out_of_range": "Параметр {name} должен быть от {low} до {high}, а передано {number}",
    "error.flag": "Параметр {name} должен быть true или false",
    "error.no_tables": "Текущая роль не может читать ни одну таблицу в этой базе. Попросите DBA выдать права SELECT.",
    "error.no_tables_in_schema": (
        "Текущая роль не может читать ни одну таблицу в схеме {schema!r}. "
        "Схемы с таблицами, доступными для чтения: {visible}."
    ),
    "error.system_schema": (
        "Схема {schema!r} системная: list_tables и search_schema системные схемы не показывают. "
        "Схемы с таблицами, доступными для чтения: {visible}."
    ),
    "error.ambiguous": "{name!r} есть в нескольких схемах: {options}. Чтобы выбрать одну, передайте schema.",
    "error.not_found": (
        "Среди доступных для чтения нет таблицы или представления {name!r}{where}. "
        "Вызовите list_tables, чтобы увидеть, что есть."
    ),
    "error.not_found_similar": (
        "Среди доступных для чтения нет таблицы или представления {name!r}{where}. Похожие имена: {similar}."
    ),
    "error.table_elsewhere": "{error} Таблица с таким именем доступна как {tables}: укажите имя вместе со схемой.",
    "error.columns_of": "{error} Колонки таблиц запроса: {tables}.",
    "error.in_schema": " в схеме {schema!r}",
    "error.no_column": "В {relation} нет колонки {name!r}. Колонки: {columns}.",
    "error.explain_row_sources": (
        "explain_query принимает команду SELECT, WITH ... SELECT, VALUES или TABLE без EXPLAIN."
    ),
    "list.more": "... записей больше {limit}; чтобы сузить список, передайте schema",
    "list.guessed_schema": "В схеме {schema!r} нет таблиц, поэтому здесь доступные для чтения таблицы всех схем:",
    "search.nothing": (
        "Ничего не нашлось по {text!r}. Попробуйте фрагмент покороче, другое написание или list_tables."
    ),
    "search.more": "... совпадений больше {limit}; уточните фрагмент или передайте schema",
    "search.guessed_schema": "В схеме {schema!r} нет таблиц, поэтому поиск шёл по всем схемам:",
    "describe.columns": "колонки:",
    "describe.constraints": "ограничения:",
    "describe.referenced_by": "на неё ссылаются:",
    "describe.indexes": "индексы:",
    "kind.r": "таблица",
    "kind.p": "секционированная таблица",
    "kind.v": "представление",
    "kind.m": "материализованное представление",
    "kind.f": "внешняя таблица",
    "estimate.unknown": "число строк неизвестно (статистику ещё не собирали)",
    "estimate.empty_or_unknown": "пусто или статистику ещё не собирали",
    "estimate.rows.one": "~{count} строка",
    "estimate.rows.few": "~{count} строки",
    "estimate.rows.many": "~{count} строк",
    "estimate.scaled": "~{amount} строк",
    "estimate.thousands": "{value} тыс.",
    "estimate.millions": "{value} млн",
    "estimate.billions": "{value} млрд",
    "rows.count.one": "({count} строка)",
    "rows.count.few": "({count} строки)",
    "rows.count.many": "({count} строк)",
    "rows.more.one": "(показана {count} строка, есть ещё: сузьте запрос через WHERE, ORDER BY или LIMIT)",
    "rows.more.few": "(показаны {count} строки, есть ещё: сузьте запрос через WHERE, ORDER BY или LIMIT)",
    "rows.more.many": "(показано {count} строк, есть ещё: сузьте запрос через WHERE, ORDER BY или LIMIT)",
    "plan.more": "... план длиннее {limit} строк",
    "budget.notice": (
        "… ответ обрезан до {budget} символов: показано строк — {shown} из {total}. "
        "Чтобы увидеть остальное, сузьте запрос: одна схема или таблица, меньше колонок или строк."
    ),
    "db.not_configured": "не настроено: {error}",
    "db.settings_invalid": (
        "Неверные настройки подключения: {error}. Исправьте блок env у сервера pg в настройках агента."
    ),
    "db.bad_sslmode": "PGSSLMODE={value!r} — не один из вариантов: {choices}",
    "db.not_a_number": "{name}={value!r} — не число",
    "db.lost": "Соединение с {target} потеряно: {error}",
    "db.session_setup": "Подключение есть, но включить сессию только на чтение не удалось: {error}",
    "db.password_unset": "сервер просит пароль, а он не задан",
    "db.password": (
        "Вход под ролью {user!r} не удался: {detail}. Запишите пароль в {passfile} одной строкой "
        "host:port:database:user:password (права на файл — 600). Никогда не вставляйте его в чат."
    ),
    "db.refused": (
        "По адресу {where} никто не принимает подключения. Проверьте PGHOST и PGPORT и доступна ли база "
        "с этой машины (нужен VPN?)."
    ),
    "db.unresolved": "Имя хоста {host!r} не находится в DNS. Проверьте PGHOST.",
    "db.timeout": "Подключение к {where} не дождалось ответа. Возможно, хост закрыт файрволом или нужен VPN.",
    "db.unreachable": "{where} недоступен с этой машины: {error}. Подключите VPN и проверьте PGHOST.",
    "db.dropped": (
        "{where} принял подключение, но закрыл его при входе под ролью {user!r}, не объяснив причину. "
        "Обычно это неверный пароль или pg_hba.conf: проверьте файл паролей, потом спросите DBA."
    ),
    "db.no_database": "На {where} нет базы {database!r}. Проверьте PGDATABASE.",
    "db.login_refused": "Сервер не пустил роль {user!r}: {detail}. Проверьте роль и pg_hba.conf вместе с DBA.",
    "db.tls": "TLS-соединение с {where} не установилось: {detail}. Проверьте PGSSLMODE и PGSSLROOTCERT.",
    "db.cannot_connect": "Не удалось подключиться к {target}: {detail}",
    "db.server_error": "{message} (SQLSTATE {code})",
    "db.server_hint": "{text}. Подсказка: {hint}",
    "sql.summary": "SELECT, WITH ... SELECT, VALUES, TABLE, EXPLAIN и SHOW",
    "sql.empty": "Параметр query должен быть непустой строкой с одной SQL-командой",
    "sql.only_comments": "В запросе нет команды, только комментарии.",
    "sql.one_statement": "Отправляйте одну команду за вызов: в запросе есть ';' между командами.",
    "sql.not_allowed": (
        "Команда {word} запрещена: этот сервер только читает, поэтому скажите пользователю, что изменить "
        "данные здесь нельзя. Разрешены команды на чтение: {allowed}."
    ),
    "sql.not_allowed_other": (
        "Такая команда запрещена: этот сервер только читает. Разрешены команды на чтение: {allowed}."
    ),
    "sql.explain": "EXPLAIN разрешён только для SELECT, WITH ... SELECT, VALUES и TABLE.",
    "sql.forbidden_word": (
        "Ключевое слово {word} запрещено: {reason}. Этот сервер только читает: скажите пользователю, что "
        "изменить данные здесь нельзя."
    ),
    "sql.row_locks": "Блокировки строк (FOR SHARE, FOR KEY SHARE) запрещены: они мешают другим сессиям.",
    "sql.function": "Функция {name}() запрещена: {reason}.",
    "sql.unterminated_dollar": "Строка в долларовых кавычках не закрыта.",
    "sql.unterminated_comment": "Комментарий /* не закрыт.",
    "sql.unterminated_quote": "Кавычка {quote} не закрыта.",
    "sql.unicode_identifier": (
        'Идентификаторы в записи U&"..." запрещены: напишите имя как есть, при необходимости в двойных кавычках.'
    ),
    "sql.word.insert": "INSERT меняет данные",
    "sql.word.update": "UPDATE меняет данные, а FOR UPDATE блокирует строки",
    "sql.word.delete": "DELETE меняет данные",
    "sql.word.merge": "MERGE меняет данные",
    "sql.word.into": "SELECT ... INTO создаёт таблицу",
    "sql.reason.ends_session": "она завершает чужую сессию",
    "sql.reason.cancels_query": "она отменяет запрос чужой сессии",
    "sql.reason.settings": "она меняет настройки сессии",
    "sql.reason.unseen_sql": "она выполняет текст SQL, который эта проверка не видела",
    "sql.reason.unseen_cursor": "она читает курсор, который эта проверка не видела",
    "sql.reason.reads_files": "она читает файлы на сервере базы",
    "sql.reason.inspects_files": "она смотрит файлы на сервере базы",
    "sql.reason.writes_files": "она пишет файлы на сервере базы",
    "sql.reason.lists_files": "она перечисляет файлы на сервере базы",
    "sql.reason.large_objects": "она меняет большие объекты",
    "sql.reason.administers": "она управляет сервером",
    "sql.reason.signals": "она посылает сигнал чужой сессии",
    "sql.reason.wal": "она пишет в WAL",
    "sql.reason.catalog": "она меняет системный каталог",
    "sql.reason.wakes": "она будит другие сессии",
    "sql.reason.sequence": "она меняет последовательность",
    "sql.reason.sleeps": "она держит соединение впустую",
    "sql.reason.dblink": "она открывает отдельное соединение, на которое режим только чтения не распространяется",
    "sql.reason.resets_stats": "она сбрасывает статистику",
    "sql.reason.replication": "она управляет репликацией",
    "sql.reason.advisory": "advisory-блокировки переживают запрос и мешают другим сессиям",
    "sql.reason.slots": "она меняет слоты репликации",
    "stands.instructions": (
        "Этот сервер работает со многими стендами. status перечисляет их, list_schemas показывает схемы "
        "стенда. Каждый инструмент принимает stand; для чтения данных нужна и schema — сервер берёт её "
        "учётку; database — только если схема есть в нескольких базах стенда. search_schema и run_select "
        "принимают вместо stand ещё и stands — несколько стендов за один вызов; compare_schema сравнивает "
        "одну схему на двух стендах."
    ),
    "stands.status_note": "Со stand ещё и проверяет этот стенд.",
    "tool.list_schemas.title": "Список схем",
    "tool.list_schemas.description": "Базы стенда и схемы с учётками; вызывайте первым на новом стенде.",
    "argument.stand": "Один из стендов: {stands}.",
    "argument.stands": 'Вместо stand: "dev*" или "dev1,dev2", не больше {limit}.',
    "argument.stand_status": "Стенд для проверки; без него — список.",
    "argument.database": "Только если схема есть в нескольких базах.",
    "argument.schema_data": "Схема, под учёткой которой идёт запрос; имена ищутся в ней.",
    "argument.schema_stand": "Одна схема; без неё — весь стенд.",
    "status.stands": "стенды: {count} ({stands}), подключений в реестре: {connections}",
    "status.registry": "реестр: {files}",
    "status.limits_stands": (
        "лимиты: list_tables возвращает не больше {tables} записей на базу, ответы обрезаются до {budget} "
        "символов, открытыми держатся не больше {connections} подключений, stands охватывает не больше {many} "
        "стендов за вызов"
    ),
    "status.stand_hint": "передайте status стенд, чтобы проверить подключение к нему",
    "status.checked_with": "проверено через {label}",
    "stands.no_stand_argument": "Передайте {argument}, один из: {stands}.",
    "stands.unknown": "В реестре нет стенда {stand!r}. Стенды: {stands}.",
    "stands.unknown_similar": "В реестре нет стенда {stand!r}. Похожие: {similar}.",
    "stands.no_stand_or_stands": (
        'Передайте stand, один из: {stands}; или stands для нескольких сразу, например "dev1,dev2" или "dev*".'
    ),
    "stands.bad_stands_argument": (
        'Параметр stands должен быть именами стендов через запятую или шаблоном, например "dev1,dev2" или "dev*". '
        "Стенды: {stands}."
    ),
    "stands.one_or_many": "Передайте либо stand, либо stands, но не оба сразу.",
    "stands.no_match": "Под {pattern!r} не подходит ни один стенд. Стенды: {stands}.",
    "stands.too_many.one": (
        "{patterns!r} — это {count} стенд ({stands}), а один вызов охватывает не больше {limit}. "
        "Сузьте шаблон или перечислите через запятую не больше {limit} стендов."
    ),
    "stands.too_many.few": (
        "{patterns!r} — это {count} стенда ({stands}), а один вызов охватывает не больше {limit}. "
        "Сузьте шаблон или перечислите через запятую не больше {limit} стендов."
    ),
    "stands.too_many.many": (
        "{patterns!r} — это {count} стендов ({stands}), а один вызов охватывает не больше {limit}. "
        "Сузьте шаблон или перечислите через запятую не больше {limit} стендов."
    ),
    "stands.out_of_time": "{stands}: не ответили за {budget} секунд вызова; спросите о них отдельным вызовом.",
    "stands.no_rows": "нет строк на {stands}",
    "stands.no_matches": "ничего не нашлось на {stands}",
    "stands.no_schema_argument": (
        "Передайте schema: на стенде {stand} сервер читает данные под учёткой схемы, которую вы назовёте. "
        "Его схемы: {schemas}."
    ),
    "stands.unknown_schema": (
        "На стенде {stand} нет схемы {schema!r}, для которой у сервера есть учётка. Его схемы: {schemas}."
    ),
    "stands.more_names": "{names} и другие, всего {count}: list_schemas покажет все",
    "stands.more_stands": "{names} и другие, всего {count}: status покажет все",
    "stands.ask_every": 'Чтобы посмотреть на всех стендах, вызовите ещё раз со stands: "*" вместо stand.',
    "stands.ask_several": (
        "Чтобы посмотреть на нескольких стендах одним вызовом, передайте stands вместо stand: список или шаблон, "
        "не больше {limit} стендов."
    ),
    "stands.elsewhere": (
        "Схема {schema} есть и на {stands}; передайте stands, чтобы спросить до {limit} стендов одним вызовом."
    ),
    "stands.unknown_schema_similar": (
        "На стенде {stand} нет схемы {schema!r}, для которой у сервера есть учётка. Похожие: {similar}."
    ),
    "stands.schema_in_databases": (
        "Схема {schema!r} есть в нескольких базах стенда {stand}: {databases}. Передайте database, чтобы выбрать."
    ),
    "stands.schema_elsewhere": (
        "Схема {schema!r} стенда {stand} есть в {databases}, а не в {database!r}. Передайте одну из них в database."
    ),
    "stands.unknown_database": "На стенде {stand} в реестре нет базы {database!r}. Базы: {databases}.",
    "stands.table_in_several": (
        "{name!r} есть в нескольких схемах стенда {stand}: {options}. Передайте schema, чтобы выбрать одну."
    ),
    "stands.no_tables": "В схемах {schemas} нет таблиц и представлений: схемы пустые или их нет в этой базе.",
    "stands.table_not_found": (
        "В схемах стенда {stand}, для которых у сервера есть учётки, нет таблицы или представления {name!r}. "
        "Вызовите search_schema или list_schemas."
    ),
    "stands.table_elsewhere": (
        "{error} В этой базе таблица с таким именем есть в схеме {schemas}: повторите вызов с этой схемой."
    ),
    "stands.unreadable": "не удалось прочитать реестр стендов {path}: {error}",
    "stands.bad_line": "{path}: {problem}",
    "stands.line": "строка {number} — не [секция] и не ключ=значение",
    "stands.no_host": "у подключения {name} в {path} нет host, пропущено",
    "stands.bad_number": "у подключения {name} в {path} {option}={value!r} — не число, пропущено",
    "stands.none_found": (
        "PG_EXPLORER_MCP_STANDS задана, но в {files} не нашлось ни одного стенда; работаю по переменным PG*"
    ),
    "stands.more_warnings": "... и ещё проблем в реестре стендов: {count}",
    "tool.compare_schema.title": "Сравнить схему",
    "tool.compare_schema.description": (
        "Чем одна схема отличается на двух стендах — или на stand_a и нескольких (stands): таблицы, колонки, "
        "ограничения, индексы. Данные не читаются."
    ),
    "argument.stand_a": "Первый стенд.",
    "argument.stand_b": "Второй стенд.",
    "argument.schema_compare": "Схема для сравнения.",
    "compare.header": "{a}  vs  {b}",
    "compare.only_on": "только на {stand}:",
    "compare.differ": "отличаются:",
    "compare.same": "одинаково: {tables}, {columns}",
    "compare.tables.one": "{count} таблица",
    "compare.tables.few": "{count} таблицы",
    "compare.tables.many": "{count} таблиц",
    "compare.columns.one": "{count} колонка",
    "compare.columns.few": "{count} колонки",
    "compare.columns.many": "{count} колонок",
    "compare.constraint": "{table}: ограничение {name}",
    "compare.index": "{table}: индекс {name}",
    "compare.only_here": "{what} только на {stand}: {definition}",
    "compare.same_stand": "stand_a и stand_b — один и тот же стенд {stand!r}. Передайте два разных стенда.",
    "argument.stands_compare": (
        'Вместо stand_b: несколько стендов против stand_a, "dev*" или "dev1,dev2", не больше {limit}.'
    ),
    "compare.many_header": "{a}  против стендов: {count}",
    "compare.like": "как {a}",
    "compare.summary": "таблиц только на {a}: {only_a}, только здесь: {only_b}, отличий: {differ}",
    "compare.totals": "как {a}: {same}; отличаются: {differ}; не проверены: {skipped}",
    "compare.b_or_stands": "Передайте либо stand_b, либо stands, не оба.",
    "compare.need_b": "Передайте stand_b или stands для нескольких стендов сразу.",
    "compare.no_others": "{pattern!r} называет только {stand!r} — сам эталон. Назовите другие стенды.",
    "compare.no_schema": (
        "На стенде {stand} в базе {database} нет схемы {schema!r}: она есть в реестре, но не в базе. Сравнивать нечего."
    ),
    "stands.readable_by_others": "в {path} есть пароль, а читать файл могут другие пользователи: chmod 600 {path}",
    "stands.reload_kept": (
        "реестр стендов изменился ({files}), но прочитать из него стенды не удалось: остаются стенды, прочитанные "
        "раньше"
    ),
    "install.help.import": "перенести подключения к стендам из DBeaver или файла pg_service.conf в pg-explorer-mcp",
    "install.help.source": "откуда брать подключения: dbeaver или pg-service",
    "install.help.file": "файл pg_service.conf, который читать (для pg-service)",
    "install.help.workspace": "папка DBeaver с data-sources.json (по умолчанию — обычное место для этой ОС)",
    "install.help.write": "записать реестр и файл паролей и зарегистрировать pg-stands; без него — только показать",
    "install.help.prune": "вместе с --write: заменить реестр, а не дополнять",
    "install.help.stand_pattern": (
        "регулярное выражение, которое находит стенд в папке, имени подключения или хосте (по умолчанию: {pattern})"
    ),
    "import.unreadable": "не удалось прочитать {path}: {error}",
    "import.bad_pattern": "шаблон стенда {pattern!r} — не регулярное выражение: {error}",
    "import.duplicate": "тот же стенд, база и пользователь, что у {other!r}: [{name}] уже занята",
    "import.no_host": "нет хоста",
    "import.no_database": "нет базы",
    "import.no_user": "нет пользователя: pg-explorer-mcp читает схему под одноимённой учёткой",
    "import.no_stand": "стенд не найден ни в папке, ни в имени, ни в хосте",
    "import.bad_name": "{part!r} не годится в имя секции (только буквы, цифры, _ $ -)",
    "import.reading": "читаю {source}",
    "import.preview_header": "в {registry} попадут подключения: {count}",
    "import.preview_no_password": "пароль не сохранён: добавьте строку в файл паролей",
    "import.preview_passwords": "в {passfile} попадут пароли: {count}",
    "import.preview_skipped": "пропущено: {count}",
    "import.nothing": "импортировать нечего",
    "import.dry_run": (
        "Это был предпросмотр, ничего не записано. Добавьте --write, чтобы записать файлы и зарегистрировать pg-stands."
    ),
    "import.cannot_write": "не удалось записать {path}: {error}. Ничего не изменено.",
    "import.written": "записан {path}",
    "import.written_backup": "записан {path}; прежний файл сохранён как {backup}",
    "import.no_dbeaver": (
        "подключения DBeaver на этой машине не найдены; передайте --workspace с папкой, где лежит data-sources.json"
    ),
    "import.need_file": "pg-service нужен файл: install.py import pg-service ~/.pg_service.conf",
    "import.offer": "Найдены подключения DBeaver в {path}. Показать, что импорт перенесёт в pg-explorer-mcp? [y/N] ",
    "import.offer_hint": "Чтобы перенести их, выполните: {command}",
    "export.help": (
        "добавить подключения к стендам в инструмент работы с БД: DBeaver, DataGrip, pgAdmin, psql, VS Code"
    ),
    "export.help.tool": (
        "инструмент: dbeaver, datagrip (и другие IDE JetBrains), pgadmin, pg-service (psql, pgcli), "
        "vscode-pgsql или sqltools"
    ),
    "export.help.stands": "какие стенды выгрузить: имена или шаблоны через запятую (по умолчанию — все)",
    "export.help.write": "записать файлы; без него — только показать, что будет записано",
    "export.help.project": (
        "для datagrip: папка проекта, куда добавить источники данных; сначала закройте проект в IDE "
        "(по умолчанию: {path})"
    ),
    "export.help.editor": "для vscode-pgsql и sqltools: редактор, в чьи настройки пользователя добавить подключения",
    "export.no_stands": (
        "стендов для выгрузки нет: сначала импортируйте их (install.py import) "
        "или проверьте PG_EXPLORER_MCP_STANDS_FILE"
    ),
    "export.no_match": "под {patterns} не подходит ни один стенд; в реестре есть {stands}",
    "export.no_dbeaver": (
        "подключения DBeaver на этой машине не найдены; передайте --workspace с папкой, где лежит data-sources.json"
    ),
    "export.not_a_folder": "{path} — не папка",
    "export.no_editor": "настройки {editor} не найдены в {path}: редактор установлен?",
    "export.unreadable": "не удалось прочитать {path}: {error}. Ничего не изменено.",
    "export.not_json": (
        "{path} — не чистый JSON (комментарии?), поэтому файл не тронут. Добавьте это в него вручную:\n{snippet}"
    ),
    "export.not_a_list": "{setting} в настройках редактора — не список. Ничего не изменено.",
    "export.preview.one": "{tool}: {count} подключение ({stands}) попадёт в:",
    "export.preview.few": "{tool}: {count} подключения ({stands}) попадут в:",
    "export.preview.many": "{tool}: {count} подключений ({stands}) попадут в:",
    "export.present_dbeaver": ("пропущены, уже есть в DBeaver с тем же хостом, портом, базой и пользователем: {count}"),
    "export.present_service": "пропущены, уже есть в {path}: {count}",
    "export.nothing_new": "в {tool} все эти подключения уже есть, записывать нечего",
    "export.dry_run": "Это был предпросмотр, ничего не записано. Добавьте --write, чтобы записать файлы.",
    "export.cannot_place": "не удалось заменить {path}: {error}. Файл остался прежним.",
    "export.next_dbeaver": (
        "Перезапустите DBeaver: подключения лежат в папках по стендам, пароли берутся из файла паролей."
    ),
    "export.next_datagrip": (
        "Откройте папку {path} как проект в DataGrip или другой IDE JetBrains (File | Open), а если проект уже "
        "открыт — откройте его заново: источники данных сгруппированы по стендам, пароли берутся из файла паролей."
    ),
    "export.next_pgadmin": (
        "В pgAdmin: Tools | Import/Export Servers, Import, файл {path}. Серверы сгруппированы по стендам "
        "и берут пароли из {passfile}."
    ),
    "export.next_pg_service": (
        'Подключайтесь так: psql "service={service}" или pgcli "service={service}"; схема идёт первой в search_path.'
    ),
    "export.next_vscode_pgsql": (
        "Перезагрузите окно редактора: подключения появятся в панели PostgreSQL, по группе на стенд. Расширение "
        "не умеет читать файл паролей: оно спросит каждый пароль один раз и сохранит его в системной связке ключей."
    ),
    "export.next_sqltools": (
        "Перезагрузите окно редактора: подключения появятся в SQLTools, по группе на стенд, пароли берутся "
        "из файла паролей."
    ),
    "export.offer": "Найдены {tools}. Добавить туда подключения к стендам ({count})? [y/N] ",
    "export.offer_hint": "Чтобы добавить их позже, выполните:",
    "install.description": "Регистрирует сервер pg-explorer-mcp в настройках агента и проверяет его.",
    "install.help.client": "агент или редактор, где зарегистрировать сервер (по умолчанию: gigacode)",
    "install.help.print": "напечатать запись и куда её положить, ничего не регистрируя",
    "clients.where_file": "для {client} добавьте это в {path}:",
    "clients.where_paste": (
        "{client} хранит серверы в настройках IDE: Settings | Tools | AI Assistant | Model Context Protocol (MCP) | "
        "Add, затем вставьте это в поле JSON configuration:"
    ),
    "clients.offer": "Найдены ещё {clients}. Зарегистрировать {name} и там? [y/N] ",
    "clients.offer_hint": "Чтобы зарегистрировать {name} там позже: {command}",
    "install.help.check": "только запустить сервер и вызвать status, ничего не меняя",
    "install.help.profile": "инструменты для группы: schema, explore (по умолчанию) или analyst (добавляет run_select)",
    "install.help.lang": "язык сервера и этого скрипта: en или ru (по умолчанию — по локали системы)",
    "install.help.name": (
        "зарегистрировать ещё одну базу как pg-ИМЯ рядом с pg (строчные латинские буквы, цифры, дефисы)"
    ),
    "install.bad_name": (
        "{name!r} не годится в имя сервера: нужны строчные латинские буквы, цифры и дефисы, до 30 знаков"
    ),
    "install.old_python": "pg-explorer-mcp нужен Python {need} или новее, а здесь {have}",
    "install.backup": "копия прежних настроек: {path}",
    "install.not_json": "{path} — не чистый JSON (комментарии?). Добавьте это в него вручную:\n{snippet}",
    "install.registered": "MCP-сервер {name!r} зарегистрирован в {path}",
    "install.did_not_run": "сервер не запустился: {error}",
    "install.no_answer": "сервер ответил не так, как ожидалось:",
    "install.protocol": "протокол {version}",
    "install.next": "дальше: {client} extensions link {path}",
    "install.old_extension": (
        "в {path} осталась запись расширения, подключённого как pg-mcp до 0.8.0: если команда ответит, что"
        " расширение уже установлено, удалите эту папку и выполните команду снова"
    ),
    "install.restart": "затем перезапустите агента, чтобы он запустил сервер с новыми настройками",
}

CATALOG: dict[str, dict[str, str]] = {"en": EN, "ru": RU}
