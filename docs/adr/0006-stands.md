# ADR 0006. Many stands in one entry

Status: accepted, 2026-10-22.

## Context

A developer works with dozens of stands: Dev1–Dev50 and IFT1–IFT3. Each stand has several databases,
each database has dozens of schemas, and each schema usually has its own user with the same name and its own
password. Some users can also read neighbouring schemas, but this cannot be relied on. Stand addresses
follow no pattern. The connections are kept in DBeaver today, and the stands are reached over VPN.

An entry per stand (`install.py --name`) does not scale: each entry brings the model its own
set of tools, about 5 thousand characters of descriptions in every request, and there are hundreds of connections.

## Decision

| Question | Choice | Why |
|---|---|---|
| How many entries in the agent | one entry for all stands, usually `pg-stands` | the tools are described once, and the context does not grow with the number of stands |
| How it is switched on | the `PG_EXPLORER_MCP_STANDS` variable: `*` or comma-separated patterns of stand names (`dev*,ift2`) | without it the server works as before, from the `PG*` variables, whatever is on disk |
| Where the registry is | the libpq `pg_service.conf` format: a section per connection named `stand.database.schema` | a standard format: `psql service=dev7.orders.billing` understands the same sections |
| Which files | `PG_EXPLORER_MCP_STANDS_FILE` (by default `~/.pg-explorer-mcp/stands.conf`, written by the import) and the user's ordinary `pg_service.conf` | the server only reads; with equal names the import file wins |
| Credentials | in the section, `host`, `port`, `dbname`, `user` (by default the schema name); the password goes into `~/.pgpass` | the password is looked up as before; `password=` in a section works but is not recommended |
| Several hosts | `host=h1,h2` with one `port`, or `port=p1,p2`, as in libpq; the server tries the hosts in order and takes the first that lets it in; the password is looked up for each host separately | this is how DBeaver stores a connection to a stand with a standby host (`h1:6544,h2` and the port 6544), and psql understands the same sections |
| What is taken from the entry | `PGSSLMODE`, `PGSSLROOTCERT`, timeouts and the password file, for all stands unless a section sets its own; the entry's host, user and password, never | there is one TLS requirement for the whole company, and someone else's login on a stand is a mistake |
| How the agent picks a connection | the `stand` and `schema` arguments, and `database` when a schema name repeats; a schema not in the registry is read by the login of a database read as a whole, and if a stand has several such databases, `database` says which one it is in | the model knows nothing about users: the server takes the login of the right schema itself |
| Structure across a stand | the system catalog through any login of the database, only for the schemas of the registry; for a database with one login, everything that login reads | the PostgreSQL catalog is visible to everyone; one query instead of a connection as each schema |
| One login per database | a database of a stand whose sections all share one login (one DBeaver connection is one section, named after its login: `[ift-caldera.caldera_ift1.cldr_testers_user]`) is read as a whole through it: `schema` takes any schema, and the list of schemas and tables comes from the catalog by the login's privileges, as for an entry for one database; a database with several logins works only with the schemas of the registry | on IFT one login reads dozens of schemas, and the import does not know which; a registry stricter than the database would hide them all, while PostgreSQL checks the privileges anyway |
| Comparing stands | `compare_schema` with `stand_a`, `stand_b` and `schema`: the structure of the schema on both stands from the catalog under its login, three queries per stand, only the differences in the answer: tables, columns, constraints, indexes; a schema missing from the database of one stand is an error | "how does orders on dev7 differ from dev12" is one call instead of describe_table for every table on two stands; no data is read |
| One reference against many | `compare_schema` with `stands` instead of `stand_b`: the reference is read once, the stands one after another within a 25-second budget, no more than 10; up to three stands get the full differences, more get a line of counts per stand; an unavailable stand does not get in the way of the others | "which stands differ from dev7" is one call; details for ten stands would not fit the answer budget |
| Data | only through the login of the right schema, with `search_path` set to that schema | the database checks privileges itself; without access to a neighbouring schema the answer is `permission denied` |
| Connections | opened on demand, no more than 8 kept, the rest closed; connecting waits 10 seconds, an open socket no less than the query timeout plus 5 seconds | hundreds of connections do not hang around; a dropped VPN does not hold up the agent for 30 seconds; a long query is not cut off by the network and is not run again |
| A host did not answer | within one call, a host that did not answer (no route, a timeout, the port refused) is not tried again under any login; a second login of the database is tried only when the server answered and rejected the first | the client waits 30 seconds for a tool's answer: a stand of three databases behind a dropped VPN would cost 60 seconds and a broken call instead of a hint |
| Checking a stand | `status` with `stand` tries the stand's first login; if the server rejected it, the second | a broken login of one schema does not make the whole stand look unavailable |
| What the answer shows | the first line of the answer is `stand / database / schema`; the same goes into the query log | it is always clear where the data comes from |
| One query on several stands | `stands` in `run_select` and `search_schema`: comma-separated names or a pattern, no more than 10 per call; the stands are queried one after another, the answer has a section per stand, and a stand's failure is a line under its heading; rows and matches are shared between the stands | "which stand has the order" is one call, not ten; the client waits 30 seconds for an answer, an unavailable stand costs a network timeout, and the limit of 10 stands with 25 seconds per call keeps the answer within bounds; threads would complicate the pool and the log |

