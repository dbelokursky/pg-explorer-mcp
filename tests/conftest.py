"""Test configuration: the integration marker, the PostgreSQL it may start itself, shared fixtures.

Integration tests need a real PostgreSQL. One is there when PG_EXPLORER_MCP_IT=1 and the PG*
variables point at it (the way CI does); without the variable, a Docker that is running gets a
postgres:16 container for the length of the run, so a plain `uv run pytest` tests everything.
PG_EXPLORER_MCP_IT=0 keeps the skips, and PGHOST set without the variable does too: a database
of one's own is opted into, not guessed at. The image is PG_EXPLORER_MCP_IT_IMAGE's, postgres:16
by default, so `PG_EXPLORER_MCP_IT_IMAGE=postgres:13 uv run pytest -m integration` tries another
version.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import suppress

import pytest

import pg_explorer_mcp  # noqa: F401  (puts the vendored packages, pg8000 among them, on sys.path)
from pg_explorer_mcp import i18n
from tests.helpers import unreachable_database_env

IT_VARIABLES = ("PG_EXPLORER_MCP_IT", "PG_MCP_IT")  # the second name is the one before 0.8.0
DEFAULT_IMAGE = "postgres:16"
INTEGRATION = any(os.environ.get(name) == "1" for name in IT_VARIABLES)
_started: object | None = None  # the container this run started, stopped when the run ends


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:  # noqa: ARG001
    """Skip tests marked `integration` unless a PostgreSQL is there, or Docker can start one."""
    if not any("integration" in item.keywords for item in items):
        return  # a unit-only run starts nothing
    global INTEGRATION  # noqa: PLW0603
    if not INTEGRATION and not any(os.environ.get(name) for name in IT_VARIABLES) and not os.environ.get("PGHOST"):
        INTEGRATION = _start_postgres()  # nobody named a database: Docker's is the least surprise
    if INTEGRATION:
        return
    skip = pytest.mark.skip(
        reason="needs PostgreSQL: PG_EXPLORER_MCP_IT=1 with PG* variables pointing at it, "
        "or a running Docker to start postgres:16 for the run"
    )
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)


def pytest_unconfigure(config: pytest.Config) -> None:  # noqa: ARG001
    if _started is not None:
        with suppress(Exception):  # the session is over either way; Ryuk would take it too
            _started.stop()  # type: ignore[attr-defined]


def _start_postgres() -> bool:
    """Start the container the tests will read, and point the PG* variables at it.

    Returns False, leaving everything as it was, when Docker is not there or cannot start it.
    """
    global _started  # noqa: PLW0603
    try:
        from testcontainers.postgres import PostgresContainer  # noqa: PLC0415  (dev-only, and only here)
    except ImportError:  # a checkout without dev dependencies: the skips explain the rest
        return False
    try:
        container = PostgresContainer(
            os.environ.get("PG_EXPLORER_MCP_IT_IMAGE", DEFAULT_IMAGE),
            # The tests create databases and roles, so the superuser it is.
            username="postgres",
            password="postgres",
            dbname="postgres",
        )
        container.start()
    except Exception:
        return False
    _started = container
    os.environ.update(
        PGHOST=str(container.get_container_host_ip()),
        PGPORT=str(container.get_exposed_port(container.port)),
        PGUSER="postgres",
        PGPASSWORD="postgres",
        PGDATABASE="postgres",
    )
    return True


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
