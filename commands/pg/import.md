---
description: Bring stand connections from DBeaver or pg_service.conf into pg-mcp, after a preview the user confirms
---
Bring the user's stand connections into pg-mcp: from DBeaver, or from a `pg_service.conf` file they name.

1. Find the pg-mcp folder and Python the way `/pg:setup` does (the `status` tool's first line, or
   `mcpServers.pg.args` in the agent settings, or ask).
2. Preview first, never write first: run `<python> <pg-mcp folder>/install.py import dbeaver` (add
   `--lang ru` if the user writes in Russian; `install.py import pg-service <file>` for a service file;
   `--workspace <folder>` when DBeaver keeps its `data-sources.json` elsewhere). The output lists the
   connections that would go into the stands registry as `[stand.database.schema]`, how many passwords
   would go into the password file, and what is left out and why. Passwords are never printed; do not
   try to print them either.
3. Show the user the preview and ask whether to write it. If a stand was not recognised, ask how the
   stand is named in their folders or connection names and retry with
   `--stand-pattern '<regular expression>'`; the default is `(dev|ift)\d+`.
4. Only after the user says yes, run the same command with `--write`. It writes the registry
   (`~/.pg-mcp/stands.conf`) and adds the passwords to the password file (`~/.pgpass`), keeping backups
   of both, and registers the `pg-stands` entry next to `pg` with `PG_MCP_STANDS=*`. With `--prune` the
   registry is replaced instead of added to; the password file is only ever added to.
5. Tell the user to restart the agent, then check with the `status` tool of `pg-stands`: it lists the
   stands and warns about anything the registry could not read. A stand the user sees in DBeaver but not
   here was left out in step 2 for a reason the preview named.
6. If the user also works with the stands in DataGrip, pgAdmin, psql or VS Code, offer to export them
   there: `<python> <pg-mcp folder>/install.py export <tool>` previews, with `<tool>` one of `datagrip`,
   `pgadmin`, `pg-service`, `vscode-pgsql`, `sqltools` or `dbeaver` (connections DBeaver has already are
   left out). Run the same command with `--write` only after the user confirms, and pass on its last
   line, which says what to do in the tool. No password goes into the tools: they log in with the
   password file.
