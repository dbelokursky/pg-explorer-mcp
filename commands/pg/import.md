---
description: Bring stand connections from DBeaver, DataGrip or pg_service.conf into pg-explorer-mcp, after a preview the user confirms
---
Bring the user's stand connections into pg-explorer-mcp: from DBeaver, from DataGrip or another JetBrains IDE
(IntelliJ IDEA, PyCharm and the like), or from a `pg_service.conf` file they name.

1. Find the pg-explorer-mcp folder and Python the way `/pg:setup` does (the `status` tool's first line, or
   `mcpServers.pg.args` in the agent settings, or ask).
2. Preview first, never write first: run `<python> <pg-explorer-mcp folder>/install.py import dbeaver` (add
   `--lang ru` if the user writes in Russian; `install.py import datagrip` for DataGrip and the other
   JetBrains IDEs; `install.py import pg-service <file>` for a service file; `--workspace <folder>` when
   DBeaver keeps its `data-sources.json` elsewhere). The output lists the connections that would go
   into the stands registry as `[stand.database.schema]`, how many passwords would go into the password
   file, and what is left out and why. The schema part is the connection's login; when that login reads
   many schemas, the server reads the whole database through it, so such a section needs no editing by
   hand. Passwords are never printed; do not try to print them either. `import datagrip` reads the
   global data sources of the IDEs and DataGrip's projects in `~/DataGripProjects`; when the stands are
   data sources of a project of code, add `--project <project folder>`. The IDEs keep passwords in the
   system keychain or KeePass, which the import never reads: the preview marks those connections, and
   the user adds their lines to `~/.pgpass` by hand, never in the chat.
3. Show the user the preview and ask whether to write it. A connection whose folder, name and host
   match no stand pattern gets its own name as the stand (`IFT-Caldera` becomes `ift-caldera`), and the
   preview marks such lines; if the user wants a numbered stand instead, ask how the stand is named in
   their folders or connection names and retry with `--stand-pattern '<regular expression>'`; the
   default is `(dev|ift)[-_ ]?\d+`, and a separator between the letters and the number is dropped.
4. Only after the user says yes, run the same command with `--write`. It writes the registry
   (`~/.pg-explorer-mcp/stands.conf`) and adds the passwords to the password file (`~/.pgpass`), keeping backups
   of both, and registers the `pg-stands` entry next to `pg` with `PG_EXPLORER_MCP_STANDS=*`. With `--prune` the
   registry is replaced instead of added to; the password file is only ever added to.
5. Tell the user to restart the agent, then check with the `status` tool of `pg-stands`: it lists the
   stands and warns about anything the registry could not read. A stand the user sees in DBeaver or
   DataGrip but not here was left out in step 2 for a reason the preview named.
6. If the user also works with the stands in other tools (DataGrip, pgAdmin, psql, VS Code), offer to
   export them there, but not to the tool they came from:
   `<python> <pg-explorer-mcp folder>/install.py export <tool>` previews, with `<tool>` one of `datagrip`,
   `pgadmin`, `pg-service`, `vscode-pgsql`, `sqltools` or `dbeaver` (connections DBeaver has already are
   left out). Run the same command with `--write` only after the user confirms, and pass on its last
   line, which says what to do in the tool. No password goes into the tools: they log in with the
   password file.
