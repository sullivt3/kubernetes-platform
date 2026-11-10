"""Workload management against a cluster's Kubernetes API.

All mutations are synchronous API calls (they complete in milliseconds; Kubernetes
controllers then reconcile asynchronously and the UI shows rollout progress) and
every one of them is recorded in the audit log with the cluster and object identity.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import re
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import yaml
from sqlalchemy.orm import Session

from app.core.errors import ConfirmationRequiredError, InvalidStateError, ValidationFailedError
from app.core.principal import Principal
from app.modules.auditing import service as audit
from app.modules.cluster_lifecycle.models import Cluster
from app.modules.kubernetes import apply as kapply
from app.modules.kubernetes import client as kclient
from app.modules.kubernetes import reads
from app.modules.kubernetes.service import KubernetesService
from app.modules.workloads import summaries
from app.modules.workloads.kinds import KINDS, get_kind

Obj = dict[str, Any]
NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$")
MAX_LOG_BYTES = 2 * 1024 * 1024


def _check_name(value: str | None, what: str) -> None:
    if value is not None and not NAME_RE.match(value):
        raise ValidationFailedError(f"Invalid {what} '{value}'")


def _contains_redacted(obj: Obj) -> bool:
    if obj.get("kind") != "Secret":
        return False
    return any(v == summaries.REDACTED for field in ("data", "stringData") for v in (obj.get(field) or {}).values())


class WorkloadService:
    def __init__(self, kubernetes: KubernetesService) -> None:
        self.kubernetes = kubernetes
        self.session: Session = kubernetes.session

    def _api(self, principal: Principal, cluster_id: uuid.UUID) -> tuple[Cluster, kclient.ClusterAPI]:
        return self.kubernetes.api(principal, cluster_id)

    def _audit(
        self,
        principal: Principal,
        cluster: Cluster,
        action: str,
        kind: str,
        namespace: str | None,
        name: str,
        **details: Any,
    ) -> None:
        audit.record(
            self.session,
            principal=principal,
            action=action,
            resource_type=f"kubernetes.{kind.lower()}",
            resource_id=f"{cluster.id}/{namespace or '-'}/{name}",
            resource_name=f"{cluster.name}:{(namespace + '/') if namespace else ''}{name}",
            details={"cluster": cluster.name, "namespace": namespace, **details},
        )
        self.session.commit()

    # ------------------------------------------------------------------ reads

    def list_resources(self, principal: Principal, cluster_id: uuid.UUID, key: str, namespace: str | None) -> Obj:
        kind = get_kind(key)
        _check_name(namespace, "namespace")
        _, api = self._api(principal, cluster_id)
        items, truncated = kclient.list_items(api, kind.collection(namespace))
        rows = [summaries.summarize(key, item) for item in items]
        rows.sort(key=lambda r: (r.get("namespace") or "", r.get("name") or ""))
        return {"items": rows, "truncated": truncated}

    def get(self, principal: Principal, cluster_id: uuid.UUID, key: str, namespace: str | None, name: str) -> Obj:
        kind = get_kind(key)
        _check_name(namespace, "namespace")
        _check_name(name, "name")
        _, api = self._api(principal, cluster_id)
        obj = kclient.get_json(api, kind.item(namespace, name))
        safe = summaries.redact_secret(obj)
        result: Obj = {
            "kind": kind.kind,
            "summary": summaries.summarize(key, obj),
            "object": summaries.clean_for_view(safe),
            "yaml": yaml.safe_dump(summaries.clean_for_edit(safe), sort_keys=False),
            "events": reads.events(
                api,
                namespace if kind.namespaced else None,
                f"involvedObject.kind={kind.kind},involvedObject.name={name}",
                limit=50,
            ),
            "capabilities": {
                "scale": kind.scalable,
                "restart": kind.restartable,
                "delete": kind.deletable,
                "namespaced": kind.namespaced,
            },
        }
        if key == "deployments":
            hpas, _ = kclient.list_items(api, f"/apis/autoscaling/v2/namespaces/{namespace}/horizontalpodautoscalers")
            policy = next(
                (
                    hpa
                    for hpa in hpas
                    if ((hpa.get("spec") or {}).get("scaleTargetRef") or {}).get("kind") == "Deployment"
                    and ((hpa.get("spec") or {}).get("scaleTargetRef") or {}).get("name") == name
                ),
                None,
            )
            result["scaling_policy"] = summaries.clean_for_view(policy) if policy else None
            result["rollout"] = summaries.deployment_rollout(obj)
            result["history"] = self._deployment_history(api, obj)
        if key in ("deployments", "statefulsets", "daemonsets", "replicasets", "jobs"):
            result["pods"] = self._owned_pods(api, obj)
        if key == "pods":
            spec = obj.get("spec") or {}
            result["containers"] = [c.get("name") for c in spec.get("containers") or []]
            result["init_containers"] = [c.get("name") for c in spec.get("initContainers") or []]
        if key == "cronjobs":
            jobs, _ = kclient.list_items(api, KINDS["jobs"].collection(namespace))
            uid = (obj.get("metadata") or {}).get("uid")
            result["jobs"] = [
                summaries.summarize("jobs", j)
                for j in jobs
                if any(o.get("uid") == uid for o in (j.get("metadata") or {}).get("ownerReferences") or [])
            ]
        return result

    def _owned_pods(self, api: kclient.ClusterAPI, obj: Obj) -> list[Obj]:
        selector = ((obj.get("spec") or {}).get("selector") or {}).get("matchLabels") or {}
        if not selector:
            return []
        label_selector = ",".join(f"{k}={v}" for k, v in selector.items())
        namespace = (obj.get("metadata") or {}).get("namespace")
        pods, _ = kclient.list_items(api, f"/api/v1/namespaces/{namespace}/pods", {"labelSelector": label_selector})
        return [summaries.summarize("pods", p) for p in pods]

    def _deployment_history(self, api: kclient.ClusterAPI, deployment: Obj) -> list[Obj]:
        meta = deployment.get("metadata") or {}
        sets, _ = kclient.list_items(api, KINDS["replicasets"].collection(meta.get("namespace")))
        current = (meta.get("annotations") or {}).get("deployment.kubernetes.io/revision")
        history = []
        for rs in sets:
            rs_meta = rs.get("metadata") or {}
            if not any(o.get("uid") == meta.get("uid") for o in rs_meta.get("ownerReferences") or []):
                continue
            annotations = rs_meta.get("annotations") or {}
            revision = annotations.get("deployment.kubernetes.io/revision")
            history.append(
                {
                    "revision": int(revision) if revision and revision.isdigit() else None,
                    "replicaset": rs_meta.get("name"),
                    "created": rs_meta.get("creationTimestamp"),
                    "images": summaries._images((rs.get("spec") or {}).get("template")),
                    "change_cause": annotations.get("kubernetes.io/change-cause"),
                    "replicas": (rs.get("status") or {}).get("replicas", 0),
                    "current": revision == current,
                }
            )
        history.sort(key=lambda h: h["revision"] or 0, reverse=True)
        return history

    def yaml_of(self, principal: Principal, cluster_id: uuid.UUID, key: str, namespace: str | None, name: str) -> str:
        return self.get(principal, cluster_id, key, namespace, name)["yaml"]

    # ------------------------------------------------------------------ mutations

    def delete(
        self,
        principal: Principal,
        cluster_id: uuid.UUID,
        key: str,
        namespace: str | None,
        name: str,
        *,
        confirm: str | None,
    ) -> None:
        kind = get_kind(key)
        if not kind.deletable:
            raise ValidationFailedError(f"Deleting {kind.kind} objects through the platform is disabled")
        _check_name(namespace, "namespace")
        _check_name(name, "name")
        if confirm != name:
            raise ConfirmationRequiredError(
                f"Deleting {kind.kind} '{name}' cannot be undone. Repeat its name to confirm.",
                details={"expected_confirmation": name},
            )
        cluster, api = self._api(principal, cluster_id)
        kclient.send_json(api, "DELETE", kind.item(namespace, name), {"propagationPolicy": "Background"})
        self._audit(
            principal,
            cluster,
            f"{kind.kind.lower()}.delete",
            kind.kind,
            namespace if kind.namespaced else None,
            name,
        )

    def scale(
        self, principal: Principal, cluster_id: uuid.UUID, key: str, namespace: str, name: str, replicas: int
    ) -> Obj:
        kind = get_kind(key)
        if not kind.scalable:
            raise ValidationFailedError(f"{kind.kind} cannot be scaled")
        if not 0 <= replicas <= 1000:
            raise ValidationFailedError("Replicas must be between 0 and 1000")
        _check_name(namespace, "namespace")
        _check_name(name, "name")
        cluster, api = self._api(principal, cluster_id)
        current = kclient.get_json(api, kind.item(namespace, name) + "/scale")
        before = (current.get("spec") or {}).get("replicas")
        kclient.send_json(
            api,
            "PATCH",
            kind.item(namespace, name) + "/scale",
            {"spec": {"replicas": replicas}},
            content_type=kclient.MERGE_PATCH,
        )
        self._audit(
            principal,
            cluster,
            f"{key[:-1]}.scale",
            kind.kind,
            namespace,
            name,
            replicas={"from": before, "to": replicas},
        )
        return {"replicas": replicas, "previous": before}

    def set_scaling_policy(
        self,
        principal: Principal,
        cluster_id: uuid.UUID,
        namespace: str,
        name: str,
        *,
        minimum: int,
        maximum: int,
        metric: str,
        target: int,
        scale_down_stabilization: int,
    ) -> Obj:
        """Create/update a native autoscaling/v2 policy for a Deployment."""
        _check_name(namespace, "namespace")
        _check_name(name, "name")
        if not (1 <= minimum <= maximum <= 1000):
            raise ValidationFailedError("Scaling bounds must satisfy 1 <= minimum <= maximum <= 1000")
        if metric not in {"cpu", "memory"} or not 1 <= target <= 100:
            raise ValidationFailedError("Choose CPU or memory utilization with a target from 1% to 100%")
        if not 0 <= scale_down_stabilization <= 3600:
            raise ValidationFailedError("Scale-down stabilization must be from 0 to 3600 seconds")
        cluster, api = self._api(principal, cluster_id)
        # Ensure the target exists and reject policies that would silently point at a typo.
        kclient.get_json(api, KINDS["deployments"].item(namespace, name))
        suffix = hashlib.sha256(f"{namespace}/{name}".encode()).hexdigest()[:8]
        hpa_name = f"kplatform-{name[:50].rstrip('-')}-{suffix}"
        resource = "cpu" if metric == "cpu" else "memory"
        body = {
            "apiVersion": "autoscaling/v2",
            "kind": "HorizontalPodAutoscaler",
            "metadata": {
                "name": hpa_name,
                "namespace": namespace,
                "labels": {"app.kubernetes.io/managed-by": "kubernetes-platform"},
            },
            "spec": {
                "scaleTargetRef": {"apiVersion": "apps/v1", "kind": "Deployment", "name": name},
                "minReplicas": minimum,
                "maxReplicas": maximum,
                "metrics": [
                    {
                        "type": "Resource",
                        "resource": {"name": resource, "target": {"type": "Utilization", "averageUtilization": target}},
                    }
                ],
                "behavior": {
                    "scaleDown": {
                        "stabilizationWindowSeconds": scale_down_stabilization,
                        "selectPolicy": "Max",
                        "policies": [{"type": "Pods", "value": max(1, maximum - minimum), "periodSeconds": 60}],
                    },
                    "scaleUp": {
                        "stabilizationWindowSeconds": 0,
                        "selectPolicy": "Max",
                        "policies": [{"type": "Pods", "value": max(1, maximum - minimum), "periodSeconds": 60}],
                    },
                },
            },
        }
        path = f"/apis/autoscaling/v2/namespaces/{namespace}/horizontalpodautoscalers/{hpa_name}"
        try:
            kclient.get_json(api, path)
            result = kclient.send_json(api, "PUT", path, body)
            action = "deployment.scaling-policy.update"
        except kclient.NotFoundInClusterError:
            result = kclient.send_json(
                api, "POST", f"/apis/autoscaling/v2/namespaces/{namespace}/horizontalpodautoscalers", body
            )
            action = "deployment.scaling-policy.create"
        self._audit(
            principal,
            cluster,
            action,
            "HorizontalPodAutoscaler",
            namespace,
            hpa_name,
            target=name,
            metric=metric,
            target_utilization=target,
            minimum=minimum,
            maximum=maximum,
        )
        return summaries.clean_for_view(result)

    def delete_scaling_policy(self, principal: Principal, cluster_id: uuid.UUID, namespace: str, name: str) -> Obj:
        _check_name(namespace, "namespace")
        _check_name(name, "name")
        cluster, api = self._api(principal, cluster_id)
        suffix = hashlib.sha256(f"{namespace}/{name}".encode()).hexdigest()[:8]
        hpa_name = f"kplatform-{name[:50].rstrip('-')}-{suffix}"
        kclient.send_json(
            api,
            "DELETE",
            f"/apis/autoscaling/v2/namespaces/{namespace}/horizontalpodautoscalers/{hpa_name}",
            {"propagationPolicy": "Background"},
        )
        self._audit(
            principal,
            cluster,
            "deployment.scaling-policy.delete",
            "HorizontalPodAutoscaler",
            namespace,
            hpa_name,
            target=name,
        )
        return {"deleted": True}

    def restart(self, principal: Principal, cluster_id: uuid.UUID, key: str, namespace: str, name: str) -> Obj:
        kind = get_kind(key)
        if not kind.restartable:
            raise ValidationFailedError(f"{kind.kind} cannot be restarted")
        _check_name(namespace, "namespace")
        _check_name(name, "name")
        cluster, api = self._api(principal, cluster_id)
        stamp = datetime.now(UTC).isoformat()
        body = {"spec": {"template": {"metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": stamp}}}}}
        kclient.send_json(api, "PATCH", kind.item(namespace, name), body, content_type=kclient.STRATEGIC_PATCH)
        self._audit(principal, cluster, f"{key[:-1]}.restart", kind.kind, namespace, name)
        return {"restarted_at": stamp}

    def rollback(self, principal: Principal, cluster_id: uuid.UUID, namespace: str, name: str, revision: int) -> Obj:
        """Roll a Deployment back to a previous revision (what `kubectl rollout undo` does)."""
        _check_name(namespace, "namespace")
        _check_name(name, "name")
        cluster, api = self._api(principal, cluster_id)
        kind = KINDS["deployments"]
        deployment = kclient.get_json(api, kind.item(namespace, name))
        history = self._deployment_history(api, deployment)
        target = next((h for h in history if h["revision"] == revision), None)
        if target is None:
            raise ValidationFailedError(f"Revision {revision} not found for deployment {name}")
        if target["current"]:
            raise InvalidStateError(f"Revision {revision} is already the current revision")
        rs = kclient.get_json(api, KINDS["replicasets"].item(namespace, target["replicaset"]))
        template = copy.deepcopy((rs.get("spec") or {}).get("template") or {})
        (template.get("metadata") or {}).get("labels", {}).pop("pod-template-hash", None)
        kclient.send_json(
            api,
            "PATCH",
            kind.item(namespace, name),
            [{"op": "replace", "path": "/spec/template", "value": template}],
            content_type=kclient.JSON_PATCH,
        )
        self._audit(principal, cluster, "deployment.rollback", "Deployment", namespace, name, revision=revision)
        return {"rolled_back_to": revision, "images": target["images"]}

    def cronjob_trigger(self, principal: Principal, cluster_id: uuid.UUID, namespace: str, name: str) -> Obj:
        _check_name(namespace, "namespace")
        _check_name(name, "name")
        cluster, api = self._api(principal, cluster_id)
        cronjob = kclient.get_json(api, KINDS["cronjobs"].item(namespace, name))
        meta = cronjob.get("metadata") or {}
        job_template = (cronjob.get("spec") or {}).get("jobTemplate") or {}
        job_name = f"{name}-manual-{datetime.now(UTC):%Y%m%d%H%M%S}"[:63].rstrip("-")
        job = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {
                "name": job_name,
                "namespace": namespace,
                "labels": (job_template.get("metadata") or {}).get("labels") or {},
                "annotations": {"cronjob.kubernetes.io/instantiate": "manual"},
                "ownerReferences": [
                    {
                        "apiVersion": "batch/v1",
                        "kind": "CronJob",
                        "name": name,
                        "uid": meta.get("uid"),
                        "controller": True,
                    }
                ],
            },
            "spec": job_template.get("spec") or {},
        }
        kclient.send_json(api, "POST", KINDS["jobs"].collection(namespace), job)
        self._audit(principal, cluster, "cronjob.trigger", "CronJob", namespace, name, job=job_name)
        return {"job": job_name}

    def cronjob_suspend(
        self, principal: Principal, cluster_id: uuid.UUID, namespace: str, name: str, suspend: bool
    ) -> Obj:
        _check_name(namespace, "namespace")
        _check_name(name, "name")
        cluster, api = self._api(principal, cluster_id)
        kclient.send_json(
            api,
            "PATCH",
            KINDS["cronjobs"].item(namespace, name),
            {"spec": {"suspend": suspend}},
            content_type=kclient.MERGE_PATCH,
        )
        self._audit(principal, cluster, "cronjob.suspend" if suspend else "cronjob.resume", "CronJob", namespace, name)
        return {"suspend": suspend}

    # ------------------------------------------------------------------ YAML plan / apply

    def apply(
        self, principal: Principal, cluster_id: uuid.UUID, manifest: str, *, namespace: str, dry_run: bool
    ) -> Obj:
        _check_name(namespace, "namespace")
        objects = kapply.parse_manifest(manifest)
        if len(objects) > 200:
            raise ValidationFailedError("At most 200 objects can be applied at once")
        for obj in objects:
            if _contains_redacted(obj):
                raise ValidationFailedError(
                    f"Secret '{(obj.get('metadata') or {}).get('name')}' still contains redacted values. "
                    "Replace each <redacted> value (use stringData for plain text) before applying."
                )
        cluster, api = self._api(principal, cluster_id)
        results = kapply.plan_objects(api, objects, default_namespace=namespace, apply=not dry_run)
        if not dry_run:
            audit.record(
                self.session,
                principal=principal,
                action="kubernetes.apply",
                resource_type="cluster",
                resource_id=cluster.id,
                resource_name=cluster.name,
                details={
                    "objects": [
                        {
                            "kind": r["kind"],
                            "namespace": r["namespace"],
                            "name": r["name"],
                            "action": r["action"],
                            "applied": r["applied"],
                        }
                        for r in results
                    ]
                },
            )
            self.session.commit()
        return {
            "dry_run": dry_run,
            "results": results,
            "ok": all(r["action"] != "error" for r in results),
        }

    # ------------------------------------------------------------------ secrets

    def reveal_secret(self, principal: Principal, cluster_id: uuid.UUID, namespace: str, name: str) -> Obj:
        _check_name(namespace, "namespace")
        _check_name(name, "name")
        cluster, api = self._api(principal, cluster_id)
        secret = kclient.get_json(api, KINDS["secrets"].item(namespace, name))
        values: Obj = {}
        for key, encoded in (secret.get("data") or {}).items():
            raw = base64.b64decode(encoded or "")
            try:
                values[key] = {"text": raw.decode("utf-8"), "binary": False}
            except UnicodeDecodeError:
                values[key] = {"text": encoded, "binary": True}
        self._audit(principal, cluster, "secret.reveal", "Secret", namespace, name, keys=sorted(values))
        return {"values": values}

    # ------------------------------------------------------------------ logs

    def logs(
        self,
        principal: Principal,
        cluster_id: uuid.UUID,
        namespace: str,
        pod: str,
        *,
        container: str | None,
        tail: int,
        previous: bool,
        timestamps: bool,
    ) -> str:
        for value, what in ((namespace, "namespace"), (pod, "pod"), (container, "container")):
            _check_name(value, what)
        _, api = self._api(principal, cluster_id)
        return kclient.get_text(
            api,
            f"/api/v1/namespaces/{namespace}/pods/{pod}/log",
            {
                "container": container,
                "tailLines": tail,
                "previous": "true" if previous else None,
                "timestamps": "true" if timestamps else None,
                "limitBytes": MAX_LOG_BYTES,
            },
            timeout=30,
        )

    def follow_logs(
        self, principal: Principal, cluster_id: uuid.UUID, namespace: str, pod: str, *, container: str | None, tail: int
    ) -> Iterator[str]:
        for value, what in ((namespace, "namespace"), (pod, "pod"), (container, "container")):
            _check_name(value, what)
        _, api = self._api(principal, cluster_id)
        return kclient.stream_lines(
            api,
            f"/api/v1/namespaces/{namespace}/pods/{pod}/log",
            {"container": container, "tailLines": tail, "follow": "true", "timestamps": "true"},
        )
