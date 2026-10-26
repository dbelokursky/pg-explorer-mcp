"""search_schema without a database: what it asks PostgreSQL and how it puts the answer together."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest

from pg_explorer_mcp import tools
from pg_explorer_mcp.db import Database, DatabaseUnavailableError, QueryFailedError
from pg_explorer_mcp.tools import PROFILES, SEARCH_LIMIT, Tool, ToolError, build_tools, missing_table_hint, query_hint
from tests.helpers import ROOT

TABLE = ("sales", "orders", "r", 1200, "Заказы клиентов", None, None, None, None)
COLUMN = ("sales", "orders", "r", None, None, "customer_id", "integer", True, "Покупатель")
VIEW_COLUMN = ("sales", "big_orders", "v", None, None, "total", "numeric(12,2)", False, None)


class CannedDatabase(Database):
    """Answers every query with the same rows and remembers the parameters it got."""

    def __init__(self, rows: Sequence[Sequence[Any]]) -> None:
        super().__init__({"PGPORT": "not-a-number"})
        self.rows = list(rows)
        self.params: list[dict[str, Any]] = []

    def query(self, sql: str, **params: Any) -> list[Sequence[Any]]:  # noqa: ARG002
        self.params.append(params)
        return self.rows


def search(db: Database, **arguments: object) -> list[str]:
    tool = {tool.name: tool for tool in build_tools(db, str(ROOT))}["search_schema"]
    return tool.call(arguments).splitlines()


def test_every_profile_can_search_because_it_shows_no_data() -> None:
    assert all("search_schema" in names for names in PROFILES.values())


def test_tables_and_columns_come_back_one_per_line() -> None:
    assert search(CannedDatabase([TABLE, COLUMN, VIEW_COLUMN]), text="order") == [
        "sales.orders | table | ~1.2k rows | Заказы клиентов",
        "sales.orders.customer_id integer NOT NULL  -- Покупатель",
        "sales.big_orders.total numeric(12,2)",
    ]


def test_the_query_gets_the_spellings_the_schema_and_one_row_more_than_the_limit() -> None:
    db = CannedDatabase([])
    search(db, text=" Заказ ", schema='"sales"')
    assert db.params[0] == {"patterns": ["%Заказ%", "%заказ%", "%ЗАКАЗ%"], "schema": "sales", "limit": SEARCH_LIMIT + 1}


@pytest.mark.parametrize(
    ("fragment", "patterns"),
    [
        ("order", ["%order%", "%ORDER%", "%Order%"]),
        ("заказ", ["%заказ%", "%ЗАКАЗ%", "%Заказ%"]),
        ("100%_done\\", ["%100\\%\\_done\\\\%", "%100\\%\\_DONE\\\\%"]),
    ],
    ids=["latin", "cyrillic", "wildcards"],
)
def test_patterns_cover_the_usual_spellings_and_match_wildcards_literally(fragment: str, patterns: list[str]) -> None:
    assert tools._like_patterns(fragment) == patterns


def test_too_many_matches_are_cut_with_a_hint() -> None:
    rows = [("s", f"t{n:03}", "v", None, None, None, None, None, None) for n in range(SEARCH_LIMIT + 1)]
    lines = search(CannedDatabase(rows), text="t")
    assert len(lines) == SEARCH_LIMIT + 1
    assert lines[-2] == f"s.t{SEARCH_LIMIT - 1:03} | view"
    assert lines[-1] == f"... more than {SEARCH_LIMIT} matches; use a longer piece of text or pass schema"


def test_nothing_found_is_an_answer_not_an_error() -> None:
    assert search(CannedDatabase([]), text="zzz") == [
        "Nothing matches 'zzz'. Try a shorter piece of the name, another spelling, or list_tables."
    ]


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"text": " "}, "text must be a non-empty string"),
        ({"text": 5}, "text must be a non-empty string"),
        ({"text": "order", "schema": 5}, "schema must be a string with the name of a schema"),
        ({}, r"Missing argument\(s\): text\."),
    ],
)
def test_bad_arguments_are_explained(arguments: dict[str, object], message: str) -> None:
    with pytest.raises(ToolError, match=message):
        search(CannedDatabase([]), **arguments)


@pytest.mark.usefixtures("russian")
def test_answers_in_russian() -> None:
    assert search(CannedDatabase([TABLE]), text="заказ") == [
        "sales.orders | таблица | ~1,2 тыс. строк | Заказы клиентов"
    ]
    assert search(CannedDatabase([]), text="zzz")[0].startswith("Ничего не нашлось по 'zzz'.")


def test_an_empty_optional_argument_counts_as_left_out() -> None:
    """Models send "" for an argument they mean to leave out: list_tables {"schema": ""} lists every schema."""
    db = CannedDatabase([TABLE])
    assert (
        search(db, text="orders", schema="")
        == search(db, text="orders", schema="  ")
        == ["sales.orders | table | ~1.2k rows | Заказы клиентов"]
    )
    assert [params["schema"] for params in db.params] == [None, None]
    seen: list[dict[str, Any]] = []

    def echo(args: dict[str, Any]) -> list[str]:
        seen.append(args)
        return []

    tool = Tool("echo", "Echo", "", {"text": {}, "note": {}}, echo, required=("text",))
    tool.call({"text": "x", "note": ""})
    assert seen == [{"text": "x"}]
    with pytest.raises(ToolError, match=r"Missing argument\(s\): text\."):
        tool.call({"text": ""})  # a required argument is still required


class NamedRelations(Database):
    """Finds relations by name as the catalog would: rows only for the names it knows."""

    def __init__(self, relations: dict[str, list[tuple[str, str]]], *, fails: bool = False) -> None:
        super().__init__({"PGPORT": "not-a-number"})
        self.relations = relations
        self.fails = fails
        self.asked: list[dict[str, Any]] = []

    def query(self, sql: str, **params: Any) -> list[Sequence[Any]]:  # noqa: ARG002
        self.asked.append(params)
        if self.fails:
            raise QueryFailedError("the catalog is out of reach")
        return [(1, schema, name, "r", None, None) for schema, name in self.relations.get(params["name"], [])]


def missing(name: str, code: str = "42P01") -> QueryFailedError:
    message = f'relation "{name}" does not exist'
    return QueryFailedError(f"PostgreSQL: {message} (SQLSTATE {code})", code=code, server_message=message)


class TestMissingTableHint:
    def test_a_table_written_without_its_schema_is_pointed_to(self) -> None:
        db = NamedRelations({"orders": [("archive", "orders"), ("shop", "orders")]})
        assert missing_table_hint(db, missing("orders"), None) == (
            'PostgreSQL: relation "orders" does not exist (SQLSTATE 42P01) A table of that name is readable as '
            "archive.orders, shop.orders: write the name with its schema."
        )
        assert db.asked == [{"name": "orders", "schema": None}]

    def test_on_a_stand_it_names_the_schema_to_call_with(self) -> None:
        db = NamedRelations({"orders": [("shop", "orders")]})
        assert missing_table_hint(db, missing("orders"), ["billing", "shop"]).endswith(
            "In this database a table of that name is in schema shop: call again with that schema."
        )
        assert db.asked == [{"name": "orders", "schema": None, "schemas": ["billing", "shop"]}]

    def test_a_quoted_name_in_capitals_is_tried_in_lower_case_too(self) -> None:
        db = NamedRelations({"orders": [("shop", "orders")]})
        assert "readable as shop.orders" in missing_table_hint(db, missing("public.Orders"), None)
        assert [params["name"] for params in db.asked] == ["Orders", "orders"]

    @pytest.mark.parametrize(
        ("db", "error"),
        [
            (NamedRelations({"orders": [("shop", "orders")]}), missing("orders", code="42703")),  # not a table
            (NamedRelations({}), missing("orders")),  # no such table anywhere
            (NamedRelations({"orders": [("shop", "orders")]}, fails=True), missing("orders")),  # catalog failed
            (NamedRelations({}), QueryFailedError("connection reset")),  # no SQLSTATE at all
        ],
    )
    def test_otherwise_the_error_stays_as_it_was(self, db: NamedRelations, error: QueryFailedError) -> None:
        assert missing_table_hint(db, error, None) == str(error)

    @pytest.mark.usefixtures("russian")
    def test_the_hint_speaks_russian(self) -> None:
        error = QueryFailedError("PostgreSQL: …", code="42P01", server_message='отношение "orders" не существует')
        hint = missing_table_hint(NamedRelations({"orders": [("shop", "orders")]}), error, None)
        assert hint.endswith("Таблица с таким именем доступна как shop.orders: укажите имя вместе со схемой.")


class ShopAndHr(Database):
    """shop has a readable table, hr only tables the role cannot read, and public none at all."""

    CUSTOMERS = ("shop", "customers", "r", 50, "Покупатели")

    def __init__(self) -> None:
        super().__init__({"PGPORT": "not-a-number"})

    def query(self, sql: str, **params: Any) -> list[Sequence[Any]]:
        if sql == tools.SCHEMAS_SQL:
            return [("shop",)]
        if sql == tools.SCHEMA_HOLDS_RELATIONS_SQL:
            return [(1,)] if params["schema"] in ("shop", "hr") else []
        if params.get("schema") not in (None, "shop"):
            return []
        if sql == tools.LIST_TABLES_SQL:
            return [self.CUSTOMERS]
        if sql == tools.SEARCH_SQL and "%customer%" in params["patterns"]:
            return [(*self.CUSTOMERS, None, None, None, None)]
        if sql == tools.FIND_RELATION_SQL and params["name"] == "customers":
            return [(1, *self.CUSTOMERS)]
        if sql == tools.COLUMNS_SQL:
            return [("id", "integer", True, None, None)]
        return []


def listing(db: Database, **arguments: object) -> list[str]:
    tool = {tool.name: tool for tool in build_tools(db, str(ROOT))}["list_tables"]
    return tool.call(arguments).splitlines()


def described(db: Database, **arguments: object) -> list[str]:
    tool = {tool.name: tool for tool in build_tools(db, str(ROOT))}["describe_table"]
    return tool.call(arguments).splitlines()


class TestGuessedSchema:
    """The model guesses "public"; when not a single table is there, the tables of every schema answer it."""

    def test_list_tables_shows_the_tables_of_every_schema(self) -> None:
        assert listing(ShopAndHr(), schema="public") == [
            "Schema 'public' has no tables, so these are the readable tables of all schemas:",
            "shop.customers | table | ~50 rows | Покупатели",
        ]

    def test_search_schema_searches_every_schema(self) -> None:
        assert search(ShopAndHr(), text="customer", schema="public") == [
            "Schema 'public' has no tables, so the search went through all schemas:",
            "shop.customers | table | ~50 rows | Покупатели",
        ]
        assert search(ShopAndHr(), text="zzz", schema="public")[1].startswith("Nothing matches 'zzz'.")

    def test_describe_table_finds_the_table_the_name_means(self) -> None:
        for arguments in ({"table": "customers", "schema": "public"}, {"table": "shop.customers", "schema": "public"}):
            assert described(ShopAndHr(), **arguments)[:3] == [
                "shop.customers | table | ~50 rows | Покупатели",
                "columns:",
                "  id integer NOT NULL",
            ]

    def test_a_schema_whose_tables_the_role_cannot_read_is_refused(self) -> None:
        """hr: the answer is that the role reads nothing there, not the tables of shop."""
        message = r"The current role cannot read any table in schema 'hr'\. Schemas with readable tables: shop\."
        with pytest.raises(ToolError, match=message):
            listing(ShopAndHr(), schema="hr")
        with pytest.raises(ToolError, match=message):
            search(ShopAndHr(), text="salary", schema="hr")
        with pytest.raises(ToolError, match=r"No readable table or view named 'customers' in schema 'hr'\."):
            described(ShopAndHr(), table="customers", schema="hr")

    def test_a_system_schema_is_left_out_and_not_called_closed(self) -> None:
        for schema in ("pg_catalog", "information_schema"):
            with pytest.raises(ToolError, match=rf"Schema '{schema}' is a system one: list_tables and search_schema"):
                listing(ShopAndHr(), schema=schema)

    def test_the_schema_of_list_tables_is_read_as_the_other_tools_read_it(self) -> None:
        assert listing(ShopAndHr(), schema=' "shop" ') == ["shop.customers | table | ~50 rows | Покупатели"]

    def test_a_readable_schema_without_matches_has_nothing_to_show(self) -> None:
        assert search(ShopAndHr(), text="zzz", schema="shop") == [
            "Nothing matches 'zzz'. Try a shorter piece of the name, another spelling, or list_tables."
        ]

    @pytest.mark.usefixtures("russian")
    def test_the_first_line_speaks_russian(self) -> None:
        assert listing(ShopAndHr(), schema="public")[0] == (
            "В схеме 'public' нет таблиц, поэтому здесь доступные для чтения таблицы всех схем:"
        )
        assert search(ShopAndHr(), text="customer", schema="public")[0] == (
            "В схеме 'public' нет таблиц, поэтому поиск шёл по всем схемам:"
        )


class KnownTables(Database):
    """Finds a relation by name as to_regclass would, and gives its columns; a name it cannot read fails."""

    def __init__(self, tables: dict[str, tuple[str, list[str]]], *, down: bool = False) -> None:
        super().__init__({"PGPORT": "not-a-number"})
        self.tables = tables  # the name as the statement writes it: (schema.relation, columns)
        self.down = down

    def query(self, sql: str, **params: Any) -> list[Sequence[Any]]:
        if self.down:
            raise DatabaseUnavailableError("the connection is gone")
        if sql == tools.RELATION_BY_NAME_SQL:
            if params["name"].isdigit():
                raise QueryFailedError("invalid name syntax", code="42602")
            found = self.tables.get(params["name"])
            return [(params["name"], *found[0].split("."))] if found else []
        return [(column,) for column in self.tables[params["oid"]][1]]


def no_column(column: str = "order_id") -> QueryFailedError:
    message = f'column "{column}" does not exist'
    return QueryFailedError(f"PostgreSQL: {message} (SQLSTATE 42703)", code="42703", server_message=message)


ORDERS = ("shop.orders", ["id", "customer_id", "status", "created_at"])
ITEMS = ("shop.order_items", ["order_id", "product_id", "quantity", "price"])


class TestMissingColumnHint:
    def test_the_columns_of_the_table_the_query_reads_follow_the_error(self) -> None:
        db = KnownTables({"shop.orders": ORDERS})
        assert query_hint(db, no_column(), "SELECT * FROM shop.orders WHERE order_id = 501", None) == (
            'PostgreSQL: column "order_id" does not exist (SQLSTATE 42703) Columns of the tables the query reads: '
            "shop.orders: id, customer_id, status, created_at."
        )

    def test_every_table_of_a_join_and_only_tables(self) -> None:
        db = KnownTables({"orders": ORDERS, "shop.order_items": ITEMS})
        query = "SELECT substring(o.note FROM 2) FROM orders o JOIN shop . order_items i ON i.order_no = o.id"
        hint = query_hint(db, no_column("i.order_no"), query, None)
        assert hint.endswith(
            "shop.orders: id, customer_id, status, created_at; shop.order_items: order_id, product_id, quantity, price."
        )

    def test_a_wide_table_is_cut(self) -> None:
        db = KnownTables({"wide": ("shop.wide", [f"c{number}" for number in range(50)])})
        assert query_hint(db, no_column(), "SELECT x FROM wide", None).endswith(", c38, c39, ….")

    @pytest.mark.parametrize(
        ("db", "query"),
        [
            (KnownTables({}), "SELECT order_id FROM nowhere"),  # nothing the catalog knows
            (KnownTables({"orders": ORDERS}), "SELECT order_id"),  # no table at all
            (KnownTables({"orders": ORDERS}, down=True), "SELECT order_id FROM orders"),  # the connection went
        ],
    )
    def test_otherwise_the_error_stays_as_it_was(self, db: KnownTables, query: str) -> None:
        assert query_hint(db, no_column(), query, None) == str(no_column())

    def test_other_errors_pass_through(self) -> None:
        error = QueryFailedError("PostgreSQL: division by zero (SQLSTATE 22012)", code="22012")
        assert query_hint(KnownTables({"orders": ORDERS}), error, "SELECT 1/0 FROM orders", None) == str(error)
