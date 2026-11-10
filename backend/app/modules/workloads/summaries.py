"""List-row summaries and rollout logic for workload kinds (pure functions over raw JSON)."""

from __future__ import annotations

import copy
from typing import Any

from app.modules.kubernetes.views import pod_view

Obj = dict[str, Any]
REDACTED = "<redacted>"
LAST_APPLIED = "kubectl.kubernetes.io/last-applied-configuration"


def _meta(obj: Obj) -> Obj:
    return obj.get("metadata") or {}


def _images(template: Obj | None) -> list[str]:
    spec = ((template or {}).get("spec")) or {}
    return [c.get("image") for c in spec.get("containers") or []]


def _owner(obj: Obj) -> str | None:
    owner = next(iter(_meta(obj).get("ownerReferences") or []), None)
    return f"{owner['kind']}/{owner['name']}" if owner else None


def _base(obj: Obj) -> Obj:
    meta = _meta(obj)
    return {
        "name": meta.get("name"),
        "namespace": meta.get("namespace"),
        "created": meta.get("creationTimestamp"),
        "labels": meta.get("labels") or {},
    }


# --------------------------------------------------------------------------- rollouts


def deployment_rollout(dep: Obj) -> Obj:
    """Rollout state, mirroring `kubectl rollout status`."""
    meta, spec, status = _meta(dep), dep.get("spec") or {}, dep.get("status") or {}
    desired = spec.get("replicas", 1)
    updated = status.get("updatedReplicas", 0)
    available = status.get("availableReplicas", 0)
    total = status.get("replicas", 0)
    conditions = {c.get("type"): c for c in status.get("conditions") or []}
    progressing = conditions.get("Progressing", {})
    if spec.get("paused"):
        return {"state": "paused", "message": "Rollout is paused"}
    if progressing.get("reason") == "ProgressDeadlineExceeded":
        return {"state": "failed", "message": progressing.get("message") or "Progress deadline exceeded"}
    if (status.get("observedGeneration") or 0) < (meta.get("generation") or 0):
        return {"state": "progressing", "message": "Waiting for the controller to observe the new spec"}
    if updated < desired:
        return {"state": "progressing", "message": f"{updated} of {desired} replicas updated"}
    if total > updated:
        return {"state": "progressing", "message": f"{total - updated} old replica(s) pending termination"}
    if available < updated:
        return {"state": "progressing", "message": f"{available} of {updated} updated replicas available"}
    return {"state": "complete", "message": f"{available} of {desired} replicas available"}


def statefulset_rollout(sts: Obj) -> Obj:
    spec, status = sts.get("spec") or {}, sts.get("status") or {}
    desired = spec.get("replicas", 1)
    ready = status.get("readyReplicas", 0)
    if status.get("updateRevision") and status.get("currentRevision") != status.get("updateRevision"):
        return {"state": "progressing", "message": f"{status.get('updatedReplicas', 0)} of {desired} pods updated"}
    if ready < desired:
        return {"state": "progressing", "message": f"{ready} of {desired} pods ready"}
    return {"state": "complete", "message": f"{ready} of {desired} pods ready"}


def daemonset_rollout(ds: Obj) -> Obj:
    status = ds.get("status") or {}
    desired = status.get("desiredNumberScheduled", 0)
    updated = status.get("updatedNumberScheduled", 0)
    available = status.get("numberAvailable", 0)
    if updated < desired:
        return {"state": "progressing", "message": f"{updated} of {desired} pods updated"}
    if available < desired:
        return {"state": "progressing", "message": f"{available} of {desired} pods available"}
    return {"state": "complete", "message": f"{available} of {desired} pods available"}


# --------------------------------------------------------------------------- rows


