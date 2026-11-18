"""Read-only checks of the security configuration declared in Kubernetes objects."""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel

from app.modules.kubernetes import client as kclient
from app.modules.kubernetes.views import pod_security_view
from app.modules.workloads.kinds import KINDS

Source = Literal[
    "namespaces",
    "serviceaccounts",
    "roles",
    "clusterroles",
    "rolebindings",
    "clusterrolebindings",
    "validatingadmissionpolicies",
]
Severity = Literal["high", "medium", "info"]
CoverageStatus = Literal["complete", "truncated", "denied", "unavailable", "error"]
Obj = dict[str, Any]

SOURCES: tuple[Source, ...] = (
    "namespaces",
    "serviceaccounts",
    "roles",
    "clusterroles",
    "rolebindings",
    "clusterrolebindings",
    "validatingadmissionpolicies",
)
MAX_ITEMS = 1000
SEVERITY_ORDER = {"high": 0, "medium": 1, "info": 2}


class Coverage(BaseModel):
    source: Source
    status: CoverageStatus
    scanned: int


class Finding(BaseModel):
    check: str
    severity: Severity
    source: Source
    name: str
    namespace: str | None
    title: str
    evidence: str
    guidance: str


class SecurityReport(BaseModel):
    cluster_id: uuid.UUID
    cluster_name: str
    observed_at: datetime
    coverage: list[Coverage]
    findings: list[Finding]


def _read_source(api: kclient.ClusterAPI, source: Source) -> tuple[Coverage, list[Obj]]:
    path = "/api/v1/namespaces" if source == "namespaces" else KINDS[source].collection(None)
    try:
        items, truncated = kclient.list_items(api, path, max_items=MAX_ITEMS)
    except kclient.ClusterAuthError:
        return Coverage(source=source, status="denied", scanned=0), []
    except kclient.NotFoundInClusterError:
        return Coverage(source=source, status="unavailable", scanned=0), []
    except kclient.KubernetesAPIError as exc:
        # Some transports return the HTTP status instead of raising ClusterAuthError.
        status: CoverageStatus = "denied" if exc.http_status in (401, 403) else "error"
        return Coverage(source=source, status=status, scanned=0), []
    truncated = truncated or len(items) > MAX_ITEMS
    items = items[:MAX_ITEMS]
    return Coverage(source=source, status="truncated" if truncated else "complete", scanned=len(items)), items


def _finding(
    source: Source, obj: Obj, check: str, severity: Severity, title: str, evidence: str, guidance: str
) -> Finding:
    meta = obj["metadata"]
    return Finding(
        check=check,
        severity=severity,
        source=source,
        name=meta["name"],
        namespace=meta.get("namespace"),
        title=title,
        evidence=evidence,
        guidance=guidance,
    )


def _namespace_findings(namespaces: list[Obj]) -> list[Finding]:
    findings = []
    for namespace in namespaces:
        mode = pod_security_view(namespace)["modes"]["enforce"]
        level = mode["level"]
        if level is None:
            findings.append(
                _finding(
                    "namespaces",
                    namespace,
                    "pod-security.enforce-unset",
                    "info",
                    "No explicit Pod Security enforce level",
                    "The namespace has no pod-security.kubernetes.io/enforce label.",
                    "Review control plane defaults and exemptions before choosing a namespace enforce level.",
                )
            )
        elif level == "privileged":
            findings.append(
                _finding(
                    "namespaces",
                    namespace,
                    "pod-security.enforce-privileged",
                    "medium",
                    "Pod Security enforce level is privileged",
                    "The namespace sets pod-security.kubernetes.io/enforce=privileged.",
                    "Check whether the namespace requires privileged workloads before choosing a stricter level.",
                )
            )
    return findings


def _broad_subject(subject: Obj) -> bool:
    name = subject.get("name") or ""
    if subject.get("kind") == "Group":
        return name in {"system:authenticated", "system:unauthenticated", "system:serviceaccounts"} or name.startswith(
            "system:serviceaccounts:"
        )
    return subject.get("kind") == "User" and name == "system:anonymous"


def _secret_reads(rule: Obj) -> bool:
    groups = rule.get("apiGroups") or []
    resources = rule.get("resources") or []
    verbs = rule.get("verbs") or []
    return (
        ("" in groups or "*" in groups)
        and ("secrets" in resources or "*" in resources)
        and any(verb in {"*", "get", "list", "watch"} for verb in verbs)
    )


