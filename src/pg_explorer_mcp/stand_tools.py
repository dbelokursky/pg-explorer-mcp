"""The tools of a server with many stands: the same tools, told which stand and schema to use.

Every tool takes stand. Reading data needs schema too, and the server connects
with that schema's own login (ADR 0006); a database the registry holds one login
for is read through it as a whole, whatever schema is named. Structure across a
whole stand comes from the catalog, one query per database, kept to the schemas
the registry has logins for, or to what the one login reads. Every answer starts
with where it came from: stand / database / schema. search_schema and run_select
also take stands, several stands for the same request: they are asked at the
same time, and the answer has a section per stand. compare_schema reads one
schema on two stands with its login on each.
"""

from __future__ import annotations

import contextvars
import platform
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from typing import Any, Optional

from . import __version__
from .budget import DEFAULT_BUDGET
from .compare import Structure, compare_schema, comparison, read_structure
from .db import Database, DatabaseUnavailableError, QueryFailedError
from .i18n import current, plural, t
from .querylog import ERROR, OK, SKIPPED, UNAVAILABLE, QueryLog
from .sqlguard import check_read_only
from .stands import (
    MAX_STANDS_PER_CALL,
    Pool,
    Registry,
    StandError,
    Target,
    by_database,
    names_several,
    stand_ranges,
    summarize,
    whole_database,
)
from .tools import (
    ALL_SCHEMAS_SQL,
    DEFAULT_NAME,
    DEFAULT_PROFILE,
    LIST_TABLES_LIMIT,
    PROFILES,
    RUN_SELECT_LIMIT,
    SAMPLE_ROWS_DEFAULT,
    SAMPLE_ROWS_LIMIT,
    SEARCH_LIMIT,
    Answer,
    Tool,
    ToolError,
    describe_table,
    diagram_properties,
    explain_query,
    list_tables,
    relations_named,
    run_select,
    sample_rows,
    schema_diagram,
    search_schema,
    split_name,
)

# The tools only a server with stands has, and the single-database tool each one follows.
STAND_ONLY_TOOLS = {"list_schemas": "status", "compare_schema": "describe_table"}
# A call over several stands asks them one after another and must answer within the client's
# timeout (install.py registers the entry with "timeout": 30000): another stand is started only
# while the call has time for that stand's connect timeout, and the rows are shared among the stands.
CALL_TIME_BUDGET_S = 25
DETAILED_COMPARE_STANDS = 3  # against more stands than this, compare_schema gives counts, not lines
ROWS_PER_STAND_MIN = 10
CATALOG_ATTEMPTS = 2  # logins tried per database for the catalog before giving up on it
SCHEMAS_PER_DATABASE = 100  # schemas list_schemas names for a database read as a whole; the rest are counted
MAX_REGISTRY_WARNINGS = 10  # more would crowd status; the rest are counted
STANDS_DESCRIBED = 12  # entries of the stand list in a tool's description; status names every stand


def _stand_names(names: Sequence[str]) -> tuple[str, ...]:
    """A profile's tools with the stand-only ones in place."""
    result: list[str] = []
    for name in names:
        result.append(name)
        result.extend(tool for tool, after in STAND_ONLY_TOOLS.items() if after == name)
    return tuple(result)


STAND_PROFILES: dict[str, tuple[str, ...]] = {profile: _stand_names(names) for profile, names in PROFILES.items()}

Arguments = Mapping[str, Any]
Down = dict[str, str]  # host:port -> why it did not answer, remembered for the rest of one call
# A catalog query on one database: the schemas to keep to, or None when one login reads the database as a whole.
Catalog = Callable[[Database, Optional[list[str]]], list[str]]


