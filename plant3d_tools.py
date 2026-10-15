"""
ProcessPower.dcf schema and relationship readers for Plant 3D P&ID projects.

Queries the SQLite database embedded in a .dcf file using the SQL scripts in
Plant3D Schema/. Exposes two MCP tools:

  - get_processpower_schema        — table names and display names (class hierarchy)
  - get_processpower_relationships — relationship types and their endpoint roles
"""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

logger = logging.getLogger(__name__)

_PLANT3D_DIR = Path(__file__).resolve().parent / "Plant3D Schema"
_DEFAULT_DCF_PATH = _PLANT3D_DIR / "ProcessPower.dcf"

_SQL_CLASS_HIERARCHY = (_PLANT3D_DIR / "classHierarchy.sql").read_text(encoding="utf-8")
_SQL_RELATIONSHIP_TYPES = (_PLANT3D_DIR / "relationshipTypes.sql").read_text(encoding="utf-8")
_SQL_RELATIONSHIP_ENDPOINTS = (_PLANT3D_DIR / "relationshipEndpoints.sql").read_text(encoding="utf-8")


def _allowed_dcf_roots() -> list[Path]:
    roots = [_PLANT3D_DIR.resolve()]
    extra = os.environ.get("PLANT3D_DCF_ALLOWED_DIR", "").strip()
    if extra:
        roots.append(Path(extra).expanduser().resolve())
    return roots


def _resolve_dcf_path(dcf_path: str | None) -> Path:
    """Resolve and validate the .dcf path (must stay under an allowed root)."""
    raw = (dcf_path or os.environ.get("PROCESSPOWER_DCF_PATH", "")).strip()
    candidate = Path(raw).expanduser() if raw else _DEFAULT_DCF_PATH
    if not candidate.is_absolute():
        candidate = (_PLANT3D_DIR / candidate).resolve()
    else:
        candidate = candidate.resolve()

    if candidate.suffix.lower() != ".dcf":
        raise ValueError("dcf_path must point to a .dcf file")

    if not any(
        candidate == root or candidate.is_relative_to(root) for root in _allowed_dcf_roots()
    ):
        raise ValueError(
            "dcf_path must be under Plant3D Schema/ or PLANT3D_DCF_ALLOWED_DIR"
        )

    if not candidate.is_file():
        raise FileNotFoundError(f"DCF file not found: {candidate}")

    return candidate


def _query_rows(dcf: Path, sql: str) -> list[dict[str, Any]]:
    with sqlite3.connect(f"file:{dcf}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(sql)
        return [dict(row) for row in cursor.fetchall()]


def _read_schema(dcf: Path) -> list[dict[str, Any]]:
    rows = _query_rows(dcf, _SQL_CLASS_HIERARCHY)
    return [
        {
            "tableName": row["TableName"],
            "displayName": row["DisplayName"],
            "parentClass": row["ParentClass"],
            "level": row["Level"],
            "abstract": str(row["Abstract"]).upper() == "TRUE",
        }
        for row in rows
    ]


def _read_relationships(dcf: Path) -> list[dict[str, Any]]:
    types = _query_rows(dcf, _SQL_RELATIONSHIP_TYPES)
    endpoints = _query_rows(dcf, _SQL_RELATIONSHIP_ENDPOINTS)

    endpoints_by_type: dict[str, list[dict[str, str]]] = {}
    for row in endpoints:
        endpoints_by_type.setdefault(row["TypeName"], []).append(
            {"role": row["Role"], "columnType": row["ColumnType"]}
        )

    return [
        {
            "typeName": row["TypeName"],
            "physicalName": row["PhysicalName"],
            "endpoints": endpoints_by_type.get(row["TypeName"], []),
        }
        for row in types
    ]


def register_plant3d_tools(mcp: FastMCP) -> None:
    """Register ProcessPower.dcf schema and relationship MCP tools."""

    @mcp.tool()
    def get_processpower_schema(
        ctx: Context,
        dcf_path: str | None = None,
    ) -> dict[str, Any]:
        """
        Read the P&ID class schema from ProcessPower.dcf.

        Returns every table (class) with its display name, parent class, hierarchy
        level, and whether it is abstract. Data is sourced from the classHierarchy
        query against the SQLite database inside the .dcf file.

        By default reads Plant3D Schema/ProcessPower.dcf. Override with dcf_path or the
        PROCESSPOWER_DCF_PATH environment variable.
        """
        _ = ctx
        try:
            dcf = _resolve_dcf_path(dcf_path)
            tables = _read_schema(dcf)
            return {
                "dcfPath": str(dcf),
                "tableCount": len(tables),
                "tables": tables,
            }
        except (OSError, sqlite3.Error, ValueError) as exc:
            logger.warning("ProcessPower schema read failed: %s", exc)
            return {"error": "schema_read_failed", "detail": str(exc)}

    @mcp.tool()
    def get_processpower_relationships(
        ctx: Context,
        dcf_path: str | None = None,
    ) -> dict[str, Any]:
        """
        Read available relationship types from ProcessPower.dcf.

        Returns each relationship type with its physical table name and endpoint
        roles (columns that hold related element PnPIDs). Abstract types have no
        physical table and therefore no endpoints.

        By default reads Plant3D Schema/ProcessPower.dcf. Override with dcf_path or the
        PROCESSPOWER_DCF_PATH environment variable.
        """
        _ = ctx
        try:
            dcf = _resolve_dcf_path(dcf_path)
            relationships = _read_relationships(dcf)
            return {
                "dcfPath": str(dcf),
                "relationshipCount": len(relationships),
                "relationships": relationships,
            }
        except (OSError, sqlite3.Error, ValueError) as exc:
            logger.warning("ProcessPower relationships read failed: %s", exc)
            return {"error": "relationships_read_failed", "detail": str(exc)}
