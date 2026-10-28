"""schema_diagram: the catalog read in a few queries, and the Mermaid drawn from it, without a database."""

from __future__ import annotations

from typing import Any

import pytest

from pg_explorer_mcp.db import Database
from pg_explorer_mcp.diagram import (
    FOREIGN_KEYS_SQL,
    KEYS_SQL,
    SCHEMA_TABLES_SQL,
    TABLE_COLUMNS_SQL,
    TABLES_BY_OID_SQL,
    Column,
    ForeignKey,
    Model,
    Table,
    mermaid_type,
    render,
)
from pg_explorer_mcp.tools import (
    FIND_RELATION_SQL,
    PROFILES,
    SCHEMA_HOLDS_RELATIONS_SQL,
    SCHEMAS_SQL,
    SIMILAR_RELATIONS_SQL,
    Tool,
    ToolError,
    build_tools,
    schema_diagram,
)
from tests.helpers import ROOT

# A shop with its orders, an employee table in another schema the orders refer to, an audit table in a
# third schema that refers to the orders, a table without any key, and a one-to-one extension of orders.
TABLES = {  # oid: schema, name
    1: ("shop", "customers"),
    2: ("shop", "orders"),
    3: ("shop", "order_items"),
    4: ("shop", "products"),
    5: ("hr", "employees"),
    6: ("shop", "notes"),
    7: ("audit", "events"),
    8: ("shop", "order_details"),
}
COLUMNS: dict[int, list[tuple[str, str, bool, str | None]]] = {  # oid: name, type, NOT NULL, comment
    1: [
        ("id", "integer", True, None),
        ("email", "character varying(100)", True, "Почта"),
        ("city", "text", False, None),
    ],
    2: [
        ("id", "integer", True, None),
        ("customer_id", "integer", True, None),
        ("manager_id", "integer", False, None),
        ("total", "numeric(10,2)", True, "Сумма"),
    ],
    3: [
        ("order_id", "integer", True, None),
        ("product_id", "integer", True, None),
        ("quantity", "integer", True, None),
    ],
    4: [("id", "integer", True, None), ("name", "text", True, None)],
    5: [("id", "integer", True, None), ("full_name", "text", True, None)],
    6: [("body", "text", False, None)],
    7: [("id", "bigint", True, None), ("order_id", "integer", True, None)],
    8: [("order_id", "integer", True, None), ("comment", "text", False, None)],
}
KEYS = [  # oid, kind, columns
    (1, "p", ["id"]),
    (1, "u", ["email"]),
    (2, "p", ["id"]),
    (3, "p", ["order_id", "product_id"]),
    (4, "p", ["id"]),
    (5, "p", ["id"]),
    (7, "p", ["id"]),
    (8, "p", ["order_id"]),
]
FOREIGN_KEYS = [  # source, target, columns, target columns, every column NOT NULL
    (2, 1, ["customer_id"], ["id"], True),
    (2, 5, ["manager_id"], ["id"], False),
    (3, 2, ["order_id"], ["id"], True),
    (3, 4, ["product_id"], ["id"], True),
    (7, 2, ["order_id"], ["id"], True),
    (8, 2, ["order_id"], ["id"], True),
]
SHOP = [
    "erDiagram",
    "  %% schema shop: 7 tables, 5 relationships",
    "  customers {",
    "    integer id PK",
    "    varchar(100) email UK",
    "  }",
    "  notes",
    "  order_details {",
    "    integer order_id PK, FK",
    "  }",
    "  order_items {",
    "    integer order_id PK, FK",
    "    integer product_id PK, FK",
    "  }",
    "  orders {",
    "    integer id PK",
    "    integer customer_id FK",
    "    integer manager_id FK",
    "  }",
    "  products {",
    "    integer id PK",
    "  }",
    '  "hr.employees" {',
    "    integer id PK",
    "  }",
    "  order_details |o--|| orders : order_id",
    "  order_items }o--|| orders : order_id",
    "  order_items }o--|| products : product_id",
    "  orders }o--|| customers : customer_id",
    '  orders }o--o| "hr.employees" : manager_id',
]


