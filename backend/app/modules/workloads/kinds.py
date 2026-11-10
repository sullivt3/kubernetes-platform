"""Resource kinds managed in the workload UI and their REST paths.

The YAML apply path works for *any* resource via discovery (``kubernetes.apply``);
this registry covers the kinds that get dedicated list/detail views and actions.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.errors import ValidationFailedError


@dataclass(frozen=True)
class Kind:
    key: str  # URL key, e.g. "deployments"
    kind: str
    api_version: str
    plural: str
    namespaced: bool = True
    scalable: bool = False
    restartable: bool = False
    deletable: bool = True

    def base(self) -> str:
        return "/api/v1" if self.api_version == "v1" else f"/apis/{self.api_version}"

    def collection(self, namespace: str | None) -> str:
        if self.namespaced and namespace:
            return f"{self.base()}/namespaces/{namespace}/{self.plural}"
        return f"{self.base()}/{self.plural}"

    def item(self, namespace: str | None, name: str) -> str:
        if self.namespaced:
            if not namespace:
                raise ValidationFailedError(f"{self.kind} is namespaced; a namespace is required")
            return f"{self.base()}/namespaces/{namespace}/{self.plural}/{name}"
        return f"{self.base()}/{self.plural}/{name}"


KINDS: dict[str, Kind] = {
    k.key: k
    for k in (
        Kind("pods", "Pod", "v1", "pods"),
        Kind("deployments", "Deployment", "apps/v1", "deployments", scalable=True, restartable=True),
        Kind("replicasets", "ReplicaSet", "apps/v1", "replicasets", scalable=True),
        Kind("statefulsets", "StatefulSet", "apps/v1", "statefulsets", scalable=True, restartable=True),
        Kind("daemonsets", "DaemonSet", "apps/v1", "daemonsets", restartable=True),
        Kind("jobs", "Job", "batch/v1", "jobs"),
        Kind("cronjobs", "CronJob", "batch/v1", "cronjobs"),
        Kind("services", "Service", "v1", "services"),
        Kind("ingresses", "Ingress", "networking.k8s.io/v1", "ingresses"),
        Kind("networkpolicies", "NetworkPolicy", "networking.k8s.io/v1", "networkpolicies"),
        Kind("endpointslices", "EndpointSlice", "discovery.k8s.io/v1", "endpointslices"),
        Kind("persistentvolumes", "PersistentVolume", "v1", "persistentvolumes", namespaced=False),
        Kind("persistentvolumeclaims", "PersistentVolumeClaim", "v1", "persistentvolumeclaims"),
        Kind("storageclasses", "StorageClass", "storage.k8s.io/v1", "storageclasses", namespaced=False),
        Kind("csidrivers", "CSIDriver", "storage.k8s.io/v1", "csidrivers", namespaced=False, deletable=False),
        Kind(
            "volumeattachments",
            "VolumeAttachment",
            "storage.k8s.io/v1",
            "volumeattachments",
            namespaced=False,
            deletable=False,
        ),
        Kind(
            "gatewayclasses",
            "GatewayClass",
            "gateway.networking.k8s.io/v1",
            "gatewayclasses",
            namespaced=False,
        ),
        Kind("gateways", "Gateway", "gateway.networking.k8s.io/v1", "gateways"),
        Kind("httproutes", "HTTPRoute", "gateway.networking.k8s.io/v1", "httproutes"),
        Kind("referencegrants", "ReferenceGrant", "gateway.networking.k8s.io/v1", "referencegrants"),
        Kind("configmaps", "ConfigMap", "v1", "configmaps"),
        Kind("secrets", "Secret", "v1", "secrets"),
        Kind("resourcequotas", "ResourceQuota", "v1", "resourcequotas"),
        Kind("limitranges", "LimitRange", "v1", "limitranges"),
    )
}


def get_kind(key: str) -> Kind:
    try:
        return KINDS[key]
    except KeyError:
        raise ValidationFailedError(
            f"Unsupported resource type '{key}'", details={"supported": sorted(KINDS)}
        ) from None
