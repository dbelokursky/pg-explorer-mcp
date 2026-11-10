"""Catalog SQL against PostgreSQL, including an ordinary role without monitoring grants."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from typing import Any

import pytest

from pg_explorer_mcp import health
from pg_explorer_mcp.db import Database, QueryFailedError, time_budget
from pg_explorer_mcp.tools import db_health
from tests.helpers import MODERN_META, rpc, run_stdio_session
from tests.test_db import connect

pytestmark = pytest.mark.integration
SCHEMA = "mcp_it_health"
HIDDEN = "mcp_it_health_hidden"
ROLE = "mcp_it_health_reader"


@pytest.fixture(scope="module")
def reader(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Database]:
    admin = connect()
    statements = [
        f"CREATE ROLE {ROLE} LOGIN PASSWORD 'health-test'",
        f"CREATE SCHEMA {SCHEMA}",
        f"CREATE SCHEMA {HIDDEN}",
        f"CREATE TABLE {SCHEMA}.items (id serial PRIMARY KEY, a int, b int NOT NULL, label text)",
        f"CREATE INDEX first_idx ON {SCHEMA}.items (a,b)",
        f"CREATE INDEX copy_idx ON {SCHEMA}.items (a,b)",
        f"CREATE INDEX mixed_sort ON {SCHEMA}.items (a DESC,b)",
        f"CREATE INDEX included ON {SCHEMA}.items (a) INCLUDE (b)",
        f"CREATE INDEX filtered ON {SCHEMA}.items (a,b) WHERE a > 0",
        f'CREATE INDEX text_c ON {SCHEMA}.items (label COLLATE "C")',
        f'CREATE INDEX text_posix ON {SCHEMA}.items (label COLLATE "POSIX")',
        f"CREATE INDEX expr1 ON {SCHEMA}.items ((a+1))",
        f"CREATE INDEX expr2 ON {SCHEMA}.items ((a+2))",
        f"CREATE UNIQUE INDEX replica_key ON {SCHEMA}.items (b)",
        f"ALTER TABLE {SCHEMA}.items REPLICA IDENTITY USING INDEX replica_key",
        f"CREATE UNIQUE INDEX spare_id ON {SCHEMA}.items (id)",
        f"CREATE TABLE {SCHEMA}.parts (id serial) PARTITION BY RANGE(id)",
        f"CREATE TABLE {SCHEMA}.part1 PARTITION OF {SCHEMA}.parts FOR VALUES FROM (0) TO (10)",
        f"CREATE TABLE {HIDDEN}.secret (id serial, payload text)",
        f"CREATE INDEX secret_idx ON {HIDDEN}.secret (id)",
        f"CREATE INDEX secret_copy ON {HIDDEN}.secret (id)",
        f"GRANT USAGE ON SCHEMA {SCHEMA} TO {ROLE}",
        f"GRANT SELECT ON ALL TABLES IN SCHEMA {SCHEMA} TO {ROLE}",
        f"INSERT INTO {SCHEMA}.items (a,b,label) SELECT i,i,repeat(md5(i::text),100) FROM generate_series(1,100) i",
        f"ANALYZE {SCHEMA}.items",
        f"CREATE SEQUENCE {SCHEMA}.down_seq INCREMENT -3 MINVALUE -100 MAXVALUE -1 START -1 CACHE 4",
        f"SELECT nextval('{SCHEMA}.down_seq')",
        f"CREATE SEQUENCE {SCHEMA}.up_seq MINVALUE 10 MAXVALUE 100 START 10 INCREMENT 3 CACHE 4",
        f"SELECT nextval('{SCHEMA}.up_seq')",
        f"CREATE SEQUENCE {SCHEMA}.cycle_seq MAXVALUE 100 CYCLE",
        f"SELECT nextval('{SCHEMA}.cycle_seq')",
        f"CREATE SEQUENCE {SCHEMA}.new_seq",
        f"GRANT SELECT ON SEQUENCE {SCHEMA}.down_seq, {SCHEMA}.up_seq, {SCHEMA}.cycle_seq, {SCHEMA}.new_seq TO {ROLE}",
    ]
    database = None
    try:
        for sql in statements:
            admin.run(sql)
        passfile = tmp_path_factory.mktemp("health") / "pgpass"
        passfile.write_text(f"*:*:*:{ROLE}:health-test\n", encoding="utf-8")
        passfile.chmod(0o600)
        env = dict(os.environ, PGUSER=ROLE, PGPASSFILE=str(passfile))
        env.pop("PGPASSWORD", None)  # CI's administrator password must not override the reader's passfile.
        database = Database(env)
        yield database
    finally:
        if database is not None:
            database.close()
        admin.run(f"DROP SCHEMA IF EXISTS {SCHEMA}, {HIDDEN} CASCADE")
        admin.run(f"DROP ROLE IF EXISTS {ROLE}")
        admin.close()


def rows(reader: Database, sql: str, schemas: list[str] | None = None) -> dict[str, Any]:
    return {
        str(name): metrics
        for name, metrics in reader.query(health.SCOPE + sql, schema=None, schemas=schemas, limit=100)
    }


def test_all_checks_work_without_monitoring_roles_and_respect_grants(reader: Database) -> None:
    assert reader.query(
        "SELECT pg_has_role(current_user, 'pg_monitor', 'MEMBER'), "
        "pg_has_role(current_user, 'pg_read_all_stats', 'MEMBER')"
    )[0] == [False, False]
    text = "\n".join(db_health(reader))
    assert "=unavailable" not in text
    assert "=not_completed" not in text
    assert "budget exhausted" not in text
    assert HIDDEN not in text
    assert f"{SCHEMA}.items" in text
    assert "no_sequence_privilege" in text
    cache = rows(reader, health.CACHE_SQL)["buffers"]
    assert cache["tables"] == 2  # items and physical partition; parent has no storage
    assert "heap_hit_pct" in cache


def test_duplicate_candidates_distinguish_semantically_different_indexes(reader: Database) -> None:
    duplicates = reader.query(health.SCOPE + health.DUPLICATE_SQL, schema=SCHEMA, schemas=None, limit=100)
    assert len(duplicates) == 2
    pair = next(row[1] for row in duplicates if row[1]["first"].endswith(".first_idx"))
    protected = next(row[1] for row in duplicates if row[1]["first"].endswith(".items_pkey"))
    assert protected["first_protected"]
    assert protected["second_protected"]
    assert {pair["first"], pair["second"]} == {f"{SCHEMA}.first_idx", f"{SCHEMA}.copy_idx"}
    assert not pair["first_protected"]
    assert not pair["second_protected"]
    unused = rows(reader, health.UNUSED_SQL)
    assert f"{SCHEMA}.items_pkey" not in unused
    assert f"{SCHEMA}.replica_key" not in unused
    assert f"{SCHEMA}.spare_id" not in unused


def test_sequences_report_missing_grants_direction_cache_and_cycle(reader: Database) -> None:
    sequences = rows(reader, health.SEQUENCE_SQL)
    assert sequences[f"{SCHEMA}.items_id_seq"]["state"] == "no_sequence_privilege"
    assert sequences[f"{SCHEMA}.parts_id_seq"]["state"] == "no_sequence_privilege"
    assert sequences[f"{SCHEMA}.items_id_seq"]["last_reserved_value"] is None
    assert sequences[f"{SCHEMA}.down_seq"]["last_reserved_value"] == -10
    assert sequences[f"{SCHEMA}.down_seq"]["remaining_calls_lower_bound"] == 30
    assert sequences[f"{SCHEMA}.up_seq"]["last_reserved_value"] == 19
    assert sequences[f"{SCHEMA}.up_seq"]["remaining_calls_lower_bound"] == 27
    assert sequences[f"{SCHEMA}.cycle_seq"]["remaining_calls_lower_bound"] is None
    assert sequences[f"{SCHEMA}.new_seq"]["state"] == "not_used_or_unlogged_on_replica"
    assert not any(HIDDEN in name for name in sequences)


def test_sizes_do_not_label_toast_as_indexes_and_do_not_count_parent_twice(reader: Database) -> None:
    sizes = rows(reader, health.SIZE_SQL)
    item = sizes[f"{SCHEMA}.items"]
    assert item["total_bytes"] == item["table_including_toast_bytes"] + item["indexes_bytes"]
    assert f"{SCHEMA}.part1" in sizes
    assert f"{SCHEMA}.parts" not in sizes
    assert not any(HIDDEN in name for name in sizes)


def test_registry_scope_is_used_instead_of_login_table_grants(reader: Database) -> None:
    sizes = rows(reader, health.SIZE_SQL, [HIDDEN])
    assert set(sizes) == {f"{HIDDEN}.secret"}
    assert len(rows(reader, health.DUPLICATE_SQL, [HIDDEN])) == 1
    assert set(rows(reader, health.SEQUENCE_SQL, [HIDDEN])) == {f"{HIDDEN}.secret_id_seq"}
    assert rows(reader, health.CACHE_SQL, ["nonexistent"])["buffers"]["heap_hit_pct"] is None


def test_deadline_cancels_query_and_next_transaction_still_works(reader: Database) -> None:
    with time_budget(0.05), pytest.raises(QueryFailedError) as exc:
        reader.query("SELECT pg_sleep(1)")
    assert exc.value.code == "57014"
    assert reader.query("SELECT 42")[0][0] == 42


def test_schema_filter_and_empty_statistics(reader: Database) -> None:
    empty = reader.query(health.SCOPE + health.MAINTENANCE_SQL, schema="absent", schemas=None, limit=10)
    assert empty == []
    maintenance = rows(reader, health.MAINTENANCE_SQL)
    partition = maintenance[f"{SCHEMA}.part1"]
    assert partition["dead_estimate"] == 0
    assert partition["dead_pct_estimate"] is None
    assert partition["last_vacuum"] is None
    assert "SQLSTATE" not in "\n".join(db_health(reader, "absent' OR true --"))


def test_sequence_limit_prioritizes_known_exhaustion_and_counts_unknowns(reader: Database) -> None:
    admin = connect()
    created = [f"{SCHEMA}.unknown_{i}" for i in range(6)]
    urgent = f"{SCHEMA}.almost_done"
    try:
        for name in created:
            admin.run(f"CREATE SEQUENCE {name} OWNED BY {SCHEMA}.items.a")
        admin.run(f"CREATE SEQUENCE {urgent} MAXVALUE 100")
        admin.run(f"SELECT setval('{urgent}', 99)")
        admin.run(f"GRANT SELECT ON SEQUENCE {urgent} TO {ROLE}")
        result = reader.query(health.SCOPE + health.SEQUENCE_SQL, schema=SCHEMA, schemas=None, limit=health.LISTED)
        assert result[0][0] == urgent
        assert result[0][1]["remaining_calls_lower_bound"] == 1
        assert result[0][1]["unknown_values_in_scope"] >= len(created)
    finally:
        for name in [*created, urgent]:
            admin.run(f"DROP SEQUENCE IF EXISTS {name}")
        admin.close()


@pytest.mark.parametrize("language", ["en", "ru"])
def test_health_is_callable_over_mcp_stdio_with_ordinary_role(reader: Database, language: str) -> None:
    settings = reader.settings
    assert settings is not None
    env = {key: value for key, value in os.environ.items() if not key.startswith("PG")}
    env.update(
        PGHOST=settings.host,
        PGPORT=str(settings.port),
        PGDATABASE=settings.database,
        PGUSER=settings.user,
        PGPASSFILE=settings.passfile,
        PGSSLMODE=settings.sslmode,
        PG_EXPLORER_MCP_LANG=language,
        PG_EXPLORER_MCP_PROFILE="explore",
    )
    lines, process = run_stdio_session(
        [
            rpc(1, "tools/list", MODERN_META),
            rpc(2, "tools/call", dict(MODERN_META, name="db_health", arguments={"schema": SCHEMA})),
        ],
        env=env,
    )
    assert process.returncode == 0
    listed, called = (json.loads(line)["result"] for line in lines)
    assert "db_health" in [tool["name"] for tool in listed["tools"]]
    assert called["isError"] is False
    answer = called["content"][0]["text"]
    assert len(answer) <= 8000
    assert "sizes=ok" in answer
    assert HIDDEN not in answer
    assert "unknown_values_in_scope" in answer
    assert "no_sequence_privilege" in answer
