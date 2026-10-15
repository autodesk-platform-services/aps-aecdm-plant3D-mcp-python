"""
Plant 3D engineering-item relationship discovery via AEC Data Model.

Workflow:
  1. Load relationship types from ProcessPower.dcf (relationshipTypes.sql)
  2. For each physical relationship type, query relationship elements by pnpClassName
  3. Find relationship rows where an endpoint matches the given className + pnpId
  4. Resolve paired endpoints and fetch tag / name / spec for each pair
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable

import httpx
from mcp.server.fastmcp import Context, FastMCP

from elements_tools import (
    _GET_PLANT3D_ELEMENTS_BY_TABLE_QUERY,
    _aecdm_graphql,
    _aecdm_tool_error,
    _pagination_variables,
)
from plant3d_tools import _read_relationships, _resolve_dcf_path

logger = logging.getLogger(__name__)

_CLASS_NAME_PARAM = "autodesk.plant3d.parameter:pnpClassName-1.0.0"
_PNP_ID_PARAM = "autodesk.plant3d.parameter:pnpId-1.0.0"
_TAG_PARAM = "autodesk.plant3d.parameter:tag-1.0.0"
_SPEC_PARAM = "autodesk.plant3d.parameter:spec-1.0.0"
_FRIENDLY_NAME_PARAM = "autodesk.plant3d.parameter:className-1.0.0"

_CLASS_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")
_PNP_ID_RE = re.compile(r"^\d+$")


def _validate_class_name(class_name: str) -> str:
    cleaned = class_name.strip()
    if not cleaned or not _CLASS_NAME_RE.fullmatch(cleaned):
        raise ValueError(
            "class_name must be a non-empty Plant 3D class name (letters, digits, underscore)"
        )
    return cleaned


def _validate_pnp_id(pnp_id: str) -> str:
    cleaned = str(pnp_id).strip()
    if not cleaned or not _PNP_ID_RE.fullmatch(cleaned):
        raise ValueError("pnp_id must be a non-empty numeric Plant 3D PnP ID")
    return cleaned


def build_relationship_type_filter(type_name: str) -> str:
    type_name = _validate_class_name(type_name)
    return f"property.id.{_CLASS_NAME_PARAM}=contains='{type_name}'"


def build_element_identity_filter(class_name: str, pnp_id: str) -> str:
    class_name = _validate_class_name(class_name)
    pnp_id = _validate_pnp_id(pnp_id)
    return (
        f"and(property.id.{_CLASS_NAME_PARAM}=contains='{class_name}',"
        f"property.id.{_PNP_ID_PARAM}=='{pnp_id}')"
    )


def _props(element: dict[str, Any]) -> list[dict[str, Any]]:
    return (element.get("properties") or {}).get("results") or []


def _read_prop_by_definition(element: dict[str, Any], definition_id: str) -> str | None:
    for prop in _props(element):
        definition = prop.get("definition") or {}
        if definition.get("id") != definition_id:
            continue
        value = prop.get("displayValue")
        if value is None:
            continue
        text = str(value).strip()
        return text or None
    return None


def _read_prop_by_name(element: dict[str, Any], name: str) -> str | None:
    for prop in _props(element):
        if prop.get("name") != name:
            continue
        value = prop.get("displayValue")
        if value is None:
            continue
        text = str(value).strip()
        return text or None
    return None


def _endpoint_class_name_candidates(role: str) -> list[str]:
    return [f"{role}ClassName", f"{role}Class", f"{role}.ClassName"]


def extract_relationship_endpoints(
    element: dict[str, Any],
    roles: set[str],
) -> list[dict[str, str | None]]:
    """Endpoint role -> pnpId, with optional per-endpoint class name when present."""
    endpoints: list[dict[str, str | None]] = []
    for prop in _props(element):
        role = prop.get("name")
        if role not in roles:
            continue
        pnp_id = prop.get("displayValue")
        if pnp_id is None or not str(pnp_id).strip():
            continue
        class_name: str | None = None
        for candidate in _endpoint_class_name_candidates(role):
            class_name = _read_prop_by_name(element, candidate)
            if class_name:
                break
        endpoints.append(
            {
                "role": role,
                "pnpId": str(pnp_id).strip(),
                "className": class_name,
            }
        )
    return endpoints


def _endpoint_matches_item(
    endpoint: dict[str, str | None],
    class_name: str,
    pnp_id: str,
) -> bool:
    if endpoint.get("pnpId") != pnp_id:
        return False
    endpoint_class = endpoint.get("className")
    if endpoint_class:
        return endpoint_class == class_name
    return True


def summarize_engineering_item(element: dict[str, Any]) -> dict[str, Any]:
    return {
        "elementId": element.get("id"),
        "name": element.get("name"),
        "pnpId": _read_prop_by_definition(element, _PNP_ID_PARAM),
        "className": _read_prop_by_definition(element, _CLASS_NAME_PARAM),
        "tag": _read_prop_by_definition(element, _TAG_PARAM),
        "spec": _read_prop_by_definition(element, _SPEC_PARAM),
        "friendlyName": _read_prop_by_definition(element, _FRIENDLY_NAME_PARAM),
        "lastModifiedOn": element.get("lastModifiedOn"),
    }


def load_relationship_types(dcf_path: str | None = None) -> list[dict[str, Any]]:
    dcf = _resolve_dcf_path(dcf_path)
    relationships = _read_relationships(dcf)
    enriched: list[dict[str, Any]] = []
    for rel in relationships:
        endpoints = rel.get("endpoints") or []
        type_name = rel.get("typeName")
        if not type_name:
            continue
        enriched.append(
            {
                **rel,
                "hasTable": bool(endpoints),
                "filterQuery": build_relationship_type_filter(type_name) if endpoints else None,
            }
        )
    return enriched


async def _fetch_elements_by_filter(
    client: httpx.AsyncClient,
    element_group_id: str,
    filter_query: str | None,
    access_token: str,
    *,
    max_pages: int = 50,
    page_limit: int = 99,
) -> tuple[list[dict[str, Any]], int]:
    all_elements: list[dict[str, Any]] = []
    cursor: str | None = None
    pages = 0

    while pages < max_pages:
        pages += 1
        variables: dict[str, Any] = {
            "groupId": element_group_id,
            "pagination": _pagination_variables(cursor, limit=page_limit),
        }
        if filter_query:
            variables["filter"] = {"query": filter_query}

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

    return all_elements, pages


async def list_engineering_item_relationships(
    element_group_id: str,
    class_name: str,
    pnp_id: str,
    access_token: str,
    *,
    dcf_path: str | None = None,
    max_pages_per_type: int = 50,
) -> dict[str, Any]:
    """
    Discover paired engineering items for a given className + pnpId.

    Scans every physical relationship type, matches relationship rows that
    reference the source item, then resolves pair tag/name/spec via a second
    elementsByElementGroup query per unique pair.
    """
    class_name = _validate_class_name(class_name)
    pnp_id = _validate_pnp_id(pnp_id)

    relationship_types = [
        rel for rel in load_relationship_types(dcf_path) if rel.get("hasTable")
    ]
    raw_matches: list[dict[str, Any]] = []
    relationship_pages = 0
    relationship_elements_scanned = 0

    async with httpx.AsyncClient(timeout=120.0) as client:
        for rel_type in relationship_types:
            type_name = rel_type["typeName"]
            roles = {endpoint["role"] for endpoint in rel_type.get("endpoints") or []}
            elements, pages = await _fetch_elements_by_filter(
                client,
                element_group_id,
                rel_type["filterQuery"],
                access_token,
                max_pages=max_pages_per_type,
            )
            relationship_pages += pages
            relationship_elements_scanned += len(elements)

            for element in elements:
                endpoints = extract_relationship_endpoints(element, roles)
                mine = next(
                    (
                        endpoint
                        for endpoint in endpoints
                        if _endpoint_matches_item(endpoint, class_name, pnp_id)
                    ),
                    None,
                )
                if mine is None:
                    continue

                for other in endpoints:
                    if other["pnpId"] == pnp_id:
                        continue
                    raw_matches.append(
                        {
                            "relationshipType": type_name,
                            "relationshipElementId": element.get("id"),
                            "sourceRole": mine.get("role"),
                            "pairRole": other.get("role"),
                            "pairPnpId": other.get("pnpId"),
                            "pairClassName": other.get("className"),
                        }
                    )

        pair_cache: dict[tuple[str, str], dict[str, Any]] = {}
        pairs: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str, str]] = set()

        for match in raw_matches:
            pair_pnp_id = match["pairPnpId"]
            if not pair_pnp_id:
                continue
            pair_class_name = match.get("pairClassName")
            dedupe_key = (
                match["relationshipType"],
                match.get("pairRole") or "",
                pair_class_name or "",
                pair_pnp_id,
            )
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)

            cache_key = (pair_class_name or "", pair_pnp_id)
            if cache_key not in pair_cache:
                if pair_class_name:
                    detail_filter = build_element_identity_filter(pair_class_name, pair_pnp_id)
                else:
                    detail_filter = f"property.id.{_PNP_ID_PARAM}=='{pair_pnp_id}'"

                detail_elements, _ = await _fetch_elements_by_filter(
                    client,
                    element_group_id,
                    detail_filter,
                    access_token,
                    max_pages=5,
                )
                pair_cache[cache_key] = (
                    summarize_engineering_item(detail_elements[0])
                    if detail_elements
                    else {
                        "elementId": None,
                        "name": None,
                        "pnpId": pair_pnp_id,
                        "className": pair_class_name,
                        "tag": None,
                        "spec": None,
                        "friendlyName": None,
                        "lastModifiedOn": None,
                    }
                )

            resolved = pair_cache[cache_key]
            pairs.append(
                {
                    "relationshipType": match["relationshipType"],
                    "relationshipElementId": match["relationshipElementId"],
                    "source": {
                        "className": class_name,
                        "pnpId": pnp_id,
                        "role": match.get("sourceRole"),
                    },
                    "pair": {
                        "role": match.get("pairRole"),
                        "className": resolved.get("className") or pair_class_name,
                        "pnpId": resolved.get("pnpId") or pair_pnp_id,
                        "name": resolved.get("name"),
                        "tag": resolved.get("tag"),
                        "spec": resolved.get("spec"),
                        "friendlyName": resolved.get("friendlyName"),
                        "elementId": resolved.get("elementId"),
                        "lastModifiedOn": resolved.get("lastModifiedOn"),
                        "resolved": resolved.get("elementId") is not None,
                    },
                }
            )

    pairs.sort(
        key=lambda item: (
            item["relationshipType"],
            item["pair"].get("className") or "",
            item["pair"].get("pnpId") or "",
        )
    )

    return {
        "elementGroupId": element_group_id,
        "source": {"className": class_name, "pnpId": pnp_id},
        "relationshipTypesScanned": len(relationship_types),
        "relationshipPagesScanned": relationship_pages,
        "relationshipElementsScanned": relationship_elements_scanned,
        "pairCount": len(pairs),
        "pairs": pairs,
    }


def register_plant3d_relationship_tools(
    mcp: FastMCP,
    *,
    require_access_token: Callable[[Context], str | None],
    not_authenticated: dict[str, str],
) -> None:
    """Register Plant 3D relationship MCP tools."""

    @mcp.tool()
    async def get_aecdm_plant3d_engineering_item_relationships(
        element_group_id: str,
        class_name: str,
        pnp_id: str,
        ctx: Context,
        dcf_path: str | None = None,
    ) -> dict[str, Any]:
        """
        Get all paired engineering items for a Plant 3D element (className + pnpId).

        Workflow:
          1. Load relationship types from ProcessPower.dcf (relationshipTypes.sql)
          2. For each physical relationship type, query relationship elements:
             property.id.autodesk.plant3d.parameter:pnpClassName-1.0.0=contains='<type>'
          3. Find relationship rows whose endpoint matches the given className and pnpId
          4. For each paired endpoint, query elementsByElementGroup again using the
             pair's className + pnpId to resolve tag, name, and spec

        `element_group_id` is a data set id from get_aecdm_plant3d_projects.
        `class_name` is the engineering item class (pnpClassName), e.g. Primary, GateValve.
        `pnp_id` is the item's PnP ID.

        Relationship endpoint roles come from relationshipEndpoints.sql via
        get_processpower_relationships.

        Uses the same APS access token as other tools.
        """
        access_token = require_access_token(ctx)
        if not access_token:
            return dict(not_authenticated)
        try:
            return await list_engineering_item_relationships(
                element_group_id,
                class_name,
                pnp_id,
                access_token,
                dcf_path=dcf_path,
            )
        except ValueError as exc:
            return {"error": "invalid_relationship_request", "detail": str(exc)}
        except (httpx.HTTPError, RuntimeError) as exc:
            return _aecdm_tool_error(exc, not_authenticated=not_authenticated)
