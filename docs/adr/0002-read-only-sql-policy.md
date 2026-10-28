# ADR 0002. Which SQL the agent may run

Status: accepted, 2026-10-22, and implemented (`sqlguard.py`, the `run_select` and
`explain_query` tools, group profiles, `docs/role.sql`). The policy and the decisions on the open questions
(the last section) were confirmed at review on 2026-10-22.

## Principle

Only reading is allowed: `SELECT`s of all kinds and statistics. Any change to data, structure,
privileges or settings is forbidden. The ban rests on three independent layers: if one misses,
the others still stop the write.

| Layer | What it does | Why it is not enough on its own |
|---|---|---|
| The PostgreSQL role | only `SELECT` on the needed schemas, no privileges to create or change anything | this is the real boundary, but the DBA sets it up, and it is easy to get wrong |
| A `READ ONLY` transaction for every query, ending in `ROLLBACK` | PostgreSQL itself rejects writes and DDL; the rollback undoes any change to session settings | does not catch side effects of functions: signals to other sessions, advisory locks, outside connections |
| A check of the query text by the server | lets only allowed statements through, cuts forbidden words and functions, tells the model why | without a real SQL parser both false positives and bypasses are possible, which is why it is the third layer and not the only one |

## Allowed statements

The decision is made by the first keyword, after skipping comments.

| Statement | Condition |
|---|---|
| `SELECT` | without `INTO` and without row locks (`FOR UPDATE`, `FOR SHARE` and their variants) |
| `WITH … SELECT` | only reads inside the CTEs: no `INSERT`, `UPDATE`, `DELETE`, `MERGE` |
| `VALUES`, `TABLE` | — |
| `EXPLAIN` | only for an allowed query; for `ANALYZE` see question 1 below |
| `SHOW` | any parameter; values closed to ordinary roles are hidden by the server itself |

## Forbidden statements

Anything not on the allowed list is forbidden. Spelled out explicitly, so the model gets a clear reason:

| Group | Statements | Why |
|---|---|---|
| Changing data | `INSERT`, `UPDATE`, `DELETE`, `MERGE`, `TRUNCATE`, `COPY` in both directions, `SELECT … INTO` | change data or create tables; `COPY … TO PROGRAM` runs commands on the server |
| Structure | `CREATE`, `ALTER`, `DROP` (including `DROP DATABASE`, `DROP SCHEMA`), `COMMENT`, `SECURITY LABEL`, `REINDEX`, `CLUSTER`, `REFRESH MATERIALIZED VIEW`, `IMPORT FOREIGN SCHEMA` | DDL |
| Privileges | `GRANT`, `REVOKE`, `REASSIGN OWNED`, `DROP OWNED`, `ALTER DEFAULT PRIVILEGES` | change access |
| Maintenance | `VACUUM`, `ANALYZE`, `CHECKPOINT`, `LOAD` | write statistics or files, load the server |
| Session and transactions | `SET`, `RESET`, `SET ROLE`, `SET SESSION AUTHORIZATION`, `BEGIN`, `START TRANSACTION`, `COMMIT`, `END`, `ROLLBACK`, `SAVEPOINT`, `RELEASE`, `PREPARE TRANSACTION`, `DISCARD` | can lift read-only mode or switch the role; the server manages the transaction |
| Code | `DO`, `CALL`, `PREPARE`, `EXECUTE`, `DEALLOCATE` | run arbitrary code the check cannot see |
| Affecting others | `LOCK`, `LISTEN`, `NOTIFY`, `UNLISTEN` | lock or wake up other sessions |
| Cursors | `DECLARE`, `FETCH`, `MOVE`, `CLOSE` | the server manages the cursor that limits rows |

## Forbidden functions, even inside SELECT

A `READ ONLY` transaction does not stop them, so they have their own list:

