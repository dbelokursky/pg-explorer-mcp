# ADR 0009. Database health from catalogs

Status: accepted, 2026-11-08.

## Context

Without a concrete query to explain, developers need a first pass over maintenance, storage and index
usage. `analyst` can query catalogs manually, but `explore` needs a bounded tool with checked semantics.
The role policy forbids `pg_monitor` and `pg_read_all_stats`, since those expose other sessions' query texts.

## Decision

Expose `db_health` in `explore` and `analyst`, with optional `schema`. Stand mode requires `stand` and
accepts `database` and `schema`. The existing catalog routing chooses the database/login and scopes
results to the registry's schemas or, for one login, to readable relations. Filter before aggregation.
No query texts, application row scans, extensions, repairs or additional monitoring grants.

A separate `health.py` owns fixed parameterized SQL and report assembly. Each check runs in its own
read-only transaction, rolled back by `Database.query`. This allows partial results after SQL errors.
Checks share a 15-second query deadline, including across databases of one stand. The remaining budget
caps statement timeout and waiting for a busy connection; context tokens restore an outer deadline.
Connection/transport timeouts remain separate, so this is not a hard wall-clock response guarantee.
Each section returns five rows plus one sentinel for truncation; the normal answer budget also applies.
A compact per-check collection status precedes the details and survives ordinary tail truncation.
SQLSTATE is reported without raw error text, which could disclose objects outside the requested scope.

Checks:

- Context: collection time, version, server start, primary/replica, track_counts, database stats reset.
  Uptime is not the observation window. Database reset does not cover individual object resets.
- Buffers: aggregate the selected physical relations, heap/index separately. Empty counters produce a
  NULL ratio. These counters exclude the OS cache and TOAST; misses do not prove physical disk reads.
- Maintenance: estimated live/dead rows, changes since analyze, latest manual/automatic vacuum/analyze.
  Rank by dead rows then modifications; old or NULL timestamps alone do not establish a problem.
- No-scan index candidates: zero idx_scan, valid and ready, excluding primary/unique/exclusion,
  replica-identity and constraint-backed indexes. Usage windows, other replicas and workload matter.
- Equivalent index candidates: match access method, keys/INCLUDE boundary, opclasses, collations,
  sort flags, expressions, predicates, uniqueness, exclusion/immediate semantics and NULL semantics.
  Use JSON lookup for indnullsnotdistinct to remain compatible with PostgreSQL 13/14. Report protected
  indexes explicitly. Pair sizes do not estimate recoverable disk space; no DROP advice is generated.
- Sequences: select visible/approved sequences, including ones owned by readable table columns even
  when sequence SELECT is absent. Distinguish missing grants from unused/standby-unknown state.
  Remaining distance follows increment direction; divide by absolute increment for remaining calls.
  Disk-reserved cached values yield a conservative lower bound, not a time-to-exhaustion prediction.
  Rank known non-cycling sequences by remaining range before unknown values and cycling sequences;
  count all unknown values in scope, so the row limit cannot hide a known exhaustion behind missing grants.
  CYCLE has no exhaustion estimate. A separate target column's type limit is not checked.
- Sizes: pg_table_size (including TOAST) and pg_indexes_size, with pg_total_relation_size. Only physical
  ordinary tables/materialized views are included; partition leaves are separate, parents omitted.

Report text is localized, and metric names retain stable technical identifiers. No matching rows or
an unavailable check is not a healthy verdict. Neither are zeros when statistics collection is disabled.

## Alternatives and limitations

Do not estimate bloat from pg_class alone: planner estimates cannot distinguish row width, fillfactor,
TOAST and actual wasted storage. A later estimator would need an explicit confidence model and more input.
No telemetry persistence, historical deltas, all-replica aggregation, lock investigation or OS metrics.
System catalogs, views and foreign-table storage are outside the object checks. Unowned sequences with
no privilege are omitted outside registry scope. Grant SELECT on chosen sequences separately when needed;
do not modify the baseline role or grant USAGE merely to diagnose headroom.

## Validation

Unit tests cover routing, partial results, truncation, localization and deadline restoration. PostgreSQL
integration tests use a role without monitoring grants, an inaccessible schema, partition storage,
semantic index differences, sequence grants/caching/direction/cycle and a canceled query followed by a
successful transaction. The existing CI integration matrix also runs these tests.
