"""Bounded catalog diagnostics, with no extensions, query texts or monitoring grants.

Statistics are evidence, not a diagnosis. Every section is independently readable and reports failure;
rows are scoped before aggregation. Partition leaves have storage; parents are deliberately omitted.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from .db import Database, DatabaseUnavailableError, OverdueError, QueryFailedError, time_budget
from .i18n import t

LISTED = 5
TIME_BUDGET_S = 15

# A registry's schema logins share the catalog. Without a registry, honor the login's own grants.
SCOPE = r"""
WITH visible_relations AS (
    SELECT c.oid, c.relname, c.relnamespace, c.relkind, n.nspname
    FROM pg_catalog.pg_class c
    JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relkind IN ('r', 'm', 'p')
      AND n.nspname <> 'information_schema' AND n.nspname NOT LIKE 'pg\_%'
      AND (CAST(:schema AS text) IS NULL OR n.nspname = :schema)
      AND CASE WHEN CAST(:schemas AS text[]) IS NULL
          THEN has_table_privilege(c.oid, 'SELECT') AND has_schema_privilege(n.oid, 'USAGE')
          ELSE n.nspname = ANY(CAST(:schemas AS text[])) END
), relations AS (
    SELECT * FROM visible_relations WHERE relkind <> 'p'
)
"""
CONTEXT_SQL = """
SELECT current_database(), json_build_object(
    'collected_at', clock_timestamp(), 'server_version', current_setting('server_version'),
    'server_started_at', pg_postmaster_start_time(), 'replica', pg_is_in_recovery(),
    'track_counts', current_setting('track_counts'),
    'database_stats_reset', (SELECT stats_reset FROM pg_catalog.pg_stat_database WHERE datname = current_database()))
"""
CACHE_SQL = """
SELECT 'buffers', json_build_object(
    'tables', count(*), 'heap_hits', coalesce(sum(s.heap_blks_hit), 0),
    'heap_reads', coalesce(sum(s.heap_blks_read), 0),
    'heap_hit_pct', round(100.0 * sum(s.heap_blks_hit) / nullif(sum(s.heap_blks_hit + s.heap_blks_read), 0), 2),
    'index_hits', coalesce(sum(s.idx_blks_hit), 0), 'index_reads', coalesce(sum(s.idx_blks_read), 0),
    'index_hit_pct', round(100.0 * sum(s.idx_blks_hit) / nullif(sum(s.idx_blks_hit + s.idx_blks_read), 0), 2))
FROM relations r JOIN pg_catalog.pg_statio_user_tables s ON s.relid = r.oid
"""
MAINTENANCE_SQL = """
SELECT format('%I.%I', r.nspname, r.relname), json_build_object(
    'live_estimate', s.n_live_tup, 'dead_estimate', s.n_dead_tup,
    'dead_pct_estimate', round(100.0 * s.n_dead_tup / nullif(s.n_live_tup::numeric + s.n_dead_tup, 0), 2),
    'changes_since_analyze', s.n_mod_since_analyze,
    'last_vacuum', greatest(s.last_vacuum, s.last_autovacuum),
    'last_analyze', greatest(s.last_analyze, s.last_autoanalyze))
FROM relations r JOIN pg_catalog.pg_stat_user_tables s ON s.relid = r.oid
ORDER BY s.n_dead_tup DESC, s.n_mod_since_analyze DESC, r.oid
LIMIT :limit
"""
UNUSED_SQL = """
SELECT format('%I.%I', r.nspname, c.relname), json_build_object(
    'table', format('%I.%I', r.nspname, r.relname), 'bytes', pg_relation_size(c.oid), 'idx_scan', s.idx_scan)
