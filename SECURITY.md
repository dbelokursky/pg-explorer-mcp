# Security

[Русский ниже](#безопасность)

## Reporting a vulnerability

Report it privately through
[GitHub Security Advisories](https://github.com/dbelokursky/pg-explorer-mcp/security/advisories/new) rather
than in a public issue. Say what the server could be made to do, with the statement or call that does
it; a fix lands in the next release and the advisory names you unless you ask otherwise.

## Supported versions

The latest release on the [Releases page](https://github.com/dbelokursky/pg-explorer-mcp/releases) and the
`develop` branch. Older releases get no fixes: update with `git pull` or the new archive.

## What the server guarantees, and in which order

pg-explorer-mcp gives an agent read-only access to PostgreSQL. Three layers keep it read-only, and they are
meant to hold in this order ([ADR 0002](docs/adr/0002-read-only-sql-policy.md), in Russian):

1. **The privileges of the PostgreSQL role.** This is the security boundary. The role the agent
   connects as should have `SELECT` on the schemas it needs and nothing else; `docs/role.sql` makes
   such a role. Nothing the server does can give the agent more than the role has.
2. **A read-only session.** Every connection runs with `default_transaction_read_only = on` and a
   statement timeout, and `run_select` runs inside a transaction that is always rolled back.
3. **The check of the statement's text.** `run_select` accepts one statement of the read-only kinds
   and refuses writes, DDL, settings, functions with side effects (`pg_terminate_backend`,
   `pg_read_file`, `dblink`, `pg_sleep` and the like) and anything it cannot parse. This is a guard
   against mistakes and prompt injection, not a substitute for the first two layers.

A report that gets past layer 3 alone is worth sending; one that gets past layer 1 is urgent.

## What the server does not promise

- The query log (`PG_EXPLORER_MCP_QUERY_LOG`, [ADR 0005](docs/adr/0005-query-log.md)) is a journal on the
  user's machine, not a tamper-proof audit trail. Audit data access on the PostgreSQL side.
- Everything a tool returns goes into the model's context. Choose the profile and the role for the
  data the group may see; the `schema` profile shows no data at all.
- Passwords belong in the libpq password file. The server never asks for them, and `install.py`
  never copies `PGPASSWORD` into the agent settings. The import from DBeaver reads DBeaver's own
  saved passwords and writes them only into that password file.

## Безопасность

Об уязвимости сообщайте приватно через
[GitHub Security Advisories](https://github.com/dbelokursky/pg-explorer-mcp/security/advisories/new), не в
открытом issue: что сервер можно заставить сделать и каким запросом или вызовом. Поддерживается
последний выпуск и ветка `develop`.

Границы безопасности, по порядку надёжности: права роли PostgreSQL (это и есть граница — `docs/role.sql`
делает роль только с `SELECT`), сессия только на чтение с таймаутом запроса, проверка текста запроса в
`run_select` ([ADR 0002](docs/adr/0002-read-only-sql-policy.md)). Журнал запросов — не аудит
([ADR 0005](docs/adr/0005-query-log.md)); ответы инструментов попадают в контекст модели; пароли
живут только в файле паролей libpq.
