"""An ER diagram of tables and their foreign keys, as Mermaid text.

The server cannot draw, but every place an agent writes to shows Mermaid: GitHub and
GitLab, Obsidian, VS Code, the chat of the agent. So schema_diagram answers with the source of an
erDiagram, which the agent puts into a ```mermaid block or a file, and the reader sees
the picture. The catalog is read in a few queries however many tables there are; the
data is not read.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from .budget import shorten
from .db import Database

TABLE_KINDS = ("r", "p", "f")  # tables, partitioned and foreign tables: views have no keys to draw
DEPTH_DEFAULT = 1
DEPTH_LIMIT = 3
# How much of the columns the diagram shows, from the most to the least: what the answer budget allows.
ALL, KEYS, NONE = "all", "keys", "none"
DETAILS = (ALL, KEYS, NONE)
COMMENT_LIMIT = 60  # characters of a column comment inside an attribute line

# The tables of a schema the role can read.
SCHEMA_TABLES_SQL = """
SELECT c.oid, n.nspname, c.relname
FROM pg_catalog.pg_class c
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = CAST(:schema AS text)
  AND c.relkind IN ('r', 'p', 'f')
  AND has_table_privilege(c.oid, 'SELECT')
ORDER BY c.relname COLLATE "C"
"""

# The names of tables a foreign key led to.
TABLES_BY_OID_SQL = """
SELECT c.oid, n.nspname, c.relname
FROM pg_catalog.pg_class c
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.oid = ANY (CAST(:oids AS oid[]))
ORDER BY n.nspname COLLATE "C", c.relname COLLATE "C"
"""

# The foreign keys that start or end in these tables: both ends, the columns of each end in the
# key's order, and whether every referencing column is NOT NULL, so that every row has its counterpart.
FOREIGN_KEYS_SQL = """
SELECT con.conrelid, con.confrelid,
       ARRAY(SELECT a.attname::text FROM unnest(con.conkey) WITH ORDINALITY AS k(attnum, n)
             JOIN pg_catalog.pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.attnum ORDER BY k.n),
       ARRAY(SELECT a.attname::text FROM unnest(con.confkey) WITH ORDINALITY AS k(attnum, n)
             JOIN pg_catalog.pg_attribute a ON a.attrelid = con.confrelid AND a.attnum = k.attnum ORDER BY k.n),
       (SELECT bool_and(a.attnotnull) FROM unnest(con.conkey) AS k(attnum)
             JOIN pg_catalog.pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.attnum)
FROM pg_catalog.pg_constraint con
WHERE con.contype = 'f'
  AND (con.conrelid = ANY (CAST(:oids AS oid[])) OR con.confrelid = ANY (CAST(:oids AS oid[])))
ORDER BY con.conrelid, con.conname COLLATE "C"
"""

TABLE_COLUMNS_SQL = """
SELECT a.attrelid, a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull, col_description(a.attrelid, a.attnum)
FROM pg_catalog.pg_attribute a
WHERE a.attrelid = ANY (CAST(:oids AS oid[])) AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attrelid, a.attnum
"""

# Primary and unique keys: the columns that identify a row, and the foreign keys that are one-to-one.
KEYS_SQL = """
SELECT con.conrelid, con.contype,
       ARRAY(SELECT a.attname::text FROM unnest(con.conkey) WITH ORDINALITY AS k(attnum, n)
             JOIN pg_catalog.pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.attnum ORDER BY k.n)
