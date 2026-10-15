"""MCP server for the Plant3D AEC Data Model tools."""

from __future__ import annotations

import contextvars
import logging
import os
from typing import Mapping

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from elements_tools import register_elements_tools
from plant3d_relationships import register_plant3d_relationship_tools
from plant3d_tools import register_plant3d_tools

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_MCP_ACCEPT = b"application/json, text/event-stream"
_request_access_token: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_access_token",
    default=None,
)
_AUTHORIZATION_HEADER_SCHEMA = "Authorization: Bearer <APS_access_token>"
_NOT_AUTHENTICATED: dict[str, str] = {
    "error": "not_authenticated",
    "detail": (
        f"APS access token required. Send {_AUTHORIZATION_HEADER_SCHEMA} on each MCP HTTP request. "
        "The MCP client must provide the caller's token in the request context."
    ),
}


def _normalize_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {key.lower(): value for key, value in headers.items()}


def _extract_bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.strip().split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip() or None
    return None


def _extract_token_from_headers(headers: Mapping[str, str]) -> str | None:
    return _extract_bearer_token(_normalize_headers(headers).get("authorization"))


def _extract_token_from_scope(scope: Scope) -> str | None:
    raw_headers = scope.get("headers", [])
    headers = {key.decode("latin-1"): value.decode("latin-1") for key, value in raw_headers}
    return _extract_token_from_headers(headers)


def _headers_from_context(ctx: Context) -> dict[str, str]:
    request = ctx.request_context.request
    if isinstance(request, Request):
        return dict(request.headers)
    return {}


def get_access_token(ctx: Context | None = None) -> str | None:
    """Resolve the APS bearer token supplied in the current MCP request."""
    if ctx is not None:
        token = _extract_token_from_headers(_headers_from_context(ctx))
        if token:
            return token
    return _request_access_token.get()


def require_access_token(ctx: Context) -> str | None:
    """Return the token, or None when the caller is unauthenticated."""
    return get_access_token(ctx)


def _transport_security() -> TransportSecuritySettings:
    """Allow configured proxy hosts while retaining localhost defaults."""
    extra_hosts = [
        host.strip()
        for host in os.environ.get("MCP_ALLOWED_HOSTS", "").split(",")
        if host.strip()
    ]
    if extra_hosts:
        return TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*", *extra_hosts],
        )
    return TransportSecuritySettings(enable_dns_rebinding_protection=False)


def _parse_accept(accept: bytes) -> tuple[bool, bool]:
    parts = [part.strip().lower() for part in accept.decode().split(",") if part.strip()]
    has_json = any(part.startswith("application/json") for part in parts)
    has_sse = any(part.startswith("text/event-stream") for part in parts)
    return has_json, has_sse


def _ensure_mcp_accept(scope: Scope) -> Scope:
    method = scope.get("method", "")
    if method not in {"GET", "POST", "DELETE", "OPTIONS"}:
        return scope

    headers = list(scope.get("headers", []))
    accept = next((value for key, value in headers if key.lower() == b"accept"), b"")
    has_json, has_sse = _parse_accept(accept)
    valid = has_sse if method == "GET" else has_json if method == "POST" else True
    if valid:
        return scope

    logger.info(
        "Normalizing Accept for MCP %s (was %r)",
        method,
        accept.decode() if accept else "<missing>",
    )
    updated_scope = dict(scope)
    updated_scope["headers"] = [
        (key, value) for key, value in headers if key.lower() != b"accept"
    ] + [(b"accept", _MCP_ACCEPT)]
    return updated_scope


class _MCPHttpMiddleware:
    """Normalize MCP Accept headers and capture per-request bearer tokens."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope.get("path", "").rstrip("/") == "/mcp":
            scope = _ensure_mcp_accept(scope)
            reset = _request_access_token.set(_extract_token_from_scope(scope))
            try:
                await self.app(scope, receive, send)
            finally:
                _request_access_token.reset(reset)
            return
        await self.app(scope, receive, send)


mcp = FastMCP(
    "Plant3D AECDM MCP Server",
    host="0.0.0.0",
    json_response=True,
    transport_security=_transport_security(),
)


@mcp.custom_route("/", methods=["GET"])
async def health(_request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


register_elements_tools(
    mcp,
    require_access_token=require_access_token,
    not_authenticated=_NOT_AUTHENTICATED,
)
register_plant3d_tools(mcp)
register_plant3d_relationship_tools(
    mcp,
    require_access_token=require_access_token,
    not_authenticated=_NOT_AUTHENTICATED,
)

app = _MCPHttpMiddleware(mcp.streamable_http_app())


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "server:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