def build_stand_tools(
    pool: Pool,
    install_dir: str,
    *,
    budget: int = DEFAULT_BUDGET,
    profile: str = DEFAULT_PROFILE,
    warnings: Sequence[str] = (),
    query_log: QueryLog | None = None,
    name: str = DEFAULT_NAME,
) -> list[Tool]:
    """The tools of this profile for the stands of the pool's registry."""
    names = STAND_PROFILES[profile]
    analyze = profile == "analyst"
    query_log = query_log or QueryLog(None)
    stand = {"type": "string", "description": t("argument.stand", stands=_stands_named(pool.registry))}
    stands = {"type": "string", "description": t("argument.stands", limit=MAX_STANDS_PER_CALL)}
    database = {"type": "string", "description": t("argument.database")}
    data_schema = {"type": "string", "description": t("argument.schema_data")}
    any_schema = {"type": "string", "description": t("argument.schema_stand")}
    table = {"type": "string", "description": t("argument.table")}
    query = {"type": "string", "description": t("argument.query")}
    explain_properties: dict[str, Any] = {"stand": stand, "schema": data_schema, "database": database, "query": query}
    if analyze:
        explain_properties["analyze"] = {"type": "boolean", "description": t("argument.analyze")}
    tools = [
        Tool(
            name="status",
            title=t("tool.status.title"),
            description=t("tool.status.description") + " " + t("stands.status_note"),
            properties={"stand": {"type": "string", "description": t("argument.stand_status")}},
            handler=lambda args: stand_status(
                pool,
                install_dir,
                budget=budget,
                profile=profile,
                names=names,
                warnings=warnings,
                query_log=query_log,
                name=name,
                stand=args.get("stand"),
            ),
        ),
        Tool(
            name="list_schemas",
            title=t("tool.list_schemas.title"),
            description=t("tool.list_schemas.description"),
            properties={"stand": stand},
            required=("stand",),
            handler=lambda args: list_schemas(pool, args.get("stand")),
        ),
        Tool(
            name="list_tables",
            title=t("tool.list_tables.title"),
            description=t("tool.list_tables.description", limit=LIST_TABLES_LIMIT),
            properties={"stand": stand, "schema": any_schema, "database": database},
            required=("stand",),
            handler=lambda args: stand_list_tables(pool, args),
        ),
        Tool(
            name="search_schema",
            title=t("tool.search_schema.title"),
            description=t("tool.search_schema.description", limit=SEARCH_LIMIT),
            properties={
                "stand": stand,
                "stands": stands,
                "text": {"type": "string", "description": t("argument.text")},
                "schema": any_schema,
                "database": database,
            },
            required=("text",),
            handler=lambda args: stand_search(pool, args),
        ),
        Tool(
            name="describe_table",
            title=t("tool.describe_table.title"),
            description=t("tool.describe_table.description"),
            properties={"stand": stand, "table": table, "schema": any_schema, "database": database},
            required=("stand", "table"),
            handler=lambda args: stand_describe(pool, args),
        ),
        Tool(
            name="compare_schema",
            title=t("tool.compare_schema.title"),
            description=t("tool.compare_schema.description"),
            properties={
                "stand_a": {"type": "string", "description": t("argument.stand_a")},
                "stand_b": {"type": "string", "description": t("argument.stand_b")},
                "stands": {"type": "string", "description": t("argument.stands_compare", limit=MAX_STANDS_PER_CALL)},
                "schema": {"type": "string", "description": t("argument.schema_compare")},
                "database": database,
            },
            required=("stand_a", "schema"),
            handler=lambda args: stand_compare(pool, args),
        ),
        Tool(
            name="schema_diagram",
            title=t("tool.schema_diagram.title"),
            description=t("tool.schema_diagram.description"),
            properties={
                "stand": stand,
                "schema": {"type": "string", "description": t("argument.schema_diagram_stand")},
                "database": database,
                **diagram_properties(),
            },
            required=("stand", "schema"),
            handler=lambda args: stand_diagram(pool, args, budget),
        ),
        Tool(
            name="sample_rows",
            title=t("tool.sample_rows.title"),
            description=t("tool.sample_rows.description", default=SAMPLE_ROWS_DEFAULT, limit=SAMPLE_ROWS_LIMIT),
            properties={
                "stand": stand,
                "table": table,
                "schema": any_schema,
                "database": database,
                "columns": {"type": "string", "description": t("argument.columns")},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": SAMPLE_ROWS_LIMIT,
                    "description": t("argument.limit", limit=SAMPLE_ROWS_LIMIT, default=SAMPLE_ROWS_DEFAULT),
                },
            },
            required=("stand", "table"),
            handler=lambda args: stand_sample(pool, args),
        ),
        Tool(
            name="explain_query",
            title=t("tool.explain_query.title"),
            description=t("tool.explain_query.description_analyze" if analyze else "tool.explain_query.description"),
            properties=explain_properties,
            required=("stand", "schema", "query"),
            handler=lambda args: stand_explain(pool, args, analyze=analyze),
        ),
        Tool(
            name="run_select",
            title=t("tool.run_select.title"),
            description=t("tool.run_select.description", limit=RUN_SELECT_LIMIT),
            properties={"stand": stand, "stands": stands, "schema": data_schema, "database": database, "query": query},
            required=("schema", "query"),
            handler=lambda args: stand_run(pool, args),
        ),
    ]
    offered = [tool for tool in tools if tool.name in names]
    return [
        replace(
            tool,
            handler=_fresh(pool, tool.handler),
            budget=budget,
            query_log=query_log,
            missing_hint=lambda args, missing: _schema_left_out(pool, args, missing),
        )
        for tool in offered
    ]


