# pg-mcp

The `pg` MCP server gives read-only access to one PostgreSQL database.

- Call its `status` tool first: it shows the database, the role and the limits.
- In a large database, find tables and columns with `search_schema` (a word from a name or comment)
  instead of listing everything with `list_tables`.
- If the `pg` tools are missing or `status` says the database is unavailable, run `/pg:setup`.
- Never ask the user to paste a database password into the chat; it belongs in ~/.pgpass.
- The server speaks English or Russian (`PG_MCP_LANG`). Whichever it speaks, reply to the user
  in the language they write in.
