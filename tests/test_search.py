"""search_schema without a database: what it asks PostgreSQL and how it puts the answer together."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest

from pg_mcp import tools
from pg_mcp.db import Database, QueryFailedError
from pg_mcp.tools import PROFILES, SEARCH_LIMIT, Tool, ToolError, build_tools, missing_table_hint
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
    assert db.params == [{"patterns": ["%Заказ%", "%заказ%", "%ЗАКАЗ%"], "schema": "sales", "limit": SEARCH_LIMIT + 1}]


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