def _schema_left_out(pool: Pool, args: Arguments, missing: Sequence[str]) -> str | None:
    """A schema left out on a stand: the registry's answer, which names the schemas of that stand.

    A model sends schema: "" and, told only that the argument is missing, spends a call on list_schemas.
    """
    argument = "stand" if args.get("stand") else "stand_a"
    stand = args.get(argument)
    if list(missing) != ["schema"] or not isinstance(stand, str) or not stand.strip():
        return None
    try:
        pool.registry.target(pool.registry.stand(stand, argument), args.get("schema"))
    except StandError as exc:
        return str(exc)
    return None


def _stands_named(registry: Registry) -> str:
    """The stands for the stand argument, so that a model knows them before its first call.

    They are the registry's at start; status and the errors name the stands as they are now.
    """
    entries = stand_ranges(registry.stands())
    if len(entries) <= STANDS_DESCRIBED:
        return ", ".join(entries) or t("error.none")
    return t("stands.more_stands", names=", ".join(entries[:STANDS_DESCRIBED]), count=len(registry.stands()))


def _fresh(pool: Pool, handler: Callable[[dict[str, Any]], Answer]) -> Callable[[dict[str, Any]], Answer]:
    """The handler, run with the pool forgetting the previous call's login: the query log names this call's."""

    def run(args: dict[str, Any]) -> Answer:
        pool.forget()
        pool.refresh()
        return handler(args)

    return run


def stand_status(
    pool: Pool,
    install_dir: str,
    *,
    budget: int,
    profile: str,
    names: Sequence[str],
    warnings: Sequence[str],
    query_log: QueryLog,
    name: str,
    stand: Any,
) -> list[str]:
    registry = pool.registry
    stands = registry.stands()
    lines = [
        t("status.version", version=__version__, python=platform.python_version(), path=install_dir),
        t("status.name", name=name),
        t("status.read_only"),
        t("status.stands", count=len(stands), stands=summarize(stands), connections=len(registry.targets)),
        t("status.registry", files=", ".join(registry.files) or t("error.none")),
        t("status.profile", profile=profile, tools=", ".join(names)),
        t("status.language", language=current()),
        query_log.describe(),
    ]
    lines.extend(t("status.warning", warning=warning) for warning in (*warnings, *registry_warnings(registry)))
    if not isinstance(stand, str) or not stand.strip():
        lines.append(
            t(
                "status.limits_stands",
                tables=LIST_TABLES_LIMIT,
                budget=budget,
                connections=pool.limit,
                many=MAX_STANDS_PER_CALL,
            )
        )
        lines.append(t("status.stand_hint"))
        return lines
    chosen = registry.stand(stand)
    lines.extend(list_schemas(pool, chosen))
    # The logins in the order list_schemas shows them: a login the server turned down is
    # reported and the next one tried; a host that did not answer ends the check.
    logins = [target for _, targets in by_database(registry.of_stand(chosen)) for target in targets]
    for target in logins[:CATALOG_ATTEMPTS]:
        failure, checked = _check(pool, target, budget)
        lines.extend(checked)
        if failure is None or failure.host_down:
            break
    return lines


def _check(pool: Pool, target: Target, budget: int) -> tuple[DatabaseUnavailableError | None, list[str]]:
    """The status lines of connecting with this login, and the failure if it did not connect."""
    lines = [t("status.checked_with", label=target.label)]
    try:
        info = pool.database(target).server_info()
    except DatabaseUnavailableError as exc:
        return exc, [*lines, t("status.unavailable", reason=exc)]
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
    return None, lines