FROM relations r
JOIN pg_catalog.pg_index i ON i.indrelid = r.oid
JOIN pg_catalog.pg_class c ON c.oid = i.indexrelid
JOIN pg_catalog.pg_stat_user_indexes s ON s.indexrelid = i.indexrelid
WHERE s.idx_scan = 0 AND i.indisvalid AND i.indisready
  AND NOT i.indisprimary AND NOT i.indisunique AND NOT i.indisexclusion AND NOT i.indisreplident
  AND NOT EXISTS (SELECT FROM pg_catalog.pg_constraint con WHERE con.conindid = i.indexrelid)
ORDER BY pg_relation_size(c.oid) DESC, c.oid
LIMIT :limit
"""
DUPLICATE_SQL = """
SELECT format('%I.%I', r.nspname, r.relname), json_build_object(
    'first', format('%I.%I', r.nspname, a.relname), 'second', format('%I.%I', r.nspname, b.relname),
    'first_bytes', pg_relation_size(a.oid), 'second_bytes', pg_relation_size(b.oid),
    'first_protected', i.indisprimary OR i.indisunique OR i.indisexclusion OR i.indisreplident
        OR EXISTS (SELECT FROM pg_catalog.pg_constraint con WHERE con.conindid = i.indexrelid),
    'second_protected', j.indisprimary OR j.indisunique OR j.indisexclusion OR j.indisreplident
        OR EXISTS (SELECT FROM pg_catalog.pg_constraint con WHERE con.conindid = j.indexrelid))
FROM relations r
JOIN pg_catalog.pg_index i ON i.indrelid = r.oid
JOIN pg_catalog.pg_index j ON j.indrelid = i.indrelid AND j.indexrelid > i.indexrelid
  AND j.indkey = i.indkey AND j.indclass = i.indclass AND j.indcollation = i.indcollation
  AND j.indoption = i.indoption AND j.indnkeyatts = i.indnkeyatts
  AND j.indisunique = i.indisunique AND j.indisexclusion = i.indisexclusion AND j.indimmediate = i.indimmediate
  AND (to_jsonb(j)->'indnullsnotdistinct') IS NOT DISTINCT FROM (to_jsonb(i)->'indnullsnotdistinct')
  AND j.indexprs::text IS NOT DISTINCT FROM i.indexprs::text
  AND j.indpred::text IS NOT DISTINCT FROM i.indpred::text
JOIN pg_catalog.pg_class a ON a.oid = i.indexrelid
JOIN pg_catalog.pg_class b ON b.oid = j.indexrelid AND b.relam = a.relam
WHERE i.indisvalid AND j.indisvalid AND i.indisready AND j.indisready
ORDER BY pg_relation_size(a.oid) + pg_relation_size(b.oid) DESC, a.oid, b.oid
LIMIT :limit
"""
SEQUENCE_SQL = r"""
, sequences AS (
    SELECT s.*, has_sequence_privilege(c.oid, 'SELECT,USAGE') AS readable
    FROM pg_catalog.pg_sequences s
    JOIN pg_catalog.pg_namespace n ON n.nspname = s.schemaname
    JOIN pg_catalog.pg_class c ON c.relnamespace = n.oid AND c.relname = s.sequencename AND c.relkind = 'S'
    WHERE n.nspname <> 'information_schema' AND n.nspname NOT LIKE 'pg\_%'
      AND (CAST(:schema AS text) IS NULL OR n.nspname = :schema)
      AND CASE WHEN CAST(:schemas AS text[]) IS NULL THEN
          (has_schema_privilege(n.oid, 'USAGE') AND has_sequence_privilege(c.oid, 'SELECT,USAGE'))
          OR EXISTS (SELECT FROM pg_catalog.pg_depend d JOIN visible_relations r ON r.oid = d.refobjid
                     WHERE d.classid = 'pg_catalog.pg_class'::regclass AND d.objid = c.oid
                       AND d.refclassid = 'pg_catalog.pg_class'::regclass AND d.deptype IN ('a', 'i'))
          ELSE n.nspname = ANY(CAST(:schemas AS text[])) END
), remaining AS (
    SELECT *, CASE WHEN increment_by > 0 THEN max_value::numeric - last_value
                   ELSE last_value::numeric - min_value END AS distance
    FROM sequences
)
SELECT format('%I.%I', schemaname, sequencename), json_build_object(
    'unknown_values_in_scope', count(*) FILTER (WHERE last_value IS NULL) OVER (),
    'last_reserved_value', last_value, 'min', min_value, 'max', max_value, 'increment', increment_by,
    'cycle', cycle, 'cache_size', cache_size,
    'remaining_calls_lower_bound', CASE WHEN NOT cycle THEN floor(distance / abs(increment_by::numeric)) END,
    'remaining_range_pct', CASE WHEN NOT cycle THEN round(100.0 * distance / (max_value::numeric - min_value), 2) END,
    'state', CASE WHEN NOT readable THEN 'no_sequence_privilege'
                  WHEN last_value IS NULL THEN 'not_used_or_unlogged_on_replica'
                  WHEN cycle THEN 'cycles' ELSE 'bounded' END)
