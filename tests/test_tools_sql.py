"""The catalog queries: what makes a relation readable in each variant.

The single-database queries keep to the role's privileges; the registry variants (the
IN_SCHEMAS ones) keep to the schemas of the registry instead, since on a stand the catalog is
read through any login of the database, whose privileges say nothing about the schema asked for.
"""

from __future__ import annotations

from pg_explorer_mcp import tools

PRIVILEGE = "has_table_privilege(c.oid, 'SELECT')"
REGISTRY_SCHEMAS = "n.nspname = ANY (CAST(:schemas AS text[]))"

PAIRS = (
    ("LIST_TABLES_SQL", "LIST_TABLES_IN_SCHEMAS_SQL"),
    ("FIND_RELATION_SQL", "FIND_RELATION_IN_SCHEMAS_SQL"),
    ("SEARCH_SQL", "SEARCH_IN_SCHEMAS_SQL"),
)


def test_the_single_database_queries_read_what_the_role_may() -> None:
    for sql in (tools.LIST_TABLES_SQL, tools.FIND_RELATION_SQL, tools.SEARCH_SQL, tools.ALL_SCHEMAS_SQL):
        assert PRIVILEGE in sql


def test_the_registry_variants_read_only_the_registry_schemas() -> None:
    for _base, registry_variant in PAIRS:
        assert REGISTRY_SCHEMAS in getattr(tools, registry_variant)
        assert PRIVILEGE not in getattr(tools, registry_variant)


def test_the_registry_variant_differs_only_in_what_makes_a_relation_readable() -> None:
    for base, registry_variant in PAIRS:
        assert getattr(tools, registry_variant) == getattr(tools, base).replace(PRIVILEGE, REGISTRY_SCHEMAS)