def registry_warnings(registry: Registry) -> list[str]:
    """The registry's problems for status: at most MAX_REGISTRY_WARNINGS of them, the rest counted."""
    shown = registry.warnings[:MAX_REGISTRY_WARNINGS]
    if len(registry.warnings) > MAX_REGISTRY_WARNINGS:
        shown.append(t("stands.more_warnings", count=len(registry.warnings) - MAX_REGISTRY_WARNINGS))
    return shown


def list_schemas(pool: Pool, stand: Any) -> list[str]:
    """The databases of a stand and their schemas: the registry's, or what the one login of a database reads.

    A database with a login per schema is listed from the registry, without connecting. One read as a
    whole is listed from its catalog, and when it cannot be read its error stands in for the names:
    status calls this first and has to answer for an unreachable stand too.
    """
    chosen = pool.registry.stand(stand)
    lines = []
    down: Down = {}
    for database, sections in by_database(pool.registry.of_stand(chosen)):
        if whole_database(sections) is None:
            names = ", ".join(target.schema for target in sections)
        else:
            names = _readable_schemas(pool, sections, down)
        lines.append(f"{chosen} / {database}: {names}")
    return lines


def _readable_schemas(pool: Pool, sections: Sequence[Target], down: Down) -> str:
    """The schemas with tables the one login of a database reads, from its catalog; its error when it cannot."""
    login = sections[0].login

    def run(db: Database, _schemas: list[str] | None) -> list[str]:
        names = [str(row[0]) for row in db.query(ALL_SCHEMAS_SQL)]
        if not names:
            return [t("stands.no_readable_schemas", login=login)]
        if len(names) > SCHEMAS_PER_DATABASE:
            return [t("stands.more_schemas", names=", ".join(names[:SCHEMAS_PER_DATABASE]), count=len(names))]
        return [", ".join(names)]

    try:
        return _catalog(pool, sections, run, down)[0]
    except (DatabaseUnavailableError, QueryFailedError, ToolError) as exc:
        return str(exc)


def stand_list_tables(pool: Pool, args: Arguments) -> list[str]:
    target = _optional_target(pool, args)
    if target is not None:
        return [target.label, *list_tables(pool.database(target), target.schema)]
    stand = pool.registry.stand(args.get("stand"))
    return _each_database(pool, stand, args.get("database"), lambda db, schemas: list_tables(db, None, schemas))


def stand_search(pool: Pool, args: Arguments) -> list[str]:
    text = args.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ToolError(t("error.text"))
    stands = _many_stands(pool, args)
    if stands is not None:
        return _search_stands(pool, stands, args, text)
    target = _optional_target(pool, args)
    if target is not None:
        return [target.label, *search_schema(pool.database(target), text, target.schema)]
    stand = pool.registry.stand(args.get("stand"))
    return _each_database(pool, stand, args.get("database"), lambda db, schemas: search_schema(db, text, None, schemas))


def _search_stands(pool: Pool, stands: Sequence[str], args: Arguments, text: str) -> list[str]:
    """search_schema on each stand in turn: one schema with its login, or every schema from the catalog."""
    limit = _share(SEARCH_LIMIT, len(stands))
    if args.get("schema") in (None, ""):

        def catalog(db: Database, schemas: list[str] | None) -> list[str]:
            return search_schema(db, text, None, schemas, limit)

        return _each_stand(pool, stands, lambda stand: _whole_stand(pool, stand, args.get("database"), catalog))
    _schema_on_some(pool, stands, args)

    def with_login(target: Target) -> list[str]:
        return search_schema(pool.database(target), text, target.schema, limit=limit)

    nothing = t("search.nothing", text=text.strip())
    answers, late = _ask_stands(pool, stands, lambda stand: _with_login(pool, stand, args, with_login))
    return _folded(pool, stands, answers, late, empty=lambda lines: lines[1:] == [nothing], key="stands.no_matches")


def stand_describe(pool: Pool, args: Arguments) -> list[str]:
    target, table = _table_target(pool, args)
    return [target.label, *describe_table(pool.database(target), table, target.schema)]


