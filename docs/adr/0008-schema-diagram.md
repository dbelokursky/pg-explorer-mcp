# ADR 0008. The data model as a Mermaid diagram

Status: accepted, 2026-10-28.

## Context

A developer needs to see the data model of a schema: tables, keys and the links through foreign keys, instead of reading
`describe_table` for every table. The server answers with text, and it has no graphics and cannot have any: under
ADR 0001 it is the standard library only, and nothing gets installed. The agent does not draw either, but everything it writes to
shows Mermaid diagrams: GitHub and GitLab in markdown, Obsidian, VS Code, agent chats. Models with a small
context cannot assemble a diagram from dozens of calls and easily make up links.

## Decision

| Question | Choice | Why |
|---|---|---|
| Where | the `schema_diagram` tool in the server, in all profiles | the server reads the catalog in a few queries and makes nothing up; a skill on top of `list_tables` and `describe_table` would cost a call per table and leave the links to the model |
| Format | Mermaid `erDiagram` text | renders where the user already reads, with nothing to install; DBML, PlantUML and Graphviz need their own renderer, draw.io a layout with coordinates |
| What is drawn | a whole schema: its tables with keys and foreign keys, the links between them and to the tables of other schemas that the schema refers to; or one table with its neighbours through foreign keys in both directions (`table`, `depth` from 0 to 3) | references to the schema from other schemas are not drawn: a table everyone refers to would drag in the whole database |
| How many columns | by default the key ones (PK, FK, UK), `columns: all` for all of them with comments, `none` for tables and links only | the answer budget is 8000 characters; keys and links are the data model |
| Budget | the diagram either fits whole or not at all: when it does not fit, the columns are dropped, then the keys, and then comes an error advising to draw a part with `table` and `depth`; what was dropped is said in a `%%` comment at the start of the diagram | a cut-off diagram does not render, unlike a cut-off list |
| Names | an entity name Mermaid cannot read without quotes (a dot, Cyrillic, a leading digit) is quoted: `"hr.employees"`, `"заказы"`; an attribute name cannot be quoted, so it keeps letters, digits, `_` and `-`, and a leading digit gets `_`; the type is one word: `varchar(50)`, `timestamptz`, `numeric` without `(10,2)` | tested on Mermaid 10.9 and 11.12; Cyrillic in attribute names works only from 11 on |
| Cardinality | `}o--\|\|` for a foreign key from NOT NULL columns, `}o--o\|` for an optional one, `\|o--\|\|` for a key on a primary key or unique columns (one to one) | exactly what the catalog knows; no "identifying" dashed lines, which not everyone reads |
| On stands | the same `stand`, `schema` and `database`: the schema is read under its own login, and the `stand / database / schema` line comes first as a Mermaid `%%` comment | the answer renders even if the agent pastes it as it is |
| Command | `/pg:diagram`: call the tool and put the ```mermaid block into a file next to the project or show it in the chat | where to save it and what to render it to SVG with is up to the agent, not the server |

## Alternatives considered

- **A skill without changing the server**: the agent assembles the diagram from `list_tables` and `describe_table`, a call per
  table, dozens of calls per schema, and the model makes up the links.
- **DBML, PlantUML, Graphviz, draw.io**: each has its own renderer or layout; DBML is easy to add as a second
  format if dbdiagram.io is needed.
- **MCP Apps**: an interactive widget in the tool answer; it works only in clients that can
  show the server's HTML resources, and goes against the server's text-only design.
- **Drawing on the server** as SVG or PNG: nothing to do it with without dependencies, and the agent would not show the picture anyway.

## Consequences

- The tool list grew by about a thousand characters in each mode; the limits in the tests were raised
  by the same amount.
- A few catalog queries per call, and no data is read; a table with hundreds of columns is drawn with its keys,
  as is a schema with a hundred tables.
- Views are not part of the diagram: they have no keys. `describe_table` shows a column and its exact
  type.