def catalog(sql: str, **params: Any) -> list[tuple[Any, ...]]:
    """What PostgreSQL would answer each query with, from the tables above."""
    if sql is SCHEMA_TABLES_SQL:
        found = [(oid, schema, name) for oid, (schema, name) in TABLES.items() if schema == params["schema"]]
        return sorted(found, key=lambda row: row[2])
    if sql is TABLES_BY_OID_SQL:
        return [(oid, *TABLES[oid]) for oid in sorted(params["oids"]) if oid in TABLES]
    if sql is FOREIGN_KEYS_SQL:
        oids = set(params["oids"])
        return [key for key in FOREIGN_KEYS if key[0] in oids or key[1] in oids]
    if sql is TABLE_COLUMNS_SQL:
        return [(oid, *column) for oid in sorted(params["oids"]) for column in COLUMNS[oid]]
    if sql is KEYS_SQL:
        oids = set(params["oids"])
        return [key for key in KEYS if key[0] in oids]
    if sql is FIND_RELATION_SQL:  # the lookup of describe_table, which a diagram around a table shares
        wanted = [(oid, schema, name) for oid, (schema, name) in TABLES.items() if name == params["name"]]
        return [(oid, schema, name, "r", 0, None) for oid, schema, name in wanted if params["schema"] in (None, schema)]
    if sql is SCHEMA_HOLDS_RELATIONS_SQL:
        return [(1,)] if any(schema == params["schema"] for schema, _ in TABLES.values()) else []
    if sql is SCHEMAS_SQL:
        return [(schema,) for schema in sorted({schema for schema, _ in TABLES.values()})]
    if sql is SIMILAR_RELATIONS_SQL:
        return []
    raise AssertionError(f"unexpected query: {sql}")


@pytest.fixture
def db(monkeypatch: pytest.MonkeyPatch) -> Database:
    def query(self: Database, sql: str, **params: Any) -> list[tuple[Any, ...]]:  # noqa: ARG001
        return catalog(sql, **params)

    monkeypatch.setattr(Database, "query", query)
    return Database({})


def by_name(tools: list[Tool]) -> dict[str, Tool]:
    return {tool.name: tool for tool in tools}


class TestSchema:
    def test_keys_and_relationships_with_the_tables_of_other_schemas_it_refers_to(self, db: Database) -> None:
        """What refers to the schema from elsewhere (audit.events) stays out; what it refers to (hr) is in."""
        assert schema_diagram(db, "shop", None, None, None) == SHOP

    def test_every_column_with_its_comment(self, db: Database) -> None:
        lines = schema_diagram(db, "shop", None, None, "all")
        assert lines[2:7] == [
            "  customers {",
            "    integer id PK",
            '    varchar(100) email UK "Почта"',
            "    text city",
            "  }",
        ]
        assert '    numeric total "Сумма"' in lines
        assert lines[-5:] == SHOP[-5:]

    def test_tables_and_relationships_only(self, db: Database) -> None:
        """Without attributes a table is named only where no relationship names it."""
        assert schema_diagram(db, "shop", None, None, "none") == [*SHOP[:2], "  notes", *SHOP[-5:]]

    def test_a_schema_that_refers_nowhere_is_its_own_tables(self, db: Database) -> None:
        assert schema_diagram(db, "hr", None, None, None) == [
            "erDiagram",
            "  %% schema hr: 1 table, 0 relationships",
            "  employees {",
            "    integer id PK",
            "  }",
        ]

    def test_what_a_schema_refers_to_elsewhere_is_drawn_with_its_keys_only(self, db: Database) -> None:
        assert schema_diagram(db, '"audit"', None, None, None) == [
            "erDiagram",
            "  %% schema audit: 2 tables, 1 relationship",
            "  events {",
            "    bigint id PK",
            "    integer order_id FK",
            "  }",
            '  "shop.orders" {',
            "    integer id PK",
            "  }",
            '  events }o--|| "shop.orders" : order_id',
        ]


