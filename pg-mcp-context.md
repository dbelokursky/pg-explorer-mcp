# pg-mcp

Each `pg` MCP server gives read-only access to one PostgreSQL database. There may be several, one per
database: `pg`, and `pg-<name>` for every further one. An entry for many stands (usually `pg-stands`)
reaches dozens of stands through the same tools.

- Call `status` first: it names the server and shows the database, the role and the limits. With several
  servers, use the one whose database the user means.
- In a large database, find tables and columns with `search_schema` (a word from a name or comment)
  instead of listing everything with `list_tables`.
- On an entry for many stands every tool takes `stand`, and reading data takes `schema` as well: the
  server uses that schema's login. `status` lists the stands and `list_schemas` the schemas of one;
  pass `database` only when an answer asks for it. Every answer starts with stand / database / schema.
- To ask several stands the same thing, `search_schema` and `run_select` take `stands` instead of `stand`:
  names separated by commas or a pattern (`dev1,dev2`, `dev*`), at most 10 per call. The answer has a
  section per stand, and a stand that did not answer says why under its line.
- To learn how a schema differs between two stands (tables, columns, constraints, indexes), call
  `compare_schema` with `stand_a`, `stand_b` and `schema` instead of describing tables on each stand.
- If the `pg` tools are missing or `status` says the database is unavailable, run `/pg:setup`.
- Never ask the user to paste a database password into the chat; it belongs in ~/.pgpass.
- The server speaks English or Russian (`PG_MCP_LANG`). Whichever it speaks, reply to the user
  in the language they write in.