## Breaking nothing

- Stands mode is switched on only by the `PG_EXPLORER_MCP_STANDS` variable. An entry without it works as before.
- The registry is read leniently: a missing file, an unreadable file, a section with an error are all skipped
  by the server, which warns in `status` and works with what it could read. If no stands are left
  at all, the server works from the `PG*` variables, as before.
- A failure of one stand (no network, a wrong password) affects only that stand.
- The import is a separate command, and the server does not run it. The import writes only its own registry file and
  appends to `~/.pgpass`, deleting nothing; on any error it writes nothing.

## Import

`install.py import dbeaver` and `install.py import pg-service <file>`: by default a preview of which
`stand.database.schema` sections would go into the registry, how many passwords into the password file, what is left out and why;
writing happens only with `--write`. The stand is recognised from the DBeaver folder, the connection name and the host with a regular
expression (by default `(dev|ift)[-_ ]?\d+`, changed with `--stand-pattern`; a separator between the letters and the
number is dropped, and a connection where the pattern found nothing takes its own name as the stand), the database comes from
the connection, from its URL first, and in place of the schema goes its login: the import does not know which schemas that login reads, and the server
reads such a database as a whole through it. DBeaver's saved passwords are taken from `credentials-config.json`: it is encrypted with AES-128-CBC
under DBeaver's default key, and `src/pg_explorer_mcp/aes.py` implements this cipher in pure Python, without
dependencies; with DBeaver's secure storage the passwords stay unknown and are added by hand.
Writing prepares both files in full, then puts them in place with a dated copy of the old ones; the password file
is only added to and updated, and the registry is added to or, with `--prune`, replaced; on a failure nothing
changes. After writing, the `pg-stands` entry is registered with `PG_EXPLORER_MCP_STANDS=*`, and the `pg` entry is left alone.
When `install.py` is run by hand in a terminal and finds DBeaver, it offers to show the preview; the agent
does the same with the `/pg:import` command and writes only after the user confirms.

The server rereads the registry when its files have changed (by time and size, before every call),
so an import is visible without restarting the agent; if the stands could not be read after a change,
the old ones stay, and `status` warns. A registry file with a `password=` line that other users
can read also gets a warning.

## Alternatives considered

- **An entry per stand**: already works through `--name`, but with hundreds of connections it eats up the model's context.
- **A list of connections in the tool description** (`enum`): hundreds of names in every request to the model.
- **A "switch to stand" command** that remembers the current one: the model loses track of where it is, and parallel calls
  become ambiguous; an explicit argument in every call is more reliable.
- **Our own registry format** (JSON, TOML): one more format where a standard one already exists.
- **Reading DBeaver at every server start**: a failure to parse someone else's format would break the server; an import
  as a separate command is safer.

## Consequences

- In stands mode the tools take `stand`; for data `schema` is required too.
- The stands entry is registered by the same `install.py`: `PG_EXPLORER_MCP_STANDS='*' install.py --name stands`. For the
  installation check it is enough that stands are found: at home, without VPN, the stands are unreachable, and that is not an installation error.
- The profile, language, budget and log are shared by all stands of an entry. If different profiles are needed, you can
  set up several entries with different `PG_EXPLORER_MCP_STANDS` filters.
- A call over several stands (`stands`) queries them at the same time, a thread per stand, no more than 100 per
  call and within 25 seconds: the call takes as long as the slowest stand, and a stand that has not answered
  in that time is named at the end of the answer. The connection pool is shared between the threads: its bookkeeping runs under a
  lock, and the connection of a stand that is still answering is not closed until its thread has finished. The rows
  of `run_select` and the matches of `search_schema` are shared between the stands, no fewer than 10 per stand, so that the answer
  fits the budget; stands with no rows and no matches are folded into one line.
- The query log writes one line for such a call, not a line per stand: the agent's call stays one
  line with its duration, as in ADR 0005. The `stands` field maps each stand to its result (`ok`,
  `unavailable`, `error`, `skipped`), `rows` is the sum over the stands, and `role` and `database` are empty, because
  each stand has its own login. A line per stand would turn one call into ten lines and lose
  its duration; the breakdown by stand is in `stands`.
