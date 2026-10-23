"""The query log: what a tool call leaves in the file, and that the file never gets in the way."""

from __future__ import annotations

import json
import os
import platform
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

from pg_mcp.db import Database
from pg_mcp.querylog import QueryLog
from pg_mcp.tools import Tool, ToolError, build_tools
from tests.helpers import ROOT


def entries(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def tools_with(query_log: QueryLog, db: Database) -> dict[str, Tool]:
    return {tool.name: tool for tool in build_tools(db, str(ROOT), profile="analyst", query_log=query_log)}


@pytest.fixture
def log_file(tmp_path: Path) -> Path:
    return tmp_path / "queries.jsonl"


def test_the_log_is_off_unless_a_file_is_named(
    unreachable_env: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    db = Database(unreachable_env)
    assert "query log: off" in tools_with(QueryLog.from_env({}, db), db)["status"].call({})
    assert list(tmp_path.iterdir()) == []


def test_a_home_relative_path_is_expanded() -> None:
    assert QueryLog.from_env({"PG_MCP_QUERY_LOG": "~/queries.jsonl"}).path == str(Path.home() / "queries.jsonl")


def test_every_call_leaves_one_line_whatever_its_outcome(unreachable_env: dict[str, str], log_file: Path) -> None:
    db = Database(unreachable_env)
    tools = tools_with(QueryLog.from_env({"PG_MCP_QUERY_LOG": str(log_file)}, db), db)
    assert f"query log: {log_file}" in tools["status"].call({})
    with pytest.raises(ToolError):
        tools["run_select"].call({"query": "DELETE FROM orders"})
    with pytest.raises(ToolError):
        tools["sample_rows"].call({"table": "orders", "rows": 5})
    with pytest.raises(ToolError):
        tools["list_tables"].call({})

    logged = entries(log_file)
    assert [(entry["tool"], entry["outcome"]) for entry in logged] == [
        ("status", "ok"),
        ("run_select", "refused"),
        ("sample_rows", "error"),
        ("list_tables", "unavailable"),
    ]
    refused = logged[1]
    assert refused["arguments"] == {"query": "DELETE FROM orders"}
    assert refused["message"].startswith("DELETE is not allowed: this server only reads")
    assert (refused["role"], refused["database"]) == ("pg_mcp_test", "pg_mcp_test")
    assert refused["pid"] == os.getpid()
    assert refused["python"] == platform.python_version()
    assert refused["time"].endswith("Z")
    assert all("rows" not in entry for entry in logged)  # no statement reached the database
    assert all(entry["duration_ms"] >= 0 for entry in logged)


def test_lines_are_appended_to_what_is_already_there(unreachable_env: dict[str, str], log_file: Path) -> None:
    log_file.write_text('{"earlier": true}\n', encoding="utf-8")
    db = Database(unreachable_env)
    tools_with(QueryLog(str(log_file), db), db)["status"].call({})
    assert [entry.get("tool", "earlier") for entry in entries(log_file)] == ["earlier", "status"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_a_new_file_is_readable_by_its_owner_only(unreachable_env: dict[str, str], log_file: Path) -> None:
    db = Database(unreachable_env)
    tools_with(QueryLog(str(log_file), db), db)["status"].call({})
    assert stat.S_IMODE(log_file.stat().st_mode) == 0o600


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks and O_NOFOLLOW")
def test_a_symlink_in_place_of_the_file_is_refused(unreachable_env: dict[str, str], tmp_path: Path) -> None:
    victim = tmp_path / "victim.txt"
    victim.write_text("untouched\n", encoding="utf-8")
    link = tmp_path / "queries.jsonl"
    link.symlink_to(victim)
    db = Database(unreachable_env)
    status = tools_with(QueryLog(str(link), db), db)["status"]
    status.call({})
    assert f"query log: {link}, not written: " in status.call({})
    assert victim.read_text(encoding="utf-8") == "untouched\n"


def test_a_relative_path_is_refused_and_nothing_is_written(
    unreachable_env: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    db = Database(unreachable_env)
    status = tools_with(QueryLog.from_env({"PG_MCP_QUERY_LOG": "queries.jsonl"}, db), db)["status"].call({})
    assert "query log: queries.jsonl, not written: the path must be absolute" in status
    assert list(tmp_path.iterdir()) == []


def test_a_file_that_cannot_be_written_never_breaks_a_call(unreachable_env: dict[str, str], tmp_path: Path) -> None:
    target = tmp_path / "missing" / "queries.jsonl"
    db = Database(unreachable_env)
    status = tools_with(QueryLog(str(target), db), db)["status"]
    status.call({})  # the line about this call cannot be written: its folder does not exist
    assert f"query log: {target}, not written: " in status.call({})
    target.parent.mkdir()
    status.call({})  # this one is written, and the problem is gone
    assert "not written" not in status.call({})
    assert len(entries(target)) == 2


def test_a_bug_is_logged_as_failed_and_still_raised(log_file: Path) -> None:
    def broken(_arguments: dict[str, Any]) -> str:
        raise RuntimeError("boom")

    tool = Tool("broken", "Broken", "Always fails.", {}, broken, query_log=QueryLog(str(log_file)))
    with pytest.raises(RuntimeError, match="boom"):
        tool.call({})
    [entry] = entries(log_file)
    assert (entry["outcome"], entry["message"], entry["role"]) == ("failed", "RuntimeError: boom", None)


@pytest.mark.usefixtures("russian")
def test_status_speaks_russian_about_the_log(unreachable_env: dict[str, str], tmp_path: Path) -> None:
    db = Database(unreachable_env)
    assert "журнал запросов: выключен" in tools_with(QueryLog(None, db), db)["status"].call({})
    relative = tools_with(QueryLog("queries.jsonl", db), db)["status"].call({})
    assert "журнал запросов: queries.jsonl, запись не удалась: путь должен быть абсолютным" in relative
    assert not (tmp_path / "queries.jsonl").exists()
