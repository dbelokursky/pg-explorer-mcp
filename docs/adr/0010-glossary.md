# ADR 0010. A glossary of descriptions kept beside the database

Status: accepted, 2026-11-15.

## Context

Databases written before the agents often carry no `COMMENT ON` anything: the model sees names and
types, and guesses. The people who know what `shop.orders.total` means or what the group calls an
"average receipt" have nowhere in the database to put it, and cannot always change production
objects just to describe them. MotherDuck's MCP server solves this with "guides" kept beside the
data and consulted on every query.

## Decision

A file of the user's folder, `~/.pg-explorer-mcp/glossary.conf` (on Windows
`%APPDATA%\pg-explorer-mcp\glossary.conf`, or the file `PG_EXPLORER_MCP_GLOSSARY_FILE` names),
holds one section per thing described: `[table shop.orders]`, `[column shop.orders.status]`,
`[term average_receipt]`, each with `en` and `ru` lines. The format is that of pg_service.conf,
read by the same lenient parser as the stands registry; a section without a text, or of another
shape, is skipped, and an unreadable file is a warning in `status`, never a failure.

The glossary never overrides the database: `describe_table`, `list_tables` and their stand
variants show a glossary text only where the catalog holds no comment of its own, so a COMMENT
added later simply wins. `search_schema` also matches the glossary: entries whose text holds the
fragment, or whose term name spells it (underscores as spaces, as a question spells them), are
added under a "from the glossary" line, after the catalog's own matches and never repeating one.
Terms come last, for the model to read after what the database itself says.

The server speaks one language, and the glossary may hold either: the server's language comes
first, the other stands in when the one is missing. The file is re-read when it changes on disk,
as the stands registry is, so an edit needs no restart.

## Consequences

The glossary is per machine, not per database or stand: one group around one set of stands shares
one file, which is the unit the group writes. Entries are matched by full name, case-insensitively;
renamed tables need their entries renamed too. The glossary adds no privileges and reads no data:
it only supplies words, and `status` names the file and its entry count so an empty or misnamed
one is noticed.
