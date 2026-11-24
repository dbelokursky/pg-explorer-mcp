"""The glossary: descriptions kept in a file beside the database, shown where comments are absent."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from pg_explorer_mcp import i18n
from pg_explorer_mcp.db import Database
from pg_explorer_mcp.glossary import Glossary, default_file
from pg_explorer_mcp.tools import build_tools
from tests.helpers import ROOT

GLOSSARY = """\
[table shop.orders]
en = Customer orders: status and time
ru = Заказы клиентов: статус и время
[column shop.orders.total]
ru = Сумма заказа
[term average_receipt]
en = Sum of the paid orders of a month, divided by their count
ru = Сумма оплаченных заказов за месяц, делённая на их количество
[table shop.customers]
en = The people who order
[weird section]
en = Not a thing the glossary describes
[table shop.empty]
"""


def glossary(path: Path, text: str = GLOSSARY) -> Glossary:
    path.write_text(text, encoding="utf-8")
    return Glossary.from_env({"PG_EXPLORER_MCP_GLOSSARY_FILE": str(path)})


class TestReading:
    def test_sections_become_entries_and_bare_ones_do_not(self, tmp_path: Path) -> None:
        found = glossary(tmp_path / "glossary.conf")
        assert [(entry.kind, entry.name) for entry in found.entries] == [
            ("table", "shop.orders"),
            ("column", "shop.orders.total"),
            ("term", "average_receipt"),
            ("table", "shop.customers"),
        ]

    def test_names_match_whatever_the_case(self, tmp_path: Path) -> None:
        found = glossary(tmp_path / "glossary.conf")
        with i18n.speaking("en"):
            assert found.describe("table", "SHOP.Orders") == "Customer orders: status and time"
            assert found.describe("column", "shop.orders.TOTAL") == "Сумма заказа"  # falls back to Russian
            expected = "Sum of the paid orders of a month, divided by their count"
            assert found.describe("term", "Average_Receipt") == expected
            assert found.describe("table", "shop.returns") is None

    def test_the_servers_language_comes_first_and_the_other_stands_in(self, tmp_path: Path) -> None:
        found = glossary(tmp_path / "glossary.conf")
        with i18n.speaking("ru"):
            assert found.entries[0].text() == "Заказы клиентов: статус и время"
            assert found.entries[1].text() == "Сумма заказа"
        with i18n.speaking("en"):
            assert found.entries[1].text() == "Сумма заказа"  # no English text: the Russian one stands in

    def test_no_file_is_no_entries_and_no_complaints(self, tmp_path: Path) -> None:
        found = Glossary.from_env({"PG_EXPLORER_MCP_GLOSSARY_FILE": str(tmp_path / "absent.conf")})
        assert (found.entries, found.warnings) == ([], [])

    def test_an_unreadable_file_is_a_warning(self, tmp_path: Path) -> None:
        folder = tmp_path / "folder.conf"
        folder.mkdir()
        found = Glossary.from_env({"PG_EXPLORER_MCP_GLOSSARY_FILE": str(folder)})
        assert found.entries == []
        assert len(found.warnings) == 1
        assert "cannot be read" in found.warnings[0]

    def test_the_default_file_lives_beside_the_stands_registry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOME", "/home/anna")
        folder = "pg-explorer-mcp" if sys.platform == "win32" else ".pg-explorer-mcp"
        assert default_file({}).parts[-2:] == (folder, "glossary.conf")


class TestSearch:
    def test_matches_by_text_in_either_language_with_terms_last(self, tmp_path: Path) -> None:
        found = glossary(tmp_path / "glossary.conf")
        with i18n.speaking("ru"):
            matches = found.search("заказ")
        assert [(entry.kind, entry.name) for entry in matches] == [
            ("table", "shop.orders"),
            ("column", "shop.orders.total"),
            ("term", "average_receipt"),
        ]

    def test_the_limit_holds(self, tmp_path: Path) -> None:
        found = glossary(tmp_path / "glossary.conf")
        assert len(found.search("s", limit=2)) == 2


class TestRefresh:
    def test_an_edited_file_is_reread_without_a_restart(self, tmp_path: Path) -> None:
        path = tmp_path / "glossary.conf"
        found = glossary(path)
        with i18n.speaking("en"):
            assert found.describe("table", "shop.orders") == "Customer orders: status and time"
            path.write_text("[table shop.orders]\nen = Orders, edited\n", encoding="utf-8")
            assert found.refresh() is True
            assert found.describe("table", "shop.orders") == "Orders, edited"
            assert found.refresh() is False  # nothing changed since

    def test_status_tells_of_a_glossary_that_cannot_be_read_after_an_edit(self, tmp_path: Path) -> None:
        """Status showed the warnings of start-up only: the descriptions were gone, and nothing said why."""
        path = tmp_path / "glossary.conf"
        found = glossary(path)
        tools = {
            tool.name: tool for tool in build_tools(Database({"PGPORT": "not-a-number"}), str(ROOT), glossary=found)
        }
        assert "cannot be read" not in tools["status"].call({})
        path.unlink()
        path.mkdir()
        assert f"warning: The glossary {path} cannot be read" in tools["status"].call({})
