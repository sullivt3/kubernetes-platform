"""Node management through the Kubernetes API: cordon, labels, taints and drain.

These work on every connected cluster (imported ones too). Cordon, labels and taints
are single audited API calls; drain is an operation (``cluster.node.drain``) because
evictions can take minutes and may wait on PodDisruptionBudgets.

Drain follows ``kubectl drain`` semantics: DaemonSet pods and static (mirror) pods are
left alone; pods without a controller and pods with emptyDir data are only removed
when explicitly allowed; every other pod is *evicted* (never deleted), so
PodDisruptionBudgets are respected.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.errors import ValidationFailedError
from app.core.principal import Principal
from app.modules.auditing import service as audit
from app.modules.cluster_lifecycle.models import Cluster
from app.modules.kubernetes import client as kclient
from app.modules.kubernetes.client import ClusterAPI, KubernetesAPIError, NotFoundInClusterError

NODE_NAME = re.compile(r"^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$")
LABEL_KEY = re.compile(r"^([a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?/)?[A-Za-z0-9]([-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?$")
LABEL_VALUE = re.compile(r"^([A-Za-z0-9]([-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?)?$")
# Labels set by kubelet and relied on by schedulers/controllers: editing them breaks things.
PROTECTED_LABELS = re.compile(
    r"^(kubernetes\.io/(hostname|os|arch)|beta\.kubernetes\.io/.*|node\.kubernetes\.io/(instance-type|windows-build)"
    r"|topology\.kubernetes\.io/.*|failure-domain\.beta\.kubernetes\.io/.*)$"
)
# Taints managed by Kubernetes itself (node lifecycle controller).
SYSTEM_TAINTS = re.compile(r"^node\.(kubernetes|cloudprovider\.kubernetes)\.io/")
EFFECTS = {"NoSchedule", "PreferNoSchedule", "NoExecute"}


class TaintIn(BaseModel):
    key: str = Field(max_length=316)
    value: str | None = Field(default=None, max_length=63)
    effect: str


def check_node_name(name: str) -> None:
    if not NODE_NAME.match(name):
        raise ValidationFailedError("Invalid node name")


def node_path(name: str) -> str:
    check_node_name(name)
    return f"/api/v1/nodes/{name}"


def set_unschedulable(api: ClusterAPI, name: str, unschedulable: bool) -> None:
    kclient.send_json(
        api,
        "PATCH",
        node_path(name),
        {"spec": {"unschedulable": unschedulable or None}},
        content_type=kclient.MERGE_PATCH,
    )


def update_labels(api: ClusterAPI, name: str, *, set_labels: dict[str, str], remove: list[str]) -> dict[str, str]:
    for key in [*set_labels, *remove]:
        if not LABEL_KEY.match(key):
            raise ValidationFailedError(f"Invalid label key '{key}'")
        if PROTECTED_LABELS.match(key):
            raise ValidationFailedError(f"'{key}' is maintained by Kubernetes and cannot be changed")
    for key, value in set_labels.items():
        if not LABEL_VALUE.match(value):
            raise ValidationFailedError(f"Invalid value for label '{key}'")
    patch: dict[str, Any] = {**set_labels, **dict.fromkeys(remove)}
    node = kclient.send_json(
        api, "PATCH", node_path(name), {"metadata": {"labels": patch}}, content_type=kclient.MERGE_PATCH
    )
    return dict((node.get("metadata") or {}).get("labels") or {})


def replace_taints(api: ClusterAPI, name: str, taints: list[TaintIn]) -> list[dict[str, Any]]:
    """Replace the user-managed taints; taints maintained by Kubernetes are kept as they are."""
    seen: set[tuple[str, str]] = set()
    for taint in taints:
        if not LABEL_KEY.match(taint.key):
            raise ValidationFailedError(f"Invalid taint key '{taint.key}'")
        if SYSTEM_TAINTS.match(taint.key):
            raise ValidationFailedError(f"'{taint.key}' taints are managed by Kubernetes")
        if taint.effect not in EFFECTS:
            raise ValidationFailedError(f"Taint effect must be one of {', '.join(sorted(EFFECTS))}")
        if taint.value and not LABEL_VALUE.match(taint.value):
            raise ValidationFailedError(f"Invalid value for taint '{taint.key}'")
        if (taint.key, taint.effect) in seen:
            raise ValidationFailedError(f"Taint {taint.key}:{taint.effect} is listed twice")
        seen.add((taint.key, taint.effect))
    current = kclient.get_json(api, node_path(name))
    system = [t for t in (current.get("spec") or {}).get("taints") or [] if SYSTEM_TAINTS.match(t.get("key", ""))]
    desired = system + [{"key": t.key, "effect": t.effect, **({"value": t.value} if t.value else {})} for t in taints]
    # A JSON merge patch replaces lists wholesale; resourceVersion guards against lost updates.
    node = kclient.send_json(
        api,
        "PATCH",
        node_path(name),
        {
            "metadata": {"resourceVersion": (current.get("metadata") or {}).get("resourceVersion")},
            "spec": {"taints": desired or None},
        },
        content_type=kclient.MERGE_PATCH,
    )
    return list((node.get("spec") or {}).get("taints") or [])


# ---- drain -------------------------------------------------------------------------------


class DrainOptions(BaseModel):
    force: bool = Field(default=False, description="Also delete pods that no controller will recreate")
    delete_emptydir_data: bool = Field(default=False, description="Allow evicting pods with emptyDir volumes")
    grace_period_seconds: int | None = Field(default=None, ge=0, le=3600)
    timeout_seconds: int = Field(default=600, ge=30, le=7200)


def _controller(pod: dict[str, Any]) -> dict[str, Any] | None:
    for ref in (pod.get("metadata") or {}).get("ownerReferences") or []:
        if ref.get("controller"):
            return ref
    return None


def classify(pods: list[dict[str, Any]], options: DrainOptions) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """(pods to evict, skipped descriptions, blocking problems)."""
    evict, skipped, problems = [], [], []
    for pod in pods:
        meta = pod.get("metadata") or {}
        ref = f"{meta.get('namespace')}/{meta.get('name')}"
        if "kubernetes.io/config.mirror" in (meta.get("annotations") or {}):
            skipped.append(f"{ref} (static pod)")
            continue
        controller = _controller(pod)
        if controller and controller.get("kind") == "DaemonSet":
            skipped.append(f"{ref} (DaemonSet)")
            continue
        if (pod.get("status") or {}).get("phase") in ("Succeeded", "Failed"):
            evict.append(pod)  # finished pods are removed without disruption
            continue
        if controller is None and not options.force:
            problems.append(f"{ref} is not managed by a controller and would be lost (allow with force)")
            continue
        if any("emptyDir" in v for v in (pod.get("spec") or {}).get("volumes") or []) and not (
            options.delete_emptydir_data
        ):
            problems.append(f"{ref} has emptyDir data that would be deleted (allow with delete_emptydir_data)")
            continue
        evict.append(pod)
    return evict, skipped, problems


def evict(api: ClusterAPI, pod: dict[str, Any], grace: int | None) -> str:
    """Request an eviction. Returns 'evicted', 'gone' or 'blocked' (PodDisruptionBudget)."""
    meta = pod["metadata"]
    body: dict[str, Any] = {
        "apiVersion": "policy/v1",
        "kind": "Eviction",
        "metadata": {"name": meta["name"], "namespace": meta["namespace"]},
    }
    if grace is not None:
        body["deleteOptions"] = {"gracePeriodSeconds": grace}
    try:
        kclient.send_json(api, "POST", f"/api/v1/namespaces/{meta['namespace']}/pods/{meta['name']}/eviction", body)
    except NotFoundInClusterError:
        return "gone"
    except KubernetesAPIError as exc:
        if exc.http_status == 429:
            return "blocked"
        raise
    return "evicted"


def drain(
    api: ClusterAPI,
    name: str,
    options: DrainOptions,
    *,
    log: Callable[[str], None],
    should_stop: Callable[[], None] = lambda: None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    pods, _ = kclient.list_items(api, "/api/v1/pods", {"fieldSelector": f"spec.nodeName={name}"})
    to_evict, skipped, problems = classify(pods, options)
    if problems:
        raise ValidationFailedError(f"Cannot drain {name}:\n- " + "\n- ".join(problems), details={"problems": problems})
    set_unschedulable(api, name, True)
    log(f"{name} cordoned; evicting {len(to_evict)} pod(s), leaving {len(skipped)}")
    for item in skipped:
        log(f"  kept {item}")

    deadline = clock() + options.timeout_seconds
    remaining = {(p["metadata"]["namespace"], p["metadata"]["name"]): p for p in to_evict}
    requested: set[tuple[str, str]] = set()
    while remaining:
        should_stop()
        for key, pod in list(remaining.items()):
            if key in requested:
                continue
            outcome = evict(api, pod, options.grace_period_seconds)
            if outcome == "blocked":
                continue  # a PodDisruptionBudget allows no disruption right now; try again
            requested.add(key)
            log(f"  {'evicted' if outcome == 'evicted' else 'already gone'}: {key[0]}/{key[1]}")
        # Wait for evicted pods to actually terminate (same UID gone).
        for key in list(remaining):
            if key not in requested:
                continue
            uid = remaining[key]["metadata"].get("uid")
            try:
                current = kclient.get_json(api, f"/api/v1/namespaces/{key[0]}/pods/{key[1]}")
                if (current.get("metadata") or {}).get("uid") != uid:
                    del remaining[key]
            except NotFoundInClusterError:
                del remaining[key]
        if not remaining:
            break
        if clock() > deadline:
            blocked = [f"{ns}/{n}" for ns, n in remaining if (ns, n) not in requested]
            waiting = [f"{ns}/{n}" for ns, n in remaining if (ns, n) in requested]
            raise ValidationFailedError(
                f"Drain of {name} timed out after {options.timeout_seconds}s"
                + (f"; blocked by PodDisruptionBudgets: {', '.join(blocked)}" if blocked else "")
                + (f"; still terminating: {', '.join(waiting)}" if waiting else "")
                + ". The node stays cordoned."
            )
        sleep(5)
    log(f"{name} drained")
    return {"evicted": len(to_evict), "skipped": skipped}


def record(
    session: Session,
    principal: Principal,
    cluster: Cluster,
    action: str,
    node: str,
    details: dict[str, Any] | None = None,
) -> None:
    audit.record(
        session,
        principal=principal,
        action=f"node.{action}",
        resource_type="node",
        resource_id=f"{cluster.id}/{node}",
        resource_name=node,
        details={"cluster": cluster.name, **(details or {})},
    )
