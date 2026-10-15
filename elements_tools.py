"""
AECDM (AEC Data Model) helpers for resolving an Autodesk APS model ID from a
hub name + project name + file version URN.

This file currently exposes a single async function, `get_aecdm_model_id`,
which walks the three-step GraphQL pipeline used by the AECDM API.
"""

import asyncio
import json
import logging
import os
import re
from typing import Any, Callable

import httpx
from mcp.server.fastmcp import Context, FastMCP

from plant3d_filters import (
    isolate_elements_by_path,
    resolve_plant3d_element_filter,
)

logger = logging.getLogger(__name__)

_AECDM_GRAPHQL_URL = "https://developer.api.autodesk.com/aec/graphql"
_AECDM_PAGE_LIMIT = 50
_FOLDER_SCAN_CONCURRENCY = 4
_PLANT3D_DATASET_NAMES = ("P&ID Data Set", "3D Piping Data Set", "3D Models Data Set")
_UNGROUPED_PLANT3D_PROJECT = "Ungrouped"

_ELEMENT_GROUP_PLANT3D_FIELDS = """
      id
      name
      parentFolder {
        id
        name
        parentFolder { id name }
      }
      alternativeIdentifiers { fileUrn fileVersionUrn }
"""


_GET_HUBS_QUERY = """
query GetHubs($pagination: PaginationInput) {
  hubs(pagination: $pagination) {
    pagination { cursor }
    results {
      id
      name
    }
  }
}
"""

_GET_PROJECTS_QUERY = """
query GetProjects($hubId: ID!, $pagination: PaginationInput) {
  projects(hubId: $hubId, pagination: $pagination) {
    pagination { cursor }
    results {
      id
      name
      alternativeIdentifiers {
        dataManagementAPIProjectId
      }
    }
  }
}
"""

_GET_FOLDERS_BY_PROJECT_QUERY = """
query GetFoldersByProject($projectId: ID!) {
  foldersByProject(projectId: $projectId) {
    results {
      id
      name
      folders {
        results {
          id
          name
        }
      }
    }
  }
}
"""

_GET_ELEMENT_GROUPS_QUERY = """
query GetElementGroupsByProject($projectId: ID!, $pagination: PaginationInput) {
  elementGroupsByProject(projectId: $projectId, pagination: $pagination) {
    pagination { cursor }
    results {
      id
      name
      alternativeIdentifiers {
        fileUrn
        fileVersionUrn
      }
    }
  }
}
"""

_GET_ELEMENT_GROUPS_FOR_PLANT3D_QUERY = f"""
query GetElementGroupsByProject(
  $projectId: ID!,
  $filter: ElementGroupFilterInput,
  $pagination: PaginationInput
) {{
  elementGroupsByProject(projectId: $projectId, filter: $filter, pagination: $pagination) {{
    pagination {{ pageSize cursor }}
    results {{
{_ELEMENT_GROUP_PLANT3D_FIELDS}
    }}
  }}
}}
"""

_GET_ELEMENT_GROUPS_BY_FOLDER_QUERY = f"""
query GetElementGroupsByFolderAndSubFolders(
  $projectId: ID!,
  $folderId: ID!,
  $filter: ElementGroupFilterInput,
  $pagination: PaginationInput
) {{
  elementGroupsByFolderAndSubFolders(
    projectId: $projectId
    folderId: $folderId
    filter: $filter
    pagination: $pagination
  ) {{
    pagination {{ pageSize cursor }}
    results {{
{_ELEMENT_GROUP_PLANT3D_FIELDS}
    }}
  }}
}}
"""


_GET_PLANT3D_ELEMENTS_BY_TABLE_QUERY = """
query GetElementsByElementGroup(
  $groupId: ID!,
  $filter: ElementFilterInput,
  $pagination: PaginationInput
) {
  elementsByElementGroup(
    elementGroupId: $groupId
    filter: $filter
    pagination: $pagination
  ) {
    pagination {
      pageSize
      cursor
    }
    results {
      name
      id
      properties {
        results {
          name
          displayValue
          definition {
            id
            name
          }
        }
      }
      lastModifiedOn
    }
  }
}
"""

_GET_ELEMENTS_FROM_CATEGORY_QUERY = """
query GetElementsFromCategory(
  $elementGroupId: ID!,
  $propertyFilter: String!,
  $originComponentFilter: OriginComponentFilter,
  $pagination: PaginationInput
) {
  elementsByElementGroup(
    elementGroupId: $elementGroupId,
    filter: {
      query: $propertyFilter,
      originComponentFilter: $originComponentFilter
    },
    pagination: $pagination
  ) {
    pagination { cursor }
    results {
      id
      name
      properties {
        results {
          name
          value
          definition {
            units { name }
          }
        }
      }
      components {
        results {
          componentType
          ... on AxisRepresentationComponent {
            curve {
              type
              ... on Line {
                position { x y z }
                range { low high type }
              }
            }
          }
          ... on OriginComponent {
            origin { x y z }
          }
          ... on BodyRepresentationComponent {
            formats {
              ... on GeometryRepresentationData {
                pieces {
                  type
                  storageLengthUnit
                  transform { type value }
                  geometry {
                    ... on GeometryPrimitive {
                      value {
                        ... on Circle { center { x y z } }
                        ... on Line {
                          position { x y z }
                          direction { z y z }
                        }
                        ... on Polyline { points { x y z } }
                        ... on Point { x y z }
                      }
                    }
                    ... on BinaryData {
                      objectId
                      firstByteAddress
                      lastByteAddress
                      format
                    }
                  }
                }
              }
            }
          }
        }
      }
    }
  }
}
"""


