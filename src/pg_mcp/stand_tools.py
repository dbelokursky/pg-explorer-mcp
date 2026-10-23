"""The tools of a server with many stands: the same tools, told which stand and schema to use.

Every tool takes stand. Reading data needs schema too, and the server connects
with that schema's own login (ADR 0006). Structure across a whole stand comes
from the catalog, one query per database, kept to the schemas the registry has
logins for. Every answer starts with where it came from: stand / database / schema.
search_schema and run_select also take stands, several stands for the same request:
they are asked one after another, and the answer has a section per stand.
compare_schema reads one schema on two stands with its login on each.
"""

from __future__ import annotations

import platform
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from itertools import groupby
from typing import Any

from . import __version__
from .budget import DEFAULT_BUDGET
from .compare import Structure, compare_schema, comparison, read_structure
from .db import Database, DatabaseUnavailableError, QueryFailedError
from .i18n import current, t
from .querylog import ERROR, OK, SKIPPED, UNAVAILABLE, QueryLog
from .sqlguard import check_read_only
from .stands import MAX_STANDS_PER_CALL, Pool, Registry, StandError, Target, summarize
from .tools import (
    DEFAULT_NAME,
    DEFAULT_PROFILE,
    FIND_RELATION_IN_SCHEMAS_SQL,
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
    explain_query,
    list_tables,
    run_select,
    sample_rows,
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
MAX_REGISTRY_WARNINGS = 10  # more would crowd status; the rest are counted


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
    stand = {"type": "string", "description": t("argument.stand")}
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
    return [replace(tool, handler=_fresh(pool, tool.handler), budget=budget, query_log=query_log) for tool in offered]


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
    """The databases of a stand and their schemas that the registry has logins for."""
    chosen = pool.registry.stand(stand)
    return [
        f"{chosen} / {database}: {', '.join(target.schema for target in targets)}"
        for database, targets in by_database(pool.registry.of_stand(chosen))
    ]


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

        def catalog(db: Database, schemas: list[str]) -> list[str]:
            return search_schema(db, text, None, schemas, limit)

        return _each_stand(pool, stands, lambda stand: _whole_stand(pool, stand, args.get("database"), catalog))
    _schema_on_some(pool, stands, args)

    def with_login(target: Target) -> list[str]:
        return search_schema(pool.database(target), text, target.schema, limit=limit)

    return _each_stand(pool, stands, lambda stand: _with_login(pool, stand, args, with_login))


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

    def run(stand: str) -> list[str]:
        try:
            target = pool.registry.target(stand, args.get("schema"), args.get("database"))
            structure = read_structure(pool, target)
        except StandError as exc:
            totals["skipped"] += 1
            pool.outcomes[stand] = ERROR
            return [stand, f"  {exc}"]
        except DatabaseUnavailableError as exc:
            totals["skipped"] += 1
            pool.outcomes[stand] = UNAVAILABLE
            return [target.label, f"  {exc}"]
        except (QueryFailedError, ToolError) as exc:
            totals["skipped"] += 1
            pool.outcomes[stand] = ERROR
            return [target.label, f"  {exc}"]
        pool.outcomes[stand] = OK
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
        return [
            target.label,
            *run_select(pool.database(target), args.get("query"), schemas=_database_schemas(pool, target)),
        ]
    query = args.get("query")
    check_read_only(query)  # a statement the policy refuses is refused once, before any stand is asked
    _schema_on_some(pool, stands, args)
    limit = _share(RUN_SELECT_LIMIT, len(stands))

    def with_login(target: Target) -> list[str]:
        return run_select(pool.database(target), query, limit, _database_schemas(pool, target))

    return _each_stand(pool, stands, lambda stand: _with_login(pool, stand, args, with_login))


def _many_stands(pool: Pool, args: Arguments) -> list[str] | None:
    """The stands of a call over several of them (stands), or None when stand names one."""
    one, many = args.get("stand"), args.get("stands")
    if many in (None, ""):
        if one in (None, ""):
            raise StandError(t("stands.no_stand_or_stands", stands=summarize(pool.registry.stands())))
        return None
    if one not in (None, ""):
        raise StandError(t("stands.one_or_many"))
    return pool.registry.matching(many)


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
    """The lines of run on the stands in turn while the call has time for another; the rest are named as not tried."""
    lines: list[str] = []
    started = time.monotonic()
    for index, stand in enumerate(stands):
        if index and time.monotonic() - started + _reserve(pool, stand) > CALL_TIME_BUDGET_S:
            rest = stands[index:]
            lines.append(t("stands.out_of_time", stands=summarize(rest), budget=CALL_TIME_BUDGET_S))
            pool.outcomes.update(dict.fromkeys(rest, SKIPPED))
            break
        lines.extend(run(stand))
    pool.current = None  # the query log names the call's stands and their outcomes, not the last login
    return lines


def _reserve(pool: Pool, stand: str) -> int:
    """Seconds the stand may take: a silent host costs the connect timeout, then a slow statement its own."""
    return max(
        pool.connect_timeout(target) + pool.statement_timeout(target) for target in pool.registry.of_stand(stand)
    )


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


def _whole_stand(pool: Pool, stand: str, database: Any, run: Callable[[Database, list[str]], list[str]]) -> list[str]:
    """The catalog of the stand's databases, as for one stand; the outcome is the best any of its databases had."""
    seen: list[str] = []

    def noted(db: Database, schemas: list[str]) -> list[str]:
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


def by_database(targets: Sequence[Target]) -> list[tuple[str, list[Target]]]:
    ordered = sorted(targets, key=lambda target: (target.database, target.schema))
    return [(database, list(group)) for database, group in groupby(ordered, key=lambda target: target.database)]


def _optional_target(pool: Pool, args: Arguments) -> Target | None:
    """The login of the schema the arguments name, or None for the whole stand."""
    stand = pool.registry.stand(args.get("stand"))
    if args.get("schema") in (None, ""):
        return None
    return pool.registry.target(stand, args.get("schema"), args.get("database"))


def _database_schemas(pool: Pool, target: Target) -> list[str]:
    """The registry's schemas of the target's database on its stand: where a missing table may be."""
    return [other.schema for other in pool.registry.of_stand(target.stand) if other.database == target.database]


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


def _catalog(
    pool: Pool, targets: Sequence[Target], run: Callable[[Database, list[str]], list[str]], down: Down
) -> list[str]:
    """Run a catalog query through the first login of the database that connects.

    A login the server turned down is followed by the next one. A host that did not
    answer goes into down for the rest of the call, and nothing else on it is tried: a
    stand behind a dropped VPN costs one timeout, not one per login and database.
    """
    schemas = [target.schema for target in targets]
    attempts = [target for target in targets if target.address not in down][:CATALOG_ATTEMPTS]
    if not attempts:
        raise DatabaseUnavailableError(down[targets[0].address], host_down=True)
    for target in attempts[:-1]:
        answer = _attempt(pool, target, schemas, run, down, last=False)
        if answer is not None:
            return answer
    return _attempt(pool, attempts[-1], schemas, run, down, last=True) or []  # the last one raises, never None


def _attempt(
    pool: Pool,
    target: Target,
    schemas: list[str],
    run: Callable[[Database, list[str]], list[str]],
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


def _each_database(pool: Pool, stand: str, database: Any, run: Callable[[Database, list[str]], list[str]]) -> list[str]:
    lines: list[str] = []
    down: Down = {}
    for name, targets in _databases(pool, stand, database):
        lines.append(f"{stand} / {name}")
        try:
            lines.extend(_catalog(pool, targets, run, down))
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


def _find(db: Database, table: str, schemas: list[str]) -> list[str]:
    """Schemas among these that hold the table; unquoted names fold to lower case."""
    rows = db.query(FIND_RELATION_IN_SCHEMAS_SQL, name=table, schema=None, schemas=schemas)
    if not rows and table != table.lower():
        rows = db.query(FIND_RELATION_IN_SCHEMAS_SQL, name=table.lower(), schema=None, schemas=schemas)
    return [str(row[1]) for row in rows]
