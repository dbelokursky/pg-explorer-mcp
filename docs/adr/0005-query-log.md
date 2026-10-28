# ADR 0005. The agent's query log

Status: accepted, 2026-10-22.

## Context

The user and those responsible for the group need to see what the agent did with the database: which tables
it looked at, which queries it ran and what it was refused. The server runs on each user's machine,
not next to the database, and writes only a log on stderr, which the agent usually shows to no one.

## Decision

| Question | Choice | Why |
|---|---|---|
| Switching on | the `PG_EXPLORER_MCP_QUERY_LOG` variable with a path to a file; without it the log is off | by default nothing is written to disk |
| Format | JSON Lines: one line per tool call | readable with `jq`, loads into any log system |
| What is written | time (UTC), OS user, PID, role and database, tool, the arguments in full (including the SQL text), outcome (`ok`, `refused`, `error`, `unavailable`, `failed`), the text of the refusal or error, the number of rows the agent's query returned, duration | this is enough to answer "who, what, when and how it ended" |
| What is not written | the data rows themselves and the tool answers | the log must not become a copy of the data |
| File permissions | created with mode 600 | literals in SQL may contain personal data |
| Writing | the file is opened for every line with `O_APPEND`, and the line goes out in a single write | several agent sessions write to one file without mixing lines; `logrotate` moves the file without restarting the server |
| Symbolic links | a link in place of the file is rejected (`O_NOFOLLOW`) | a link planted in a shared folder will not redirect the writes into someone else's file |
| Path | absolute only, `~` is allowed | the server's working folder depends on the agent and is unpredictable |
| Write failure | the tool call is not interrupted; `status` shows the reason, stderr gets a warning | the log is auxiliary; the agent's work matters more |
| Rotation | none built in | `logrotate` (with `create`) or moving the file: the server creates a new one on the next write |

## What the log is not

The file lives on the user's machine, and the user can change or delete it. It is a log of the
agent's actions, for the user and for looking into cases, not a protected audit trail.
Auditing of data access is done on the PostgreSQL server: `pgaudit`, or `log_statement` and
`log_connections`. The server's connections are marked `application_name = 'pg-explorer-mcp'`, so they are easy to tell
apart from the rest.

## Alternatives considered

- **Rotation through `logging` (`RotatingFileHandler`)**: works incorrectly when several processes
  write to one file, and each agent session has its own server process.
- **syslog or the OS log**: works differently on Linux, macOS and Windows, and is hard for users
  to set up and read.
- **Writing to the database itself**: the agent's role only reads (ADR 0002), and it should stay that way.
- **Keeping the tool answers too**: the log would become a copy of the data with personal information.

## Consequences

- The log is switched on at installation: `PG_EXPLORER_MCP_QUERY_LOG=~/pg-explorer-mcp-queries.jsonl python3 install.py`
  carries the variable over into the agent settings.
- On shared network folders (NFS, SMB) appending with `O_APPEND` is not atomic: each user had better
  write to their own file.
- The database keeps two counters, the agent's queries run and the rows fetched, so that the log knows
  how many rows a call returned without looking into the data itself.
