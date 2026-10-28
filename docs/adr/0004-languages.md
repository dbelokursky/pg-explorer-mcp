# ADR 0004. English and Russian

Status: accepted, 2026-10-22.

## Context

The server is used by groups that work in Russian and groups that work in English.
The server's texts have two readers. The model reads tool descriptions, instructions and errors and
uses them to decide what to do next. A person sees the same tool answers in the agent's interface, the output
of `install.py` and the descriptions in `/mcp`. All of this has to be in the group's language. At the same time
the server still runs without dependencies and starts from a clone (ADR 0001).

## Decision

| Question | Choice | Why |
|---|---|---|
| Languages | `en` (the default) and `ru` | there are no other groups yet; the catalog grows by a language key |
| Where the texts live | `src/pg_explorer_mcp/i18n.py`: two dictionaries `key → template`, the functions `t()` and `plural()` | under two hundred lines, no `.mo` files and no build step; tests compare the catalogs |
| How the language is chosen | `PG_EXPLORER_MCP_LANG`; without it, the locale variables in the order of the gettext module (`LANGUAGE`, `LC_ALL`, `LC_MESSAGES`, `LANG`), then the system locale | on Windows `LANG` is usually not set, and `locale.getlocale()` shows `Russian_Russia` |
| Pinning | `install.py` writes the chosen language into the server's `env` block | the agent may start the server without the shell's locale variables: by default the MCP SDK passes on only a short list of variables |
| Plurals | CLDR categories: `one`/`other` for English, `one`/`few`/`many` for Russian | "1 строка, 2 строки, 5 строк" (1, 2, 5 rows); the verb agrees too, so each form holds the whole phrase |
| What is not translated | tool and argument names, JSON-RPC errors, the log on stderr, PostgreSQL messages | names are the interface; clients look at error codes; the log is read by an administrator; the text of database errors is set by its `lc_messages` |
| Answering the user | the server's instructions ask the model to answer in the user's language | the server's language affects what the model reads but should not force the language of the answer |

The language is chosen once at start-up and kept in a `ContextVar`. Tests switch it with the
`speaking()` context manager and leave no traces in neighbouring tests.

## Alternatives considered

- **gettext with `.po`/`.mo` files**: the standard for large projects and convenient for translators.
  But the standard library reads only compiled `.mo` files: we would have to keep binary files in the
  repository or add a build step, which distribution as sources does not have.
  For two languages and one translator there is no gain.
- **English-only descriptions for the model, translating only what a person sees**: small
  local models sometimes pick tools from English descriptions a little more reliably. But a person
  sees the same texts in `/mcp` and in tool answers, so everything is translated. If a group's model
  copes worse in Russian, `PG_EXPLORER_MCP_LANG=en` is enough: it will still answer the user
  in their language.
- **A language per request** (by the user's language): the server does not see the user's messages,
  and guessing from the SQL text is unreliable.

## Consequences

- Each new text goes into both dictionaries; `tests/test_i18n.py` checks that the keys,
  plural forms and placeholders match, that every key used in the code is in the catalog
  and that the catalog has no unused texts.
- The answer budget is counted in characters. Russian text takes more tokens per character than
  English, so with a small model context the budget should be lowered, not raised.
