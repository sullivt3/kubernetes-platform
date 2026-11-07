"""Kubeconfig parsing, validation and minification.

Only self-contained credentials are accepted: embedded CA data plus a bearer token
or an embedded client certificate. Rejected, each with an actionable message:

* ``exec`` / ``auth-provider`` plugins (EKS, GKE, AKS, OIDC helpers) — they would
  run arbitrary programs on the platform host. Use a service-account token.
* File references (``certificate-authority``, ``client-certificate``, ``token-file``)
  — the paths belong to the machine the kubeconfig came from.
* ``insecure-skip-tls-verify`` — the platform always verifies the API server.

Stored kubeconfigs are minified to the single selected context (least privilege).
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from typing import Any

import yaml

from app.core.errors import ValidationFailedError

MAX_KUBECONFIG_BYTES = 512 * 1024


class KubeconfigError(ValidationFailedError):
    code = "invalid_kubeconfig"


@dataclass(frozen=True)
class ContextInfo:
    name: str
    cluster: str
    user: str
    server: str | None
    namespace: str | None
    auth_type: str
    supported: bool
    problem: str | None


@dataclass(frozen=True)
class ResolvedKubeconfig:
    context: str
    server: str
    ca_pem: str
    tls_server_name: str | None
    token: str | None
    client_cert_pem: str | None
    client_key_pem: str | None


def parse(text: str) -> dict[str, Any]:
    if len(text.encode()) > MAX_KUBECONFIG_BYTES:
        raise KubeconfigError("Kubeconfig is too large (max 512 KiB)")
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError:
        raise KubeconfigError("Kubeconfig is not valid YAML") from None
    if not isinstance(doc, dict) or not isinstance(doc.get("contexts"), list) or not doc["contexts"]:
        raise KubeconfigError("Kubeconfig has no contexts")
    if not isinstance(doc.get("clusters"), list) or not isinstance(doc.get("users", []), list):
        raise KubeconfigError("Kubeconfig is missing its clusters or users list")
    return doc


def _named(items: list[Any], name: str, kind: str) -> dict[str, Any]:
    for item in items or []:
        if isinstance(item, dict) and item.get("name") == name:
            return item.get(kind) or {}
    raise KubeconfigError(f"Kubeconfig refers to unknown {kind} '{name}'")


def auth_type(user: dict[str, Any]) -> str:
    if "exec" in user:
        return "exec"
    if "auth-provider" in user:
        return "auth-provider"
    if user.get("token") or user.get("tokenFile"):
        return "token"
    if user.get("client-certificate-data") or user.get("client-certificate"):
        return "client-certificate"
    if user.get("username"):
        return "basic"
    return "none"


def _problem(cluster: dict[str, Any], user: dict[str, Any]) -> str | None:
    kind = auth_type(user)
    if kind in ("exec", "auth-provider"):
        return (
            "Uses an external authentication plugin, which the platform will not execute. "
            "Create a service-account token kubeconfig for the platform instead."
        )
    if kind == "basic":
        return "Basic authentication is not supported by Kubernetes any more; use a token or client certificate."
    if kind == "none":
        return "No credentials for this context."
    if user.get("tokenFile") or user.get("client-certificate") or user.get("client-key"):
        return "References files on another machine; export with: kubectl config view --raw --minify --flatten"
    if kind == "client-certificate" and not user.get("client-key-data"):
        return "Client certificate without client-key-data."
    if cluster.get("certificate-authority"):
        return "References a CA file; export with: kubectl config view --raw --minify --flatten"
    if cluster.get("insecure-skip-tls-verify"):
        return "Disables TLS verification, which the platform does not allow. Include certificate-authority-data."
    if not cluster.get("certificate-authority-data"):
        return "Missing certificate-authority-data (the platform must be able to verify the API server)."
    if not str(cluster.get("server", "")).startswith("https://"):
        return "The API server must be an https:// URL."
    if cluster.get("proxy-url"):
        return "Proxy URLs are not supported yet."
    return None


def list_contexts(doc: dict[str, Any]) -> list[ContextInfo]:
    result = []
    for entry in doc["contexts"]:
        if not isinstance(entry, dict) or not entry.get("name"):
            continue
        ctx = entry.get("context") or {}
        try:
            cluster = _named(doc.get("clusters", []), ctx.get("cluster", ""), "cluster")
            user = _named(doc.get("users", []), ctx.get("user", ""), "user")
            problem = _problem(cluster, user)
            kind = auth_type(user)
        except KubeconfigError as exc:
            cluster, problem, kind = {}, exc.message, "unknown"
        result.append(
            ContextInfo(
                name=entry["name"],
                cluster=ctx.get("cluster", ""),
                user=ctx.get("user", ""),
                server=cluster.get("server"),
                namespace=ctx.get("namespace"),
                auth_type=kind,
                supported=problem is None,
                problem=problem,
            )
        )
    return result


def current_context(doc: dict[str, Any]) -> str | None:
    value = doc.get("current-context")
    return value if isinstance(value, str) and value else None


def minify(doc: dict[str, Any], context: str | None) -> tuple[dict[str, Any], str]:
    """Return a kubeconfig containing only ``context`` (default: current context)."""
    name = context or current_context(doc) or doc["contexts"][0].get("name")
    entry = next((c for c in doc["contexts"] if isinstance(c, dict) and c.get("name") == name), None)
    if entry is None:
        raise KubeconfigError(f"Context '{name}' not found in the kubeconfig")
    ctx = entry.get("context") or {}
    cluster = _named(doc.get("clusters", []), ctx.get("cluster", ""), "cluster")
    user = _named(doc.get("users", []), ctx.get("user", ""), "user")
    problem = _problem(cluster, user)
    if problem:
        raise KubeconfigError(f"Context '{name}' cannot be used: {problem}")
    minified = {
        "apiVersion": "v1",
        "kind": "Config",
        "current-context": name,
        "clusters": [{"name": ctx["cluster"], "cluster": cluster}],
        "users": [{"name": ctx["user"], "user": user}],
        "contexts": [
            {"name": name, "context": {k: v for k, v in ctx.items() if k in ("cluster", "user", "namespace")}}
        ],
    }
    return minified, name


def dump(doc: dict[str, Any]) -> str:
    return yaml.safe_dump(doc, sort_keys=False)


def _decode_pem(value: str, what: str) -> str:
    try:
        text = base64.b64decode(value, validate=True).decode()
    except (binascii.Error, UnicodeDecodeError):
        raise KubeconfigError(f"{what} is not valid base64-encoded PEM") from None
    if "-----BEGIN" not in text:
        raise KubeconfigError(f"{what} does not contain a PEM block")
    return text


def resolve(text: str) -> ResolvedKubeconfig:
    """Extract connection material from a (minified, stored) kubeconfig."""
    doc = parse(text)
    minified, name = minify(doc, current_context(doc))
    cluster = minified["clusters"][0]["cluster"]
    user = minified["users"][0]["user"]
    return ResolvedKubeconfig(
        context=name,
        server=str(cluster["server"]).rstrip("/"),
        ca_pem=_decode_pem(cluster["certificate-authority-data"], "certificate-authority-data"),
        tls_server_name=cluster.get("tls-server-name"),
        token=user.get("token"),
        client_cert_pem=_decode_pem(user["client-certificate-data"], "client-certificate-data")
        if user.get("client-certificate-data")
        else None,
        client_key_pem=_decode_pem(user["client-key-data"], "client-key-data") if user.get("client-key-data") else None,
    )