class TestAroundATable:
    def test_the_neighbours_in_either_direction(self, db: Database) -> None:
        lines = schema_diagram(db, None, "orders", None, None)
        assert lines[:2] == [
            "erDiagram",
            "  %% shop.orders and its neighbours within 1 foreign key: 6 tables, 5 relationships",
        ]
        assert [line for line in lines if line.endswith(" {")] == [
            "  customers {",
            "  order_details {",
            "  order_items {",
            "  orders {",
            '  "audit.events" {',
            '  "hr.employees" {',
        ]
        assert lines[-5:] == [
            '  "audit.events" }o--|| orders : order_id',
            "  order_details |o--|| orders : order_id",
            "  order_items }o--|| orders : order_id",
            "  orders }o--|| customers : customer_id",
            '  orders }o--o| "hr.employees" : manager_id',
        ]

    def test_further_neighbours_with_depth(self, db: Database) -> None:
        lines = schema_diagram(db, "shop", "orders", 2, None)
        assert lines[1] == "  %% shop.orders and its neighbours within 2 foreign keys: 7 tables, 6 relationships"
        assert "  order_items }o--|| products : product_id" in lines

    def test_the_table_alone(self, db: Database) -> None:
        assert schema_diagram(db, None, "shop.orders", 0, None) == [
            "erDiagram",
            "  %% shop.orders alone: 1 table, 0 relationships",
            "  orders {",
            "    integer id PK",
            "  }",
        ]

    def test_depth_beyond_the_neighbourhood_changes_nothing(self, db: Database) -> None:
        assert schema_diagram(db, "shop", "notes", 3, "all") == [
            "erDiagram",
            "  %% shop.notes and its neighbours within 3 foreign keys: 1 table, 0 relationships",
            "  notes {",
            "    text body",
            "  }",
        ]

    def test_the_schema_of_the_table_sets_the_bare_names(self, db: Database) -> None:
        lines = schema_diagram(db, "hr", "employees", None, None)
        assert lines[1] == "  %% hr.employees and its neighbours within 1 foreign key: 2 tables, 1 relationship"
        assert lines[-1] == '  "shop.orders" }o--o| employees : manager_id'


class TestBudget:
    def test_columns_go_first_then_keys_then_nothing_fits(self, db: Database) -> None:
        everything = "\n".join(schema_diagram(db, "shop", None, None, "all"))
        keys = schema_diagram(db, "shop", None, None, "all", budget=len(everything) - 1)
        assert keys[1:3] == [
            SHOP[1],
            "  %% columns left out: with them the diagram would not fit the answer; pass table and depth for a part",
        ]
        assert keys[3:] == SHOP[2:]
        bare = schema_diagram(db, "shop", None, None, "all", budget=len("\n".join(keys)) - 1)
        assert bare[2] == "  %% tables and relationships only: even the keys would not fit the answer"
        assert bare[3:] == ["  notes", *SHOP[-5:]]
        with pytest.raises(ToolError) as caught:
            schema_diagram(db, "shop", None, None, "all", budget=len("\n".join(bare)) - 1)
        assert str(caught.value) == (
            f"The diagram does not fit {len(chr(10).join(bare)) - 1} characters even without columns: 7 tables, "
            "5 relationships. Pass table with depth to draw a part of it."
        )


