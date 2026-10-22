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
    """The language to speak, and the PG_MCP_LANG value it had to ignore, if any.

    PG_MCP_LANG decides when it is set. Otherwise the locale variables do and,
    when none is set (as on Windows), the locale the system reports.
    """
    explicit = env.get("PG_MCP_LANG", "").strip()
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
        "Read-only access to one PostgreSQL database. Call status first: it shows the database, the role, "
        "the tools this profile offers and the limits in force. Then learn the structure before querying data: "
        "list_tables for an overview, search_schema to find tables and columns by name or comment in a large "
        "database, describe_table for the details. Reply to the user in the language they write in."
    ),
    "number.decimal": ".",
    # Tools as the model sees them in tools/list.
    "tool.status.title": "Database connection status",
    "tool.status.description": (
        "Show which PostgreSQL database this server uses, as which role, the profile with its tools, "
        "and the limits in force. Call it first, and again whenever another tool reports a connection "
        "problem: its output says what to fix."
    ),
    "tool.list_tables.title": "List tables",
    "tool.list_tables.description": (
        "List the tables, views, materialized views and foreign tables the current role can read, "
        "with estimated row counts and comments. At most {limit} entries; pass schema to narrow the list."
    ),
    "tool.list_tables.schema": 'Only list objects in this schema, e.g. "public". Omit to list all schemas.',
    "tool.search_schema.title": "Search the schema",
    "tool.search_schema.description": (
        "Find tables, views and columns whose name or comment contains a piece of text, in every schema the "
        "current role can read. In a large database use it instead of paging through list_tables. "
        "At most {limit} matches."
    ),
    "tool.describe_table.title": "Describe a table",
    "tool.describe_table.description": (
        "Describe one table or view the current role can read: columns with types, NOT NULL, defaults and "
        "comments, then primary, unique and foreign keys, the tables that reference it, and its indexes. "
        'Accepts "orders" or "sales.orders".'
    ),
    "tool.sample_rows.title": "Sample rows",
    "tool.sample_rows.description": (
        "Show a few rows of one table or view the current role can read, optionally only some columns. "
        "Default {default} rows, at most {limit}, in no particular order."
    ),
    "tool.explain_query.title": "Explain a query",
    "tool.explain_query.description": (
        "Show PostgreSQL's plan for one read-only query: SELECT, WITH ... SELECT, VALUES or TABLE, "
        "passed without EXPLAIN. The query is not run."
    ),
    "tool.explain_query.description_analyze": (
        "Show PostgreSQL's plan for one read-only query: SELECT, WITH ... SELECT, VALUES or TABLE, "
        "passed without EXPLAIN. The query is not run unless analyze is true: then it runs inside a "
        "read-only transaction and the plan shows real timings."
    ),
    "tool.run_select.title": "Run a read-only query",
    "tool.run_select.description": (
        "Run one read-only SQL statement in the PostgreSQL dialect: SELECT, WITH ... SELECT, VALUES, TABLE, "
        "EXPLAIN or SHOW. Returns at most {limit} rows; use WHERE, ORDER BY and LIMIT to get the rows you "
        "need. Statements that change data, structure, settings or other sessions are refused."
    ),
    "argument.table": 'Table or view name, optionally with its schema: "orders" or "sales.orders".',
    "argument.schema": "Schema to look in. Omit it to search every schema the role can read.",
    "argument.columns": 'Comma-separated column names, e.g. "id, total". Omit for all.',
    "argument.limit": "How many rows, 1 to {limit}. Default {default}.",
    "argument.query": "One SQL statement in the PostgreSQL dialect.",
    "argument.text": 'A piece of a name or comment, e.g. "order" or "customer".',
    "argument.analyze": "Run the query to get real timings. Default false.",
    # status
    "status.version": "pg-mcp {version} on Python {python}, installed at {path}",
    "status.name": "server name: {name}",
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
    "warning.profile": "PG_MCP_PROFILE={value!r} is not one of {choices}; using {fallback!r}",
    "warning.language": "PG_MCP_LANG={value!r} is not one of {choices}; using {fallback!r}",
    # Arguments and answers of the tools.
    "error.arguments": "arguments must be a JSON object",
    "error.unknown_arguments": "Unknown argument(s): {unknown}. Allowed: {allowed}.",
    "error.missing_arguments": "Missing argument(s): {missing}.",
    "error.none": "none",
    "error.schema": 'schema must be a non-empty string such as "public"; omit it to list all schemas',
    "error.schema_search": 'schema must be a non-empty string such as "public"; omit it to search every schema',
    "error.table": 'table must be a non-empty string such as "orders" or "sales.orders"',
    "error.columns": 'columns must be a comma-separated string such as "id, total"',
    "error.text": 'text must be a non-empty string such as "order"',
    "error.whole_number": "{name} must be a whole number from {low} to {high}",
    "error.out_of_range": "{name} must be from {low} to {high}, got {number}",
    "error.flag": "{name} must be true or false",
    "error.no_tables": "The current role cannot read any table in this database. Ask the DBA to grant SELECT.",
    "error.no_tables_in_schema": "No readable tables in schema {schema!r}. Schemas with readable tables: {visible}.",
    "error.ambiguous": "{name!r} exists in several schemas: {options}. Pass schema to pick one.",
    "error.not_found": "No readable table or view named {name!r}{where}. Call list_tables to see what exists.",
    "error.not_found_similar": "No readable table or view named {name!r}{where}. Similar names: {similar}.",
    "error.in_schema": " in schema {schema!r}",
    "error.no_column": "No column {name!r} in {relation}. Columns: {columns}.",
    "error.explain_row_sources": (
        "explain_query takes a SELECT, WITH ... SELECT, VALUES or TABLE statement, without EXPLAIN."
    ),
    "list.more": "... more than {limit} entries; pass schema to narrow the list",
    "search.nothing": "Nothing matches {text!r}. Try a shorter piece of the name, another spelling, or list_tables.",
    "search.more": "... more than {limit} matches; use a longer piece of text or pass schema",
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
    "sql.not_allowed": "{word} is not allowed. Only read-only statements are: {allowed}.",
    "sql.not_allowed_other": "This statement is not allowed. Only read-only statements are: {allowed}.",
    "sql.explain": "EXPLAIN is allowed only for SELECT, WITH ... SELECT, VALUES and TABLE.",
    "sql.forbidden_word": "{word} is not allowed: {reason}. This server only reads.",
    "sql.row_locks": "Row locks (FOR SHARE, FOR KEY SHARE) are not allowed: they block other sessions.",
    "sql.function": "Function {name}() is not allowed: {reason}.",
    "sql.unterminated_dollar": "Unterminated dollar-quoted string.",
    "sql.unterminated_comment": "Unterminated /* comment.",
    "sql.unterminated_quote": "Unterminated {quote} quote.",
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
        "This server reaches many stands. status lists them and list_schemas shows the schemas of one; every "
        "tool takes stand, and reading data takes schema as well. search_schema and run_select take stands "
        "instead of stand to ask several stands the same thing in one call."
    ),
    "stands.tool_note": "Pass stand, and schema to read data with that schema's login.",
    "stands.tool_note_many": (
        "Pass stand, or stands to ask several stands in one call; and schema to read data with that schema's login."
    ),
    "stands.status_note": "Without stand it lists the stands; with stand it also checks that stand's connection.",
    "tool.list_schemas.title": "List schemas",
    "tool.list_schemas.description": (
        "List the databases of a stand and their schemas that this server has logins for. "
        "Call it before working on a stand you have not used yet."
    ),
    "argument.stand": 'Stand name, e.g. "dev7"; status lists the stands.',
    "argument.stands": (
        'Several stands in one call, instead of stand: names separated by commas or a pattern, e.g. "dev1,dev2" '
        'or "dev*"; at most {limit} stands.'
    ),
    "argument.stand_status": 'A stand to check, e.g. "dev7". Omit it for the list of stands.',
    "argument.database": "Database on the stand. Needed only when the schema exists in several of its databases.",
    "argument.schema_data": "Schema to work in: the server uses its login, and unqualified names resolve in it.",
    "argument.schema_stand": "Only this schema, read with its login. Omit it to cover every schema of the stand.",
    "status.stands": "stands: {count} ({stands}), connections in the registry: {connections}",
    "status.registry": "registry: {files}",
    "status.limits_stands": (
        "limits: list_tables returns at most {tables} entries per database, answers are cut at {budget} "
        "characters, at most {connections} connections stay open, stands covers at most {many} stands per call"
    ),
    "status.stand_hint": "pass stand to status to check that stand's connection",
    "status.checked_with": "checked with {label}",
    "stands.no_stand_argument": "Pass stand, one of: {stands}.",
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
        "{stands}: not tried, the call would exceed its {budget} seconds; ask for them in another call."
    ),
    "stands.no_schema_argument": (
        "Pass schema: on stand {stand} the server reads data with the login of the schema you name. "
        "list_schemas shows them."
    ),
    "stands.unknown_schema": (
        "Stand {stand} has no schema {schema!r} that the server has a login for. Call list_schemas to see them."
    ),
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
    "stands.unreadable": "cannot read the stands registry {path}: {error}",
    "stands.bad_line": "{path}: {problem}",
    "stands.line": "line {number} is neither [section] nor key=value",
    "stands.no_host": "stand connection {name} in {path} has no host, skipped",
    "stands.bad_number": "stand connection {name} in {path} has {option}={value!r}, not a number, skipped",
    "stands.none_found": "PG_MCP_STANDS is set, but no stands were found in {files}; working with the PG* settings",
    "stands.more_warnings": "... and {count} more problems in the stands registry",
    # install.py
    "install.description": "Register the pg-mcp server in the agent's settings and check it.",
    "install.help.client": "whose settings.json to edit",
    "install.help.check": "only start the server and call status; change nothing",
    "install.help.profile": "tools for this group: schema, explore (default) or analyst (adds run_select)",
    "install.help.lang": "language of the server and of this script: en or ru (default: from the system locale)",
    "install.help.name": "register a further database as pg-NAME next to pg (lowercase letters, digits, hyphens)",
    "install.bad_name": "{name!r} cannot be a server name: use lowercase letters, digits and hyphens, at most 30",
    "install.old_python": "pg-mcp needs Python {need} or newer, this is {have}",
    "install.backup": "backup of the old settings: {path}",
    "install.not_json": "{path} is not plain JSON (comments?). Merge this into it by hand:\n{snippet}",
    "install.registered": "registered MCP server {name!r} in {path}",
    "install.did_not_run": "the server did not run: {error}",
    "install.no_answer": "the server did not answer as expected:",
    "install.protocol": "protocol {version}",
    "install.next": "next: {client} extensions link {path}",
    "install.restart": "then restart the agent so it starts the server with the new settings",
}

