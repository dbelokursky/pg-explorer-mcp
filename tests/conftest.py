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
import shutil
import subprocess
import tarfile
from collections.abc import Iterator, Mapping
from contextlib import suppress
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest

import pg_explorer_mcp  # noqa: F401  (puts the vendored packages, pg8000 among them, on sys.path)
from pg_explorer_mcp import i18n
from tests.helpers import PsqlFile, unreachable_database_env

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
    try:  # 4.15 moved the module; 4.13, the one Python 3.9 resolves to, has only the old one
        from testcontainers.community.postgres import PostgresContainer  # noqa: PLC0415  (dev-only, and only here)
    except ImportError:
        try:
            from testcontainers.postgres import PostgresContainer  # noqa: PLC0415
        except ImportError:  # a checkout without dev dependencies: the skips explain the rest
            return False
    try:
        container = PostgresContainer(
            os.environ.get("PG_EXPLORER_MCP_IT_IMAGE", DEFAULT_IMAGE),
            # The tests create databases and roles, so the superuser it is; the password is this
            # throwaway container's own, not a secret anywhere.
            username="postgres",
            password="postgres",  # noqa: S106
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


@pytest.fixture(scope="session")
def own_postgres() -> object | None:
    """The container this run started, or None when the database came from outside.

    Tests that need a program beside PostgreSQL find it in that container; an external
    database keeps them on the host, as it always did. pytest_unconfigure stops it.
    """
    return _started


@pytest.fixture(scope="session")
def run_psql_file(own_postgres: object | None) -> Iterator[PsqlFile | None]:
    """A way to run a SQL file with psql the way a DBA would; None when there is no way.

    With this run's own container, psql runs inside it — the version matching the server, over
    the local socket, the file copied in. With an external database, the host's psql connects
    the way the PG* variables say. Neither being there is the caller's skip to explain.
    """
    if own_postgres is not None:

        def inside(sql: Path, variables: Mapping[str, str]) -> None:
            wrapped = own_postgres.get_wrapped_container()  # type: ignore[attr-defined]
            _copy_into(wrapped, f"/tmp/{sql.name}", sql.read_bytes())  # noqa: S108  (the container's /tmp)
            flags = [f"-v{name}={value}" for name, value in variables.items()]
            code, (_out, err) = wrapped.exec_run(
                ["psql", "-X", "-q", *flags, "-f", f"/tmp/{sql.name}"],  # noqa: S108  (the container's /tmp, not the host's)
                environment={  # exec runs as root: the role is named, and it is the container's superuser
                    "PGUSER": "postgres",
                    "PGDATABASE": os.environ.get("PGDATABASE", "postgres"),
                },
                demux=True,
            )
            if code != 0:
                tail = (err or b"").decode("utf-8", "replace").strip()[-2000:]
                raise AssertionError(f"psql in the container failed ({code}): {tail}")

        yield inside
        return

    if shutil.which("psql") is None:
        yield None
        return

    def on_host(sql: Path, variables: Mapping[str, str]) -> None:
        flags = [f"-v{name}={value}" for name, value in variables.items()]
        done = subprocess.run(["psql", "-X", "-q", *flags, "-f", str(sql)], capture_output=True, check=False)
        if done.returncode != 0:
            tail = done.stderr.decode("utf-8", "replace").strip()[-2000:]
            raise AssertionError(f"psql failed ({done.returncode}): {tail}")

    yield on_host


def _copy_into(wrapped: Any, path: str, data: bytes) -> None:
    """A file into the container, the way the Docker API takes files: as a tar stream."""
    stream = BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        info = tarfile.TarInfo(name=path.rsplit("/", 1)[-1])
        info.size = len(data)
        archive.addfile(info, BytesIO(data))
    wrapped.put_archive("/tmp", stream.getvalue())  # noqa: S108  (the container's /tmp)