def _graphql_operation_name(query: str) -> str:
    match = re.search(r"(?:query|mutation)\s+(\w+)", query)
    return match.group(1) if match else "anonymous"


def _aecdm_raw_logging_enabled() -> bool:
    return os.environ.get("AECDM_LOG_RAW_RESPONSE", "true").strip().lower() not in {
        "0",
        "false",
        "no",
    }


def _aecdm_access_token_logging_enabled() -> bool:
    # Local demo debugging only — disable with AECDM_LOG_ACCESS_TOKEN=false in production.
    return os.environ.get("AECDM_LOG_ACCESS_TOKEN", "true").strip().lower() not in {
        "0",
        "false",
        "no",
    }


def _log_aecdm_access_token(operation: str, access_token: str) -> None:
    if not _aecdm_access_token_logging_enabled():
        return
    print(f"[aecdm:raw] {operation} access_token={access_token}", flush=True)


def _log_aecdm_raw_response(
    operation: str,
    variables: dict[str, Any],
    status_code: int,
    body: Any,
) -> None:
    """Print the full AECDM GraphQL payload to the server console."""
    if not _aecdm_raw_logging_enabled():
        return

    try:
        raw = json.dumps(body, indent=2, ensure_ascii=False)
    except TypeError:
        raw = str(body)

    max_len = int(os.environ.get("AECDM_LOG_RAW_MAX_CHARS", "20000"))
    if len(raw) > max_len:
        raw = f"{raw[:max_len]}\n... [truncated, {len(raw) - max_len} chars omitted]"

    point_value = body.get("extensions", {}).get("pointValue") if isinstance(body, dict) else None
    cost_note = f" pointValue={point_value}" if point_value is not None else ""

    print(
        f"\n[aecdm:raw] {operation} status={status_code}{cost_note}\n"
        f"[aecdm:raw] variables={json.dumps(variables, ensure_ascii=False)}\n"
        f"[aecdm:raw] response=\n{raw}\n",
        flush=True,
    )
    logger.info("AECDM %s status=%s (raw response logged)", operation, status_code)


async def _aecdm_graphql(
    client: httpx.AsyncClient,
    query: str,
    variables: dict[str, Any],
    access_token: str,
) -> dict[str, Any]:
    """POST a GraphQL query to the AECDM endpoint, raising on transport or GraphQL errors."""
    operation = _graphql_operation_name(query)
    _log_aecdm_access_token(operation, access_token)
    response = await client.post(
        _AECDM_GRAPHQL_URL,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        json={"query": query, "variables": variables},
    )

    try:
        body = response.json()
    except ValueError:
        body = {"_non_json_body": response.text}

    _log_aecdm_raw_response(operation, variables, response.status_code, body)
    response.raise_for_status()

    if isinstance(body, dict) and body.get("errors"):
        raise RuntimeError(f"AECDM GraphQL errors: {body['errors']}")
    return body.get("data") or {} if isinstance(body, dict) else {}


def _pagination_variables(
    cursor: str | None,
    *,
    limit: int = _AECDM_PAGE_LIMIT,
) -> dict[str, Any]:
    return {"limit": limit, "cursor": cursor}


async def _fetch_all_aecdm_pages(
    client: httpx.AsyncClient,
    query: str,
    field: str,
    variables: dict[str, Any],
    access_token: str,
    *,
    max_pages: int = 50,
) -> tuple[list[dict[str, Any]], int]:
    """Walk a cursor-paginated AECDM query until the cursor is null."""
    all_results: list[dict[str, Any]] = []
    cursor: str | None = None
    pages = 0

    while pages < max_pages:
        pages += 1
        data = await _aecdm_graphql(
            client,
            query,
            {
                **variables,
                "pagination": _pagination_variables(cursor),
            },
            access_token,
        )
        block = data.get(field) or {}
        results = block.get("results") or []
        all_results.extend(results)
        cursor = (block.get("pagination") or {}).get("cursor")
        if not cursor:
            break

    return all_results, pages


async def list_aecdm_hubs(
    access_token: str,
    *,
    max_pages: int = 50,
) -> dict[str, Any]:
    """
    List all AEC Data Model hubs (paginated).

    Uses the GetHubs GraphQL query from the AECDM tutorial:
    https://aps.autodesk.com/en/docs/aecdatamodel/v1/tutorials/tutorial01/gethubs/
    """
    async with httpx.AsyncClient(timeout=60.0) as client:
        all_hubs, pages = await _fetch_all_aecdm_pages(
            client,
            _GET_HUBS_QUERY,
            "hubs",
            {},
            access_token,
            max_pages=max_pages,
        )

    return {
        "hubCount": len(all_hubs),
        "pagesScanned": pages,
        "hubs": all_hubs,
    }


