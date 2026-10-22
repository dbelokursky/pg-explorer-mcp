from __future__ import annotations

import pytest

from pg_mcp.db import Database
from pg_mcp.tools import PROFILES, Tool, build_tools, profile_from_env
from tests.helpers import ROOT


def tools_for(profile: str, warning: str | None = None) -> dict[str, Tool]:
    database = Database({"PGPORT": "not-a-number"})  # never connects: building tools needs no database
    warnings = [warning] if warning else []
    return {tool.name: tool for tool in build_tools(database, str(ROOT), profile=profile, warnings=warnings)}


@pytest.mark.parametrize(
    ("env", "expected"),
    [({}, ("explore", None)), ({"PG_MCP_PROFILE": " Analyst "}, ("analyst", None))],
)
def test_profile_comes_from_the_environment(env: dict[str, str], expected: tuple[str, None]) -> None:
    assert profile_from_env(env) == expected


def test_unknown_profile_falls_back_to_the_strictest_with_a_warning() -> None:
    profile, warning = profile_from_env({"PG_MCP_PROFILE": "admin"})
    assert profile == "schema"
    assert warning is not None
    assert "PG_MCP_PROFILE='admin'" in warning
    status = tools_for(profile, warning)["status"].call({})
    assert "profile: schema (status, list_tables, search_schema, describe_table)" in status
    assert "warning: PG_MCP_PROFILE='admin'" in status


@pytest.mark.parametrize(("profile", "names"), PROFILES.items())
def test_each_profile_offers_its_tools_in_a_fixed_order(profile: str, names: tuple[str, ...]) -> None:
    assert tuple(tools_for(profile)) == names


def test_only_analysts_may_ask_explain_to_run_the_query() -> None:
    assert "analyze" not in tools_for("explore")["explain_query"].properties
    assert "analyze" in tools_for("analyst")["explain_query"].properties
