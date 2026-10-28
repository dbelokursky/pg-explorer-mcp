# pg-explorer-mcp

**English** | [Русский](README.ru.md)

Called pg-mcp until 0.8.0: what to do after an update is in [Updating and removing](#updating-and-removing).

[![tests](https://github.com/dbelokursky/pg-explorer-mcp/actions/workflows/tests.yml/badge.svg?branch=develop)](https://github.com/dbelokursky/pg-explorer-mcp/actions/workflows/tests.yml)
[![release](https://img.shields.io/github/v/release/dbelokursky/pg-explorer-mcp)](https://github.com/dbelokursky/pg-explorer-mcp/releases)
[![downloads](https://img.shields.io/github/downloads/dbelokursky/pg-explorer-mcp/total)](https://github.com/dbelokursky/pg-explorer-mcp/releases)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

An MCP server that gives an agent **read-only** access to a PostgreSQL database, or through one entry to
[dozens of stands](#many-stands). The agent sees the database structure, can look at sample rows and
query plans, and analysts can run their own `SELECT`s.
Nothing can change data, structure or settings through this server.

Built for GigaCode CLI and Qwen Code, and works with any MCP client that starts servers over stdio.
Runs on Python 3.9+ with nothing to install: the pg8000 driver ships inside the repository. Speaks
English or Russian.

## Contents

- [What it does](#what-it-does)
- [Quick start](#quick-start)
- [Try it on the demo database](#try-it-on-the-demo-database)
- [Installation step by step](#installation-step-by-step)
- [Usage](#usage)
- [Many stands](#many-stands)
- [Group profiles](#group-profiles)
- [Language](#language)
- [Security](#security)
- [Query log](#query-log)
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
| `list_schemas` | with [many stands](#many-stands): the databases of a stand and their schemas, those the server has logins for or all that a single login reads | all |
| `list_tables` | tables, views and foreign tables the role can read, with estimated row counts and comments | all |
| `search_schema` | tables, views and columns whose name or comment contains a piece of text, in every readable schema; for large databases | all |
| `describe_table` | columns with types, `NOT NULL`, defaults and comments; keys, tables referencing it, indexes | all |
| `compare_schema` | with [many stands](#many-stands): how one schema differs between two stands: tables and views, columns, constraints, indexes | all |
| `sample_rows` | a few rows of a table, optionally only some columns | `explore`, `analyst` |
| `explain_query` | a query plan without running the query; in the `analyst` profile also with real timings (`ANALYZE`) | `explore`, `analyst` |
| `run_select` | one read-only statement: `SELECT`, `WITH … SELECT`, `VALUES`, `TABLE`, `EXPLAIN`, `SHOW` | `analyst` |

The group's administrator picks the profile at installation: [details](#group-profiles).

## Quick start

You need Python 3.9+, GigaCode CLI (or Qwen Code) and a database role from your DBA.

```bash
git clone https://github.com/dbelokursky/pg-explorer-mcp.git
cd pg-explorer-mcp
export PGHOST=db.example.com PGDATABASE=app PGUSER=mcp_agent   # where the database is, which role
python3 install.py --lang en                                     # register and check
gigacode extensions link "$(pwd)"                                # context and the /pg:setup command
```

Put the password into `~/.pgpass` ([how](#4-the-password-goes-into-the-password-file)), restart
GigaCode and ask the agent: "Check the database connection".

There is a shorter way too: link the extension (`gigacode extensions link`), restart GigaCode and run
`/pg:setup` in it. The agent finds Python, runs `install.py` and works through the hints itself.

## Try it on the demo database

The answers shown in [Usage](#usage) come from a small demo database: a shop the agent may read and an
`hr` schema it may not. `scripts/demo.py` makes it on any PostgreSQL where your `PG*` variables may
create a database and a role (a fresh local PostgreSQL will do):

```bash
python3 scripts/demo.py                                     # pg_mcp_demo and the role mcp_demo_agent, password demo
echo "localhost:5432:pg_mcp_demo:mcp_demo_agent:demo" >> ~/.pgpass && chmod 600 ~/.pgpass
PGDATABASE=pg_mcp_demo PGUSER=mcp_demo_agent python3 install.py --profile analyst
```

Restart the agent and ask it: "How many orders are there per status?", "Describe the orders table",
"What is in the hr schema?". The data is the same on every copy (`docs/demo.sql` builds it with
arithmetic, not `random()`), so the agent's answers match the examples in this README.
`python3 scripts/demo.py --drop` removes the database and the role.

## Installation step by step

### 1. The code

```bash
git clone https://github.com/dbelokursky/pg-explorer-mcp.git
```

Without git, take `pg-explorer-mcp-<version>.zip` from the latest release on the
[Releases page](https://github.com/dbelokursky/pg-explorer-mcp/releases) and unpack it anywhere: inside is a `pg-explorer-mcp` folder
with everything the server needs, and `SHA256SUMS` next to the archive holds its checksum. The archive
carries a provenance attestation: `gh attestation verify pg-explorer-mcp-<version>.zip --repo dbelokursky/pg-explorer-mcp`
confirms that GitHub built it from this repository's tag. Do not move the folder afterwards: its path
goes into the agent settings. If you do move it, repeat step 5.

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
      "args": ["-I", "/Users/me/pg-explorer-mcp/run.py"],
      "timeout": 30000,
      "env": {
        "PGHOST": "db.example.com",
        "PGDATABASE": "app",
        "PGUSER": "mcp_agent",
        "PG_EXPLORER_MCP_PROFILE": "analyst",
        "PG_EXPLORER_MCP_LANG": "en"
      }
    }
  }
}
```

If `settings.json` contains comments, the script leaves it alone and prints the snippet to add by hand.

#### Several databases

One clone serves any number of databases: register each further one under its own name. Its entry becomes
`pg-<name>` next to `pg`, with its own connection, profile, language and query log:

```bash
PGDATABASE=sales python3 install.py --name sales
PGDATABASE=hr PGUSER=mcp_hr python3 install.py --name hr --profile schema
```

The agent then gets the tools of every entry, and the `status` of each names its server and database.

#### Other agents and editors

`--client` registers the server with other MCP clients as well, in each one's own file and format:

| `--client` | Where the entry goes |
|---|---|
| `gigacode` (the default), `qwen` | `~/.gigacode/settings.json`, `~/.qwen/settings.json` |
| `vscode` | the user `mcp.json` of VS Code (GitHub Copilot agent mode), under `servers` |
| `cursor` | `~/.cursor/mcp.json` |
| `windsurf` | Windsurf, now Devin Desktop: `~/.config/devin/mcp_config.json`, or the older file where one is in use |
| `cline` | `~/.cline/data/settings/cline_mcp_settings.json`, shared by Cline in VS Code, JetBrains and its CLI |
| `roo` | the settings file of the Roo Code extension in VS Code (the extension is no longer developed) |
| `continue` | a block file `~/.continue/mcpServers/pg.yaml` |
| `lmstudio` | `~/.lmstudio/mcp.json` |
| `claude-desktop` | the app's `claude_desktop_config.json` |
| `junie` | `~/.junie/mcp/mcp.json` |
| `jetbrains` | nothing is written: AI Assistant keeps its servers in Settings, so the entry is printed to paste |

```bash
python3 install.py --client cursor            # write into Cursor's file (with a backup) and check
python3 install.py --client vscode --print    # only show the entry and where it goes
```

`--print` suits a client this list lacks, or a file you would rather edit yourself. A file with comments
is never rewritten: `install.py` prints what to add instead. Run at a terminal, `install.py` notices the
other clients installed on this machine once it has registered the server and offers to add it there
too. The extension (the context and the `/pg:` commands) exists for GigaCode CLI and Qwen Code only.

### 6. The extension

```bash
gigacode extensions link /path/to/pg-explorer-mcp     # Qwen Code: qwen extensions link …
```

The extension gives the agent a short context (where to start, never to ask for passwords in the chat,
to answer in the user's language), the `/pg:setup` command for installing and repairing the server and
`/pg:import` for bringing stand connections in from DBeaver or `pg_service.conf`.

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
pg-explorer-mcp 0.7.0 on Python 3.9.6, installed at /Users/me/pg-explorer-mcp
server name: pg
access: read only. Nothing can be inserted, changed or deleted through this server: decline such requests instead of asking the user to confirm them.
target: mcp_demo_agent@localhost:5432/pg_mcp_demo (sslmode=prefer)
profile: analyst (status, list_tables, search_schema, describe_table, sample_rows, explain_query, run_select)
language: en
query log: off
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
1 | shipped | 2026-01-01 09:00:00+02:00
2 | paid | 2026-01-01 16:00:00+02:00
3 | cancelled | 2026-01-01 23:00:00+02:00
(3 rows shown, there are more: narrow the query with WHERE, ORDER BY or LIMIT)
```

An error is a hint as well: the model sees what to fix and tries again.

```text
No column 'total' in shop.orders. Columns: id, customer_id, status, created_at.
DELETE is not allowed. Only read-only statements are: SELECT, WITH ... SELECT, VALUES, TABLE, EXPLAIN and SHOW.
No readable table or view named 'salaries' in schema 'hr'. Call list_tables to see what exists.
```

The last line is about the `hr` schema, which the role has no rights to: the tools show none of its tables.

### Limits

| What | How much |
|---|---|
| Entries from `list_tables` | at most 200; pass `schema` to narrow the list |
| Matches from `search_schema` | at most 100 |
| Rows from `sample_rows` | 10 by default, at most 50 |
| Rows from `run_select` | at most 200 |
| Plan lines | at most 500 |
| One value in an answer | at most 200 characters |
| A whole tool answer | 8000 characters (`PG_EXPLORER_MCP_MAX_RESPONSE_CHARS`) |
| One query | 5 seconds (`PG_EXPLORER_MCP_STATEMENT_TIMEOUT_MS`) |
| Open connections of an entry for stands | at most 8 (`PG_EXPLORER_MCP_MAX_CONNECTIONS`) |
| Stands in one call (`stands`) | at most 100, asked at the same time; the rows of `run_select` and the matches of `search_schema` are shared among them, at least 10 per stand |

A longer answer is cut at whole lines, and its last line says how much is shown. Local models run with
small context windows, and an answer that is too long can end the agent's session.

## Many stands

When there are dozens of stands (`dev1` to `dev50`, `ift1` to `ift3`), each with several databases and a
login per schema, an entry per connection would cost the model thousands of characters of tool
descriptions in every request. One entry serves them all instead: the tools are described once, and
each call names the stand and the schema.

**1. The registry.** Connections are kept in the `pg_service.conf` format of libpq, a section per schema
named `stand.database.schema`:

```ini
[dev7.demo.shop]
host=dev7-db.example.com

[dev7.demo.hr]
host=dev7-db.example.com

[ift2.demo.shop]
host=ift2-db.example.com
port=6432
user=shop_reader
```

- only `host` is required; `port` (5432), `dbname` (the database part of the name), `user` (the schema
  name), `sslmode`, `sslrootcert` and `connect_timeout` (10 seconds) are optional;
- `host` may list several hosts separated by commas, as libpq takes them: `host=h1,h2` with one `port` for
  both, or `port=6544,6545` with one per host. The server tries them in order and keeps the first that lets
  the login in, so a stand with a standby answers while its primary is down; when none does, the error
  names every host;
- a database whose sections all name one login (a single section is one) is read as a whole through that
  login: `schema` takes any schema the database holds, and `list_schemas` and `list_tables` show what the
  login can read, as for one database; a database with a login per schema keeps to the schemas of the
  registry;
- passwords go into the password file, a line per login, just as for one database;
- the server reads `~/.pg-explorer-mcp/stands.conf` (on Windows `%APPDATA%\pg-explorer-mcp\stands.conf`, or the file
  `PG_EXPLORER_MCP_STANDS_FILE` names) and your own `~/.pg_service.conf`; with equal names the first file wins.
  While the new folder has no `stands.conf`, the one pg-mcp 0.7.0 wrote to `~/.pg-mcp/` is read, and the
  import and the exports use that folder too. Sections named otherwise are ordinary psql services and stay
  out of it;
- psql reads the same sections: `psql service=dev7.demo.shop`.

**2. The entry.**

```bash
PG_EXPLORER_MCP_STANDS='*' python3 install.py --name stands
```

This registers `pg-stands` next to `pg`. `PG_EXPLORER_MCP_STANDS` switches stands on: `*` takes every stand,
patterns such as `dev*,ift2` take some. The check succeeds once the stands are found: a stand behind a
VPN is reached only when a tool needs it.

**Importing connections.** When the connections already live in DBeaver, or in a `pg_service.conf`
file, let `install.py` bring them in:

```bash
python3 install.py import dbeaver            # a preview: what would go where; nothing is written
python3 install.py import dbeaver --write    # write the registry and the password file, register pg-stands
python3 install.py import pg-service ~/.pg_service.conf --write
```

The stand comes from the DBeaver folder, then the connection name, then the host, matched with the
regular expression `(dev|ift)[-_ ]?\d+` (`--stand-pattern` changes it); a separator between the letters
and the number is dropped, so `DEV-27` is the stand `dev27`. A connection the pattern matches nowhere
takes its own name as the stand, `IFT-Caldera` becoming `ift-caldera`, and the preview says so. The
database is the connection's, from its URL first, and the section is named after its login: which
schemas that login reads the import cannot know, so the server reads such a database as a whole through
it, and a stand with a login per schema gets a section per login, as before. The preview lists
what would be written and what is left out and why; passwords are never shown. `--write` rewrites
`~/.pg-explorer-mcp/stands.conf` (adding to what is there, or replacing it with `--prune`), adds the passwords
to the password file without touching its other lines, keeps a dated backup of both files and
registers `pg-stands`; when anything fails, nothing is changed. DBeaver's saved passwords are read
from its own files while DBeaver keeps them under its default key; with its secure storage switched
on they stay unknown, and the preview says which logins need a line in the password file by hand.
The agent does the same with `/pg:import`, writing only after you confirm. A running server notices
a changed registry on its next call, so an import needs no restart; a registry file that holds a
`password=` line and is readable by other users gets a warning in `status`. A DBeaver connection with
several hosts (`h1:6544,h2` with the port 6544, as DBeaver writes it, or a URL
`jdbc:postgresql://h1:6544,h2:6544/db`) keeps them all, and the password file gets a line per host,
since libpq matches a line against one host.

**Exporting connections to database tools.** The other way round, `install.py export` puts the stands
into the tools you query them with yourself: a folder or group per stand, every connection logging in
with the password file. No password is copied anywhere.

```bash
python3 install.py export dbeaver                # a preview: what would go where; nothing is written
python3 install.py export dbeaver --write
python3 install.py export pgadmin --write --stands 'dev*,ift2'
```

| Tool | `export` | What it writes | Then |
|---|---|---|---|
| DBeaver | `dbeaver` | `data-sources-pg-mcp.json` next to DBeaver's `data-sources.json`, or in the folder `--workspace` names | Restart DBeaver |
| DataGrip and the JetBrains IDEs with database tools | `datagrip` | `.idea/dataSources.xml` and `dataSources.local.xml` in `~/.pg-explorer-mcp/datagrip`, or in the project `--project` names (close it first) | Open the folder as a project |
| pgAdmin 4 | `pgadmin` | `~/.pg-explorer-mcp/pgadmin-servers.json` | Tools > Import/Export Servers > Import |
| psql, pgcli | `pg-service` | Sections of `~/.pg_service.conf`, the schema first on the search path | `psql "service=dev7.demo.shop"` |
| PostgreSQL extension of VS Code (Microsoft) | `vscode-pgsql` | `pgsql.connections` in the user settings; `--editor cursor` or `windsurf` for those editors | Reload the window |
| SQLTools | `sqltools` | `sqltools.connections` in the user settings, with an empty password | Reload the window |

A stand with several hosts goes to psql, DBeaver and DataGrip with all of them. pgAdmin and the PostgreSQL
extension have one port field, so they get the list when the hosts share a port and the first host
otherwise; SQLTools connects to one host and gets the first.

An export replaces only the connections it brings, found by a stable id or by name, and keeps the
rest: what you made in the tool, and what an earlier export brought for other stands. Each changed
file keeps a dated copy and its permissions, and a file that is a link stays a link. Connections
DBeaver already has with the same host, port, database and user are left out, since the stands most
often come from DBeaver in the first place. In `~/.pg_service.conf` every exported section sits under
the line `# pg-explorer-mcp export`: the next export rewrites only such sections, the server does not take them
for stands, and a section of the same name that you wrote yourself stays as it is while its stand is
left out. The TLS settings of a stand, `sslmode` and `sslrootcert`, go along. A settings file with
comments is left alone, and the export prints what to add to it by hand. The PostgreSQL extension of
Microsoft cannot read the password file: it asks for each password once and keeps it in the system
keychain. Importing the file into pgAdmin again adds the servers again, so remove the old ones first.
After `import --write`, and after registering an entry while stands exist, `install.py` at a terminal
looks for these tools on the machine and offers to export into them.

**3. Use.** Every tool takes `stand`. Reading data also takes `schema`: the server connects with that
schema's login and looks for unqualified names in it first. `database` is needed only when the schema
exists in several databases of the stand.

| You ask | The agent calls |
|---|---|
| "Which stands are there?" | `status` |
| "Is dev7 reachable?" | `status` with `stand` |
| "Which schemas are on dev7?" | `list_schemas` |
| "Where are the orders on dev7?" | `search_schema` with `stand` and `text` |
| "How many orders per status are in shop on dev7?" | `run_select` with `stand` and `schema` (profile `analyst`) |
| "On which dev stand is there an order 123?" | `run_select` with `stands` and `schema` |
| "Which stands have a discount column in shop?" | `search_schema` with `stands` and `text` |
| "How does shop on dev7 differ from dev12?" | `compare_schema` with `stand_a`, `stand_b` and `schema` |
| "Which dev stands differ from dev7 in shop?" | `compare_schema` with `stand_a`, `stands` and `schema` |

Every answer starts with where it came from. Real answers on the demo database:

`list_schemas` with `stand: dev7`:

```text
dev7 / demo: hr, shop
```

`run_select` with `stand: dev7` and `schema: shop`:

```text
dev7 / demo / shop
status | count
shipped | 174
paid | 162
cancelled | 90
new | 74
(4 rows)
```

`compare_schema` with `stand_a: dev7`, `stand_b: dev12` and `schema: shop`, where dev12 is a copy of the demo
database in which the price gained two digits, orders gained a note and a table of returns, and an index
was dropped:

```text
dev7 / demo / shop  vs  dev12 / demo / shop
only on dev12:
  shop.returns | table
differ:
  shop.order_items.price: dev7 numeric(10,2) NOT NULL | dev12 numeric(12,2) NOT NULL
  shop.orders.note only on dev12: text
  shop.orders: index orders_customer_idx only on dev7: CREATE INDEX orders_customer_idx ON shop.orders USING btree (customer_id)
```

`compare_schema` with `stand_a: dev7`, `stands: dev*` and `schema: shop` — the stands that match, each
against dev7; up to three stands get the full differences, more stands get one line of counts each:

```text
dev7 / demo / shop  vs  3 stands:
dev8 / demo / shop: same as dev7
dev9 / demo / shop: same as dev7
dev12 / demo / shop
only on dev12:
  shop.returns | table
differ:
  shop.order_items.price: dev7 numeric(10,2) NOT NULL | dev12 numeric(12,2) NOT NULL
  shop.orders.note only on dev12: text
  shop.orders: index orders_customer_idx only on dev7: CREATE INDEX orders_customer_idx ON shop.orders USING btree (customer_id)
same as dev7: 2; different: 1; not checked: 0
```

The structure comes from the system catalog of each stand, read with the schema's own login in three
queries however many tables there are; the data is not read. A schema with no differences gets a single
line, such as `identical: 1 table, 3 columns`, and a schema the registry lists but one stand's database
lacks is an error, not a stand full of differences.

Without `schema`, `list_tables`, `search_schema` and `describe_table` cover the whole stand: the names
come from the system catalog, which every login can see, through one login per database, and only the
schemas of the registry are shown; a database with one login shows whatever that login can read. The data
stays behind each schema's own privileges: a login that cannot read a neighbouring schema gets
`permission denied` there.

**4. Several stands at once.** `search_schema` and `run_select` take `stands` instead of `stand`: stand
names separated by commas or a pattern, `dev1,dev2,dev3` or `dev*`, at most 100 stands in one call. The
stands are asked all at the same time, so the call takes as long as the slowest stand, not the sum, and
the answer lists them in the order `status` does, a section per stand under its `stand / database /
schema` line. A stand that is unreachable, has no login for the schema or rejects the query gets one line
saying so under its header, and the other stands still answer. Stands that answered with no rows, or no
matches, are folded into one line naming them, so the stand that has what was asked for stands out. The
200 rows of `run_select` and the 100 matches of `search_schema` are shared among the stands, at least 10
per stand, so the whole answer fits the budget. The call keeps to 25 of the client's 30 seconds: a stand
that has not answered by then is named as such, and the agent asks about it in another call.

`run_select` with `stands: dev*`, `schema: shop` and `SELECT count(*) FROM orders WHERE id = 123`:

```text
dev1 / demo / shop
count
1
(1 row)
dev2 / demo / shop
count
0
(1 row)
dev3 / demo / shop
  Connecting to dev3-db.example.com:5432 timed out. The host may be behind a firewall or a VPN.
```

A failure stays small. An unreachable stand or a broken login affects only the answers that need it,
and `status` with `stand` tries a second login when the server turned the first one down. Without
`PG_EXPLORER_MCP_STANDS`, or when no stand is found, the entry works with the `PG*` variables as before, and
`status` says why. At most 8 connections stay open (`PG_EXPLORER_MCP_MAX_CONNECTIONS`). A host that does not
answer is given up after 10 seconds and not tried again for the rest of that call, whatever else the
stand has on it; a second login of a database is tried only when the server answered and turned the
first one down. The reasoning is in [ADR 0006](docs/adr/0006-stands.md) (in Russian).

## Group profiles

The profile decides which tools the agent sees:

| Profile | Tools | For |
|---|---|---|
| `schema` | `status`, `list_tables`, `search_schema`, `describe_table` | people who need the structure only: the agent sees no data rows at all |
| `explore` | plus `sample_rows`, `explain_query` | developers; the default |
| `analyst` | plus `run_select` and `explain_query` with `ANALYZE` | analysts who need their own SQL |

Set it at registration (`python3 install.py --profile schema`) or with `PG_EXPLORER_MCP_PROFILE` in the server's
`env` block. An unknown value turns on the strictest profile, `schema`, and `status` warns about it.
A profile is a convenience, not an access boundary: what can be read is decided by the role's
privileges in the database.

## Language

The server speaks English (`en`) or Russian (`ru`): tool descriptions, answers, hints, refusals and
the output of `install.py`. The language is chosen like this:

1. the `PG_EXPLORER_MCP_LANG` variable (`install.py --lang` writes it);
2. otherwise the system locale: the `LANGUAGE`, `LC_ALL`, `LC_MESSAGES` and `LANG` variables, and the
   regional settings on Windows;
3. otherwise English.

`status` shows the current language. To change it, run `python3 install.py --lang ru` or edit
`PG_EXPLORER_MCP_LANG` in the settings, then restart the agent.

Tool and argument names, JSON-RPC protocol errors, the server log and PostgreSQL's own messages are not
translated; the language of the latter is set by the database's `lc_messages`.

Either way, the model is asked to answer in the user's language. If your group's model picks tools less
reliably from Russian descriptions, set `PG_EXPLORER_MCP_LANG=en`: it will still answer in Russian. The reasons
behind this design are in [ADR 0004](docs/adr/0004-languages.md) (in Russian).

## Security

Any change to data, structure, privileges, settings or other sessions is refused. There are three
layers of defence, and each one holds even if the others miss something:

1. **The PostgreSQL role** is the real boundary: it has only `SELECT` on the chosen schemas
   ([a script for DBAs](#for-dbas-a-role-for-the-agent)).
2. **A `READ ONLY` transaction for every query**, always rolled back, with the statement timeout and the
   schema's search path set for that transaction alone: a connection pooler in transaction mode between
   the server and PostgreSQL (pgbouncer, Odyssey) changes nothing. A query goes out as a single
   statement, so PostgreSQL rejects a string such as `SELECT 1; COMMIT; DROP …`.
3. **A check of the query text** lets only read-only statements through and refuses `INSERT`, `UPDATE`,
   `DELETE`, `MERGE`, `SELECT … INTO`, row locks and functions with side effects (`pg_terminate_backend`,
   `set_config`, `dblink`, `pg_sleep` and others), telling the model why.

The full policy is in [ADR 0002](docs/adr/0002-read-only-sql-policy.md) (in Russian).

Everything the tools return goes into the model's context. With a local model inside your organisation
the data stays there, but pick the profile by the task: `schema` shows no data at all.

The server takes passwords from the [password file](#4-the-password-goes-into-the-password-file), as
`psql` does, and only from the line for its own login.

## Query log

To keep a journal of what the agent asked the database, set `PG_EXPLORER_MCP_QUERY_LOG` to an absolute file path
(`~` is fine) before running `install.py`, or add it to the server's `env` block later:

```bash
export PG_EXPLORER_MCP_QUERY_LOG=~/pg-explorer-mcp-queries.jsonl
python3 install.py
```

Every tool call adds one JSON line. These two are real, from the demo database:

```json
{"time": "2026-10-22T13:54:20.570Z", "os_user": "anna", "pid": 95715, "python": "3.9.6", "role": "mcp_demo_agent", "database": "pg_mcp_demo", "tool": "run_select", "arguments": {"query": "SELECT status, count(*) FROM shop.orders GROUP BY status"}, "outcome": "ok", "rows": 4, "duration_ms": 12.6}
{"time": "2026-10-22T13:54:20.571Z", "os_user": "anna", "pid": 95715, "python": "3.9.6", "role": "mcp_demo_agent", "database": "pg_mcp_demo", "tool": "run_select", "arguments": {"query": "DELETE FROM shop.orders WHERE status = 'cancelled'"}, "outcome": "refused", "message": "DELETE is not allowed. Only read-only statements are: SELECT, WITH ... SELECT, VALUES, TABLE, EXPLAIN and SHOW.", "duration_ms": 0.0}
```

- `outcome` is `ok`, `refused` (by the SQL check), `error` (bad arguments or a PostgreSQL error),
  `unavailable` (no connection) or `failed` (a bug in the server);
- `rows` is how many rows the agent's own statement returned (`sample_rows`, `run_select`, `explain_query`);
- `python` is the version of Python the server runs on: the report below counts who runs which, which is
  what raising the minimum version would cost;
- a call over several stands (`stands`) is still one line: `stands` maps each stand to its outcome (`ok`,
  `unavailable`, `error`, or `skipped` when the call had no time left for it), `rows` is the total, and
  `role` and `database` are empty, since every stand has its own;
- the rows themselves and the tools' answers are never written.

The file is created readable by its owner only, since literals in SQL may carry personal data, and a
symlink in its place is refused. It is opened anew for every line, so several agent sessions can share
it and `logrotate` can move it away. If the file cannot be written, tool calls keep working and
`status` says why.

To sum the log up for whoever looks after the group, run `scripts/querylog_report.py` with the files,
rotated `.1` and `.gz` ones too, and, if needed, a UTC window of `--since` and `--until` (a date or a
date-time; a bare date in `--until` covers that whole day):

```bash
python3 scripts/querylog_report.py ~/pg-explorer-mcp-queries.jsonl ~/pg-explorer-mcp-queries.jsonl.1.gz --since 2026-10-20
```

The report counts calls by tool, by OS user and, in stands mode, by stand (a call over several stands
counts on each), and the users on each version of Python; it gives the median and 95th percentile of `duration_ms`, the ten longest calls and every
call the SQL check refused, which is what an attempt to write looks like. Arguments go on one line cut to
80 characters, values of keys that look like a password are hidden, and lines that are not complete log
lines, say cut by a crash or by rotation, are skipped and counted:

```text
Files: /home/anna/pg-explorer-mcp-queries.jsonl (13 lines, 1 broken)
Calls: 12  ok 8  error 1  refused 2  unavailable 1  failed 0
Period: 2026-10-22 09:12:03 .. 2026-10-22 16:41:05 UTC
Users: 2  Tools: 7
Duration: median 11.2 ms, 95th percentile 2003.5 ms

By tool
  tool            calls  ok  error  refused  unavailable  failed  problems
  run_select          5   2      1        2            0       0     60.0%
  list_tables         2   1      0        0            1       0     50.0%
  describe_table      1   1      0        0            0       0      0.0%
  explain_query       1   1      0        0            0       0      0.0%
  sample_rows         1   1      0        0            0       0      0.0%
  search_schema       1   1      0        0            0       0      0.0%
  status              1   1      0        0            0       0      0.0%

By user
  user   calls  queries  rows  refused
  anna       7        1     4        2
  boris      5        3    18        0

Longest calls (top 10)
  duration_ms  time                 user   tool            arguments
       2003.5  2026-10-22 16:30:00  anna   list_tables
         48.3  2026-10-22 14:05:11  boris  run_select      query=SELECT c.name, sum(o.total) AS total FROM shop.orders o JOIN shop.custome…
         35.0  2026-10-22 14:02:15  boris  search_schema   pattern=customer
  …

Refused by the SQL check (2)
  time                 user  tool        arguments
  2026-10-22 13:54:20  anna  run_select  query=DELETE FROM shop.orders WHERE status = 'cancelled'
  2026-10-22 16:41:05  anna  run_select  query=UPDATE shop.products SET price = price * 1.1
```

`--format json` gives the same figures to scripts, `--format csv` one table, the breakdown that
`--by user|tool|stand|day` chooses.

The file lives on the user's machine, so it is a journal, not a tamper-proof audit. To audit data access,
log on the PostgreSQL server (`pgaudit` or `log_statement`); the server's connections carry
`application_name = 'pg-explorer-mcp'`. The reasoning is in [ADR 0005](docs/adr/0005-query-log.md) (in Russian).

## Settings

All settings are environment variables in the `env` block of the `pg` server in the agent settings.
`install.py` copies them there from the terminal. Settings written by pg-mcp 0.7.0 and earlier name them
`PG_MCP_*`: those names keep working, the `PG_EXPLORER_MCP_*` one wins when both are set, and `install.py`
writes only the new ones.

| Variable | Default | Meaning |
|---|---|---|
| `PGHOST` | `localhost` | database host, or several separated by commas, tried in turn; or the Unix socket directory (starts with `/`) |
| `PGPORT` | `5432` | port; with several hosts, one for all of them or one per host, separated by commas |
| `PGDATABASE` | same as `PGUSER` | database |
| `PGUSER` | the OS user name | role |
| `PGSSLMODE` | `prefer` | `disable`, `allow`, `prefer`, `require`, `verify-ca`, `verify-full`; only the last two check the certificate |
| `PGSSLROOTCERT` | — | CA certificate for `verify-ca` and `verify-full` |
| `PGPASSFILE` | `~/.pgpass`, on Windows `%APPDATA%\postgresql\pgpass.conf` | password file |
| `PGPASSWORD` | — | works, but `install.py` never copies it: the password belongs in the password file |
| `PG_EXPLORER_MCP_PROFILE` | `explore` | `schema`, `explore` or `analyst` |
| `PG_EXPLORER_MCP_LANG` | from the locale | `en` or `ru` |
| `PG_EXPLORER_MCP_MAX_RESPONSE_CHARS` | `8000` | budget of one answer in characters, at least 500 |
| `PG_EXPLORER_MCP_STATEMENT_TIMEOUT_MS` | `5000` | timeout of one query in milliseconds |
| `PG_EXPLORER_MCP_LOG_LEVEL` | `INFO` | detail of the stderr log: `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `PG_EXPLORER_MCP_QUERY_LOG` | — (off) | absolute path of the [query log](#query-log) file |
| `PG_EXPLORER_MCP_NAME` | `pg` | the entry's name that `status` shows; `install.py --name` writes it |
| `PG_EXPLORER_MCP_SOCKET_TIMEOUT_S` | `30`, for stands `10` | seconds to wait for a new connection; once connected, the socket waits at least the query timeout plus 5 seconds |
| `PG_EXPLORER_MCP_STANDS` | — (off) | `*` or stand name patterns such as `dev*,ift2`: one entry for [many stands](#many-stands) |
| `PG_EXPLORER_MCP_STANDS_FILE` | `~/.pg-explorer-mcp/stands.conf`, on Windows `%APPDATA%\pg-explorer-mcp\stands.conf` | the stands registry |
| `PGSERVICEFILE` | `~/.pg_service.conf`, on Windows `%APPDATA%\postgresql\.pg_service.conf` | your psql services; stands are read from it too |
| `PG_EXPLORER_MCP_MAX_CONNECTIONS` | `8` | connections an entry for stands keeps open |

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
| "… is unreachable from this machine" | no route to the host: the VPN is down | connect the VPN |
| "… accepted the connection, then closed it during the login" | the server refused the login, but its message was lost (Windows drops it on a reset); usually the password or `pg_hba.conf` | check the password file, then ask the DBA |
| "Login as … failed" or "the server asks for a password" | no password in the password file, a wrong line, or file permissions | check the path, the line format and `chmod 600` |
| "Database … does not exist" | wrong database name | fix `PGDATABASE` |
| "The server refused the login for …" | `pg_hba.conf`, or the role lacks `LOGIN`/`CONNECT` | ask the DBA |
| "TLS with … failed" | the TLS mode does not match the server | check `PGSSLMODE` and `PGSSLROOTCERT` |
| "The current role cannot read any table" | the role has no `SELECT` | ask the DBA: [a role for the agent](#for-dbas-a-role-for-the-agent) |
| `settings.json` "is not plain JSON" | the settings file has comments | add the printed snippet by hand |
| An answer ends with "… answer cut to" | the answer budget kicked in | narrow the question, or raise `PG_EXPLORER_MCP_MAX_RESPONSE_CHARS` with the model's context in mind |
| `python3` not found | Python is missing or has another name | `python --version`, on Windows `py -3 --version`; install Python 3.9+ |
| "query log: …, not written: …" | the folder does not exist, no permission, a relative path, or a symlink in place of the file | fix `PG_EXPLORER_MCP_QUERY_LOG` |
| "No stand … in the registry" | a typo, or the stand is missing from the registry or from `PG_EXPLORER_MCP_STANDS` | `status` lists the stands; check the registry |
| "PG_EXPLORER_MCP_STANDS is set, but no stands were found" | no registry file, or its sections are not named `stand.database.schema` | check the files the warning names |

The server logs to stderr; `PG_EXPLORER_MCP_LOG_LEVEL=DEBUG` adds detail. If the server does not answer the
check, `install.py --check` prints that log.

## Updating and removing

To update a clone, run `git pull` in its folder and restart the agent. To update an unpacked archive, take
the new `pg-explorer-mcp-<version>.zip` from the
[Releases page](https://github.com/dbelokursky/pg-explorer-mcp/releases), delete the old folder, unpack the new
one in its place and restart the agent. Run `install.py` again only if the folder's path changed or
you changed Python, the profile or the language.

**From pg-mcp 0.7.0 and earlier.**

- A clone needs only `git pull` and a restart. The agent settings point at its `run.py`, the extension
  shows up under the new name by itself, and the old `PG_MCP_*` variables, the stands registry in
  `~/.pg-mcp/` and earlier exports keep working.
- An archive now unpacks into `pg-explorer-mcp/` rather than `pg-mcp/`. Run `install.py` from the new
  folder once: it rewrites the paths in the agent settings. Then link the extension again.
- GigaCode keeps an extension linked before 0.8.0 in `~/.gigacode/extensions/pg-mcp` (Qwen Code in
  `~/.qwen/extensions/pg-mcp`) and refuses a new link while that folder is there. Delete it first;
  `install.py` names it when it finds it.
- The server's connections now carry `application_name = 'pg-explorer-mcp'`: a DBA's filter on
  `pg-mcp` needs the new name.

To remove, delete `pg` (and any `pg-<name>` entries) from `mcpServers` in the agent settings (backups of earlier settings sit next to
the file), run `gigacode extensions uninstall pg-explorer-mcp` and delete the folder. An extension linked
before 0.8.0 is removed by deleting `~/.gigacode/extensions/pg-mcp`.

## FAQ

**Does it need internet access, pip or admin rights?** No. Everything the server needs is in the
repository; it only needs a network path to the database.

**Is the `pg-mcp` package on PyPI this server?** No, that is another project; this server was called
pg-mcp too until 0.8.0. It is not on PyPI and installs only from GitHub, as a clone or a release archive
([step 1](#1-the-code)): `pip install pg-mcp` and `uvx pg-mcp` install the other project.

**Can the agent change anything in the database?** No. Even if text inside the data tricks the model,
the role's privileges, the read-only transaction and the text check stop the query.

**Which PostgreSQL versions does it work with?** CI tests it against PostgreSQL 13 to 18. Before 14,
a table PostgreSQL has never analyzed looks exactly like an empty one, so `list_tables` calls both
"empty or never analyzed".

**Which MCP protocol versions does it speak?** Revisions 2024-11-05 to 2025-11-25 (with the
`initialize` handshake) and 2026-07-28 (without it).

**Which clients is it checked with?** CI starts Qwen Code, the open-source base of GigaCode CLI, with the
server registered by `install.py`: the client must connect to the server and see the extension. GigaCode
CLI itself is not available outside companies, so it is checked by hand before a release.

**Will another MCP client do?** Yes, any client that starts servers over stdio. `install.py --client`
writes the entry for VS Code, Cursor, Windsurf, Cline, Roo Code, Continue, LM Studio, Claude Desktop and
JetBrains ([other agents and editors](#other-agents-and-editors)); for any other client,
`install.py --client cursor --print` shows the command, the arguments and the `PG*` variables to give it.

**Why does the agent answer in the wrong language?** The model chooses the language of its answers;
the server asks it to use the user's language. `PG_EXPLORER_MCP_LANG` sets the language of the tools themselves.

## Development

Environment, checks, code layout and rules are in [CONTRIBUTING.md](CONTRIBUTING.md). Decisions and
their reasons are in [docs/adr/](docs/adr/), the version history is in [CHANGELOG.md](CHANGELOG.md).
These documents are in Russian. How to report a vulnerability is in [SECURITY.md](SECURITY.md).

## License

pg-explorer-mcp is under the MIT license ([LICENSE](LICENSE)). The packages it ships unchanged in
`src/pg_explorer_mcp/_vendor` keep their own licenses, listed in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
