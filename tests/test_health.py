"""Diagnostics must preserve uncertainty, scope and partial results."""

from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Any

import pytest

from pg_explorer_mcp import db as database_module
from pg_explorer_mcp import health
from pg_explorer_mcp.db import Database, DatabaseUnavailableError, OverdueError, QueryFailedError, Settings
from pg_explorer_mcp.tools import ToolError, build_tools, db_health


class Evidence(Database):
    def __init__(self, failure: Exception | None = None) -> None:
        super().__init__({"PGPORT": "invalid"})
        self.calls: list[dict[str, Any]] = []
        self.failure = failure

    def query(self, sql: str, **params: Any) -> list[Sequence[Any]]:
        self.calls.append(params)
        if sql == health.SCOPE + health.CACHE_SQL and self.failure is not None:
            raise self.failure
        if sql.endswith(health.SEQUENCE_SQL):
            return [("shop.id_seq", {"state": "no_sequence_privilege", "last_reserved_value": None})]
        if sql.endswith(health.SIZE_SQL):
            return [(f"shop.t{i}", {"total_bytes": 8192}) for i in range(health.LISTED + 1)]
        return []


def test_permission_failure_keeps_other_checks_and_does_not_leak_error_details() -> None:
    db = Evidence(QueryFailedError("secret_schema.secret_table", code="42501"))
    text = "\n".join(db_health(db, ' "shop" ', ["shop", "billing"]))
    assert "42501" in text
    assert "secret_schema" not in text
    assert "no_sequence_privilege" in text
    assert '"last_reserved_value": null' in text
    assert "shop.t4" in text
    assert "shop.t5" not in text
    assert "more exist" in text
    assert len(db.calls) == len(health.SECTIONS)
    assert all(call["schema"] == "shop" and call["schemas"] == ["shop", "billing"] for call in db.calls[1:])
    assert database_module.deadline() is None


@pytest.mark.parametrize(
    ("failure", "message"),
    [(OverdueError(), "time budget exhausted"), (DatabaseUnavailableError("secret-host"), "Database unavailable")],
)
def test_interruption_keeps_partial_evidence_and_marks_remaining_checks(failure: Exception, message: str) -> None:
    db = Evidence(failure)
    text = "\n".join(db_health(db))
    assert message in text
    assert "remaining checks" in text
    assert len(db.calls) == 2
    assert "secret-host" not in text
    assert database_module.deadline() is None


@pytest.mark.parametrize("schema", [123, [], " "])
def test_invalid_schema_does_not_connect(schema: Any) -> None:
    db = Evidence()
    with pytest.raises(ToolError):
        db_health(db, schema)
    assert not db.calls


def test_unknown_sqlstate_and_russian_report(russian: None) -> None:  # noqa: ARG001
    text = "\n".join(db_health(Evidence(QueryFailedError("private"))))
    assert "SQLSTATE unknown" in text
    assert "Проверка недоступна" in text
    assert "Крупнейшие физические объекты" in text


def test_time_budget_restores_outer_deadline_and_clamps_statement_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    settings = Settings.from_env({"PG_EXPLORER_MCP_STATEMENT_TIMEOUT_MS": "5000"})
    with database_module.time_budget(3):
        assert database_module.deadline() == 103.0
        with database_module.time_budget(10):
            assert database_module.deadline() == 103.0
            assert "statement_timeout = 3000" in database_module.transaction_start(settings)
            assert "statement_timeout = 3000" in database_module.transaction_start(None)
        now[0] = 102.9
        assert "statement_timeout = 99" in database_module.transaction_start(settings)
        now[0] = 104
        with pytest.raises(OverdueError):
            database_module.transaction_start(settings)
    assert database_module.deadline() is None
    assert "statement_timeout = 5000" in database_module.transaction_start(settings)


@pytest.mark.parametrize("budget", [500, 900])
def test_small_mcp_response_keeps_late_failure_visible(budget: int) -> None:
    class Crowded(Evidence):
        def query(self, sql: str, **_params: Any) -> list[Sequence[Any]]:
            if sql.endswith(health.SIZE_SQL):
                raise QueryFailedError("private relation", code="42501")
            return [(f"shop.t{i}", {"description": "x" * 300}) for i in range(health.LISTED)]

    tool = next(tool for tool in build_tools(Crowded(), ".", budget=budget) if tool.name == "db_health")
    answer = tool.call({})
    assert len(answer) <= budget
    assert "sizes=unavailable[42501]" in answer
    assert "private relation" not in answer
