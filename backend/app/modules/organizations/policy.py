"""Application roles. Identity-provider role claims never grant platform access."""

from __future__ import annotations

from starlette.requests import HTTPConnection

from app.core.errors import PermissionDeniedError
from app.core.principal import Principal

READ = frozenset(
    {"catalog.read", "system.read", "clusters.read", "infrastructure.read", "credentials.read", "operations.read"}
)
WRITE = frozenset(
    {
        "clusters.write",
        "infrastructure.write",
        "credentials.write",
        "operations.write",
        "secrets.reveal",
        "terminal.exec",
    }
)
ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "platform-admin": READ | WRITE | {"audit.read", "organization.manage", "platform.manage"},
    "org-admin": READ | WRITE | {"audit.read", "organization.manage"},
    "operator": READ | WRITE,
    "viewer": READ,
    "member": frozenset({"catalog.read", "system.read"}),
}


def permissions(principal: Principal) -> frozenset[str]:
    return frozenset().union(*(ROLE_PERMISSIONS.get(role, frozenset()) for role in principal.roles))


def require(principal: Principal, permission: str) -> None:
    if principal.is_system or permission in permissions(principal):
        return
    raise PermissionDeniedError(f"This action requires {permission}")


def request_permission(request: HTTPConnection) -> str:
    route = request.scope.get("route")
    path = getattr(route, "path", request.url.path)
    prefix = request.app.state.api_prefix
    if path.startswith(prefix):
        path = path[len(prefix) :]
    read = request.scope.get("method", "GET") in {"GET", "HEAD", "OPTIONS"}
    if request.scope["type"] == "websocket":
        return "terminal.exec"
    if path.startswith("/administration"):
        if path.endswith("/permissions"):
            return "system.read"
        return "platform.manage" if path.startswith("/administration/users") else "organization.manage"
    if path.startswith("/audit"):
        return "audit.read"
    if path.startswith("/catalog"):
        return "catalog.read"
    if path.startswith("/system"):
        return "system.read" if read else "platform.manage"
    if path.startswith("/clusters"):
        if path.endswith("/reveal"):
            return "secrets.reveal"
        return "clusters.read" if read else "clusters.write"
    if path.startswith("/credentials"):
        return "credentials.read" if read else "credentials.write"
    if path.startswith(("/machines", "/provider-connections", "/discovery", "/network")):
        return "infrastructure.read" if read else "infrastructure.write"
    if path.startswith("/operations"):
        return "operations.read" if read else "operations.write"
    return "platform.manage"