RU: dict[str, str] = {
    "server.instructions": (
        "Доступ только на чтение к одной базе PostgreSQL. Сначала вызовите status: он покажет базу, роль, "
        "инструменты этого профиля и действующие лимиты. Затем, прежде чем запрашивать данные, изучите "
        "структуру: list_tables — для обзора, search_schema — чтобы найти таблицы и колонки по имени или "
        "комментарию в большой базе, describe_table — для подробностей. Отвечайте пользователю на том языке, "
        "на котором он пишет."
    ),
    "number.decimal": ",",
    "tool.status.title": "Состояние подключения к базе",
    "tool.status.description": (
        "Показывает, к какой базе PostgreSQL подключён сервер и под какой ролью, какой профиль включён и какие "
        "в нём инструменты, а также действующие лимиты. Вызывайте первым, а также каждый раз, когда другой "
        "инструмент сообщает о проблеме с подключением: в ответе сказано, что исправить."
    ),
    "tool.list_tables.title": "Список таблиц",
    "tool.list_tables.description": (
        "Перечисляет таблицы, представления, материализованные представления и внешние таблицы, которые "
        "текущая роль может читать, с оценкой числа строк и комментариями. Не больше {limit} записей; "
        "чтобы сузить список, передайте schema."
    ),
    "tool.list_tables.schema": 'Показать объекты только этой схемы, например "public". Без него — все схемы.',
    "tool.search_schema.title": "Поиск по схеме",
    "tool.search_schema.description": (
        "Находит таблицы, представления и колонки, в имени или комментарии которых есть заданный фрагмент, "
        "во всех схемах, доступных текущей роли для чтения. В большой базе используйте его вместо листания "
        "list_tables. Не больше {limit} совпадений."
    ),
    "tool.describe_table.title": "Описание таблицы",
    "tool.describe_table.description": (
        "Описывает одну таблицу или представление, доступные текущей роли для чтения: колонки с типами, "
        "NOT NULL, значениями по умолчанию и комментариями, затем первичный, уникальные и внешние ключи, "
        'таблицы, которые на неё ссылаются, и индексы. Принимает "orders" или "sales.orders".'
    ),
    "tool.sample_rows.title": "Примеры строк",
    "tool.sample_rows.description": (
        "Показывает несколько строк одной таблицы или представления, доступных текущей роли для чтения, — "
        "все колонки или только выбранные. По умолчанию {default} строк, не больше {limit}, "
        "в произвольном порядке."
    ),
    "tool.explain_query.title": "План запроса",
    "tool.explain_query.description": (
        "Показывает план PostgreSQL для одного запроса только на чтение: SELECT, WITH ... SELECT, VALUES или "
        "TABLE, переданного без EXPLAIN. Запрос не выполняется."
    ),
    "tool.explain_query.description_analyze": (
        "Показывает план PostgreSQL для одного запроса только на чтение: SELECT, WITH ... SELECT, VALUES или "
        "TABLE, переданного без EXPLAIN. Запрос не выполняется, если только analyze не равен true: тогда он "
        "выполняется в транзакции только на чтение, и в плане видно реальное время."
    ),
    "tool.run_select.title": "Выполнить запрос на чтение",
    "tool.run_select.description": (
        "Выполняет одну SQL-команду только на чтение на диалекте PostgreSQL: SELECT, WITH ... SELECT, VALUES, "
        "TABLE, EXPLAIN или SHOW. Возвращает не больше {limit} строк; нужные строки отбирайте через WHERE, "
        "ORDER BY и LIMIT. Команды, которые меняют данные, структуру или настройки либо затрагивают другие "
        "сессии, отклоняются."
    ),
    "argument.table": 'Имя таблицы или представления, можно со схемой: "orders" или "sales.orders".',
    "argument.schema": "Схема, в которой искать. Без неё поиск идёт по всем схемам, доступным роли.",
    "argument.columns": 'Имена колонок через запятую, например "id, total". Без него — все колонки.',
    "argument.limit": "Сколько строк, от 1 до {limit}. По умолчанию {default}.",
    "argument.query": "Одна SQL-команда на диалекте PostgreSQL.",
    "argument.text": 'Фрагмент имени или комментария, например "order" или "заказ".',
    "argument.analyze": "Выполнить запрос, чтобы увидеть реальное время. По умолчанию false.",
    "status.version": "pg-mcp {version} на Python {python}, установлен в {path}",
    "status.name": "имя сервера: {name}",
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
    "warning.profile": "PG_MCP_PROFILE={value!r} — нет такого профиля (есть {choices}); включён {fallback!r}",
    "warning.language": "PG_MCP_LANG={value!r} — нет такого языка (есть {choices}); включён {fallback!r}",
    "error.arguments": "Аргументы должны быть JSON-объектом",
    "error.unknown_arguments": "Неизвестные аргументы: {unknown}. Допустимые: {allowed}.",
    "error.missing_arguments": "Не хватает аргументов: {missing}.",
    "error.none": "нет",
    "error.schema": (
        'Параметр schema должен быть непустой строкой, например "public"; чтобы увидеть все схемы, не передавайте его'
    ),
    "error.schema_search": (
        'Параметр schema должен быть непустой строкой, например "public"; чтобы искать во всех схемах, '
        "не передавайте его"
    ),
    "error.table": 'Параметр table должен быть непустой строкой, например "orders" или "sales.orders"',
    "error.columns": 'Параметр columns должен быть строкой с именами через запятую, например "id, total"',
    "error.text": 'Параметр text должен быть непустой строкой, например "заказ"',
    "error.whole_number": "Параметр {name} должен быть целым числом от {low} до {high}",
    "error.out_of_range": "Параметр {name} должен быть от {low} до {high}, а передано {number}",
    "error.flag": "Параметр {name} должен быть true или false",
    "error.no_tables": "Текущая роль не может читать ни одну таблицу в этой базе. Попросите DBA выдать права SELECT.",
    "error.no_tables_in_schema": (
        "В схеме {schema!r} нет таблиц, доступных для чтения. Схемы с такими таблицами: {visible}."
    ),
    "error.ambiguous": "{name!r} есть в нескольких схемах: {options}. Чтобы выбрать одну, передайте schema.",
    "error.not_found": (
        "Среди доступных для чтения нет таблицы или представления {name!r}{where}. "
        "Вызовите list_tables, чтобы увидеть, что есть."
    ),
    "error.not_found_similar": (
        "Среди доступных для чтения нет таблицы или представления {name!r}{where}. Похожие имена: {similar}."
    ),
    "error.in_schema": " в схеме {schema!r}",
    "error.no_column": "В {relation} нет колонки {name!r}. Колонки: {columns}.",
    "error.explain_row_sources": (
        "explain_query принимает команду SELECT, WITH ... SELECT, VALUES или TABLE без EXPLAIN."
    ),
    "list.more": "... записей больше {limit}; чтобы сузить список, передайте schema",
    "search.nothing": (
        "Ничего не нашлось по {text!r}. Попробуйте фрагмент покороче, другое написание или list_tables."
    ),
    "search.more": "... совпадений больше {limit}; уточните фрагмент или передайте schema",
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
    "sql.not_allowed": "Команда {word} запрещена. Разрешены только команды на чтение: {allowed}.",
    "sql.not_allowed_other": "Такая команда запрещена. Разрешены только команды на чтение: {allowed}.",
    "sql.explain": "EXPLAIN разрешён только для SELECT, WITH ... SELECT, VALUES и TABLE.",
    "sql.forbidden_word": "Ключевое слово {word} запрещено: {reason}. Этот сервер только читает.",
    "sql.row_locks": "Блокировки строк (FOR SHARE, FOR KEY SHARE) запрещены: они мешают другим сессиям.",
    "sql.function": "Функция {name}() запрещена: {reason}.",
    "sql.unterminated_dollar": "Строка в долларовых кавычках не закрыта.",
    "sql.unterminated_comment": "Комментарий /* не закрыт.",
    "sql.unterminated_quote": "Кавычка {quote} не закрыта.",
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
        "Этот сервер работает со многими стендами. status перечисляет их, list_schemas показывает схемы стенда; "
        "каждый инструмент принимает stand, а для чтения данных нужна ещё и schema. search_schema и run_select "
        "принимают вместо stand ещё и stands: один и тот же запрос к нескольким стендам за один вызов."
    ),
    "stands.tool_note": "Передайте stand, а для чтения данных — schema: данные читаются под учёткой этой схемы.",
    "stands.tool_note_many": (
        "Передайте stand или stands — несколько стендов за один вызов, а для чтения данных — schema: данные "
        "читаются под учёткой этой схемы."
    ),
    "stands.status_note": "Без stand перечисляет стенды; со stand ещё и проверяет подключение к этому стенду.",
    "tool.list_schemas.title": "Список схем",
    "tool.list_schemas.description": (
        "Показывает базы стенда и их схемы, для которых у сервера есть учётки. "
        "Вызывайте перед работой со стендом, на котором ещё не были."
    ),
    "argument.stand": 'Имя стенда, например "dev7"; status перечисляет стенды.',
    "argument.stands": (
        'Несколько стендов за один вызов, вместо stand: имена через запятую или шаблон, например "dev1,dev2" '
        'или "dev*"; не больше {limit} стендов.'
    ),
    "argument.stand_status": 'Стенд, который нужно проверить, например "dev7". Без него — список стендов.',
    "argument.database": "База на стенде. Нужна, только если схема есть в нескольких его базах.",
    "argument.schema_data": "Схема, в которой работать: сервер берёт её учётку, а имена без схемы ищутся в ней.",
    "argument.schema_stand": "Только эта схема, под её учёткой. Без неё — все схемы стенда.",
    "status.stands": "стенды: {count} ({stands}), подключений в реестре: {connections}",
    "status.registry": "реестр: {files}",
    "status.limits_stands": (
        "лимиты: list_tables возвращает не больше {tables} записей на базу, ответы обрезаются до {budget} "
        "символов, открытыми держатся не больше {connections} подключений, stands охватывает не больше {many} "
        "стендов за вызов"
    ),
    "status.stand_hint": "передайте status стенд, чтобы проверить подключение к нему",
    "status.checked_with": "проверено через {label}",
    "stands.no_stand_argument": "Передайте stand, один из: {stands}.",
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
    "stands.out_of_time": (
        "{stands}: не проверялись, вызов не уложился бы в {budget} секунд; спросите о них отдельным вызовом."
    ),
    "stands.no_schema_argument": (
        "Передайте schema: на стенде {stand} сервер читает данные под учёткой схемы, которую вы назовёте. "
        "list_schemas покажет схемы."
    ),
    "stands.unknown_schema": (
        "На стенде {stand} нет схемы {schema!r}, для которой у сервера есть учётка. "
        "Вызовите list_schemas, чтобы увидеть схемы."
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
    "stands.unreadable": "не удалось прочитать реестр стендов {path}: {error}",
    "stands.bad_line": "{path}: {problem}",
    "stands.line": "строка {number} — не [секция] и не ключ=значение",
    "stands.no_host": "у подключения {name} в {path} нет host, пропущено",
    "stands.bad_number": "у подключения {name} в {path} {option}={value!r} — не число, пропущено",
    "stands.none_found": "PG_MCP_STANDS задана, но в {files} не нашлось ни одного стенда; работаю по переменным PG*",
    "stands.more_warnings": "... и ещё проблем в реестре стендов: {count}",
    "install.description": "Регистрирует сервер pg-mcp в настройках агента и проверяет его.",
    "install.help.client": "чей settings.json править",
    "install.help.check": "только запустить сервер и вызвать status, ничего не меняя",
    "install.help.profile": "инструменты для группы: schema, explore (по умолчанию) или analyst (добавляет run_select)",
    "install.help.lang": "язык сервера и этого скрипта: en или ru (по умолчанию — по локали системы)",
    "install.help.name": (
        "зарегистрировать ещё одну базу как pg-ИМЯ рядом с pg (строчные латинские буквы, цифры, дефисы)"
    ),
    "install.bad_name": (
        "{name!r} не годится в имя сервера: нужны строчные латинские буквы, цифры и дефисы, до 30 знаков"
    ),
    "install.old_python": "pg-mcp нужен Python {need} или новее, а здесь {have}",
    "install.backup": "копия прежних настроек: {path}",
    "install.not_json": "{path} — не чистый JSON (комментарии?). Добавьте это в него вручную:\n{snippet}",
    "install.registered": "MCP-сервер {name!r} зарегистрирован в {path}",
    "install.did_not_run": "сервер не запустился: {error}",
    "install.no_answer": "сервер ответил не так, как ожидалось:",
    "install.protocol": "протокол {version}",
    "install.next": "дальше: {client} extensions link {path}",
    "install.restart": "затем перезапустите агента, чтобы он запустил сервер с новыми настройками",
}

CATALOG: dict[str, dict[str, str]] = {"en": EN, "ru": RU}
