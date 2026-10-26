"""The PG_MCP_* names of pg-mcp 0.7.0 keep working next to the new PG_EXPLORER_MCP_* ones (ADR 0007)."""

from __future__ import annotations

from pg_explorer_mcp import environment


def test_an_old_name_stands_in_for_a_missing_new_one() -> None:
    env = {"PG_MCP_PROFILE": "analyst", "PGHOST": "db"}
    assert environment.with_legacy_names(env) == {
        "PG_MCP_PROFILE": "analyst",
        "PG_EXPLORER_MCP_PROFILE": "analyst",
        "PGHOST": "db",
    }
    assert environment.legacy_names(env) == ["PG_MCP_PROFILE"]


def test_the_new_name_wins_when_both_are_set() -> None:
    env = {"PG_MCP_PROFILE": "analyst", "PG_EXPLORER_MCP_PROFILE": "schema"}
    assert environment.with_legacy_names(env)["PG_EXPLORER_MCP_PROFILE"] == "schema"
    assert environment.legacy_names(env) == []


def test_an_environment_without_old_names_stays_as_it_is() -> None:
    env = {"PG_EXPLORER_MCP_LANG": "ru", "PGDATABASE": "app"}
    assert environment.with_legacy_names(env) == env
    assert environment.legacy_names(env) == []


def test_the_given_environment_is_not_changed() -> None:
    env = {"PG_MCP_LANG": "ru"}
    environment.with_legacy_names(env)
    assert env == {"PG_MCP_LANG": "ru"}
