"""search_schema without a database: what it asks PostgreSQL and how it puts the answer together."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest

from pg_mcp import tools
from pg_mcp.db import Database
from pg_mcp.tools import PROFILES, SEARCH_LIMIT, ToolError, build_tools
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
        ({"text": "order", "schema": " "}, "schema must be a non-empty string"),
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