def stand_compare(pool: Pool, args: Arguments) -> list[str]:
    """One schema on two stands, or on stand_a against several; database picks the login the same way on all."""
    registry = pool.registry
    first = registry.stand(args.get("stand_a"), "stand_a")
    schema, database = args.get("schema"), args.get("database")
    many, second_name = args.get("stands"), args.get("stand_b")
    if many not in (None, ""):
        if second_name not in (None, ""):
            raise StandError(t("compare.b_or_stands"))
        others = [stand for stand in registry.matching(many) if stand != first]
        if not others:
            raise StandError(t("compare.no_others", pattern=many, stand=first))
        return _compare_many(pool, registry.target(first, schema, database), others, args)
    if second_name in (None, ""):
        raise StandError(t("compare.need_b"))
    second = registry.stand(second_name, "stand_b")
    if first == second:
        raise StandError(t("compare.same_stand", stand=first))
    return compare_schema(pool, registry.target(first, schema, database), registry.target(second, schema, database))


def _compare_many(pool: Pool, reference: Target, stands: Sequence[str], args: Arguments) -> list[str]:
    """The reference read once, each stand in turn; detail for a few stands, counts for many."""
    base = read_structure(pool, reference)  # the reference failing fails the call: nothing to compare with
    detailed = len(stands) <= DETAILED_COMPARE_STANDS
    totals = {"same": 0, "differ": 0, "skipped": 0}
    counting = threading.Lock()  # the stands are read in threads, and each adds itself to one total

    def run(stand: str) -> list[str]:
        try:
            target = pool.registry.target(stand, args.get("schema"), args.get("database"))
            structure = read_structure(pool, target)
        except StandError as exc:
            pool.outcomes[stand] = ERROR
            with counting:
                totals["skipped"] += 1
            return [stand, f"  {exc}"]
        except DatabaseUnavailableError as exc:
            pool.outcomes[stand] = UNAVAILABLE
            with counting:
                totals["skipped"] += 1
            return [target.label, f"  {exc}"]
        except (QueryFailedError, ToolError) as exc:
            pool.outcomes[stand] = ERROR
            with counting:
                totals["skipped"] += 1
            return [target.label, f"  {exc}"]
        pool.outcomes[stand] = OK
        with counting:
            return _compared(base, structure, totals, detailed=detailed)

    lines = [t("compare.many_header", a=reference.label, count=len(stands))]
    lines.extend(_each_stand(pool, stands, run))
    totals["skipped"] += sum(1 for stand in stands if pool.outcomes.get(stand) == SKIPPED)
    lines.append(t("compare.totals", a=reference.stand, **totals))
    return lines


def _compared(base: Structure, structure: Structure, totals: dict[str, int], *, detailed: bool) -> list[str]:
    found = comparison(base, structure)
    if found.same:
        totals["same"] += 1
        return [f"{structure.target.label}: {t('compare.like', a=base.target.stand)}"]
    totals["differ"] += 1
    if detailed:
        return [structure.target.label, *found.sections()]
    return [f"{structure.target.label}: {found.summary()}"]


def stand_diagram(pool: Pool, args: Arguments, budget: int) -> list[str]:
    """The diagram of a schema of a stand, read with the schema's login, under its label as a Mermaid comment.

    The label is a comment so that the whole answer renders when the agent pastes it as it is.
    """
    target = _required_target(pool, args)
    label = f"%% {target.label}"
    db = pool.database(target)
    room = budget - len(label) - 1
    lines = schema_diagram(db, target.schema, args.get("table"), args.get("depth"), args.get("columns"), budget=room)
    return [label, *lines]


def stand_sample(pool: Pool, args: Arguments) -> list[str]:
    target, table = _table_target(pool, args)
    rows = sample_rows(pool.database(target), table, target.schema, args.get("columns"), args.get("limit"))
    return [target.label, *rows]


def stand_explain(pool: Pool, args: Arguments, *, analyze: bool) -> list[str]:
    target = _required_target(pool, args)
    analyzed = args.get("analyze") if analyze else None
    plan = explain_query(pool.database(target), args.get("query"), analyzed, _database_schemas(pool, target))
    return [target.label, *plan]


def stand_run(pool: Pool, args: Arguments) -> list[str]:
    stands = _many_stands(pool, args)
    if stands is None:
        target = _required_target(pool, args)
        lines = run_select(pool.database(target), args.get("query"), schemas=_database_schemas(pool, target))
        return [target.label, *lines, *_elsewhere(pool, target, lines)]
    query = args.get("query")
    check_read_only(query)  # a statement the policy refuses is refused once, before any stand is asked
    _schema_on_some(pool, stands, args)
    limit = _share(RUN_SELECT_LIMIT, len(stands))

    def with_login(target: Target) -> list[str]:
        return run_select(pool.database(target), query, limit, _database_schemas(pool, target))

    no_rows = plural("rows.count", 0)
    answers, late = _ask_stands(pool, stands, lambda stand: _with_login(pool, stand, args, with_login))
    return _folded(pool, stands, answers, late, empty=lambda lines: lines[-1] == no_rows, key="stands.no_rows")


