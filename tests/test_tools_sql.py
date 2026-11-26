"""The catalog queries: what makes a relation readable in each variant.

The single-database queries keep to the role's privileges; the registry variants (the
IN_SCHEMAS ones) keep to the schemas of the registry instead, since on a stand the catalog is
read through any login of the database, whose privileges say nothing about the schema asked for.
"""

from __future__ import annotations

from pg_explorer_mcp import tools

PRIVILEGE = "has_table_privilege(c.oid, 'SELECT')"
USAGE = "has_schema_privilege(n.oid, 'USAGE')"
REGISTRY_SCHEMAS = "n.nspname = ANY (CAST(:schemas AS text[]))"

PAIRS = (
    ("LIST_TABLES_SQL", "LIST_TABLES_IN_SCHEMAS_SQL"),
    ("FIND_RELATION_SQL", "FIND_RELATION_IN_SCHEMAS_SQL"),
    ("SEARCH_SQL", "SEARCH_IN_SCHEMAS_SQL"),
)


def test_the_single_database_queries_read_what_the_role_may() -> None:
    """SELECT on a table is worth nothing without USAGE on its schema: the query for the table ends with 42501."""
    for name in ("LIST_TABLES_SQL", "FIND_RELATION_SQL", "SEARCH_SQL", "ALL_SCHEMAS_SQL"):
        sql = getattr(tools, name)
        assert PRIVILEGE in sql, name
        assert USAGE in sql, name
    # The others name the privilege their own way: a relation found by a statement's name, the tables that
    # refer to one, names similar to one.
    assert f"{PRIVILEGE} AND {USAGE}" in tools.RELATION_BY_NAME_SQL
    assert f"has_table_privilege(con.conrelid, 'SELECT') AND {USAGE}" in tools.REFERENCED_BY_SQL
    assert f"{PRIVILEGE} AND {USAGE}" in tools.SIMILAR_RELATIONS_SQL


def test_the_registry_variants_read_only_the_registry_schemas() -> None:
    for _base, registry_variant in PAIRS:
        assert REGISTRY_SCHEMAS in getattr(tools, registry_variant)
        assert PRIVILEGE not in getattr(tools, registry_variant)
        assert USAGE not in getattr(tools, registry_variant)


def test_the_registry_variant_differs_only_in_what_makes_a_relation_readable() -> None:
    for base, registry_variant in PAIRS:
        assert getattr(tools, registry_variant) == getattr(tools, base).replace(tools.READABLE, REGISTRY_SCHEMAS)
