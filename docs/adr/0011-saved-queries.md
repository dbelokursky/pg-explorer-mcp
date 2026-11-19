# ADR 0011. Saved queries: the group's SQL, run by name

Status: accepted, 2026-11-15.

## Context

A group that does not want the agent composing SQL still wants it to answer questions with data:
counts, sums, statuses, in the joins and limits the group counts them. The `analyst` profile is all
or nothing, and its query log shows refusals, not restraint. DBHub keeps custom tools in a TOML file
and Google Toolbox curates tools instead of free SQL; the same shape fits here, next to the stands
registry and the glossary.

## Decision

A file of the user's folder, `~/.pg-explorer-mcp/queries.conf` (on Windows
`%APPDATA%\\pg-explorer-mcp\\queries.conf`, or the file `PG_EXPLORER_MCP_QUERIES_FILE` names),
holds one section per query: the name, `en` and `ru` descriptions, and `query` — one read-only
statement that returns rows. Each query passes through the same read-only guard as `run_select`
when the file is read; a query that changes anything is left out with a warning in `status`,
never run. The name matches the stand names' alphabet, and descriptions fall back between
languages as the glossary's do.

The tool `run_saved_query` is offered in `explore` and `analyst` **only when the file holds a
query**: a group that does not use it carries none of its tokens. An unknown name lists the saved
ones, as an unknown stand lists the stands. Rows run through the same cursor and limit as
`run_select`, so the budget and the pooler behaviour are the ones already tested. On stands the
query takes the named schema's login, as `run_select` does; a call over several stands is a later
question, as parameters are: named placeholders would have to bypass the cursor, and a saved
query that needs arguments can be saved once per shape instead.

The file is re-read when it changes on disk, as the stands registry is; the tool's presence in the
list is decided at startup, so the first query costs a restart.

## Consequences

The SQL the model runs is exactly what the group wrote: the query log names the query, not
free SQL. Groups gain a middle ground between `explore` and `analyst`, and the DBA gains one
file to review. Sections are single-line, as the pg_service.conf format is; long SQL reads worst
there, and a richer format can follow if it hurts.
