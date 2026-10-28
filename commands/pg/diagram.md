---
description: Draw the data model of a schema, or of a table with its neighbours, as a Mermaid diagram the user can see and keep
---
Draw the user's data model with the `schema_diagram` tool and put the picture where they will see it.

1. Find out what to draw: a schema (`schema`), or one table with the tables it refers to and that refer
   to it (`table`, with `depth` from 0 to 3; default 1). On an entry for many stands pass `stand` and
   `schema` as well. Call `list_schemas` or `list_tables` first only when the user did not name the schema.
2. Call `schema_diagram`. Its answer is the source of a Mermaid erDiagram: the key columns by default,
   `columns: all` for every column with its comment, `columns: none` for tables and relationships only.
   The comment lines at the top say what was drawn and whether columns were left out for size; a diagram
   that does not fit the answer says to pass `table` and `depth` for a part of it.
3. Show the answer as it is in a ```mermaid block, without changing a name. When the user wants a file,
   write the block into `<schema>.md` (or the bare source into `<schema>.mmd`) next to the project, or
   where they say: GitHub, Obsidian and VS Code render it. With mermaid-cli installed,
   `mmdc -i <schema>.mmd -o <schema>.svg` makes a picture file.
4. The diagram is structure only: no data is read, and a view is not drawn. For a column's exact type,
   its default or the indexes of a table, call `describe_table`.
