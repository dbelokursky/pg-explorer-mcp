"""The demo database of the README: scripts/demo.py makes it, and the tools answer as the README shows.

Needs a PostgreSQL where the PG* role may create databases and roles; the database
pg_mcp_demo_it and the role mcp_demo_it_agent are made and dropped here.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import demo
import pytest

from pg_mcp.db import Database
from pg_mcp.tools import Tool, ToolError, build_tools
from tests.helpers import ROOT

pytestmark = pytest.mark.integration

DATABASE = "pg_mcp_demo_it"
ROLE = "mcp_demo_it_agent"
PASSWORD = "demo-it"  # noqa: S105


@pytest.fixture(scope="module")
def tools() -> Iterator[dict[str, Tool]]:
    assert demo.main(["--database", DATABASE, "--role", ROLE, "--drop"]) == 0
    assert demo.main(["--database", DATABASE, "--role", ROLE, "--password", PASSWORD]) == 0
    env = dict(os.environ, PGDATABASE=DATABASE, PGUSER=ROLE, PGPASSWORD=PASSWORD)
    database = Database(env)
    yield {tool.name: tool for tool in build_tools(database, str(ROOT), profile="analyst")}
    database.close()
    assert demo.main(["--database", DATABASE, "--role", ROLE, "--drop"]) == 0


def lines_of(tool: Tool, **arguments: object) -> list[str]:
    return tool.call(arguments).splitlines()


def test_the_agent_sees_the_shop_as_the_readme_shows(tools: dict[str, Tool]) -> None:
    assert lines_of(tools["list_tables"]) == [
        "shop.customers | table | ~50 rows | Покупатели",
        "shop.order_items | table | ~2.2k rows | Позиции заказов: цена фиксируется на момент заказа",
        "shop.orders | table | ~500 rows | Заказы",
        "shop.products | table | ~20 rows | Каталог товаров",
    ]


def test_search_finds_the_table_and_the_column(tools: dict[str, Tool]) -> None:
    assert lines_of(tools["search_schema"], text="customer") == [
        "shop.customers | table | ~50 rows | Покупатели",
        "shop.orders.customer_id integer NOT NULL",
    ]


def test_orders_are_described_with_keys_and_indexes(tools: dict[str, Tool]) -> None:
    lines = lines_of(tools["describe_table"], table="shop.orders")
    assert lines[0] == "shop.orders | table | ~500 rows | Заказы"
    assert "  status text NOT NULL  -- new, paid, shipped или cancelled" in lines
    assert "  orders_customer_id_fkey: FOREIGN KEY (customer_id) REFERENCES shop.customers(id)" in lines
    assert "  shop.order_items: FOREIGN KEY (order_id) REFERENCES shop.orders(id)" in lines
    assert "  CREATE INDEX orders_customer_idx ON shop.orders USING btree (customer_id)" in lines


def test_the_orders_per_status_are_the_readme_numbers(tools: dict[str, Tool]) -> None:
    query = "SELECT status, count(*) FROM shop.orders GROUP BY status ORDER BY 2 DESC"
    assert lines_of(tools["run_select"], query=query) == [
        "status | count",
        "shipped | 174",
        "paid | 162",
        "cancelled | 90",
        "new | 74",
        "(4 rows)",
    ]


def test_hr_does_not_exist_for_the_agent(tools: dict[str, Tool]) -> None:
    with pytest.raises(ToolError, match="No readable table or view named 'salaries' in schema 'hr'"):
        tools["describe_table"].call({"table": "hr.salaries"})
    with pytest.raises(ToolError, match="permission denied for schema hr"):
        tools["run_select"].call({"query": "SELECT * FROM hr.employees"})


def test_the_data_is_the_same_on_every_copy(tools: dict[str, Tool]) -> None:
    lines = lines_of(tools["sample_rows"], table="shop.orders", columns="id, status, created_at", limit=3)
    assert lines[:2] == ["shop.orders:", "id | status | created_at"]
    assert lines[2].startswith("1 | shipped | 2026-01-01 ")
    assert lines[3].startswith("2 | paid | 2026-01-01 ")