FROM pg_catalog.pg_constraint con
WHERE con.conrelid = ANY (CAST(:oids AS oid[])) AND con.contype IN ('p', 'u')
ORDER BY con.conrelid, con.contype, con.conname COLLATE "C"
"""

# What Mermaid takes without quotes as an entity or a relationship label; anything else goes in quotes.
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# An attribute name cannot be quoted: letters of any alphabet, digits, _ and - stay, the rest becomes _.
_ATTRIBUTE_UNSAFE = re.compile(r"[^\w-]")
# Types with spaces, as format_type writes them, by their SQL aliases.
_TIME_ZONE = re.compile(r"\b(timestamp|time)(\(\d+\))? (with|without) time zone")
_TYPE_ALIASES = (("character varying", "varchar"), ("double precision", "float8"), ("bit varying", "varbit"))
_TYPE_SCHEMA = re.compile(r"^[\w$]+\.")
_MODIFIER = re.compile(r"\(([^)]*)\)")
_TYPE_UNSAFE = re.compile(r"[^A-Za-z0-9_\-\[\]()]")


@dataclass
class Column:
    name: str
    data_type: str
    not_null: bool
    comment: str | None = None


@dataclass
class Table:
    oid: int
    schema: str
    name: str
    columns: list[Column] = field(default_factory=list)
    primary: tuple[str, ...] = ()
    unique: list[tuple[str, ...]] = field(default_factory=list)

    @property
    def qualified(self) -> str:
        return f"{self.schema}.{self.name}"

    def identifies(self, columns: Sequence[str]) -> bool:
        """Whether these columns are the primary key or a unique key: a foreign key on them is one-to-one."""
        wanted = frozenset(columns)
        return any(frozenset(key) == wanted for key in (self.primary, *self.unique) if key)


@dataclass(frozen=True)
class ForeignKey:
    source: int  # the table with the key
    target: int  # the table it refers to
    columns: tuple[str, ...]
    target_columns: tuple[str, ...]
    not_null: bool  # every column of the key is NOT NULL: each row has its counterpart


@dataclass
class Model:
    """The tables to draw and the foreign keys among them; the tables of `schema` go by their bare names."""

    schema: str
    tables: dict[int, Table]
    foreign_keys: list[ForeignKey]


def of_schema(db: Database, schema: str) -> Model:
    """The readable tables of a schema with the keys among them and to the tables they refer to elsewhere.

    What refers to the schema from other schemas stays out: a table every schema refers to would
    bring the whole database in.
    """
    tables = {oid: Table(oid, nspname, relname) for oid, nspname, relname in db.query(SCHEMA_TABLES_SQL, schema=schema)}
    if not tables:
        return Model(schema, {}, [])
    keys = [key for key in _foreign_keys(db, list(tables)) if key.source in tables]
    _name_tables(db, tables, sorted({key.target for key in keys} - set(tables)))
    _fill(db, tables)
    return Model(schema, tables, keys)


def around(db: Database, oid: int, schema: str, name: str, depth: int) -> Model:
    """One table with the tables within `depth` foreign keys of it, in either direction, and the keys among them."""
    tables = {oid: Table(oid, schema, name)}
    frontier = [oid]
    for _ in range(depth):
        ends = {end for key in _foreign_keys(db, frontier) for end in (key.source, key.target)}
        frontier = sorted(ends - set(tables))
        if not frontier:
            break
        _name_tables(db, tables, frontier)
    keys = [key for key in _foreign_keys(db, list(tables)) if key.source in tables and key.target in tables]
    _fill(db, tables)
    return Model(schema, tables, keys)


def _foreign_keys(db: Database, oids: Sequence[int]) -> list[ForeignKey]:
    rows = db.query(FOREIGN_KEYS_SQL, oids=list(oids))
    return [
        ForeignKey(source, target, tuple(columns), tuple(targets), bool(not_null))
        for source, target, columns, targets, not_null in rows
    ]


def _name_tables(db: Database, tables: dict[int, Table], oids: Sequence[int]) -> None:
    if oids:
        for oid, schema, name in db.query(TABLES_BY_OID_SQL, oids=list(oids)):
            tables[oid] = Table(oid, schema, name)


def _fill(db: Database, tables: dict[int, Table]) -> None:
    """The columns and the keys of every table, in two queries."""
    oids = list(tables)
    for oid, name, data_type, not_null, comment in db.query(TABLE_COLUMNS_SQL, oids=oids):
        tables[oid].columns.append(Column(name, data_type, bool(not_null), comment))
    for oid, kind, columns in db.query(KEYS_SQL, oids=oids):
        if kind == "p":
            tables[oid].primary = tuple(columns)
        else:
            tables[oid].unique.append(tuple(columns))


def render(model: Model, detail: str = KEYS, notes: Sequence[str] = ()) -> list[str]:
    """The erDiagram: the notes as comments, an entity per table, a relationship per foreign key.

    detail says how much of the columns to show: every column with its comment, the key
    columns only, or none. Without attributes a table is named only where no relationship names it.
    """
    lines = ["erDiagram", *(f"  %% {note}" for note in notes)]
    related = {end for key in model.foreign_keys for end in (key.source, key.target)}
    foreign = {column for key in model.foreign_keys for column in ((key.source, name) for name in key.columns)}
    for table in _ordered(model):
        attributes = [] if detail == NONE else _attributes(table, foreign, detail)
        if attributes:
            lines.append(f"  {_entity(model, table)} {{")
            lines.extend(f"    {attribute}" for attribute in attributes)
            lines.append("  }")
        elif detail != NONE or table.oid not in related:
            lines.append(f"  {_entity(model, table)}")
    for key in sorted(model.foreign_keys, key=lambda key: (_entity(model, model.tables[key.source]), key.columns)):
        source, target = model.tables[key.source], model.tables[key.target]
        left = "|o" if source.identifies(key.columns) else "}o"
        right = "||" if key.not_null else "o|"
        label = _name(", ".join(key.columns))
        lines.append(f"  {_entity(model, source)} {left}--{right} {_entity(model, target)} : {label}")
    return lines


def _ordered(model: Model) -> list[Table]:
    """The tables of the schema drawn first, by name; those of other schemas after them."""
    return sorted(model.tables.values(), key=lambda table: (table.schema != model.schema, table.schema, table.name))


def _entity(model: Model, table: Table) -> str:
    return _name(table.name if table.schema == model.schema else table.qualified)


def _name(text: str) -> str:
    """A name as Mermaid reads it: bare when it is a plain word, else in double quotes."""
    return text if _WORD.fullmatch(text) else '"' + text.replace('"', "'") + '"'


def _attributes(table: Table, foreign: set[tuple[int, str]], detail: str) -> list[str]:
    """A line per column: type, name, PK / FK / UK, and with every column its comment too."""
    unique = {column for key in table.unique for column in key}
    attributes = []
    for column in table.columns:
        markers = []
        if column.name in table.primary:
            markers.append("PK")
        if (table.oid, column.name) in foreign:
            markers.append("FK")
        if column.name in unique and column.name not in table.primary:
            markers.append("UK")
        if detail == KEYS and not markers:
            continue
        line = f"{mermaid_type(column.data_type)} {_attribute_name(column.name)}"
        if markers:
            line += " " + ", ".join(markers)
        if detail == ALL and column.comment:
            line += ' "' + shorten(column.comment, COMMENT_LIMIT).replace('"', "'") + '"'
        attributes.append(line)
    return attributes


def _attribute_name(name: str) -> str:
    """A column name as Mermaid takes an attribute: it cannot start with a digit or hold punctuation."""
    text = _ATTRIBUTE_UNSAFE.sub("_", name)
    return text if not text[:1].isdigit() else f"_{text}"


def mermaid_type(data_type: str) -> str:
    """A PostgreSQL type as Mermaid takes an attribute type: one word, no comma inside the parentheses.

    Multi-word types go by their SQL aliases (varchar, timestamptz), a modifier with a comma is
    dropped (numeric(10,2) becomes numeric), and the schema of a user-defined type is left out.
    """
    text = _TIME_ZONE.sub(lambda m: m.group(1) + ("tz" if m.group(3) == "with" else "") + (m.group(2) or ""), data_type)
    for long, short in _TYPE_ALIASES:
        text = text.replace(long, short)
    text = _TYPE_SCHEMA.sub("", text.replace('"', ""))
    text = _MODIFIER.sub(lambda m: "" if "," in m.group(1) else m.group(0), text)
    text = _TYPE_UNSAFE.sub("_", text.strip())
    return text if text[:1].isalpha() else f"t_{text}"
