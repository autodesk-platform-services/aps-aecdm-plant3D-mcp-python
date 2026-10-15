---
name: find-plant3d-schema-table-names
description: 'Find valid Plant 3D schema table identifiers for AECDM element filters by querying the local .dcf SQLite schema with classHierarchy.sql. Use when a display label needs to be mapped to its canonical TableName.'
---

# Find Plant3D Schema Table Names

## When to use

- Find available Plant 3D schema classes/tables before querying elements.
- Map a human-readable table display name to its exact AECDM identifier.
- Build or validate a `tableNames` filter for `get_aecdm_plant3d_elements_by_table`.

## Procedure

1. Use the schema assets in `Plant3D Schema/`. For P&ID classes, query `ProcessPower.dcf` with `classHierarchy.sql`. Select another `.dcf` in this folder only when it matches the target model's schema.
2. Execute the local SQL file against the DCF as a read-only SQLite database. The query returns `Level`, `Hierarchy`, `TableName`, `DisplayName`, `ParentClass`, and `Abstract`.

   ```python
   from pathlib import Path
   import sqlite3

   schema_dir = Path("Plant3D Schema")
   dcf_path = (schema_dir / "ProcessPower.dcf").resolve()
   query = (schema_dir / "classHierarchy.sql").read_text(encoding="utf-8")

   with sqlite3.connect(f"file:{dcf_path}?mode=ro", uri=True) as connection:
       rows = connection.execute(query).fetchall()

   for level, hierarchy, table_name, display_name, parent_class, abstract in rows:
       print(f"{hierarchy} | TableName={table_name} | DisplayName={display_name}")
   ```

3. Select the canonical `TableName` value for the AECDM query. Do not pass `DisplayName` as `table_name` or use it in the `tableNames` filter. For example, the local schema maps `TableName=Primary` to `DisplayName=Primary Line Segment`.
4. Prefer passing the canonical value as the tool's `table_name` argument. The server derives the class ancestry from the DCF and builds the filter:

   ```json
   {
     "element_group_id": "<Plant3D-data-set-id>",
     "table_name": "Primary"
   }
   ```

5. If constructing a raw AECDM filter, use the `TableName` ancestry chain in leaf-first order, as stored in the Plant 3D `tableNames` property. For `Primary`, the current DCF yields:

   ```text
   property.id.autodesk.plant3d.parameter:tableNames-1.0.0=contains='Primary,PipeLines,Lines,EngineeringItems'
   ```

6. Treat the `Hierarchy` and `DisplayName` columns as presentation aids. `TableName` and the ancestry made from `TableName`/`ParentClass` are the filter identifiers. Confirm the chosen DCF matches the target data set before querying.

## Filter behavior

AECDM's `contains` operator can overmatch similarly named classes. When using the MCP tool, supplying `table_name` lets the server inspect the hierarchy and client-isolate unsafe results. The schema/filter metadata includes `filterSafe`, `filterOverMatches`, and `scanOnly`; heed these values rather than assuming a substring filter is exact.

If a table name is rejected or returns an unexpected result, verify it against `classHierarchy.sql` and check that the correct DCF was queried. Do not substitute a display label or guess an identifier.
