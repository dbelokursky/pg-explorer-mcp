"""One schema on two stands: how it looks on each, and the lines that say where they differ.

The structure comes from the system catalog through the schema's own login on each
stand, in three queries per stand however many tables it has: relations with their
columns, then constraints, then indexes. The answer names what exists on one stand
only and what differs; the data is never read (ADR 0006).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from .budget import shorten
from .db import Database, DatabaseUnavailableError, QueryFailedError
from .i18n import plural, t
from .stands import Pool, Target
from .tools import ToolError

# The schema comes first, so the rows tell a schema the database lacks (no rows at all) from an
# empty one (a row without a relation) and a relation without columns (a row without a column).
# The kinds are RELATION_KINDS of tools.py.
RELATIONS_SQL = """
SELECT c.relname, c.relkind, a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull,
       pg_get_expr(d.adbin, d.adrelid)
FROM pg_catalog.pg_namespace n
LEFT JOIN pg_catalog.pg_class c ON c.relnamespace = n.oid AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
LEFT JOIN pg_catalog.pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
LEFT JOIN pg_catalog.pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
WHERE n.nspname = CAST(:schema AS text)
ORDER BY c.relname COLLATE "C", a.attnum
"""

# NOT NULL constraints (contype 'n', PostgreSQL 18+) are left out: the columns carry them.
SCHEMA_CONSTRAINTS_SQL = """
SELECT c.relname, c.relkind, con.conname, pg_get_constraintdef(con.oid, true)
FROM pg_catalog.pg_constraint con
JOIN pg_catalog.pg_class c ON c.oid = con.conrelid
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = CAST(:schema AS text)
  AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
  AND con.contype IN ('p', 'u', 'f', 'c', 'x')
"""

SCHEMA_INDEXES_SQL = """
SELECT c.relname, c.relkind, i.relname, pg_get_indexdef(ix.indexrelid)
FROM pg_catalog.pg_index ix
JOIN pg_catalog.pg_class i ON i.oid = ix.indexrelid
JOIN pg_catalog.pg_class c ON c.oid = ix.indrelid
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = CAST(:schema AS text)
  AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
"""

Members = dict[str, str]  # name -> definition, as the catalog states it


@dataclass
class Relation:
    """A table or view of the schema: its kind and what the catalog says about its parts."""

    kind: str
    columns: Members = field(default_factory=dict)
    constraints: Members = field(default_factory=dict)
    indexes: Members = field(default_factory=dict)


@dataclass(frozen=True)
class Structure:
    """What the schema looks like on one stand."""

    target: Target
    relations: dict[str, Relation]


def compare_schema(pool: Pool, a: Target, b: Target) -> list[str]:
    """How the schema differs between two stands, read on each with the schema's login there."""
    return differences(read_structure(pool, a), read_structure(pool, b))


def read_structure(pool: Pool, target: Target) -> Structure:
    """The schema's relations on one stand; a failure names the stand, since one call reads two."""
    try:
        relations = _relations(pool.database(target), target.schema)
    except DatabaseUnavailableError as exc:
        raise DatabaseUnavailableError(f"{target.label}: {exc}", host_down=exc.host_down) from exc
    except QueryFailedError as exc:
        raise QueryFailedError(f"{target.label}: {exc}") from exc
    if relations is None:
        raise ToolError(t("compare.no_schema", stand=target.stand, schema=target.schema, database=target.database))
    return Structure(target, relations)


def _relations(db: Database, schema: str) -> dict[str, Relation] | None:
    """Relations with their columns, constraints and indexes; None when the database has no such schema."""
    rows = db.query(RELATIONS_SQL, schema=schema)
    if not rows:
        return None
    relations: dict[str, Relation] = {}
    for name, kind, column, data_type, not_null, default in rows:
        if name is None:
            continue  # the schema exists and holds nothing
        relation = relations.setdefault(name, Relation(kind))
        if column is not None:
            relation.columns[column] = _column(data_type, not_null=not_null, default=default)
    for name, kind, constraint, definition in db.query(SCHEMA_CONSTRAINTS_SQL, schema=schema):
        relations.setdefault(name, Relation(kind)).constraints[constraint] = definition
    for name, kind, index, definition in db.query(SCHEMA_INDEXES_SQL, schema=schema):
        relations.setdefault(name, Relation(kind)).indexes[index] = definition
    return relations


def _column(data_type: str, *, not_null: bool, default: str | None) -> str:
    """Type, NOT NULL and default, as describe_table shows them after the name."""
    definition = data_type
    if not_null:
        definition += " NOT NULL"
    if default:
        definition += f" DEFAULT {default}"
    return definition


def differences(a: Structure, b: Structure) -> list[str]:
    """The header, what is on one stand only and what differs; a single line when nothing does.

    Names are sorted by code point, as COLLATE "C" sorts them, so the answer does not depend
    on either server's locale. Definitions are compared whole and shortened only for display.
    """
    first, second = a.target.stand, b.target.stand
    lines = [t("compare.header", a=a.target.label, b=b.target.label)]
    only_a = sorted(set(a.relations) - set(b.relations))
    only_b = sorted(set(b.relations) - set(a.relations))
    differing: list[str] = []
    for name in sorted(set(a.relations) & set(b.relations)):
        differing.extend(_relation_differences(name, a.relations[name], b.relations[name], first, second))
    if not (only_a or only_b or differing):
        columns = sum(len(relation.columns) for relation in a.relations.values())
        tables = plural("compare.tables", len(a.relations))
        return [*lines, t("compare.same", tables=tables, columns=plural("compare.columns", columns))]
    sections = (
        (t("compare.only_on", stand=first), [_relation_line(name, a.relations[name]) for name in only_a]),
        (t("compare.only_on", stand=second), [_relation_line(name, b.relations[name]) for name in only_b]),
        (t("compare.differ"), differing),
    )
    for title, entries in sections:
        if entries:
            lines.append(title)
            lines.extend(f"  {entry}" for entry in entries)
    return lines


def _relation_line(name: str, relation: Relation) -> str:
    return f"{name} | {_kind(relation.kind)}"


def _kind(kind: str) -> str:
    return t(f"kind.{kind}")


def _relation_differences(name: str, left: Relation, right: Relation, a: str, b: str) -> list[str]:
    """A relation both stands have: its kind, then its columns, constraints and indexes."""
    lines: list[str] = []
    if left.kind != right.kind:
        lines.append(_differs(name, a, _kind(left.kind), b, _kind(right.kind)))
    lines.extend(_member_differences(left.columns, right.columns, lambda column: f"{name}.{column}", a, b))
    lines.extend(
        _member_differences(
            left.constraints, right.constraints, lambda c: t("compare.constraint", table=name, name=c), a, b
        )
    )
    lines.extend(
        _member_differences(left.indexes, right.indexes, lambda i: t("compare.index", table=name, name=i), a, b)
    )
    return lines


def _member_differences(left: Members, right: Members, what: Callable[[str], str], a: str, b: str) -> list[str]:
    """The columns, constraints or indexes of one relation, in name order: on one stand only, or different."""
    lines: list[str] = []
    for member in sorted(set(left) | set(right)):
        if member not in right:
            lines.append(t("compare.only_here", what=what(member), stand=a, definition=shorten(left[member])))
        elif member not in left:
            lines.append(t("compare.only_here", what=what(member), stand=b, definition=shorten(right[member])))
        elif left[member] != right[member]:
            lines.append(_differs(what(member), a, shorten(left[member]), b, shorten(right[member])))
    return lines


def _differs(what: str, a: str, left: str, b: str, right: str) -> str:
    return f"{what}: {a} {left} | {b} {right}"
