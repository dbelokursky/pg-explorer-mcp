"""The export against a real client: psql connects through the service file install.py export writes.

Needs PostgreSQL where the PG* role may create a schema, and psql on PATH; the schema mcp_it_export
is made and dropped here.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pg8000.native
import pytest

from pg_explorer_mcp import exports
from pg_explorer_mcp.db import Settings

pytestmark = pytest.mark.integration

SCHEMA = "mcp_it_export"


@pytest.fixture
def settings() -> Iterator[Settings]:
    settings = Settings.from_env(os.environ)
    admin = pg8000.native.Connection(
        user=settings.user,
        password=settings.password,
        host=settings.host,
        port=settings.port,
        database=settings.database,
    )
    try:
        admin.run(f"DROP SCHEMA IF EXISTS {SCHEMA}")
        admin.run(f"CREATE SCHEMA {SCHEMA}")
        yield settings
    finally:
        admin.run(f"DROP SCHEMA IF EXISTS {SCHEMA}")
        admin.close()


def test_psql_connects_through_the_exported_service_with_the_schema_first(settings: Settings, tmp_path: Path) -> None:
    psql = shutil.which("psql")
    if psql is None:
        pytest.skip("needs psql on PATH")
    name = f"dev1.{settings.database}.{SCHEMA}"
    registry = tmp_path / "stands.conf"
    registry.write_text(
        f"[{name}]\nhost={settings.host}\nport={settings.port}\ndbname={settings.database}\nuser={settings.user}\n",
        encoding="utf-8",
    )
    service = tmp_path / "pg_service.conf"
    env = {"PG_EXPLORER_MCP_STANDS_FILE": str(registry), "PGSERVICEFILE": str(service)}
    items, _ = exports.connections(env)
    exports.write(exports.prepare("pg-service", items, env, tmp_path))
    # Only the service names the server: what libpq would take from the shell is left out.
    shell = {key: value for key, value in os.environ.items() if key not in ("PGHOST", "PGPORT", "PGDATABASE", "PGUSER")}
    answer = subprocess.run(
        [psql, f"service={name}", "-XAt", "-c", "SELECT current_database(), current_setting('search_path')"],
        env=dict(shell, PGSERVICEFILE=str(service)),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert answer.returncode == 0, answer.stderr
    assert answer.stdout.strip() == f"{settings.database}|{SCHEMA},public"  # as the export spells it