async def list_aecdm_projects(
    hub_id: str,
    access_token: str,
    *,
    max_pages: int = 50,
) -> dict[str, Any]:
    """
    List all AEC Data Model projects in a hub (paginated).

    Uses the GetProjects GraphQL query from the AECDM tutorial:
    https://aps.autodesk.com/en/docs/aecdatamodel/v1/tutorials/tutorial01/getprojects/
    """
    async with httpx.AsyncClient(timeout=60.0) as client:
        all_projects, pages = await _fetch_all_aecdm_pages(
            client,
            _GET_PROJECTS_QUERY,
            "projects",
            {"hubId": hub_id},
            access_token,
            max_pages=max_pages,
        )

    return {
        "hubId": hub_id,
        "projectCount": len(all_projects),
        "pagesScanned": pages,
        "projects": all_projects,
    }


async def list_aecdm_project_folders(
    project_id: str,
    access_token: str,
) -> dict[str, Any]:
    """
    List first-level project folders and their immediate subfolders.

    Uses the GetFoldersByProject GraphQL query (Plant 3D workflow step 1).
    """
    async with httpx.AsyncClient(timeout=60.0) as client:
        data = await _aecdm_graphql(
            client,
            _GET_FOLDERS_BY_PROJECT_QUERY,
            {"projectId": project_id},
            access_token,
        )

    roots = (data.get("foldersByProject") or {}).get("results") or []
    folders: list[dict[str, Any]] = []
    all_folders: list[dict[str, str]] = []

    for root in roots:
        subfolders = [
            {"id": sub["id"], "name": sub["name"]}
            for sub in ((root.get("folders") or {}).get("results") or [])
            if sub.get("id") and sub.get("name")
        ]
        folders.append(
            {
                "id": root.get("id"),
                "name": root.get("name"),
                "subfolders": subfolders,
            }
        )
        if root.get("id") and root.get("name"):
            all_folders.append({"id": root["id"], "name": root["name"], "level": "root"})
        for sub in subfolders:
            all_folders.append(
                {"id": sub["id"], "name": sub["name"], "level": "subfolder", "parentId": root.get("id")}
            )

    return {
        "projectId": project_id,
        "folderCount": len(folders),
        "allFolderCount": len(all_folders),
        "folders": folders,
        "allFolders": all_folders,
    }


def _is_plant3d_dataset(name: str | None) -> bool:
    if not name:
        return False
    trimmed = name.strip()
    return any(trimmed == known for known in _PLANT3D_DATASET_NAMES)


def _extract_groups_way(folder_id: str | None, extract_groups_way: str | None) -> str:
    if folder_id:
        return "folder"
    raw = (extract_groups_way or os.environ.get("EXTRACT_GROUPS_WAY", "folder")).strip().lower()
    return raw if raw in {"project", "folder"} else "folder"