class TestArguments:
    @pytest.mark.parametrize(
        ("arguments", "message"),
        [
            ({}, "Pass schema to draw a schema, or table to draw a table with its neighbours. Schemas with readable"),
            (
                {"schema": "nowhere"},
                "The current role cannot read any table in schema 'nowhere'. Schemas with readable",
            ),
            ({"schema": "shop", "columns": "some"}, 'columns must be "keys", "all" or "none"'),
            ({"schema": "shop", "depth": 5}, "depth must be from 0 to 3, got 5"),
            ({"table": "parcels"}, "No readable table or view named 'parcels'. Call list_tables"),
            ({"schema": "hr", "table": "orders"}, "No readable table or view named 'orders' in schema 'hr'."),
        ],
    )
    def test_wrong_arguments_are_explained(self, db: Database, arguments: dict[str, Any], message: str) -> None:
        tool = by_name(build_tools(db, str(ROOT)))["schema_diagram"]
        with pytest.raises(ToolError) as caught:
            tool.call(arguments)
        assert str(caught.value).startswith(message)

    def test_the_tool_takes_its_arguments_as_a_model_sends_them(self, db: Database) -> None:
        tool = by_name(build_tools(db, str(ROOT), profile="schema"))["schema_diagram"]
        assert tool.call({"schema": "shop", "columns": "ALL", "depth": ""}).splitlines()[4] == (
            '    varchar(100) email UK "Почта"'
        )
        assert (
            tool.call({"table": "orders", "depth": "2", "schema": ""})
            .splitlines()[1]
            .endswith("7 tables, 6 relationships")
        )
        assert tool.describe()["inputSchema"]["properties"]["columns"]["enum"] == ["all", "keys", "none"]

    @pytest.mark.parametrize("profile", list(PROFILES))
    def test_every_profile_draws(self, db: Database, profile: str) -> None:
        assert "schema_diagram" in by_name(build_tools(db, str(ROOT), profile=profile))

    @pytest.mark.usefixtures("russian")
    def test_in_russian(self, db: Database) -> None:
        assert schema_diagram(db, "shop", None, None, None)[1] == "  %% схема shop: 7 таблиц, 5 связей"
        assert schema_diagram(db, None, "orders", 1, None)[1] == (
            "  %% shop.orders и соседи не дальше 1 внешнего ключа: 6 таблиц, 5 связей"
        )
        assert schema_diagram(db, None, "orders", 0, None)[1] == "  %% shop.orders без соседей: 1 таблица, 0 связей"


class TestMermaid:
    """What Mermaid takes: names it cannot read go in quotes or are mended, types become one word."""

    def test_names_mermaid_cannot_read(self) -> None:
        orders = Table(
            1, "shop", "заказы", [Column("номер", "integer", True), Column("1st", "text", False)], ("номер",)
        )
        other = Table(2, "shop", "2nd-table", [Column("my col", "integer", False)])
        both = Table(3, "arch", "items", [Column("a", "integer", True), Column("b", "integer", True)], ("a", "b"))
        keys = [ForeignKey(2, 1, ("my col",), ("номер",), False), ForeignKey(3, 1, ("a", "b"), ("номер", "x"), True)]
        assert render(Model("shop", {1: orders, 2: other, 3: both}, keys), "all") == [
            "erDiagram",
            '  "2nd-table" {',
            "    integer my_col FK",
            "  }",
            '  "заказы" {',
            "    integer номер PK",
            "    text _1st",
            "  }",
            '  "arch.items" {',
            "    integer a PK, FK",
            "    integer b PK, FK",
            "  }",
            '  "2nd-table" }o--o| "заказы" : "my col"',
            '  "arch.items" |o--|| "заказы" : "a, b"',
        ]

    @pytest.mark.parametrize(
        ("data_type", "expected"),
        [
            ("integer", "integer"),
            ("character varying(50)", "varchar(50)"),
            ("character(1)", "character(1)"),
            ("numeric(10,2)", "numeric"),
            ("numeric(10,2)[]", "numeric[]"),
            ("timestamp with time zone", "timestamptz"),
            ("timestamp(3) without time zone", "timestamp(3)"),
            ("time with time zone", "timetz"),
            ("double precision", "float8"),
            ("bit varying(8)", "varbit(8)"),
            ("integer[]", "integer[]"),
            ("shop.order_status", "order_status"),
            ('"MyType"', "MyType"),
            ("interval day to second", "interval_day_to_second"),
            ("123weird", "t_123weird"),
        ],
    )
    def test_types_as_mermaid_takes_them(self, data_type: str, expected: str) -> None:
        assert mermaid_type(data_type) == expected
