# pg-mcp

**English** | [Русский](README.ru.md)

An MCP server that gives an agent **read-only** access to one PostgreSQL database. The agent sees the
database structure, can look at sample rows and query plans, and analysts can run their own `SELECT`s.
Nothing can change data, structure or settings through this server.

Built for GigaCode CLI and Qwen Code, and works with any MCP client that starts servers over stdio.
Runs on Python 3.9+ with nothing to install: the pg8000 driver ships inside the repository. Speaks
English or Russian.

## Contents

- [What it does](#what-it-does)
- [Quick start](#quick-start)
- [Installation step by step](#installation-step-by-step)
- [Usage](#usage)
- [Group profiles](#group-profiles)
- [Language](#language)
- [Security](#security)
- [Settings](#settings)
- [For DBAs: a role for the agent](#for-dbas-a-role-for-the-agent)
- [Troubleshooting](#troubleshooting)
- [Updating and removing](#updating-and-removing)
- [FAQ](#faq)
- [Development](#development)

## What it does

| Tool | What it does | Profiles |
|---|---|---|
| `status` | which database, as which role, which profile and language, which limits; on failure, what to fix | all |
| `list_tables` | tables, views and foreign tables the role can read, with estimated row counts and comments | all |
| `search_schema` | tables, views and columns whose name or comment contains a piece of text, in every readable schema; for large databases | all |
| `describe_table` | columns with types, `NOT NULL`, defaults and comments; keys, tables referencing it, indexes | all |
| `sample_rows` | a few rows of a table, optionally only some columns | `explore`, `analyst` |
| `explain_query` | a query plan without running the query; in the `analyst` profile also with real timings (`ANALYZE`) | `explore`, `analyst` |
| `run_select` | one read-only statement: `SELECT`, `WITH … SELECT`, `VALUES`, `TABLE`, `EXPLAIN`, `SHOW` | `analyst` |

The group's administrator picks the profile at installation: [details](#group-profiles).

## Quick start

You need Python 3.9+, GigaCode CLI (or Qwen Code) and a database role from your DBA.

```bash
git clone https://github.com/dbelokursky/pg-mcp.git
cd pg-mcp
export PGHOST=db.example.com PGDATABASE=app PGUSER=mcp_agent   # where the database is, which role
python3 install.py --lang en                                     # register and check
gigacode extensions link "$(pwd)"                                # context and the /pg:setup command
```

Put the password into `~/.pgpass` ([how](#4-the-password-goes-into-the-password-file)), restart
GigaCode and ask the agent: "Check the database connection".

There is a shorter way too: link the extension (`gigacode extensions link`), restart GigaCode and run
`/pg:setup` in it. The agent finds Python, runs `install.py` and works through the hints itself.

## Installation step by step

### 1. The code

```bash
git clone https://github.com/dbelokursky/pg-mcp.git
```

Without git, download the repository archive and unpack it anywhere. Do not move the folder afterwards:
its path goes into the agent settings. If you do move it, repeat step 5.

### 2. Python 3.9 or newer

| System | How to check | Where to get it |
|---|---|---|
| macOS | `python3 --version` | comes with the Command Line Tools (installing `git` brings them) |
| Linux | `python3 --version` | your distribution's `python3` package |
| Windows | `py -3 --version` or `python --version` | the installer from python.org, or the "embeddable package" zip, which unpacks without admin rights |

Nothing needs to be installed with pip.

### 3. Where the database is

The server reads the same environment variables as `psql`. Set them in the terminal you run
`install.py` from, and the script copies them into the server settings.

macOS and Linux:

```bash
export PGHOST=db.example.com PGPORT=5432 PGDATABASE=app PGUSER=mcp_agent
```

Windows (PowerShell):

```powershell
$env:PGHOST = "db.example.com"; $env:PGDATABASE = "app"; $env:PGUSER = "mcp_agent"
```

For TLS with certificate checks add `PGSSLMODE=verify-full` and `PGSSLROOTCERT=<path to the CA>`.
All variables are listed under [settings](#settings).

### 4. The password goes into the password file

The password lives only in PostgreSQL's password file, the same one `psql` reads. Never put it into
the agent settings and never send it in the chat.

macOS and Linux use `~/.pgpass`:

```bash
nano ~/.pgpass        # or any other editor
chmod 600 ~/.pgpass   # otherwise the file is ignored: others could read it
```

Windows uses `%APPDATA%\postgresql\pgpass.conf`:

```powershell
mkdir "$env:APPDATA\postgresql" -Force
notepad "$env:APPDATA\postgresql\pgpass.conf"
```

One line per connection: `host:port:database:role:password`. An asterisk matches any value; escape
a colon or a backslash inside a value as `\:` and `\\`.

```text
db.example.com:5432:app:mcp_agent:password-from-the-DBA
```

### 5. Register the server with the agent

```bash
python3 install.py                              # GigaCode, profile explore, language from the system locale
python3 install.py --profile analyst --lang en  # profile and language spelled out
python3 install.py --client qwen                # Qwen Code instead of GigaCode
py -3 install.py                                # Windows
```

What the script does:

1. backs up the agent settings (`~/.gigacode/settings.json.bak-<date>`);
2. adds the server `pg` to `mcpServers`: the Python that runs the script, the path to `run.py`, the
   `PG*` variables from the terminal (except `PGPASSWORD`), the profile and the language;
3. starts the server the way the agent will, calls `status` and prints the answer. Exit code 0 means
   the database is reachable.

`python3 install.py --check` only starts the server and calls `status`; it changes nothing.

This is the entry in `~/.gigacode/settings.json` after `install.py --profile analyst --lang en`:

```json
{
  "mcpServers": {
    "pg": {
      "command": "/usr/bin/python3",
      "args": ["-I", "/Users/me/pg-mcp/run.py"],
      "timeout": 30000,
      "env": {
        "PGHOST": "db.example.com",
        "PGDATABASE": "app",
        "PGUSER": "mcp_agent",
        "PG_MCP_PROFILE": "analyst",
        "PG_MCP_LANG": "en"
      }
    }
  }
}
```

If `settings.json` contains comments, the script leaves it alone and prints the snippet to add by hand.

### 6. The extension

```bash
gigacode extensions link /path/to/pg-mcp     # Qwen Code: qwen extensions link …
```

The extension gives the agent a short context (where to start, never to ask for passwords in the chat,
to answer in the user's language) and the `/pg:setup` command for installing and repairing the server.

### 7. Check

Restart GigaCode so it starts the server with the new settings, and ask: "Check the database
connection". The agent calls `status` and tells you what it says.

## Usage

Talk to the agent in plain words; it picks the tools.

| You ask | The agent calls |
|---|---|
| "Check the database connection" | `status` |
| "Which tables are in the shop schema?" | `list_tables` |
| "Where are the customers stored?" | `search_schema` |
| "Describe the orders table" | `describe_table` |
| "Show me a few orders" | `sample_rows` |
| "Why is this query slow: …" | `explain_query` |
| "How many orders are there per status?" | `run_select` (profile `analyst`) |

Below are real answers of the tools on a demo database. The model reads them, and you see them in the
agent's interface.

`status`:

```text
pg-mcp 0.2.0 on Python 3.9.6, installed at /Users/me/pg-mcp
target: mcp_demo_agent@localhost:5432/pg_mcp_demo (sslmode=prefer)
profile: analyst (status, list_tables, search_schema, describe_table, sample_rows, explain_query, run_select)
language: en
database: connected to pg_mcp_demo as mcp_demo_agent, PostgreSQL 16.15
limits: read only = on, statement timeout = 5s, list_tables returns at most 200 entries, answers are cut at 8000 characters
```

`list_tables`: table comments help the model see what lives where.

```text
shop.customers | table | ~50 rows | Покупатели
shop.order_items | table | ~2.2k rows | Позиции заказов: цена фиксируется на момент заказа
shop.orders | table | ~500 rows | Заказы
shop.products | table | ~20 rows | Каталог товаров
```

`search_schema` with `text: "customer"` finds the table and a column of another table:

```text
shop.customers | table | ~50 rows | Покупатели
shop.orders.customer_id integer NOT NULL
```

`describe_table` for `shop.orders`:

```text
shop.orders | table | ~500 rows | Заказы
columns:
  id integer NOT NULL
  customer_id integer NOT NULL
  status text NOT NULL  -- new, paid, shipped или cancelled
  created_at timestamp with time zone NOT NULL
constraints:
  orders_pkey: PRIMARY KEY (id)
  orders_customer_id_fkey: FOREIGN KEY (customer_id) REFERENCES shop.customers(id)
  orders_status_check: CHECK (status = ANY (ARRAY['new'::text, 'paid'::text, 'shipped'::text, 'cancelled'::text]))
referenced by:
  shop.order_items: FOREIGN KEY (order_id) REFERENCES shop.orders(id)
indexes:
  CREATE INDEX orders_created_idx ON shop.orders USING btree (created_at)
  CREATE INDEX orders_customer_idx ON shop.orders USING btree (customer_id)
  CREATE UNIQUE INDEX orders_pkey ON shop.orders USING btree (id)
```

The comments come from the database itself, so they stay in whatever language they were written in.

`sample_rows` with `columns: "id, status, created_at"` and `limit: 3`:

```text
shop.orders:
id | status | created_at
1 | paid | 2026-05-03 16:49:13.795859+02:00
2 | cancelled | 2026-09-13 17:03:59.690907+02:00
3 | paid | 2026-05-22 21:08:31.308385+02:00
(3 rows shown, there are more: narrow the query with WHERE, ORDER BY or LIMIT)
```

An error is a hint as well: the model sees what to fix and tries again.

```text
No column 'total' in shop.orders. Columns: id, customer_id, status, created_at.
DELETE is not allowed. Only read-only statements are: SELECT, WITH ... SELECT, VALUES, TABLE, EXPLAIN and SHOW.
No readable table or view named 'salaries' in schema 'hr'. Call list_tables to see what exists.
```

The last line is about the `hr` schema, which the role has no rights to: for the agent it does not exist.

### Limits

| What | How much |
|---|---|
| Entries from `list_tables` | at most 200; pass `schema` to narrow the list |
| Matches from `search_schema` | at most 100 |
| Rows from `sample_rows` | 10 by default, at most 50 |
| Rows from `run_select` | at most 200 |
| Plan lines | at most 500 |
| One value in an answer | at most 200 characters |
| A whole tool answer | 8000 characters (`PG_MCP_MAX_RESPONSE_CHARS`) |
| One query | 5 seconds (`PG_MCP_STATEMENT_TIMEOUT_MS`) |

A longer answer is cut at whole lines, and its last line says how much is shown. Local models run with
small context windows, and an answer that is too long can end the agent's session.

## Group profiles

The profile decides which tools the agent sees:

| Profile | Tools | For |
|---|---|---|
| `schema` | `status`, `list_tables`, `search_schema`, `describe_table` | people who need the structure only: the agent sees no data rows at all |
| `explore` | plus `sample_rows`, `explain_query` | developers; the default |
| `analyst` | plus `run_select` and `explain_query` with `ANALYZE` | analysts who need their own SQL |

Set it at registration (`python3 install.py --profile schema`) or with `PG_MCP_PROFILE` in the server's
`env` block. An unknown value turns on the strictest profile, `schema`, and `status` warns about it.
A profile is a convenience, not an access boundary: what can be read is decided by the role's
privileges in the database.

## Language

The server speaks English (`en`) or Russian (`ru`): tool descriptions, answers, hints, refusals and
the output of `install.py`. The language is chosen like this:

1. the `PG_MCP_LANG` variable (`install.py --lang` writes it);
2. otherwise the system locale: the `LANGUAGE`, `LC_ALL`, `LC_MESSAGES` and `LANG` variables, and the
   regional settings on Windows;
3. otherwise English.

`status` shows the current language. To change it, run `python3 install.py --lang ru` or edit
`PG_MCP_LANG` in the settings, then restart the agent.

Tool and argument names, JSON-RPC protocol errors, the server log and PostgreSQL's own messages are not
translated; the language of the latter is set by the database's `lc_messages`.

Either way, the model is asked to answer in the user's language. If your group's model picks tools less
reliably from Russian descriptions, set `PG_MCP_LANG=en`: it will still answer in Russian. The reasons
behind this design are in [ADR 0004](docs/adr/0004-languages.md) (in Russian).

## Security

Any change to data, structure, privileges, settings or other sessions is refused. There are three
layers of defence, and each one holds even if the others miss something:

1. **The PostgreSQL role** is the real boundary: it has only `SELECT` on the chosen schemas
   ([a script for DBAs](#for-dbas-a-role-for-the-agent)).
2. **A `READ ONLY` transaction for every query**, always rolled back. A query goes out as a single
   statement, so PostgreSQL rejects a string such as `SELECT 1; COMMIT; DROP …`.
3. **A check of the query text** lets only read-only statements through and refuses `INSERT`, `UPDATE`,
   `DELETE`, `MERGE`, `SELECT … INTO`, row locks and functions with side effects (`pg_terminate_backend`,
   `set_config`, `dblink`, `pg_sleep` and others), telling the model why.

The full policy is in [ADR 0002](docs/adr/0002-read-only-sql-policy.md) (in Russian).

Everything the tools return goes into the model's context. With a local model inside your organisation
the data stays there, but pick the profile by the task: `schema` shows no data at all.

## Settings

All settings are environment variables in the `env` block of the `pg` server in the agent settings.
`install.py` copies them there from the terminal.

| Variable | Default | Meaning |
|---|---|---|
| `PGHOST` | `localhost` | database host, or the Unix socket directory (starts with `/`) |
| `PGPORT` | `5432` | port |
| `PGDATABASE` | same as `PGUSER` | database |
| `PGUSER` | the OS user name | role |
| `PGSSLMODE` | `prefer` | `disable`, `allow`, `prefer`, `require`, `verify-ca`, `verify-full`; only the last two check the certificate |
| `PGSSLROOTCERT` | — | CA certificate for `verify-ca` and `verify-full` |
| `PGPASSFILE` | `~/.pgpass`, on Windows `%APPDATA%\postgresql\pgpass.conf` | password file |
| `PGPASSWORD` | — | works, but `install.py` never copies it: the password belongs in the password file |
| `PG_MCP_PROFILE` | `explore` | `schema`, `explore` or `analyst` |
| `PG_MCP_LANG` | from the locale | `en` or `ru` |
| `PG_MCP_MAX_RESPONSE_CHARS` | `8000` | budget of one answer in characters, at least 500 |
| `PG_MCP_STATEMENT_TIMEOUT_MS` | `5000` | timeout of one query in milliseconds |
| `PG_MCP_LOG_LEVEL` | `INFO` | detail of the stderr log: `DEBUG`, `INFO`, `WARNING`, `ERROR` |

The budget counts characters. Russian text takes more tokens per character than English, so if the
model's context is small, lower the budget rather than raise it.

## For DBAs: a role for the agent

The [docs/role.sql](docs/role.sql) script creates a role that can only read the chosen schemas. Run
it as a superuser or as the owner of the schemas, in the database the agent will read:

```bash
psql -d app -v role=mcp_agent -v password='…' -v schemas=sales,reports -v owner=app_owner -f docs/role.sql
```

| Parameter | Meaning |
|---|---|
| `role` | name of the new role; PostgreSQL reserves names that start with `pg_` |
| `password` | its password; users put it into their password file |
| `schemas` | comma-separated schemas the agent may read |
| `owner` | the role that creates tables in those schemas, so future tables become readable too |

The role gets `LOGIN` with no other attributes, at most 5 connections, `default_transaction_read_only`,
statement (5 s) and idle-in-transaction (60 s) timeouts, `CONNECT` on the database, `USAGE` on the
schemas, `SELECT` on all their tables, and default privileges on the owner's future tables.

Never grant this role `pg_read_all_stats` or `pg_monitor` (they show other sessions' query texts),
`pg_read_server_files`, `pg_write_server_files`, `pg_execute_server_program`, `pg_signal_backend`, or
membership in a role that owns tables.

Give each group its own role and its own set of schemas. For connections from outside, turn on TLS and
ask users to set `PGSSLMODE=verify-full`.

## Troubleshooting

Start with `python3 install.py --check`, or ask the agent to run `/pg:setup`: `status` nearly always
says what is wrong.

| What you see | What it means | What to do |
|---|---|---|
| The agent has no `pg` tools | the agent was not restarted, or the entry is missing | restart the agent; check that `pg` is in `mcpServers`; `install.py --check` |
| "Nothing accepts connections at …" | wrong host or port, the database is down, no VPN | check `PGHOST`, `PGPORT` and the VPN; compare with `psql` |
| "Host … cannot be resolved" | a typo in `PGHOST`, or a VPN is needed | fix `PGHOST` |
| "Connecting to … timed out" | a firewall or a VPN | check that the port is reachable from this machine |
| "Login as … failed" or "the server asks for a password" | no password in the password file, a wrong line, or file permissions | check the path, the line format and `chmod 600` |
| "Database … does not exist" | wrong database name | fix `PGDATABASE` |
| "The server refused the login for …" | `pg_hba.conf`, or the role lacks `LOGIN`/`CONNECT` | ask the DBA |
| "TLS with … failed" | the TLS mode does not match the server | check `PGSSLMODE` and `PGSSLROOTCERT` |
| "The current role cannot read any table" | the role has no `SELECT` | ask the DBA: [a role for the agent](#for-dbas-a-role-for-the-agent) |
| `settings.json` "is not plain JSON" | the settings file has comments | add the printed snippet by hand |
| An answer ends with "… answer cut to" | the answer budget kicked in | narrow the question, or raise `PG_MCP_MAX_RESPONSE_CHARS` with the model's context in mind |
| `python3` not found | Python is missing or has another name | `python --version`, on Windows `py -3 --version`; install Python 3.9+ |

The server logs to stderr; `PG_MCP_LOG_LEVEL=DEBUG` adds detail. If the server does not answer the
check, `install.py --check` prints that log.

## Updating and removing

To update, run `git pull` in the pg-mcp folder and restart the agent. Run `install.py` again only if
you moved the folder or changed Python, the profile or the language.

To remove, delete `pg` from `mcpServers` in the agent settings (backups of earlier settings sit next to
the file), run `gigacode extensions uninstall pg-mcp` and delete the folder.

## FAQ

**Does it need internet access, pip or admin rights?** No. Everything the server needs is in the
repository; it only needs a network path to the database.

**Can the agent change anything in the database?** No. Even if text inside the data tricks the model,
the role's privileges, the read-only transaction and the text check stop the query.

**Which PostgreSQL versions does it work with?** CI tests it against PostgreSQL 13 to 18. Before 14,
a table PostgreSQL has never analyzed looks exactly like an empty one, so `list_tables` calls both
"empty or never analyzed".

**Which MCP protocol versions does it speak?** Revisions 2024-11-05 to 2025-11-25 (with the
`initialize` handshake) and 2026-07-28 (without it).

**Will another MCP client do?** Yes, any client that starts servers over stdio. Give it the command
`python3`, the arguments `-I /path/to/pg-mcp/run.py` and the `PG*` variables in the environment. If the
client reads the `mcpServers` format, the entry from [step 5](#5-register-the-server-with-the-agent)
works as is.

**Why does the agent answer in the wrong language?** The model chooses the language of its answers;
the server asks it to use the user's language. `PG_MCP_LANG` sets the language of the tools themselves.

## Development

Environment, checks, code layout and rules are in [CONTRIBUTING.md](CONTRIBUTING.md). Decisions and
their reasons are in [docs/adr/](docs/adr/), the version history is in [CHANGELOG.md](CHANGELOG.md).
These documents are in Russian.