FROM remaining
ORDER BY CASE WHEN last_value IS NOT NULL AND NOT cycle THEN 0 WHEN last_value IS NULL THEN 1 ELSE 2 END,
         distance / (max_value::numeric - min_value), schemaname, sequencename
LIMIT :limit
"""
SIZE_SQL = """
SELECT format('%I.%I', r.nspname, r.relname), json_build_object(
    'total_bytes', pg_total_relation_size(r.oid), 'table_including_toast_bytes', pg_table_size(r.oid),
    'indexes_bytes', pg_indexes_size(r.oid))
FROM relations r
ORDER BY pg_total_relation_size(r.oid) DESC, r.oid
LIMIT :limit
"""
SECTIONS = (
    ("health.context", CONTEXT_SQL),
    ("health.cache", CACHE_SQL),
    ("health.maintenance", MAINTENANCE_SQL),
    ("health.unused", UNUSED_SQL),
    ("health.duplicates", DUPLICATE_SQL),
    ("health.sequences", SEQUENCE_SQL),
    ("health.sizes", SIZE_SQL),
)


def report(db: Database, schema: str | None, schemas: Sequence[str] | None) -> list[str]:
    """Return partial evidence even if a section is denied or exceeds the remaining query budget."""
    lines = [t("health.intro"), t("health.scope", schema=schema or "*"), t("health.limits")]
    checks = {title.removeprefix("health."): "not_completed" for title, _sql in SECTIONS}
    with time_budget(TIME_BUDGET_S):
        for title, sql in SECTIONS:
            lines.append(t(title))
            check = title.removeprefix("health.")
            try:
                rows = (
                    db.query(sql)
                    if sql == CONTEXT_SQL
                    else db.query(
                        SCOPE + sql,
                        schema=schema,
                        schemas=list(schemas) if schemas is not None else None,
                        limit=LISTED + 1,
                    )
                )
            except OverdueError:
                lines.append(t("health.budget"))
                break
            except QueryFailedError as exc:
                # SQLSTATE explains failure without accidentally returning names outside the selected scope.
                checks[check] = f"unavailable[{exc.code or 'unknown'}]"
                lines.append(t("health.unavailable", code=exc.code or "unknown"))
                continue
            except DatabaseUnavailableError:
                checks[check] = "unavailable"
                lines.append(t("health.disconnected"))
                break
            checks[check] = "limited" if len(rows) > LISTED else "ok"
            lines.extend(_rows(rows))
    # Keep failure/coverage information ahead of details: the shared response budget trims the tail.
    summary = "; ".join(f"{name}={state}" for name, state in checks.items())
    return [t("health.checks", checks=summary), *lines]


def _rows(rows: list[Sequence[Any]]) -> list[str]:
    if not rows:
        return [t("health.empty")]
    result = [f"  {name}: {json.dumps(metrics, ensure_ascii=False, default=str)}" for name, metrics in rows[:LISTED]]
    if len(rows) > LISTED:
        result.append(t("health.more", limit=LISTED))
    return result
