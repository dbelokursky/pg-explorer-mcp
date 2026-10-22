# pg-mcp

The `pg` MCP server gives read-only access to one PostgreSQL database.

- Call its `status` tool first: it shows the database, the role and the limits.
- If the `pg` tools are missing or `status` says the database is unavailable, run `/pg:setup`.
- Never ask the user to paste a database password into the chat; it belongs in ~/.pgpass.
