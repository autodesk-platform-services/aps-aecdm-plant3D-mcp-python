"""
Plant 3D / AEC Data Model element filtering helpers.

Builds tableNames-based filters from ProcessPower.dcf class hierarchy and applies
client-side isolation when server-side `=contains=` filters over-match.

See:
  https://aps.autodesk.com/en/docs/aecdatamodel/v1/developers_guide/filtering/standard-filtering/
  https://aps.autodesk.com/en/docs/aecdatamodel/v1/developers_guide/filtering/advanced-filtering/
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

from plant3d_tools import _read_schema, _resolve_dcf_path

TABLE_NAMES_PARAM = "autodesk.plant3d.parameter:tableNames-1.0.0"
TABLE_NAMES_PROPERTY_ID = f"property.id.{TABLE_NAMES_PARAM}"
SCAN_ONLY_BRANCHES = frozenset({"EngineeringItems", "NonEngineeringItems"})


def build_table_names_filter(filter_value: str) -> str:
    """Standard filter: tableNames ancestry chain (leaf-first, comma-separated)."""
    return f"{TABLE_NAMES_PROPERTY_ID}=contains='{filter_value}'"


def element_table_names_ancestry(element: dict[str, Any]) -> list[str]:
    """
    Root-first class ancestry from the Table Names property.

    API value is leaf-first (e.g. Primary,PipeLines,Lines,EngineeringItems);
    returned list is root-first for path comparisons.
    """
    for prop in (element.get("properties") or {}).get("results") or []:
        definition = prop.get("definition") or {}
        if definition.get("id") != TABLE_NAMES_PARAM:
            continue
        display_value = prop.get("displayValue")
        if not display_value:
            return []
        parts = [part.strip() for part in str(display_value).split(",") if part.strip()]
        return list(reversed(parts))
    return []


def isolate_elements_by_path(
    elements: list[dict[str, Any]],
    path_root_first: list[str],
) -> list[dict[str, Any]]:
    """Keep elements whose Table Names ancestry starts with the requested path."""
    if not path_root_first:
        return list(elements)

    isolated: list[dict[str, Any]] = []
    for element in elements:
        ancestry = element_table_names_ancestry(element)
        if len(ancestry) < len(path_root_first):
            continue
        if all(ancestry[index] == segment for index, segment in enumerate(path_root_first)):
            isolated.append(element)
    return isolated


def _chain_leaf_first(index: dict[str, dict[str, Any]], table_name: str) -> list[str]:
    chain: list[str] = []
    node = index.get(table_name)
    while node and node["tableName"] != "PnPBase":
        chain.append(node["tableName"])
        parent = node.get("parentClass") or ""
        node = index.get(parent) if parent else None
    return chain


def _subtree_table_names(index: dict[str, dict[str, Any]], table_name: str) -> set[str]:
    node = index.get(table_name)
    if not node:
        return set()

    names = {table_name}
    for child_name in _child_table_names(index, table_name):
        names.update(_subtree_table_names(index, child_name))
    return names


def _child_table_names(index: dict[str, dict[str, Any]], parent_name: str) -> list[str]:
    return [
        row["tableName"]
        for row in index.values()
        if row.get("parentClass") == parent_name
    ]


@lru_cache(maxsize=4)
def _class_filter_index_key(dcf_path_str: str) -> dict[str, dict[str, Any]]:
    dcf = _resolve_dcf_path(dcf_path_str or None)
    tables = _read_schema(dcf)
    index = {row["tableName"]: row for row in tables if row.get("tableName")}

    metadata: dict[str, dict[str, Any]] = {}
    class_names = [name for name in index if name != "PnPBase"]

    for table_name in class_names:
        path_leaf_first = _chain_leaf_first(index, table_name)
        filter_value = ",".join(path_leaf_first)
        metadata[table_name] = {
            "tableName": table_name,
            "displayName": index[table_name].get("displayName"),
            "tableNamesPathLeafFirst": path_leaf_first,
            "tableNamesPathRootFirst": list(reversed(path_leaf_first)),
            "filterValue": filter_value,
            "filterQuery": build_table_names_filter(filter_value) if filter_value else None,
            "scanOnly": table_name in SCAN_ONLY_BRANCHES,
        }

    for table_name in class_names:
        needle = metadata[table_name]["filterValue"]
        mine = _subtree_table_names(index, table_name)
        over_matches = sorted(
            other_name
            for other_name in class_names
            if other_name not in mine and needle in metadata[other_name]["filterValue"]
        )
        node = metadata[table_name]
        node["filterOverMatches"] = over_matches
        node["filterSafe"] = len(over_matches) == 0

    if "PnPBase" in index:
        metadata["PnPBase"] = {
            "tableName": "PnPBase",
            "displayName": index["PnPBase"].get("displayName"),
            "tableNamesPathLeafFirst": [],
            "tableNamesPathRootFirst": [],
            "filterValue": None,
            "filterQuery": None,
            "scanOnly": True,
            "filterOverMatches": [],
            "filterSafe": False,
        }

    return metadata


def get_class_filter_index(dcf_path: str | Path | None = None) -> dict[str, dict[str, Any]]:
    return _class_filter_index_key(str(dcf_path or ""))


def resolve_plant3d_element_filter(
    *,
    table_name: str | None = None,
    filter_query: str | None = None,
    dcf_path: str | Path | None = None,
) -> dict[str, Any]:
    """
    Resolve the server-side AECDM filter and whether client isolation is required.

    Provide `table_name` (from ProcessPower schema) and/or a custom `filter_query`
    using AECDM standard/advanced filtering syntax.
    """
    table_name_clean = (table_name or "").strip()
    filter_query_clean = (filter_query or "").strip()

    if not table_name_clean and not filter_query_clean:
        raise ValueError("Provide table_name, filter_query, or both")

    table_meta: dict[str, Any] | None = None
    if table_name_clean:
        index = get_class_filter_index(dcf_path)
        if table_name_clean not in index:
            raise ValueError(f"Unknown Plant 3D table name: {table_name_clean}")
        table_meta = index[table_name_clean]

    if filter_query_clean:
        server_filter_query = filter_query_clean
        filter_source = "custom_filter_query"
    elif table_meta:
        server_filter_query = table_meta.get("filterQuery")
        filter_source = "table_names_contains"
    else:
        server_filter_query = None
        filter_source = "none"

    requires_client_isolation = False
    if table_meta:
        requires_client_isolation = bool(
            table_meta.get("scanOnly") or not table_meta.get("filterSafe")
        )

    return {
        "tableName": table_name_clean or None,
        "filterQuery": server_filter_query,
        "filterSource": filter_source,
        "requiresClientIsolation": requires_client_isolation,
        "tableNamesPathRootFirst": (table_meta or {}).get("tableNamesPathRootFirst") or [],
        "tableNamesPathLeafFirst": (table_meta or {}).get("tableNamesPathLeafFirst") or [],
        "filterValue": (table_meta or {}).get("filterValue"),
        "filterSafe": (table_meta or {}).get("filterSafe"),
        "filterOverMatches": (table_meta or {}).get("filterOverMatches") or [],
        "scanOnly": bool((table_meta or {}).get("scanOnly")),
        "filter": {"query": server_filter_query} if server_filter_query else None,
        "filteringDocs": {
            "standard": (
                "https://aps.autodesk.com/en/docs/aecdatamodel/v1/developers_guide/"
                "filtering/standard-filtering/"
            ),
            "advanced": (
                "https://aps.autodesk.com/en/docs/aecdatamodel/v1/developers_guide/"
                "filtering/advanced-filtering/"
            ),
        },
    }
