"""Test configuration: the integration marker and fixtures shared by several modules."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

import pg_explorer_mcp  # noqa: F401  (puts the vendored packages, pg8000 among them, on sys.path)
from pg_explorer_mcp import i18n
from tests.helpers import unreachable_database_env

INTEGRATION = os.environ.get("PG_MCP_IT") == "1"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:  # noqa: ARG001
    """Skip tests marked `integration` unless PG_MCP_IT=1 says a PostgreSQL is there."""
    if INTEGRATION:
        return
    skip = pytest.mark.skip(reason="needs PG_MCP_IT=1 and PG* variables pointing at PostgreSQL")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def english() -> Iterator[None]:
    """Every test starts in English and leaves no other language behind, even if main() switched it."""
    with i18n.speaking("en"):
        yield


@pytest.fixture
def russian() -> Iterator[None]:
    """Russian for the length of one test."""
    with i18n.speaking("ru"):
        yield


@pytest.fixture
def unreachable_env() -> dict[str, str]:
    """PG* variables that point at a local port where nothing listens."""
    return unreachable_database_env()
