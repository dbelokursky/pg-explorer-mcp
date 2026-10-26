from __future__ import annotations

import logging

import pytest

from pg_explorer_mcp.budget import DEFAULT_BUDGET, MIN_BUDGET, budget_from_env, fit_to_budget, shorten

HEADER = "sales.orders | table | ~1.2k rows | Заказы клиентов"


def answer(columns: int, width: int = 60) -> list[str]:
    return [HEADER, "columns:"] + [f"  column_{i:03d} text  -- " + "x" * width for i in range(columns)]


def fit(lines: list[str], budget: int) -> list[str]:
    """fit_to_budget plus the contract from its docstring."""
    result = fit_to_budget(list(lines), budget)
    assert len("\n".join(result)) <= budget
    if result != lines:
        assert result, "a cut answer needs at least the notice"
        assert result[-1].startswith("…"), f"no notice at the end: {result[-1]}"
        kept = result[:-1]
        if kept:
            assert kept[:-1] == lines[: len(kept) - 1], "kept lines must be the input's first lines"
            assert lines[len(kept) - 1].startswith(kept[-1].rstrip("…")), "the last kept line may only be shortened"
    return result


def test_answer_that_fits_comes_back_unchanged() -> None:
    lines = answer(5)
    assert fit(lines, DEFAULT_BUDGET) == lines


def test_answer_of_exactly_the_budget_is_not_cut() -> None:
    lines = answer(3)
    assert fit(lines, len("\n".join(lines))) == lines


@pytest.mark.parametrize("budget", [MIN_BUDGET, 1000, DEFAULT_BUDGET])
def test_long_answer_is_cut_with_a_notice_and_keeps_the_header(budget: int) -> None:
    lines = answer(500)
    result = fit(lines, budget)
    assert result != lines
    assert result[0] == HEADER
    assert f"of {len(lines)} lines shown" in result[-1]


def test_one_huge_line_still_fits() -> None:
    result = fit(["x" * 20000], MIN_BUDGET)
    assert result[0].endswith("…")


def test_empty_answer_stays_empty() -> None:
    assert fit_to_budget([], MIN_BUDGET) == []


def test_shorten_collapses_whitespace_into_one_line() -> None:
    assert shorten("Заказы\n  клиентов\tза год") == "Заказы клиентов за год"


def test_shorten_cuts_long_values_with_an_ellipsis() -> None:
    value = shorten("a" * 500, limit=20)
    assert len(value) == 20
    assert value.endswith("…")


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({}, DEFAULT_BUDGET),
        ({"PG_MCP_MAX_RESPONSE_CHARS": "12000"}, 12000),
        ({"PG_MCP_MAX_RESPONSE_CHARS": "10"}, MIN_BUDGET),
    ],
)
def test_budget_comes_from_the_environment_with_a_floor(env: dict[str, str], expected: int) -> None:
    assert budget_from_env(env) == expected


def test_garbage_budget_falls_back_to_the_default(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="pg_explorer_mcp.budget"):
        assert budget_from_env({"PG_MCP_MAX_RESPONSE_CHARS": "lots"}) == DEFAULT_BUDGET
    assert "is not a number" in caplog.text
