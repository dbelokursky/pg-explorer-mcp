# ADR 0001. Python with no external dependencies

Status: accepted, 2026-10-22.

## Context

- The client is a corporate build of a CLI agent from the Qwen Code / Gemini CLI family.
- The server runs locally for each user and talks to the agent over stdio.
  The agent runs on Qwen or DeepSeek models deployed inside the organisation.
- We distribute only the sources from the repository, not built artefacts.
  In corporate mode the client installs extensions only from a local folder.
- The users are different groups, and no runtime is guaranteed: not everyone has a JDK,
  Node may be missing too, and the network is corporate (SSL inspection, mirrors).
- Python 3.9+ is available almost everywhere: on Linux as the system Python, on macOS with the
  Command Line Tools (3.9.6), on Windows for some of the users.

## Decision

- Python 3.9+, standard library only.
- We implement the MCP protocol (JSON-RPC over stdio) ourselves, to the extent of a server that offers
  only tools: `initialize`, `ping`, `tools/list`, `tools/call`; revisions from 2024-11-05
  to 2025-11-25.
- The PostgreSQL driver is pg8000 (pure Python, BSD-3-Clause) with its dependencies scramp, asn1crypto,
  python-dateutil and six. Their sources live in `src/pg_explorer_mcp/_vendor/` together with their licenses.
- Registration is done by `install.py`: it writes the server into the agent settings with the absolute
  path to the interpreter it runs on, and checks that the server starts. The extension
  gives the agent a short context and the `/pg:setup` command for repairs.

## Alternatives considered

| Option | Why not |
|---|---|
| Java + Spring AI or the MCP Java SDK | not everyone has a JDK; building from source needs Maven and a configured mirror |
| TypeScript + the official SDK | Node may not be on PATH: the corporate and standalone builds of Qwen Code carry their own Node and do not expose it |
| Go, a single binary | we distribute only sources, and users have no Go toolchain |
| The official Python SDK (`mcp`) | needs Python 3.10+, pip and native packages (pydantic-core, cryptography, pywin32) |
| A remote server | the requirement is a local server for each user |

## Consequences

- The protocol code is ours: we keep it in line with the specification through tests and follow new revisions ourselves.
- Revision 2026-07-28 removed `initialize`: the protocol version travels in the `_meta` of every request, and
  `server/discover` reports which revisions the server understands. Since version 0.2.0 the server supports both
  eras: a request with a version in `_meta` is served under 2026-07-28, and `initialize` switches on the old rules.
- stdin is read as bytes, and answers are ASCII-only JSON, so the Windows console code page
  (cp866/cp1251) does not break the protocol.
- `Database.query` runs exactly one statement through the extended protocol, and PostgreSQL rejects a string
  such as `SELECT 1; COMMIT; DROP ...`. This closes the class of holes found
  in the archived reference server `server-postgres`.
- We update the dependencies in `src/pg_explorer_mcp/_vendor/` by hand, following `src/pg_explorer_mcp/_vendor/README.md`.
