---
---
name: fetch-plant3d-projects
description: 'Find Plant 3D projects and data sets in an ACC project using AECDM. Use when discovering P&ID, 3D Piping, or 3D Models data sets and their element-group IDs.'
---

# Fetch Plant3D Projects

## When to use

- Find the Plant 3D projects (data sets) inside an AECDM/ACC project.
- Resolve the element-group ID needed for subsequent element and relationship queries.
- Present the discovered data sets grouped under their Plant 3D project folders.

## Workflow

1. Call `get_aecdm_hubs` and select the target hub. Use its returned `id`; do not reuse an example or guessed ID.
2. Call `get_aecdm_projects` with that `hub_id`. Select the target ACC project and retain its returned project `id`.
3. Call `get_aecdm_plant3d_folders` with the project ID if the caller wants to inspect folders or limit discovery to a folder. Use a returned folder ID as `folder_id` in the next step.
4. Call `get_aecdm_plant3d_projects` with `project_id`. By default it scans the project folders and their subfolders. If a folder was selected, pass `folder_id` to limit the scan. To query the project directly instead of scanning folders, set `extract_groups_way` to `project`.
5. The tool paginates element-group results internally, using a page limit of 50. It keeps only exact matches for `P&ID Data Set`, `3D Piping Data Set`, and `3D Models Data Set`, then groups each match by its `parentFolder.parentFolder` as the Plant 3D project.
6. Present the returned `tree` as a concise hierarchy. Include each data set's `name` and `id`; retain `fileUrn` and `fileVersionUrn` when useful for later model/version resolution.
7. Check `stats.truncated`. If true, the scan reached its 200-page cap with another cursor available and the result may be incomplete. Narrow the scan with `folder_id`; if it remains truncated, the page cap must be increased in the implementation before claiming the list is complete. For folder mode, `stats.truncatedFolders` reports how many folder scans hit the cap.

## Result shape

The tool returns `projectCount`, `dataSetCount`, `stats`, `tree`, and a flat `dataSets` list. Each tree entry has the Plant 3D project folder's `id` and `name`, plus a `dataSets` array. Use the individual data-set ID as `element_group_id` for element or relationship tools.

When there are no matching data sets, report that none of the three supported data-set names was found. Do not infer that an API error or a truncated scan means the project contains no Plant 3D data.

## Notes

- Folder mode is the default; it first gets the project's root folders and scans each folder subtree.
- A supplied `folder_id` forces folder mode, even if `extract_groups_way` is set to `project`.
- The output is structured JSON. Render it in the consuming chat or UI; this MCP tool does not serve a web UI itself.
 