def _elsewhere(pool: Pool, target: Target, lines: Sequence[str]) -> list[str]:
    """For an answer without rows, the other stands with the schema: what one stand lacks, another may hold.

    Asked which stand has an order, a model looked on one stand, found nothing, and named stands that do not exist.
    """
    if lines[-1:] != [plural("rows.count", 0)]:
        return []
    registry = pool.registry
    others = [
        stand
        for stand in registry.stands()
        if stand != target.stand and any(other.schema == target.schema for other in registry.of_stand(stand))
    ]
    if not others:
        return []
    return [t("stands.elsewhere", schema=target.schema, stands=summarize(others), limit=MAX_STANDS_PER_CALL)]


def _many_stands(pool: Pool, args: Arguments) -> list[str] | None:
    """The stands of a call over several of them (stands), or None when stand names one.

    A model asked which stand has something sends stand: "*" for every stand; no stand is named so,
    and a pattern or a list in stand goes as stands.
    """
    one, many = args.get("stand"), args.get("stands")
    if many in (None, ""):
        if one in (None, ""):
            raise StandError(t("stands.no_stand_or_stands", stands=summarize(pool.registry.stands())))
        if isinstance(one, str) and names_several(one):
            return pool.registry.matching(one)
        _known_stand(pool, one)
        return None
    if one not in (None, ""):
        raise StandError(t("stands.one_or_many"))
    return pool.registry.matching(many)


def _known_stand(pool: Pool, stand: Any) -> None:
    """A stand of the registry, or its error with the way to look on several stands at once.

    Told only that there is no stand 'pg' (the server's name), a model asked the user which stand to take.
    """
    try:
        pool.registry.stand(stand)
    except StandError as exc:
        every = len(pool.registry.stands()) <= MAX_STANDS_PER_CALL
        hint = t("stands.ask_every") if every else t("stands.ask_several", limit=MAX_STANDS_PER_CALL)
        raise StandError(f"{exc} {hint}") from exc


