"""Saved queries: the group's own SQL, kept beside the database and run by name."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from pg_explorer_mcp import i18n
from pg_explorer_mcp.db import Database
from pg_explorer_mcp.queries import SavedQueries, default_file
from pg_explorer_mcp.tools import ToolError, build_tools
from tests.helpers import ROOT

FILE = """\
[orders_by_status]
ru = Заказы по статусам
en = Orders by status
query = SELECT status, count(*) FROM shop.orders GROUP BY status
[delete_orders]
query = DELETE FROM shop.orders
[a plain service]
host=db.example.com
[no_sql]
en = A section without a query
"""


def saved(path: Path, text: str = FILE) -> SavedQueries:
    path.write_text(text, encoding="utf-8")
    return SavedQueries.from_env({"PG_EXPLORER_MCP_QUERIES_FILE": str(path)})


class TestReading:
    def test_only_read_queries_that_return_rows_are_taken(self, tmp_path: Path) -> None:
        found = saved(tmp_path / "queries.conf")
        assert found.names() == ["orders_by_status"]
        query = found.named("Orders_By_Status")
        assert query is not None
        with i18n.speaking("ru"):
            assert query.text() == "Заказы по статусам"

    def test_a_query_that_changes_anything_is_a_warning_naming_it(self, tmp_path: Path) -> None:
        found = saved(tmp_path / "queries.conf")
        assert len(found.warnings) == 1
        assert found.warnings[0].startswith("Query delete_orders was left out:")
        assert "DELETE is not allowed" in found.warnings[0]

    def test_a_query_that_returns_no_rows_is_a_warning_naming_it(self, tmp_path: Path) -> None:
        """The text used to forget the name: a query was left out, and nobody said which."""
        found = saved(tmp_path / "queries.conf", "[explain]\nquery = EXPLAIN SELECT 1\n")
        assert found.names() == []
        assert found.warnings == [
            "Query explain was left out: only a query that returns rows can be saved (SELECT, WITH, VALUES, TABLE)"
        ]

    def test_an_unreadable_file_is_a_warning(self, tmp_path: Path) -> None:
        folder = tmp_path / "folder.conf"
        folder.mkdir()
        found = SavedQueries.from_env({"PG_EXPLORER_MCP_QUERIES_FILE": str(folder)})
        assert (found.queries, len(found.warnings)) == ([], 1)

    def test_no_file_is_no_queries_and_no_complaints(self, tmp_path: Path) -> None:
        found = SavedQueries.from_env({"PG_EXPLORER_MCP_QUERIES_FILE": str(tmp_path / "absent.conf")})
        assert (found.queries, found.warnings) == ([], [])

    def test_an_edited_file_is_reread_without_a_restart(self, tmp_path: Path) -> None:
        path = tmp_path / "queries.conf"
        found = saved(path, "[one]\nquery = SELECT 1\n")
        assert found.names() == ["one"]
        # Another size, as on Windows the timestamps of two writes in one tick can come out equal.
        path.write_text("[two]\nquery = SELECT 2222\n", encoding="utf-8")
        assert found.refresh() is True
        assert found.names() == ["two"]
        assert found.refresh() is False

    def test_the_default_file_lives_beside_the_glossary(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOME", "/home/anna")
        folder = "pg-explorer-mcp" if sys.platform == "win32" else ".pg-explorer-mcp"
        assert default_file({}).parts[-2:] == (folder, "queries.conf")


class TestTheTool:
    def test_the_tool_stays_away_until_the_group_saves_a_query(self, tmp_path: Path) -> None:
        database = Database({"PGPORT": "not-a-number"})  # building tools needs no database
        assert "run_saved_query" not in {tool.name for tool in build_tools(database, str(ROOT))}
        found = saved(tmp_path / "queries.conf")
        tools = build_tools(database, str(ROOT), saved=found)
        assert "run_saved_query" in {tool.name for tool in tools}

    def test_an_unknown_name_lists_the_saved_ones(self, tmp_path: Path) -> None:
        found = saved(tmp_path / "queries.conf")
        tools = {tool.name: tool for tool in build_tools(Database({"PGPORT": "not-a-number"}), str(ROOT), saved=found)}
        with pytest.raises(ToolError, match=r"No saved query 'orders_by_month'\. Saved: orders_by_status\."):
            tools["run_saved_query"].call({"name": "orders_by_month"})

    def test_a_name_of_the_wrong_kind_is_explained(self, tmp_path: Path) -> None:
        found = saved(tmp_path / "queries.conf")
        tools = {tool.name: tool for tool in build_tools(Database({"PGPORT": "not-a-number"}), str(ROOT), saved=found)}
        with pytest.raises(ToolError, match="name must be a non-empty string"):
            tools["run_saved_query"].call({"name": 7})