def summarize(key: str, obj: Obj) -> Obj:
    spec, status = obj.get("spec") or {}, obj.get("status") or {}
    row = _base(obj)
    if key == "pods":
        return pod_view(obj) | {"labels": row["labels"]}
    if key == "deployments":
        row.update(
            ready=f"{status.get('readyReplicas', 0)}/{spec.get('replicas', 1)}",
            replicas=spec.get("replicas", 1),
            up_to_date=status.get("updatedReplicas", 0),
            available=status.get("availableReplicas", 0),
            images=_images(spec.get("template")),
            strategy=(spec.get("strategy") or {}).get("type"),
            rollout=deployment_rollout(obj),
        )
    elif key == "replicasets":
        row.update(
            desired=spec.get("replicas", 0),
            current=status.get("replicas", 0),
            ready=status.get("readyReplicas", 0),
            owner=_owner(obj),
            revision=(_meta(obj).get("annotations") or {}).get("deployment.kubernetes.io/revision"),
            images=_images(spec.get("template")),
        )
    elif key == "statefulsets":
        row.update(
            ready=f"{status.get('readyReplicas', 0)}/{spec.get('replicas', 1)}",
            replicas=spec.get("replicas", 1),
            service=spec.get("serviceName"),
            images=_images(spec.get("template")),
            rollout=statefulset_rollout(obj),
        )
    elif key == "daemonsets":
        row.update(
            desired=status.get("desiredNumberScheduled", 0),
            current=status.get("currentNumberScheduled", 0),
            ready=status.get("numberReady", 0),
            up_to_date=status.get("updatedNumberScheduled", 0),
            available=status.get("numberAvailable", 0),
            node_selector=((spec.get("template") or {}).get("spec") or {}).get("nodeSelector") or {},
            images=_images(spec.get("template")),
            rollout=daemonset_rollout(obj),
        )
    elif key == "jobs":
        conditions = {c.get("type"): c.get("status") for c in status.get("conditions") or []}
        state = (
            "Complete"
            if conditions.get("Complete") == "True"
            else "Failed"
            if conditions.get("Failed") == "True"
            else "Suspended"
            if spec.get("suspend")
            else "Running"
        )
        row.update(
            completions=f"{status.get('succeeded', 0)}/{spec.get('completions', 1)}",
            status=state,
            active=status.get("active", 0),
            failed=status.get("failed", 0),
            started=status.get("startTime"),
            finished=status.get("completionTime"),
            owner=_owner(obj),
            images=_images(spec.get("template") or {}),
        )
    elif key == "cronjobs":
        row.update(
            schedule=spec.get("schedule"),
            timezone=spec.get("timeZone"),
            suspend=bool(spec.get("suspend")),
            active=len(status.get("active") or []),
            last_schedule=status.get("lastScheduleTime"),
            last_success=status.get("lastSuccessfulTime"),
            images=_images(((spec.get("jobTemplate") or {}).get("spec") or {}).get("template")),
        )
    elif key == "services":
        ingress = ((status.get("loadBalancer") or {}).get("ingress")) or []
        row.update(
            type=spec.get("type", "ClusterIP"),
            cluster_ip=spec.get("clusterIP"),
            external_name=spec.get("externalName"),
            external=[i.get("ip") or i.get("hostname") for i in ingress] + list(spec.get("externalIPs") or []),
            ports=[
                f"{p.get('port')}{':' + str(p['nodePort']) if p.get('nodePort') else ''}/{p.get('protocol', 'TCP')}"
                for p in spec.get("ports") or []
            ],
            selector=spec.get("selector") or {},
        )
    elif key == "ingresses":
        rules = spec.get("rules") or []
        addresses = (status.get("loadBalancer") or {}).get("ingress") or []
        row.update(
            ingress_class=spec.get("ingressClassName"),
            hosts=[rule.get("host") or "*" for rule in rules],
            paths=sum(len((rule.get("http") or {}).get("paths") or []) for rule in rules),
            addresses=[item.get("ip") or item.get("hostname") for item in addresses],
            tls_hosts=[host for tls in spec.get("tls") or [] for host in tls.get("hosts") or []],
        )
    elif key == "networkpolicies":
        policy_types = spec.get("policyTypes") or ["Ingress"]
        row.update(
            pod_selector=(spec.get("podSelector") or {}).get("matchLabels") or {},
            policy_types=policy_types,
            ingress_rules=len(spec.get("ingress") or []),
            egress_rules=len(spec.get("egress") or []),
        )
    elif key == "endpointslices":
        endpoints = obj.get("endpoints") or []
        row.update(
            service=(row["labels"] or {}).get("kubernetes.io/service-name"),
            address_type=obj.get("addressType"),
            endpoints=len(endpoints),
            ready_endpoints=sum(
                1 for endpoint in endpoints if (endpoint.get("conditions") or {}).get("ready") is not False
            ),
            addresses=[address for endpoint in endpoints for address in endpoint.get("addresses") or []],
            ready_addresses=[
                address
                for endpoint in endpoints
                if (endpoint.get("conditions") or {}).get("ready") is not False
                for address in endpoint.get("addresses") or []
            ],
            ports=[
                f"{p.get('name') + ':' if p.get('name') else ''}{p.get('port')}/{p.get('protocol', 'TCP')}"
                for p in obj.get("ports") or []
            ],
        )
    elif key == "persistentvolumes":
        claim = spec.get("claimRef") or {}
        row.update(
            phase=status.get("phase", "Available"),
            capacity=(spec.get("capacity") or {}).get("storage"),
            access_modes=spec.get("accessModes") or [],
            reclaim_policy=spec.get("persistentVolumeReclaimPolicy", "Retain"),
            storage_class=spec.get("storageClassName"),
            claim=f"{claim.get('namespace')}/{claim.get('name')}" if claim.get("name") else None,
        )
    elif key == "persistentvolumeclaims":
        row.update(
            phase=status.get("phase", "Pending"),
            requested=((spec.get("resources") or {}).get("requests") or {}).get("storage"),
            capacity=((status.get("capacity") or {}).get("storage")),
            access_modes=status.get("accessModes") or spec.get("accessModes") or [],
            storage_class=spec.get("storageClassName"),
            volume=spec.get("volumeName"),
        )
    elif key == "storageclasses":
        row.update(
            provisioner=obj.get("provisioner"),
            reclaim_policy=obj.get("reclaimPolicy", "Delete"),
            binding_mode=obj.get("volumeBindingMode", "Immediate"),
            allow_expansion=bool(obj.get("allowVolumeExpansion", False)),
            default=(
                (_meta(obj).get("annotations") or {}).get("storageclass.kubernetes.io/is-default-class") == "true"
                or (_meta(obj).get("annotations") or {}).get("storageclass.beta.kubernetes.io/is-default-class")
                == "true"
            ),
        )
    elif key == "csidrivers":
        row.update(
            attach_required=spec.get("attachRequired"),
            pod_info_on_mount=spec.get("podInfoOnMount"),
            fs_group_policy=spec.get("fsGroupPolicy"),
            volume_lifecycle_modes=spec.get("volumeLifecycleModes") or [],
            storage_capacity_tracking=bool(spec.get("storageCapacity", False)),
        )
    elif key == "volumeattachments":
        source = spec.get("source") or {}
        row.update(
            driver=spec.get("attacher"),
            node=spec.get("nodeName"),
            persistent_volume=source.get("persistentVolumeName"),
            attached=bool(status.get("attached", False)),
            attach_error=(status.get("attachError") or {}).get("message"),
            detach_error=(status.get("detachError") or {}).get("message"),
        )
    elif key == "gatewayclasses":
        conditions = {item.get("type"): item for item in status.get("conditions") or []}
        row.update(
            controller=spec.get("controllerName"),
            accepted=(conditions.get("Accepted") or {}).get("status"),
            reason=(conditions.get("Accepted") or {}).get("reason"),
        )
    elif key == "gateways":
        conditions = {item.get("type"): item for item in status.get("conditions") or []}
        row.update(
            gateway_class=(spec.get("gatewayClassName")),
            listeners=len(spec.get("listeners") or []),
            addresses=[item.get("value") for item in status.get("addresses") or []],
            programmed=(conditions.get("Programmed") or {}).get("status"),
            accepted=(conditions.get("Accepted") or {}).get("status"),
        )
    elif key == "httproutes":
        parent_conditions = [
            condition for parent in status.get("parents") or [] for condition in parent.get("conditions") or []
        ]
        conditions = {item.get("type"): item for item in parent_conditions}
        row.update(
            hosts=spec.get("hostnames") or ["*"],
            parents=[
                f"{parent.get('namespace') + '/' if parent.get('namespace') else ''}{parent.get('name')}"
                for parent in spec.get("parentRefs") or []
            ],
            rules=len(spec.get("rules") or []),
            accepted=(conditions.get("Accepted") or {}).get("status"),
            resolved_refs=(conditions.get("ResolvedRefs") or {}).get("status"),
        )
    elif key == "referencegrants":
        row.update(from_count=len(spec.get("from") or []), to_count=len(spec.get("to") or []))
    elif key == "resourcequotas":
        hard = status.get("hard") or spec.get("hard") or {}
        used = status.get("used") or {}
        row.update(
            hard=[f"{key}: {value}" for key, value in sorted(hard.items())],
            used=[f"{key}: {value}" for key, value in sorted(used.items())],
            resource_count=len(hard),
        )
    elif key == "limitranges":
        limits = spec.get("limits") or []
        row.update(
            limit_types=[item.get("type") for item in limits],
            limit_count=len(limits),
        )
    elif key == "configmaps":
        keys = sorted([*(obj.get("data") or {}), *(obj.get("binaryData") or {})])
        row.update(keys=keys)
    elif key == "secrets":
        row.update(type=obj.get("type"), keys=sorted(obj.get("data") or {}))
    return row