def _binding_findings(inventory: dict[Source, list[Obj]], coverage: dict[Source, CoverageStatus]) -> list[Finding]:
    roles = {(obj["metadata"]["namespace"], obj["metadata"]["name"]): obj for obj in inventory["roles"]}
    cluster_roles = {obj["metadata"]["name"]: obj for obj in inventory["clusterroles"]}
    findings = []
    for source in ("rolebindings", "clusterrolebindings"):
        for binding in inventory[source]:
            subjects = binding.get("subjects") or []
            if not subjects:
                continue
            ref = binding["roleRef"]
            namespace = binding["metadata"].get("namespace")
            role_source: Source = "roles" if ref["kind"] == "Role" else "clusterroles"
            role = roles.get((namespace, ref["name"])) if role_source == "roles" else cluster_roles.get(ref["name"])
            reference = f"{ref['kind']} {ref['name']}"
            if role is None:
                if coverage[role_source] == "complete":
                    findings.append(
                        _finding(
                            source,
                            binding,
                            "rbac.role-reference-missing",
                            "info",
                            "Referenced role missing from inventory",
                            f"{reference} was absent when the report read roles.",
                            "Verify the role reference and review whether the binding is still needed.",
                        )
                    )
                continue

            rules = role.get("rules") or []
            wildcard = any(
                "*" in (rule.get("resources") or [])
                or (source == "clusterrolebindings" and "*" in (rule.get("nonResourceURLs") or []))
                for rule in rules
            )
            broad_groups = sorted({s["name"] for s in subjects if _broad_subject(s)})
            if broad_groups and (wildcard or any(_secret_reads(rule) for rule in rules)):
                findings.append(
                    _finding(
                        source,
                        binding,
                        "rbac.broad-sensitive-binding",
                        "high",
                        "Broad subject on a binding with sensitive rules",
                        f"{reference} declares wildcard or Secret read rules for {', '.join(broad_groups)}.",
                        "Review which identities need access and narrow the subjects and rules where possible.",
                    )
                )
            elif wildcard:
                default_admin = (
                    source == "clusterrolebindings"
                    and binding["metadata"]["name"] == "cluster-admin"
                    and ref["name"] == "cluster-admin"
                    and len(subjects) == 1
                    and subjects[0].get("kind") == "Group"
                    and subjects[0].get("name") == "system:masters"
                )
                severity: Severity = (
                    "info" if default_admin else "high" if source == "clusterrolebindings" else "medium"
                )
                findings.append(
                    _finding(
                        source,
                        binding,
                        "rbac.wildcard-binding",
                        severity,
                        "Default system:masters binding" if default_admin else "Wildcard rules on a bound role",
                        f"{reference} declares wildcard resources or endpoints. Subjects in binding: {len(subjects)}.",
                        "Review membership of system:masters."
                        if default_admin
                        else "Review the binding scope, subjects, and rules against the access they require.",
                    )
                )
    return findings


def collect(api: kclient.ClusterAPI, cluster_id: uuid.UUID, cluster_name: str) -> SecurityReport:
    with ThreadPoolExecutor(max_workers=len(SOURCES)) as pool:
        snapshots = list(pool.map(lambda source: _read_source(api, source), SOURCES))
    coverage = [result for result, _ in snapshots]
    inventory = {result.source: items for result, items in snapshots}
    findings = _namespace_findings(inventory["namespaces"])
    findings.extend(_binding_findings(inventory, {result.source: result.status for result in coverage}))
    for account in inventory["serviceaccounts"]:
        if account["metadata"]["name"] == "default" and account.get("automountServiceAccountToken") is not False:
            findings.append(
                _finding(
                    "serviceaccounts",
                    account,
                    "service-account.default-automount",
                    "info",
                    "Default service account allows automatic token mounts",
                    "The account does not set automountServiceAccountToken to false. Pod settings can override it.",
                    "Disable token automount where workloads do not need Kubernetes API credentials.",
                )
            )
    for policy in inventory["validatingadmissionpolicies"]:
        warnings = ((policy.get("status") or {}).get("typeChecking") or {}).get("expressionWarnings") or []
        if warnings:
            findings.append(
                _finding(
                    "validatingadmissionpolicies",
                    policy,
                    "admission.expression-warnings",
                    "medium",
                    "Admission policy has expression warnings",
                    f"Kubernetes reported {len(warnings)} CEL type-checking warning(s).",
                    "Inspect the warnings and verify the expressions against the resource types the policy matches.",
                )
            )
    findings.sort(key=lambda f: (SEVERITY_ORDER[f.severity], f.check, f.source, f.namespace or "", f.name))
    return SecurityReport(
        cluster_id=cluster_id,
        cluster_name=cluster_name,
        observed_at=datetime.now(UTC),
        coverage=coverage,
        findings=findings,
    )
