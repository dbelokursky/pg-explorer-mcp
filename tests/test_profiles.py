from __future__ import annotations

from pathlib import Path

import pytest

from pg_explorer_mcp.db import Database
from pg_explorer_mcp.queries import SavedQueries
from pg_explorer_mcp.tools import PROFILES, Tool, build_tools, profile_from_env
from tests.helpers import ROOT


def tools_for(profile: str, warning: str | None = None) -> dict[str, Tool]:
    database = Database({"PGPORT": "not-a-number"})  # never connects: building tools needs no database
    warnings = [warning] if warning else []
    return {tool.name: tool for tool in build_tools(database, str(ROOT), profile=profile, warnings=warnings)}


@pytest.mark.parametrize(
    ("env", "expected"),
    [({}, ("explore", None)), ({"PG_EXPLORER_MCP_PROFILE": " Analyst "}, ("analyst", None))],
)
def test_profile_comes_from_the_environment(env: dict[str, str], expected: tuple[str, None]) -> None:
    assert profile_from_env(env) == expected


def test_unknown_profile_falls_back_to_the_strictest_with_a_warning() -> None:
    profile, warning = profile_from_env({"PG_EXPLORER_MCP_PROFILE": "admin"})
    assert profile == "schema"
    assert warning is not None
    assert "PG_EXPLORER_MCP_PROFILE='admin'" in warning
    status = tools_for(profile, warning)["status"].call({})
    assert "profile: schema (status, list_tables, search_schema, describe_table, schema_diagram)" in status
    # Said in the answer too, which no size limit binds: a model told "read only = on" still offered to delete.
    assert "access: read only. Nothing can be inserted, changed or deleted through this server" in status
    assert "warning: PG_EXPLORER_MCP_PROFILE='admin'" in status


# run_saved_query is offered only when the group saved queries (queries.py): its place in the list,
# not its presence, is what the profile decides.
OFFERED_WITHOUT_SAVED = tuple(name for name in PROFILES["analyst"] if name != "run_saved_query")


@pytest.mark.parametrize(("profile", "names"), PROFILES.items())
def test_each_profile_offers_its_tools_in_a_fixed_order(profile: str, names: tuple[str, ...]) -> None:
    assert tuple(tools_for(profile)) == tuple(name for name in names if name != "run_saved_query")


def test_saved_queries_add_their_tool_where_the_profile_says(tmp_path: Path) -> None:
    file = tmp_path / "queries.conf"
    file.write_text("[one]\nquery = SELECT 1\n", encoding="utf-8")
    saved = SavedQueries.from_env({"PG_EXPLORER_MCP_QUERIES_FILE": str(file)})
    tools = build_tools(Database({"PGPORT": "not-a-number"}), str(ROOT), profile="analyst", saved=saved)
    assert tuple(tool.name for tool in tools) == PROFILES["analyst"]


def test_only_analysts_may_ask_explain_to_run_the_query() -> None:
    assert "analyze" not in tools_for("explore")["explain_query"].properties
    assert "analyze" in tools_for("analyst")["explain_query"].properties