# --------------------------------------------------------------------------- YAML-ready objects


def clean_for_edit(obj: Obj) -> Obj:
    """Strip server-managed fields so the object can be edited and re-applied."""
    result = copy.deepcopy(obj)
    result.pop("status", None)
    meta = result.get("metadata") or {}
    for field in ("managedFields", "uid", "resourceVersion", "generation", "creationTimestamp", "selfLink"):
        meta.pop(field, None)
    annotations = meta.get("annotations") or {}
    annotations.pop(LAST_APPLIED, None)
    annotations.pop("deployment.kubernetes.io/revision", None)
    if not annotations:
        meta.pop("annotations", None)
    return result


def clean_for_view(obj: Obj) -> Obj:
    result = copy.deepcopy(obj)
    meta = result.get("metadata") or {}
    meta.pop("managedFields", None)
    (meta.get("annotations") or {}).pop(LAST_APPLIED, None)
    return result


def redact_secret(obj: Obj) -> Obj:
    """Replace Secret values with a placeholder (values are revealed only on explicit, audited request)."""
    if obj.get("kind") != "Secret":
        return obj
    result = copy.deepcopy(obj)
    for field in ("data", "stringData"):
        if isinstance(result.get(field), dict):
            result[field] = {k: REDACTED for k in result[field]}
    return result
