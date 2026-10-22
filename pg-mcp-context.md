# pg-mcp

Each `pg` MCP server gives read-only access to one PostgreSQL database. There may be several, one per
database: `pg`, and `pg-<name>` for every further one.

- Call `status` first: it names the server and shows the database, the role and the limits. With several
  servers, use the one whose database the user means.
- In a large database, find tables and columns with `search_schema` (a word from a name or comment)
  instead of listing everything with `list_tables`.
- If the `pg` tools are missing or `status` says the database is unavailable, run `/pg:setup`.
- Never ask the user to paste a database password into the chat; it belongs in ~/.pgpass.
- The server speaks English or Russian (`PG_MCP_LANG`). Whichever it speaks, reply to the user
  in the language they write in.