def _share(limit: int, stands: int) -> int:
    """Rows or matches per stand of a call over several: the limit shared out, at least ROWS_PER_STAND_MIN each."""
    return max(ROWS_PER_STAND_MIN, limit // stands)


def _schema_on_some(pool: Pool, stands: Sequence[str], args: Arguments) -> None:
    """Fail as one stand would when no stand of the call has the schema; stands that lack it get a line each."""
    complaints = [_complaint(pool, stand, args) for stand in stands]
    first = complaints[0]
    if first is not None and all(complaints):
        raise first


def _complaint(pool: Pool, stand: str, args: Arguments) -> StandError | None:
    """What the registry holds against the schema on this stand, if anything."""
    try:
        pool.registry.target(stand, args.get("schema"), args.get("database"))
    except StandError as exc:
        return exc
    return None


def _each_stand(pool: Pool, stands: Sequence[str], run: Callable[[str], list[str]]) -> list[str]:
    """The lines of run on every stand, in the order of stands; the stands that have not answered in time last."""
    answers, late = _ask_stands(pool, stands, run)
    lines = [line for stand in stands if stand in answers for line in answers[stand]]
    return [*lines, *_not_in_time(late)]


def _ask_stands(
    pool: Pool, stands: Sequence[str], run: Callable[[str], list[str]]
) -> tuple[dict[str, list[str]], list[str]]:
    """run on every stand at the same time, one thread each, for as long as the call may take.

    The answers of the stands that made it, and the stands that did not: the client waits only so long
    for the call, so the call does not wait for them. Their threads finish on their own, and the pool
    keeps their connections until then. The time a call takes is that of its slowest stand, not the sum.

    Each thread runs in a copy of the call's context, so that t() speaks the server's language there:
    a thread of its own starts with the default language, English, whatever the server was told.
    """
    answers: dict[str, list[str]] = {}

    def ask(stand: str) -> None:
        try:
            answers[stand] = run(stand)
        except Exception as exc:  # whatever stops a thread is the stand's line, not a lost stand
            pool.outcomes[stand] = ERROR
            answers[stand] = [stand, f"  {exc}"]

    threads = [
        threading.Thread(
            target=contextvars.copy_context().run, args=(ask, stand), name=f"pg-explorer-mcp {stand}", daemon=True
        )
        for stand in stands
    ]
    deadline = time.monotonic() + CALL_TIME_BUDGET_S
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(max(0.0, deadline - time.monotonic()))
    late = [stand for stand in stands if stand not in answers]
    pool.outcomes.update(dict.fromkeys(late, SKIPPED))
    pool.current = None  # the query log names the call's stands and their outcomes, not the last login
    pool.trim()  # the connections the call opened beyond the limit, except those a late stand still uses
    return answers, late


def _not_in_time(late: Sequence[str]) -> list[str]:
    return [t("stands.out_of_time", stands=summarize(late), budget=CALL_TIME_BUDGET_S)] if late else []


def _folded(
    pool: Pool,
    stands: Sequence[str],
    answers: dict[str, list[str]],
    late: Sequence[str],
    *,
    empty: Callable[[list[str]], bool],
    key: str,
) -> list[str]:
    """The answers in order, with the stands that answered but found nothing folded into one line.

    Asked which of fifty stands has a row, a model reads one stand with rows and one line naming the rest.
    """
    folded = [
        stand
        for stand in stands
        if len(stands) > 1 and pool.outcomes.get(stand) == OK and stand in answers and empty(answers[stand])
    ]
    lines = [line for stand in stands if stand in answers and stand not in folded for line in answers[stand]]
    if folded:
        lines.append(t(key, stands=summarize(folded)))
    return [*lines, *_not_in_time(late)]


def _with_login(pool: Pool, stand: str, args: Arguments, run: Callable[[Target], list[str]]) -> list[str]:
    """run with the schema's login on the stand, under its label; whatever stops it is one line under the label."""
    try:
        target = pool.registry.target(stand, args.get("schema"), args.get("database"))
    except StandError as exc:
        pool.outcomes[stand] = ERROR
        return [stand, f"  {exc}"]
    try:
        answer = run(target)
    except DatabaseUnavailableError as exc:
        pool.outcomes[stand] = UNAVAILABLE
        return [target.label, f"  {exc}"]
    except (QueryFailedError, ToolError) as exc:
        pool.outcomes[stand] = ERROR
        return [target.label, f"  {exc}"]
    pool.outcomes[stand] = OK
    return [target.label, *answer]


def _whole_stand(pool: Pool, stand: str, database: Any, run: Catalog) -> list[str]:
    """The catalog of the stand's databases, as for one stand; the outcome is the best any of its databases had."""
    seen: list[str] = []

    def noted(db: Database, schemas: list[str] | None) -> list[str]:
        try:
            answer = run(db, schemas)
        except DatabaseUnavailableError:
            seen.append(UNAVAILABLE)
            raise
        except (QueryFailedError, ToolError):
            seen.append(ERROR)
            raise
        seen.append(OK)
        return answer

    try:
        lines = _each_database(pool, stand, database, noted)
    except StandError as exc:
        pool.outcomes[stand] = ERROR
        return [stand, f"  {exc}"]
    pool.outcomes[stand] = next((outcome for outcome in (OK, ERROR, UNAVAILABLE) if outcome in seen), UNAVAILABLE)
    return lines


def _optional_target(pool: Pool, args: Arguments) -> Target | None:
    """The login of the schema the arguments name, or None for the whole stand."""
    stand = pool.registry.stand(args.get("stand"))
    if args.get("schema") in (None, ""):
        return None
    return pool.registry.target(stand, args.get("schema"), args.get("database"))


def _database_schemas(pool: Pool, target: Target) -> list[str] | None:
    """The registry's schemas of the target's database on its stand, where a missing table may be.

    None for a database read as a whole: there the hint names whatever schema the login reads it in.
    """
    sections = [other for other in pool.registry.of_stand(target.stand) if other.database == target.database]
    return _scope(sections)[1]


def _required_target(pool: Pool, args: Arguments) -> Target:
    """The login of the schema the arguments name; reading data always needs one."""
    stand = pool.registry.stand(args.get("stand"))
    return pool.registry.target(stand, args.get("schema"), args.get("database"))


def _databases(pool: Pool, stand: str, database: Any) -> list[tuple[str, list[Target]]]:
    groups = by_database(pool.registry.of_stand(stand))
    if isinstance(database, str) and database.strip():
        groups = [group for group in groups if group[0].lower() == database.strip().lower()]
        if not groups:
            known = ", ".join(name for name, _ in by_database(pool.registry.of_stand(stand)))
            raise StandError(t("stands.unknown_database", stand=stand, database=database, databases=known))
    return groups


def _scope(sections: Sequence[Target]) -> tuple[list[Target], list[str] | None]:
    """The logins the catalog of a database may be read through, and the schemas to keep to: None for one login.

    A database read as a whole has one login, and what it reads is the answer. One with a login per
    schema keeps to the registry's schemas, through any of its logins: the catalog is the same under each.
    """
    section = whole_database(sections)
    if section is not None:
        return [section], None
    logins: dict[str, Target] = {}
    for target in sections:
        logins.setdefault(target.login, target)
    return list(logins.values()), [target.schema for target in sections]


def _catalog(pool: Pool, sections: Sequence[Target], run: Catalog, down: Down) -> list[str]:
    """Run a catalog query through the first login of the database that connects.

    A login the server turned down is followed by the next one. A host that did not
    answer goes into down for the rest of the call, and nothing else on it is tried: a
    stand behind a dropped VPN costs one timeout, not one per login and database.
    """
    logins, schemas = _scope(sections)
    attempts = [target for target in logins if target.address not in down][:CATALOG_ATTEMPTS]
    if not attempts:
        raise DatabaseUnavailableError(down[logins[0].address], host_down=True)
    for target in attempts[:-1]:
        answer = _attempt(pool, target, schemas, run, down, last=False)
        if answer is not None:
            return answer
    return _attempt(pool, attempts[-1], schemas, run, down, last=True) or []  # the last one raises, never None


def _attempt(
    pool: Pool,
    target: Target,
    schemas: list[str] | None,
    run: Catalog,
    down: Down,
    *,
    last: bool,
) -> list[str] | None:
    try:
        return run(pool.database(target), schemas)
    except DatabaseUnavailableError as exc:
        if exc.host_down:
            down[target.address] = str(exc)
        if exc.host_down or last:
            raise
        return None  # the server turned this login down: the next one may do


def _each_database(pool: Pool, stand: str, database: Any, run: Catalog) -> list[str]:
    lines: list[str] = []
    down: Down = {}
    for name, sections in _databases(pool, stand, database):
        lines.append(f"{stand} / {name}")
        try:
            lines.extend(_catalog(pool, sections, run, down))
        except (DatabaseUnavailableError, QueryFailedError, ToolError) as exc:
            lines.append(f"  {exc}")  # one database failing does not hide the others
    return lines


def _table_target(pool: Pool, args: Arguments) -> tuple[Target, str]:
    """The login of the schema the table lives in, and the table's bare name."""
    stand = pool.registry.stand(args.get("stand"))
    table, schema = split_name(args.get("table"), args.get("schema") or None)
    if schema:
        return pool.registry.target(stand, schema, args.get("database")), table
    found: list[tuple[str, str]] = []
    failure: DatabaseUnavailableError | None = None
    down: Down = {}
    for name, targets in _databases(pool, stand, args.get("database")):
        try:
            rows = _catalog(pool, targets, lambda db, schemas: _find(db, table, schemas), down)
        except DatabaseUnavailableError as exc:
            failure = exc
            continue
        found.extend((name, schema_name) for schema_name in rows)
    if len(found) == 1:
        name, schema_name = found[0]
        return pool.registry.target(stand, schema_name, name), table
    if found:
        options = ", ".join(f"{name}.{schema_name}.{table}" for name, schema_name in found)
        raise StandError(t("stands.table_in_several", name=table, stand=stand, options=options))
    if failure is not None:
        raise failure
    raise StandError(t("stands.table_not_found", name=table, stand=stand))


def _find(db: Database, table: str, schemas: list[str] | None) -> list[str]:
    """Schemas among these, or any the login reads, that hold the table; unquoted names fold to lower case."""
    rows = relations_named(db, table, schemas)
    if not rows and table != table.lower():
        rows = relations_named(db, table.lower(), schemas)
    return [str(row[1]) for row in rows]
