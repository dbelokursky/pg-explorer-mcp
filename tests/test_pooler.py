"""A connection pooler in transaction mode between the server and PostgreSQL, as a stand may have.

pgbouncer here serves each transaction from any server session and resets the session in between
(DISCARD ALL), so what the server SET when it connected is gone by the next call, and a prepared
statement does not live from one message to the next. The tools must work all the same: every call is
one transaction that carries its own limits (db.transaction_start). Needs pgbouncer on PATH besides
PG_EXPLORER_MCP_IT=1 and the PG* variables; skipped otherwise.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
from collections.abc import Iterator

import pg8000.native
import pytest

from pg_explorer_mcp.db import Database, Settings
from pg_explorer_mcp.tools import Tool, build_tools
from tests.helpers import ROOT

pytestmark = pytest.mark.integration

SCHEMA = "mcp_it_pool"


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture(scope="module")
def pooler(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, str]]:
    """PG* variables that point at a pgbouncer in transaction mode in front of the test database."""
    if shutil.which("pgbouncer") is None:
        pytest.skip("needs pgbouncer on PATH")
    settings = Settings.from_env(os.environ)
    folder = tmp_path_factory.mktemp("pgbouncer")
    port = free_port()
    userlist = folder / "userlist.txt"
    userlist.write_text(f'"{settings.user}" "{settings.password or ""}"\n', encoding="utf-8")
    ini = folder / "pgbouncer.ini"
    ini.write_text(
        f"[databases]\n* = host={settings.host} port={settings.port}\n\n"
        "[pgbouncer]\nlisten_addr = 127.0.0.1\n"
        f"listen_port = {port}\nunix_socket_dir =\nauth_type = trust\nauth_file = {userlist}\n"
        "pool_mode = transaction\nmax_prepared_statements = 0\n"
        "server_reset_query = DISCARD ALL\nserver_reset_query_always = 1\n"
        f"logfile = {folder / 'pgbouncer.log'}\npidfile = {folder / 'pgbouncer.pid'}\n",
        encoding="utf-8",
    )
    process = subprocess.Popen(["pgbouncer", str(ini)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            output = process.stdout.read().decode("utf-8", "replace") if process.stdout else ""
            pytest.skip(f"pgbouncer did not start: {output.strip()[-500:]}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                break
        except OSError:
            time.sleep(0.2)
    env = {
        "PGHOST": "127.0.0.1",
        "PGPORT": str(port),
        "PGDATABASE": settings.database,
        "PGUSER": settings.user,
        "PGSSLMODE": "disable",
        "PGPASSFILE": settings.passfile,
        "PG_EXPLORER_MCP_SEARCH_PATH": SCHEMA,
        "PG_EXPLORER_MCP_LANG": "en",
    }
    if settings.password:
        env["PGPASSWORD"] = settings.password
    try:
        yield env
    finally:
        process.terminate()
        process.wait(timeout=10)


@pytest.fixture(scope="module")
def schema() -> Iterator[None]:
    """A schema with one table, made straight on PostgreSQL."""
    settings = Settings.from_env(os.environ)
    admin = pg8000.native.Connection(
        user=settings.user,
        password=settings.password,
        host=settings.host,
        port=settings.port,
        database=settings.database,
    )
    admin.run(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    admin.run(f"CREATE SCHEMA {SCHEMA}")
    admin.run(f"CREATE TABLE {SCHEMA}.pooled (id int PRIMARY KEY, note text)")
    admin.run(f"INSERT INTO {SCHEMA}.pooled VALUES (1, 'a'), (2, 'b'), (3, 'c')")
    try:
        yield
    finally:
        admin.run(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        admin.close()


@pytest.fixture(scope="module")
def tools(pooler: dict[str, str], schema: None) -> Iterator[dict[str, Tool]]:  # noqa: ARG001
    database = Database(pooler)
    yield {tool.name: tool for tool in build_tools(database, str(ROOT), profile="analyst")}
    database.close()


def test_the_pooler_really_forgets_the_session_between_transactions(pooler: dict[str, str]) -> None:
    """Otherwise the tests below would prove nothing."""
    conn = pg8000.native.Connection(
        user=pooler["PGUSER"],
        password=pooler.get("PGPASSWORD"),
        host=pooler["PGHOST"],
        port=int(pooler["PGPORT"]),
        database=pooler["PGDATABASE"],
        ssl_context=False,
    )
    try:
        conn.run("SET statement_timeout = 7")
        assert conn.run("SHOW statement_timeout")[0][0] == "0"  # DISCARD ALL ran in between
    finally:
        conn.close()


def test_every_tool_works_and_the_limits_hold(tools: dict[str, Tool]) -> None:
    status = tools["status"].call({})
    assert "database: connected to" in status
    assert "read only = on" in status
    assert "statement timeout = 5s" in status
    assert f"{SCHEMA}.pooled | table" in tools["list_tables"].call({"schema": SCHEMA})
    assert "id integer NOT NULL" in tools["describe_table"].call({"table": "pooled"})
    assert "(3 rows)" in tools["sample_rows"].call({"table": "pooled"})
    # An unqualified name needs the schema first on the search path of this very transaction.
    assert tools["run_select"].call({"query": "SELECT count(*) FROM pooled"}).splitlines()[1] == "3"
    assert tools["run_select"].call({"query": "SHOW statement_timeout"}).splitlines()[1] == "5s"
    assert "Seq Scan" in tools["explain_query"].call({"query": "SELECT * FROM pooled"})
