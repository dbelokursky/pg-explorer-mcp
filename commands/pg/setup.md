---
description: Install or repair the pg-mcp server and check the database connection
---
Set up or repair the read-only PostgreSQL MCP server `pg` from the pg-mcp extension.

1. Find the pg-mcp folder. If the `status` tool of the `pg` server works, its first line says where
   pg-mcp is installed. Otherwise read `mcpServers.pg.args` in `~/.gigacode/settings.json` (the path
   before `server/run.py`), or ask the user where they cloned pg-mcp.
2. Find Python 3.9 or newer: try `python3 --version`, then `python --version`, and on Windows
   `py -3 --version`. If none works, tell the user to install Python 3.9+ (from the corporate
   software catalog, or the "embeddable package" zip from python.org, which needs no admin rights)
   and stop.
3. Run `<python> <pg-mcp folder>/install.py`. It registers the server in `~/.gigacode/settings.json`,
   backs up the old file, starts the server and prints its status.
4. If the status says the database is unavailable, follow its hint. Connection settings go into the
   `env` block of `mcpServers.pg`: PGHOST, PGPORT, PGDATABASE, PGUSER, PGSSLMODE. The password goes
   into `~/.pgpass`, which the user edits themselves. Then run
   `<python> <pg-mcp folder>/install.py --check` again.
5. When the check reports "database: connected", tell the user to restart GigaCode so it starts the
   server with the new settings.
