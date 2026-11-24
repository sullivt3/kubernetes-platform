"""Keep ephemeral administrator runtimes out of ordinary management API paths.

This is a server-side routing guard, not Kubernetes admission or a sandbox for
malicious cluster administrators. Session lifecycle handlers explicitly opt out.
"""

from __future__ import annotations

import json
import re
from urllib.parse import unquote, urlsplit

from app.core.errors import PermissionDeniedError
from app.modules.kubernetes.client import KubeClient, KubernetesAPIError

PREFIX = "kp-shell-"


def is_reserved(name):
    return isinstance(name, str) and re.fullmatch(r"kp-shell-[a-f0-9]{32}", name) is not None


def protected_object(obj):
    if not isinstance(obj, dict):
        return False
    meta = obj.get("metadata") or {}
    return is_reserved(meta.get("namespace")) or (
        obj.get("kind") in {"Namespace", "ClusterRoleBinding"} and is_reserved(meta.get("name"))
    )


def check_path(path):
    parts = unquote(urlsplit(path).path).split("/")
    if any(
        part in {"namespaces", "clusterrolebindings"} and i + 1 < len(parts) and is_reserved(parts[i + 1])
        for i, part in enumerate(parts)
    ):
        raise PermissionDeniedError("Interactive runtime resources are reserved; use Command Center session controls")


def check_manifest(obj):
    if protected_object(obj):
        raise PermissionDeniedError("Interactive runtime namespaces and grants are reserved")
    if isinstance(obj, dict) and obj.get("kind") == "List":
        for item in obj.get("items", []):
            check_manifest(item)


class ReservedAPI:
    def __init__(self, api):
        self.api = api

    def __getattr__(self, name):
        return getattr(self.api, name)

    def request(self, method, path, **kwargs):
        check_path(path)
        body = kwargs.get("body")
        if isinstance(body, (bytes, str)):
            try:
                body = json.loads(body)
            except (ValueError, UnicodeDecodeError):
                body = None
        if body is not None:
            check_manifest(body)
        status, data = self.api.request(method, path, **kwargs)
        if method == "GET" and status == 200:
            try:
                value = json.loads(data)
                if isinstance(value, dict) and isinstance(value.get("items"), list):
                    value["items"] = [obj for obj in value["items"] if not protected_object(obj)]
                    data = json.dumps(value).encode()
            except (ValueError, UnicodeDecodeError):
                pass
        return status, data

    def bounded_request(self, method, path, *, max_response_bytes, **kwargs):
        if isinstance(self.api, KubeClient):
            kwargs["max_response_bytes"] = max_response_bytes
        status, data = self.request(method, path, **kwargs)
        if len(data) > max_response_bytes:
            raise KubernetesAPIError("Response exceeded the configured size limit")
        return status, data

    def stream(self, path, params=None, **kwargs):
        check_path(path)
        return self.api.stream(path, params, **kwargs)

    def close(self):
        self.api.close()