async def _page_plant3d_element_groups(
    client: httpx.AsyncClient,
    query: str,
    field: str,
    variables: dict[str, Any],
    access_token: str,
    *,
    max_pages: int = 200,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Paginate an element-group query and keep only Plant 3D data sets."""
    data_sets: list[dict[str, Any]] = []
    cursor: str | None = None
    pages = 0
    scanned = 0

    while pages < max_pages:
        pages += 1
        data = await _aecdm_graphql(
            client,
            query,
            {
                **variables,
                "filter": {"searchType": "ALL"},
                "pagination": {"limit": _AECDM_PAGE_LIMIT, "cursor": cursor},
            },
            access_token,
        )
        block = data.get(field) or {}
        results = block.get("results") or []
        scanned += len(results)
        data_sets.extend(group for group in results if _is_plant3d_dataset(group.get("name")))
        cursor = (block.get("pagination") or {}).get("cursor")
        if not cursor:
            break

    return data_sets, {
        "pages": pages,
        "scanned": scanned,
        "matched": len(data_sets),
        "truncated": bool(cursor),
    }


def _build_plant3d_project_tree(data_sets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group data sets under their parent's parent folder (the Plant 3D project)."""
    groups: dict[str, dict[str, Any]] = {}

    for data_set in data_sets:
        parent = data_set.get("parentFolder") or {}
        grandparent = parent.get("parentFolder") or {}
        key = grandparent.get("id") or _UNGROUPED_PLANT3D_PROJECT

        if key not in groups:
            groups[key] = {
                "id": grandparent.get("id"),
                "name": grandparent.get("name") or _UNGROUPED_PLANT3D_PROJECT,
                "dataSets": [],
            }

        alt = data_set.get("alternativeIdentifiers") or {}
        groups[key]["dataSets"].append(
            {
                "id": data_set.get("id"),
                "name": data_set.get("name"),
                "folder": parent.get("name"),
                "fileUrn": alt.get("fileUrn"),
                "fileVersionUrn": alt.get("fileVersionUrn"),
            }
        )

    tree = list(groups.values())
    for group in tree:
        group["dataSets"].sort(key=lambda item: item.get("name") or "")
    tree.sort(
        key=lambda group: (
            group.get("name") == _UNGROUPED_PLANT3D_PROJECT,
            group.get("name") or "",
        )
    )
    return tree


async def _list_root_folder_targets(
    client: httpx.AsyncClient,
    project_id: str,
    access_token: str,
) -> list[dict[str, str]]:
    data = await _aecdm_graphql(
        client,
        _GET_FOLDERS_BY_PROJECT_QUERY,
        {"projectId": project_id},
        access_token,
    )
    roots = (data.get("foldersByProject") or {}).get("results") or []
    return [
        {"id": root["id"], "name": root.get("name") or root["id"]}
        for root in roots
        if root.get("id")
    ]


async def list_aecdm_plant3d_projects(
    project_id: str,
    access_token: str,
    *,
    folder_id: str | None = None,
    extract_groups_way: str | None = None,
    max_pages: int = 200,
) -> dict[str, Any]:
    """
    Discover Plant 3D data sets in an AECDM project and group them into a tree.

    Implements the Fetch Plant3D projects skill workflow:
      1. Scan element groups (by project or by folder subtree)
      2. Keep only P&ID / 3D Piping / 3D Models data sets
      3. Paginate until cursor is null
      4. Build a tree grouped by parentFolder.parentFolder

        Check `stats.truncated` in the result. If true, the 200-page limit was reached
        while another page remained, so the returned project/data-set list is incomplete.
    """
    mode = _extract_groups_way(folder_id, extract_groups_way)

    async with httpx.AsyncClient(timeout=120.0) as client:
        if mode == "project":
            data_sets, stats = await _page_plant3d_element_groups(
                client,
                _GET_ELEMENT_GROUPS_FOR_PLANT3D_QUERY,
                "elementGroupsByProject",
                {"projectId": project_id},
                access_token,
                max_pages=max_pages,
            )
            stats["mode"] = "project"
            stats["folders"] = 0
        else:
            if folder_id:
                targets = [{"id": folder_id, "name": folder_id}]
            else:
                targets = await _list_root_folder_targets(client, project_id, access_token)

            semaphore = asyncio.Semaphore(_FOLDER_SCAN_CONCURRENCY)

            async def _scan_folder(
                target: dict[str, str],
            ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
                async with semaphore:
                    return await _page_plant3d_element_groups(
                        client,
                        _GET_ELEMENT_GROUPS_BY_FOLDER_QUERY,
                        "elementGroupsByFolderAndSubFolders",
                        {"projectId": project_id, "folderId": target["id"]},
                        access_token,
                        max_pages=max_pages,
                    )

            per_folder = await asyncio.gather(*[_scan_folder(target) for target in targets])
            data_sets = [group for folder_groups, _ in per_folder for group in folder_groups]
            stats = {
                "mode": "folder",
                "folders": len(targets),
                "pages": sum(folder_stats["pages"] for _, folder_stats in per_folder),
                "scanned": sum(folder_stats["scanned"] for _, folder_stats in per_folder),
                "matched": len(data_sets),
                "truncated": any(folder_stats["truncated"] for _, folder_stats in per_folder),
                "truncatedFolders": sum(
                    folder_stats["truncated"] for _, folder_stats in per_folder
                ),
            }

    tree = _build_plant3d_project_tree(data_sets)
    return {
        "projectId": project_id,
        "folderId": folder_id,
        "extractGroupsWay": mode,
        "stats": stats,
        "projectCount": len(tree),
        "dataSetCount": len(data_sets),
        "tree": tree,
        "dataSets": [
            {
                "id": data_set.get("id"),
                "name": data_set.get("name"),
                "parentFolder": data_set.get("parentFolder"),
                "alternativeIdentifiers": data_set.get("alternativeIdentifiers"),
            }
            for data_set in data_sets
        ],
    }


async def list_plant3d_elements_by_table(
    element_group_id: str,
    access_token: str,
    *,
    table_name: str | None = None,
    filter_query: str | None = None,
    dcf_path: str | None = None,
    skip_client_isolation: bool = False,
    max_pages: int = 50,
    page_limit: int = 99,
) -> dict[str, Any]:
    """
    Fetch Plant 3D elements from an element group.

    Uses schema-derived tableNames filters when `table_name` is provided, optional
    custom AECDM filter expressions, and client-side isolation when `=contains=`
    over-matches (e.g. EngineeringItems vs NonEngineeringItems).
    """
    filter_plan = resolve_plant3d_element_filter(
        table_name=table_name,
        filter_query=filter_query,
        dcf_path=dcf_path,
    )
    server_filter = filter_plan.get("filter")
    apply_client_isolation = (
        not skip_client_isolation and filter_plan.get("requiresClientIsolation")
    )
    path_root_first = filter_plan.get("tableNamesPathRootFirst") or []

    all_elements: list[dict[str, Any]] = []
    cursor: str | None = None
    pages = 0

    async with httpx.AsyncClient(timeout=120.0) as client:
        while pages < max_pages:
            pages += 1
            variables: dict[str, Any] = {
                "groupId": element_group_id,
                "pagination": _pagination_variables(cursor, limit=page_limit),
            }
            if server_filter:
                variables["filter"] = server_filter

            data = await _aecdm_graphql(
                client,
                _GET_PLANT3D_ELEMENTS_BY_TABLE_QUERY,
                variables,
                access_token,
            )
            block = data.get("elementsByElementGroup") or {}
            results = block.get("results") or []
            all_elements.extend(results)
            cursor = (block.get("pagination") or {}).get("cursor")
            if not cursor:
                break

    fetched_count = len(all_elements)
    isolated_elements = (
        isolate_elements_by_path(all_elements, path_root_first)
        if apply_client_isolation and path_root_first
        else all_elements
    )

    return {
        "elementGroupId": element_group_id,
        "tableName": filter_plan.get("tableName"),
        "filter": server_filter,
        "filterSource": filter_plan.get("filterSource"),
        "filterValue": filter_plan.get("filterValue"),
        "filterSafe": filter_plan.get("filterSafe"),
        "filterOverMatches": filter_plan.get("filterOverMatches"),
        "scanOnly": filter_plan.get("scanOnly"),
        "tableNamesPathRootFirst": path_root_first,
        "tableNamesPathLeafFirst": filter_plan.get("tableNamesPathLeafFirst"),
        "clientIsolationApplied": apply_client_isolation and bool(path_root_first),
        "requiresClientIsolation": filter_plan.get("requiresClientIsolation"),
        "pagesScanned": pages,
        "fetchedCount": fetched_count,
        "elementCount": len(isolated_elements),
        "clientFilteredCount": fetched_count - len(isolated_elements),
        "elements": isolated_elements,
        "filteringDocs": filter_plan.get("filteringDocs"),
    }


async def get_elements_by_filter(
    element_group_id: str,
    property_filter: str,
    access_token: str,
    *,
    origin_component_filter: dict[str, Any] | None = None,
    max_pages: int = 50,
) -> dict[str, Any]:
    """
    Fetch elements (with properties + components) from an AECDM elementGroup,
    filtered by a property-filter expression and/or an origin bounding box.

    `property_filter` examples:
      - "'property.name.External ID'==552d2a83-4642-4d5c-8e7f-5de799129097-000d04f3"
      - "'property.name.Category'=='Walls'"

    `origin_component_filter` shape:
      {
        "originRange": {
          "startPoints": {"x": x1, "y": y1, "z": z1},
          "endPoints":   {"x": x2, "y": y2, "z": z2}
        }
      }

    Pagination is followed until exhausted or `max_pages` is reached.
    """
    print(
        f"\n[aecdm]  === get_elements_by_filter === "
        f"elementGroupId='{element_group_id[:32]}…' filter='{property_filter}' "
        f"originFilter={'yes' if origin_component_filter else 'no'}"
    )
    if origin_component_filter:
        rng = (origin_component_filter.get("originRange") or {})
        print(
            f"[aecdm]  originRange -> start={rng.get('startPoints')} "
            f"end={rng.get('endPoints')}"
        )

    all_elements: list[dict[str, Any]] = []
    cursor: str | None = None
    pages = 0

    async with httpx.AsyncClient(timeout=120.0) as client:
        while pages < max_pages:
            pages += 1
            print(f"[aecdm]  page {pages} -> elementsByElementGroup")
            data = await _aecdm_graphql(
                client,
                _GET_ELEMENTS_FROM_CATEGORY_QUERY,
                {
                    "elementGroupId": element_group_id,
                    "propertyFilter": property_filter,
                    "originComponentFilter": origin_component_filter,
                    "pagination": _pagination_variables(cursor),
                },
                access_token,
            )
            block = data.get("elementsByElementGroup") or {}
            results = block.get("results") or []
            print(f"[aecdm]  page {pages} -> {len(results)} element(s)")
            all_elements.extend(results)

            cursor = (block.get("pagination") or {}).get("cursor")
            if not cursor:
                break

    print(
        f"[aecdm]  === done === total={len(all_elements)} element(s) across {pages} page(s)\n"
    )
    return {
        "elementGroupId": element_group_id,
        "propertyFilter": property_filter,
        "originComponentFilter": origin_component_filter,
        "pagesScanned": pages,
        "elementCount": len(all_elements),
        "elements": all_elements,
    }


async def get_aecdm_model_id(
    hub_name: str,
    project_name: str,
    version_urn: str,
    access_token: str,
) -> dict[str, Any]:
    """
    Resolve the AECDM model (elementGroup) id for a given file version URN.

    Pipeline:
      1. GetHubs(name=hub_name)                             -> hub.id
      2. GetProjects(hubId, name=project_name)              -> project.id
      3. GetElementGroupsByProject(projectId), paginated    -> match by fileVersionUrn

    Returns a dict with `modelId` on success, or an `error` key on failure.
    """
    print(
        f"\n[aecdm]  === get_aecdm_model_id === "
        f"hub='{hub_name}' project='{project_name}' urn='{version_urn[:48]}…'"
    )
    async with httpx.AsyncClient(timeout=60.0) as client:
        print("[aecdm]  step 1 -> GetHubs (paginate, filter by name client-side)")
        hub_match: dict[str, Any] | None = None
        hub_cursor: str | None = None
        hub_pages = 0
        hub_scanned = 0
        while True:
            hub_pages += 1
            hubs_data = await _aecdm_graphql(
                client,
                _GET_HUBS_QUERY,
                {"pagination": _pagination_variables(hub_cursor)},
                access_token,
            )
            block = hubs_data.get("hubs") or {}
            results = block.get("results") or []
            hub_scanned += len(results)
            for h in results:
                if h.get("name") == hub_name:
                    hub_match = h
                    break
            if hub_match:
                break
            hub_cursor = (block.get("pagination") or {}).get("cursor")
            if not hub_cursor:
                break
        print(
            f"[aecdm]  step 1 -> scanned {hub_scanned} hub(s) across {hub_pages} page(s), "
            f"match={'yes' if hub_match else 'no'}"
        )
        if not hub_match:
            return {"error": "hub_not_found", "hubName": hub_name}
        hub_id = hub_match["id"]
        print(f"[aecdm]  step 1 -> hubId={hub_id}")

        print("[aecdm]  step 2 -> GetProjects (paginate, filter by name client-side)")
        project_match: dict[str, Any] | None = None
        project_cursor: str | None = None
        project_pages = 0
        project_scanned = 0
        while True:
            project_pages += 1
            projects_data = await _aecdm_graphql(
                client,
                _GET_PROJECTS_QUERY,
                {
                    "hubId": hub_id,
                    "pagination": _pagination_variables(project_cursor),
                },
                access_token,
            )
            block = projects_data.get("projects") or {}
            results = block.get("results") or []
            project_scanned += len(results)
            for p in results:
                if p.get("name") == project_name:
                    project_match = p
                    break
            if project_match:
                break
            project_cursor = (block.get("pagination") or {}).get("cursor")
            if not project_cursor:
                break
        print(
            f"[aecdm]  step 2 -> scanned {project_scanned} project(s) across "
            f"{project_pages} page(s), match={'yes' if project_match else 'no'}"
        )
        if not project_match:
            return {
                "error": "project_not_found",
                "hubId": hub_id,
                "projectName": project_name,
            }
        project_id = project_match["id"]
        dm_project_id = (
            (project_match.get("alternativeIdentifiers") or {}).get("dataManagementAPIProjectId")
        )
        print(f"[aecdm]  step 2 -> projectId={project_id}")

        print("[aecdm]  step 3 -> GetElementGroupsByProject (paginate + filter by fileVersionUrn)")
        cursor: str | None = None
        match: dict[str, Any] | None = None
        pages = 0
        scanned = 0
        while True:
            pages += 1
            data = await _aecdm_graphql(
                client,
                _GET_ELEMENT_GROUPS_QUERY,
                {
                    "projectId": project_id,
                    "pagination": _pagination_variables(cursor),
                },
                access_token,
            )
            block = data.get("elementGroupsByProject") or {}
            results = block.get("results") or []
            scanned += len(results)
            for eg in results:
                alt = eg.get("alternativeIdentifiers") or {}
                if alt.get("fileVersionUrn") == version_urn:
                    match = eg
                    break
            if match:
                break
            cursor = (block.get("pagination") or {}).get("cursor")
            if not cursor:
                break

        print(
            f"[aecdm]  step 3 -> scanned {scanned} elementGroup(s) across {pages} page(s), "
            f"match={'yes' if match else 'no'}"
        )
        if not match:
            return {
                "error": "model_not_found",
                "hubId": hub_id,
                "projectId": project_id,
                "fileVersionUrn": version_urn,
                "pagesScanned": pages,
                "elementGroupsScanned": scanned,
            }

        result = {
            "hubId": hub_id,
            "projectId": project_id,
            "dataManagementApiProjectId": dm_project_id,
            "fileVersionUrn": version_urn,
            "modelId": match["id"],
            "modelName": match.get("name"),
            "fileUrn": (match.get("alternativeIdentifiers") or {}).get("fileUrn"),
        }
        print(f"[aecdm]  === done === modelId={result['modelId']}\n")
        return result


def _aecdm_tool_error(
    exc: Exception,
    *,
    not_authenticated: dict[str, str],
) -> dict[str, Any]:
    if isinstance(exc, httpx.HTTPStatusError):
        logger.warning("AECDM call returned %s", exc.response.status_code)
        try:
            body: Any = exc.response.json()
        except ValueError:
            body = exc.response.text
        if exc.response.status_code == 401:
            return dict(not_authenticated)
        return {
            "error": "autodesk_api_error",
            "status_code": exc.response.status_code,
            "body": body,
        }
    if isinstance(exc, httpx.RequestError):
        logger.warning("AECDM request failed: %s", type(exc).__name__)
        return {"error": "request_failed", "detail": str(exc)}
    if isinstance(exc, RuntimeError):
        return {"error": "aecdm_graphql_error", "detail": str(exc)}
    raise exc


def register_elements_tools(
    mcp: FastMCP,
    *,
    require_access_token: Callable[[Context], str | None],
    not_authenticated: dict[str, str],
) -> None:
    """Register AECDM-related MCP tools onto the given FastMCP instance."""

    @mcp.tool()
    async def get_aecdm_hubs(ctx: Context) -> dict[str, Any]:
        """
        List hubs via the AEC Data Model GraphQL API.

        Follows the GetHubs tutorial:
        https://aps.autodesk.com/en/docs/aecdatamodel/v1/tutorials/tutorial01/gethubs/

        Uses the APS bearer token supplied in the current MCP request context.
        """
        access_token = require_access_token(ctx)
        if not access_token:
            return dict(not_authenticated)
        try:
            return await list_aecdm_hubs(access_token)
        except (httpx.HTTPError, RuntimeError) as exc:
            return _aecdm_tool_error(exc, not_authenticated=not_authenticated)

    @mcp.tool()
    async def get_aecdm_projects(hub_id: str, ctx: Context) -> dict[str, Any]:
        """
        List projects in an AEC Data Model hub.

        Follows the GetProjects tutorial:
        https://aps.autodesk.com/en/docs/aecdatamodel/v1/tutorials/tutorial01/getprojects/

        `hub_id` is the AECDM hub id from get_aecdm_hubs (e.g.
        urn:adsk.workspace:prod.hub:...).

        Uses the same APS access token as other tools.
        """
        access_token = require_access_token(ctx)
        if not access_token:
            return dict(not_authenticated)
        try:
            return await list_aecdm_projects(hub_id, access_token)
        except (httpx.HTTPError, RuntimeError) as exc:
            return _aecdm_tool_error(exc, not_authenticated=not_authenticated)

    @mcp.tool()
    async def get_aecdm_plant3d_folders(project_id: str, ctx: Context) -> dict[str, Any]:
        """
        List available Plant 3D project folders for an AEC Data Model project.

        Calls the GetFoldersByProject GraphQL query, returning each top-level folder
        with its immediate subfolders. Use a folder id from this response with
        element-group queries in the Plant 3D workflow.

        `project_id` is the AECDM project id from get_aecdm_projects (e.g.
        urn:adsk.workspace:prod.project:...).

        Uses the same APS access token as other tools.
        """
        access_token = require_access_token(ctx)
        if not access_token:
            return dict(not_authenticated)
        try:
            return await list_aecdm_project_folders(project_id, access_token)
        except (httpx.HTTPError, RuntimeError) as exc:
            return _aecdm_tool_error(exc, not_authenticated=not_authenticated)

    @mcp.tool()
    async def get_aecdm_plant3d_projects(
        project_id: str,
        ctx: Context,
        folder_id: str | None = None,
        extract_groups_way: str | None = None,
    ) -> dict[str, Any]:
        """
        List Plant 3D projects (data sets) inside an AEC Data Model ACC project.

        Follows the Fetch Plant3D projects skill workflow:
          1. Scan element groups via elementGroupsByFolderAndSubFolders (default)
             or elementGroupsByProject when extract_groups_way is "project"
          2. Keep only Plant 3D data sets (P&ID Data Set, 3D Piping Data Set,
             3D Models Data Set)
          3. Paginate until the cursor is null
          4. Build a tree grouped by parentFolder.parentFolder (the Plant 3D project)

        `project_id` is the AECDM project id from get_aecdm_projects.
        Pass `folder_id` to scan a single folder subtree (from get_aecdm_plant3d_folders).
        Set `extract_groups_way` to "project" or "folder" to override EXTRACT_GROUPS_WAY.

        Uses the same APS access token as other tools.
        """
        access_token = require_access_token(ctx)
        if not access_token:
            return dict(not_authenticated)
        try:
            return await list_aecdm_plant3d_projects(
                project_id,
                access_token,
                folder_id=folder_id,
                extract_groups_way=extract_groups_way,
            )
        except (httpx.HTTPError, RuntimeError) as exc:
            return _aecdm_tool_error(exc, not_authenticated=not_authenticated)

    @mcp.tool()
    async def get_aecdm_plant3d_elements_by_table(
        element_group_id: str,
        ctx: Context,
        table_name: str | None = None,
        filter_query: str | None = None,
        dcf_path: str | None = None,
        skip_client_isolation: bool = False,
    ) -> dict[str, Any]:
        """
        Get Plant 3D elements in an element group filtered by table name and/or
        a custom AECDM filter expression.

        `element_group_id` is a data set id from get_aecdm_plant3d_projects.

        `table_name` — class from get_processpower_schema (e.g. Primary, Equipment,
        EngineeringItems). Builds a tableNames `=contains=` filter from the full
        class ancestry chain in ProcessPower.dcf.

        `filter_query` — optional custom AECDM filter (standard or advanced syntax).
        See filtering docs:
          https://aps.autodesk.com/en/docs/aecdatamodel/v1/developers_guide/filtering/standard-filtering/
          https://aps.autodesk.com/en/docs/aecdatamodel/v1/developers_guide/filtering/advanced-filtering/

        Examples:
          property.id.autodesk.plant3d.parameter:tag-1.0.0=contains='P-101'
          and(property.id.autodesk.plant3d.parameter:tableNames-1.0.0=contains='Primary,PipeLines,Lines,EngineeringItems',property.id.autodesk.plant3d.parameter:area-1.0.0=contains='100')

        Client-side isolation: `=contains=` is a substring test, so filters like
        EngineeringItems also match NonEngineeringItems. When the schema marks a
        class as unsafe (or scan-only), results are post-filtered by exact Table
        Names ancestry path. Set `skip_client_isolation=true` to return the raw
        server response.

        Returns element id, name, properties, lastModifiedOn, plus filter metadata
        (filterSafe, filterOverMatches, fetchedCount, clientFilteredCount).

        Uses the same APS access token as other tools.
        """
        access_token = require_access_token(ctx)
        if not access_token:
            return dict(not_authenticated)
        try:
            return await list_plant3d_elements_by_table(
                element_group_id,
                access_token,
                table_name=table_name,
                filter_query=filter_query,
                dcf_path=dcf_path,
                skip_client_isolation=skip_client_isolation,
            )
        except ValueError as exc:
            return {"error": "invalid_filter", "detail": str(exc)}
        except (httpx.HTTPError, RuntimeError) as exc:
            return _aecdm_tool_error(exc, not_authenticated=not_authenticated)

    @mcp.tool()
    async def get_aecdm_model_id_by_version_urn(
        hub_name: str,
        project_name: str,
        version_urn: str,
        ctx: Context,
    ) -> dict[str, Any]:
        """
        Resolve the AECDM model id for a hub + project + file-version URN.

        Pipeline:
          1. GetHubs(name=hub_name)               -> hub.id
          2. GetProjects(hubId, name=project_name) -> project.id
          3. GetElementGroupsByProject(projectId) -> match by fileVersionUrn

        `version_urn` should be the bubble version URN, e.g.
        `urn:adsk.wipprod:fs.file:vf.8wY9DdApT2OL68gxG-vuBg?version=28`.
        """
        access_token = require_access_token(ctx)
        if not access_token:
            print("[aecdm]  auth -> NOT AUTHENTICATED, aborting")
            return dict(not_authenticated)

        try:
            return await get_aecdm_model_id(
                hub_name, project_name, version_urn, access_token
            )
        except httpx.HTTPStatusError as exc:
            logger.warning("AECDM call returned %s", exc.response.status_code)
            try:
                body: Any = exc.response.json()
            except ValueError:
                body = exc.response.text
            if exc.response.status_code == 401:
                return dict(not_authenticated)
            return {
                "error": "autodesk_api_error",
                "status_code": exc.response.status_code,
                "body": body,
            }
        except httpx.RequestError as exc:
            logger.warning("AECDM request failed: %s", type(exc).__name__)
            return {"error": "request_failed", "detail": str(exc)}
        except RuntimeError as exc:
            return {"error": "aecdm_graphql_error", "detail": str(exc)}

    @mcp.tool()
    async def get_aecdm_element_properties(
        element_group_id: str,
        property_filter: str,
        ctx: Context,
        origin_component_filter: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Get elements (with properties + geometry components) from an AECDM
        elementGroup, filtered by a property-filter expression and/or an
        origin bounding box.

        Args:
          element_group_id: The AECDM elementGroup id (model id), e.g.
            `YWVjZH5OR2dmQ0VMdjJ2Rmh6TVRIcTViWkY1X0wyQ342ZTY3YmU1MC0xZGY2LTViNDktYmY1Ny1lYTJiMDJhODhmNGQ`.
          property_filter: AECDM property-filter expression, e.g.
            `'property.name.External ID'==552d2a83-4642-4d5c-8e7f-5de799129097-000d04f3`
            or `'property.name.Category'=='Walls'`.
          origin_component_filter: Optional spatial filter on origin component, e.g.
            ```
            {
              "originRange": {
                "startPoints": {"x": 0, "y": 0, "z": 0},
                "endPoints":   {"x": 10, "y": 10, "z": 10}
              }
            }
            ```

        Returns the full list of matching elements (paginated until exhausted),
        each with `properties` and `components` populated per the AECDM schema.

        components contains the bounding-box data of the element: Axis + Range. 
        """
        access_token = require_access_token(ctx)
        if not access_token:
            print("[aecdm]  auth -> NOT AUTHENTICATED, aborting")
            return dict(not_authenticated)

        try:
            return await get_elements_by_filter(
                element_group_id,
                property_filter,
                access_token,
                origin_component_filter=origin_component_filter,
            )
        except httpx.HTTPStatusError as exc:
            logger.warning("AECDM call returned %s", exc.response.status_code)
            try:
                body: Any = exc.response.json()
            except ValueError:
                body = exc.response.text
            if exc.response.status_code == 401:
                return dict(not_authenticated)
            return {
                "error": "autodesk_api_error",
                "status_code": exc.response.status_code,
                "body": body,
            }
        except httpx.RequestError as exc:
            logger.warning("AECDM request failed: %s", type(exc).__name__)
            return {"error": "request_failed", "detail": str(exc)}
        except RuntimeError as exc:
            return {"error": "aecdm_graphql_error", "detail": str(exc)}