| Group | Functions | Why |
|---|---|---|
| Signals to other sessions | `pg_terminate_backend`, `pg_cancel_backend` | break off other people's work |
| Settings | `set_config` | changes session parameters, going around the ban on `SET` |
| Advisory locks | `pg_advisory_lock` and all `pg_*advisory*` | session locks survive `ROLLBACK` and get in the way of applications |
| Outside connections | `dblink`, `dblink_exec`, `dblink_connect` and the whole `dblink*` family | a separate connection does not obey our `READ ONLY` |
| SQL from a string | `query_to_xml`, `query_to_xmlschema`, `query_to_xml_and_xmlschema`, `cursor_to_xml`, `cursor_to_xmlschema` | run query text our check has not seen |
| Server files | `pg_read_file`, `pg_read_binary_file`, `pg_stat_file`, `pg_ls_*`, `lo_import`, `lo_export` | read and write files on the database server |
| Administration | `pg_reload_conf`, `pg_rotate_logfile`, `pg_switch_wal`, `pg_create_restore_point`, `pg_promote`, `pg_*replication_slot*`, `pg_logical_emit_message`, `pg_notify` | control the server and replication |
| Sequences and large objects | `nextval`, `setval`, `lo_create`, `lo_unlink`, `lo_put`, `lowrite` | change state |
| Delays | `pg_sleep`, `pg_sleep_for`, `pg_sleep_until` | hold the connection for nothing |

## Statistics we allow

- The system catalogs `pg_catalog.*` and `information_schema.*`, within the role's privileges.
- Table and index statistics: `pg_stat_user_tables`, `pg_stat_user_indexes`, `pg_statio_user_*`,
  `pg_stats` (the server shows it only for tables the role has `SELECT` on).
- Sizes: `pg_relation_size`, `pg_total_relation_size`, `pg_table_size`, `pg_indexes_size`, `pg_size_pretty`.
- Row estimates from `pg_class.reltuples`, plans through `EXPLAIN`.
- `pg_stat_activity` and `pg_stat_statements`: see question 2 below.

## How `run_select` will run

1. A check of the text: the first word is an allowed one, and there is no forbidden word or function outside
   strings, comments and quoted identifiers. Identifiers written as `U&"..."` are rejected:
   PostgreSQL expands escape sequences in them, while the check reads names as written, and
   `U&"pg\005fsleep"` would pass it as a harmless name. Otherwise, an error with the reason for the model.
2. `BEGIN TRANSACTION READ ONLY` and, in the same message, `SET LOCAL statement_timeout` and `SET LOCAL search_path`:
   a pooler in transaction mode (pgbouncer, Odyssey) may hand each transaction to a different server session or
   reset the session between them (`DISCARD ALL`), so session settings made at connect time do not survive, and
   a named prepared statement does not live until the next message. The transaction pins one session for all
   messages of the call and carries its own limits; catalog queries run the same way.
3. The query as a single statement through the extended protocol (PostgreSQL rejects a string of several statements)
   inside a cursor: the server fetches no more than the row limit plus one, to know there are more.
4. `ROLLBACK`, always, even after success.
5. The answer goes through the answer budget.

## A role for the agent

The script for DBAs is [docs/role.sql](../role.sql): a `NOSUPERUSER … CONNECTION LIMIT 5` role with
`default_transaction_read_only = on`, `statement_timeout`, `GRANT SELECT` on the chosen schemas and
default privileges for future tables. An integration test runs it through `psql` and
checks that the role reads the granted schema and cannot write, even with read-only mode switched off.

What not to grant the role: `pg_read_all_stats` and `pg_monitor` (they expose the text of other people's queries),
`pg_read_server_files`, `pg_write_server_files`, `pg_execute_server_program`, `pg_signal_backend`,
membership in the roles that own the tables. The `CREATE` privilege on schemas is not needed either: in PostgreSQL 15+
it is revoked on `public` by default; on older versions it is worth checking.

## Decisions on the open questions

Confirmed at review on 2026-10-22 without changes.

1. **`EXPLAIN ANALYZE`** is allowed for `SELECT` and only in the `analyst` profile: the query runs inside
   `READ ONLY` and under `statement_timeout`, and in return the plan shows real timings.
2. **`pg_stat_activity` and `pg_stat_statements`** are allowed; it is the role without
   `pg_read_all_stats`, not the server, that protects other people's query text.
3. **The row limit of `run_select`** is 200; on top of that the answer budget cuts it further.
4. **`run_select`** exists only in the `analyst` profile; the `schema` and `explore` profiles make do with
   `describe_table`, `sample_rows` and `explain_query`.
5. **False positives** are accepted: a column named `update` or `delete` has to be quoted,
   and the check's error says which word stopped it.
6. **`U&"..."` identifiers** are rejected (added 2026-10-27): the model does not need this notation, and
   expanding escape sequences together with `UESCAPE` in a check without a parser is extra code where
   a refusal with a hint to write the name as it is will do.
