"""Render kubeadm configuration documents.

Documents are emitted as JSON objects separated by ``---``: JSON is valid YAML, so
kubeadm reads them natively and no YAML library is needed. kubeadm's config API is
``v1beta4`` from Kubernetes 1.31 and ``v1beta3`` before that.
"""

from __future__ import annotations

import json
import re
import secrets
import string
from typing import Any

from pydantic import BaseModel, Field, field_validator

from app.modules.cluster_lifecycle.distributions.spec import ClusterSpec

CRI_SOCKET = "unix:///run/containerd/containerd.sock"
MIN_MINOR = 30  # oldest Kubernetes minor version the installer supports

# Pinned, verified CNI manifests.
CNI_MANIFESTS: dict[str, tuple[str, str]] = {
    "calico": (
        "v3.33.0",
        "https://raw.githubusercontent.com/projectcalico/calico/{version}/manifests/calico.yaml",
    ),
    "flannel": (
        "v0.28.10",
        "https://github.com/flannel-io/flannel/releases/download/{version}/kube-flannel.yml",
    ),
}

_PREFLIGHT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-]{0,127}$")


class KubeadmOptions(BaseModel):
    """Distribution-specific options (``ClusterSpec.options``)."""

    allow_swap: bool = Field(
        default=False,
        description="Leave swap enabled and run kubelet with failSwapOn=false (otherwise swap is disabled).",
    )
    control_plane_workloads: bool | None = Field(
        default=None,
        description="Allow regular workloads on control-plane nodes (default: only without workers).",
    )
    ignore_preflight_errors: list[str] = Field(
        default_factory=list, description="kubeadm preflight checks to downgrade to warnings (e.g. 'Swap')."
    )
    api_server_extra_sans: list[str] = Field(
        default_factory=list, description="Additional names/IPs for the API server certificate."
    )
    kube_proxy_conntrack_max_per_core: int | None = Field(
        default=None,
        ge=0,
        description=(
            "kube-proxy conntrack.maxPerCore. 0 leaves the host's nf_conntrack_max untouched (needed when "
            "/proc/sys is read-only, e.g. container-based nodes)."
        ),
    )
    kubelet_resolv_conf: str | None = Field(
        default=None,
        pattern=r"^/[A-Za-z0-9._/-]{1,255}$",
        description=(
            "resolv.conf passed to pods by kubelet. Set this when the host's /etc/resolv.conf only lists "
            "loopback nameservers (local caching resolvers), which would make CoreDNS forward to itself."
        ),
    )
    ready_timeout_seconds: int = Field(default=900, ge=120, le=7200)

    @field_validator("ignore_preflight_errors")
    @classmethod
    def _preflight_names(cls, values: list[str]) -> list[str]:
        for value in values:
            if not _PREFLIGHT_NAME.match(value):
                raise ValueError(f"invalid preflight check name '{value}'")
        return values

    @field_validator("api_server_extra_sans")
    @classmethod
    def _sans(cls, values: list[str]) -> list[str]:
        for value in values:
            if not re.match(r"^[A-Za-z0-9.\-:*]{1,253}$", value):
                raise ValueError(f"invalid certificate SAN '{value}'")
        return values


def parse_version(version: str) -> tuple[int, int, int | None]:
    match = re.match(r"^v?1\.(\d{1,2})(?:\.(\d{1,3}))?$", version)
    if not match:
        raise ValueError(f"invalid Kubernetes version '{version}'")
    return 1, int(match.group(1)), int(match.group(2)) if match.group(2) else None


def minor_of(version: str) -> str:
    _, minor, _ = parse_version(version)
    return f"1.{minor}"


def _api_version(version: str) -> str:
    return "kubeadm.k8s.io/v1beta4" if parse_version(version)[1] >= 31 else "kubeadm.k8s.io/v1beta3"


def _extra_args(version: str, args: dict[str, str]) -> Any:
    if parse_version(version)[1] >= 31:
        return [{"name": k, "value": v} for k, v in args.items()]
    return args


def generate_bootstrap_token() -> str:
    alphabet = string.ascii_lowercase + string.digits
    return (
        "".join(secrets.choice(alphabet) for _ in range(6)) + "." + "".join(secrets.choice(alphabet) for _ in range(16))
    )


def generate_certificate_key() -> str:
    return secrets.token_hex(32)


def control_plane_endpoint(spec: ClusterSpec) -> str | None:
    endpoint = spec.control_plane.endpoint
    if not endpoint:
        return None
    return endpoint if re.search(r":\d+$", endpoint) else f"{endpoint}:6443"


def _node_registration(version: str, name: str, ip: str, options: KubeadmOptions) -> dict[str, Any]:
    registration: dict[str, Any] = {
        "name": name,
        "criSocket": CRI_SOCKET,
        "kubeletExtraArgs": _extra_args(version, {"node-ip": ip}),
    }
    if options.ignore_preflight_errors:
        registration["ignorePreflightErrors"] = options.ignore_preflight_errors
    return registration


def _documents(*docs: dict[str, Any]) -> str:
    return "\n---\n".join(json.dumps(doc, indent=2) for doc in docs) + "\n"


def render_init_config(
    *,
    spec: ClusterSpec,
    options: KubeadmOptions,
    version: str,
    node_name: str,
    advertise_address: str,
    bootstrap_token: str,
    certificate_key: str | None,
) -> str:
    api = _api_version(version)
    init: dict[str, Any] = {
        "apiVersion": api,
        "kind": "InitConfiguration",
        "bootstrapTokens": [
            {
                "token": bootstrap_token,
                "ttl": "2h0m0s",
                "usages": ["signing", "authentication"],
                "groups": ["system:bootstrappers:kubeadm:default-node-token"],
            }
        ],
        "nodeRegistration": _node_registration(version, node_name, advertise_address, options),
        "localAPIEndpoint": {"advertiseAddress": advertise_address, "bindPort": 6443},
    }
    if certificate_key:
        init["certificateKey"] = certificate_key

    endpoint = control_plane_endpoint(spec)
    sans = sorted({*options.api_server_extra_sans, *([endpoint.rsplit(":", 1)[0]] if endpoint else [])})
    cluster: dict[str, Any] = {
        "apiVersion": api,
        "kind": "ClusterConfiguration",
        "clusterName": spec.name,
        "kubernetesVersion": f"v{version}",
        "networking": {
            "podSubnet": spec.network.pod_cidr,
            "serviceSubnet": spec.network.service_cidr,
            "dnsDomain": spec.network.dns_domain,
        },
    }
    if endpoint:
        cluster["controlPlaneEndpoint"] = endpoint
    if sans:
        cluster["apiServer"] = {"certSANs": sans}

    kubelet: dict[str, Any] = {
        "apiVersion": "kubelet.config.k8s.io/v1beta1",
        "kind": "KubeletConfiguration",
        "cgroupDriver": "systemd",
    }
    if options.kubelet_resolv_conf:
        kubelet["resolvConf"] = options.kubelet_resolv_conf
    if options.allow_swap:
        kubelet["failSwapOn"] = False
        kubelet["memorySwap"] = {"swapBehavior": "NoSwap"}
    documents = [init, cluster, kubelet]
    if options.kube_proxy_conntrack_max_per_core is not None:
        documents.append(
            {
                "apiVersion": "kubeproxy.config.k8s.io/v1alpha1",
                "kind": "KubeProxyConfiguration",
                "conntrack": {"maxPerCore": options.kube_proxy_conntrack_max_per_core},
            }
        )
    return _documents(*documents)


def render_join_config(
    *,
    version: str,
    options: KubeadmOptions,
    node_name: str,
    advertise_address: str,
    api_server_endpoint: str,
    token: str,
    ca_cert_hash: str,
    certificate_key: str | None = None,
) -> str:
    join: dict[str, Any] = {
        "apiVersion": _api_version(version),
        "kind": "JoinConfiguration",
        "discovery": {
            "bootstrapToken": {
                "apiServerEndpoint": api_server_endpoint,
                "token": token,
                "caCertHashes": [ca_cert_hash],
            }
        },
        "nodeRegistration": _node_registration(version, node_name, advertise_address, options),
    }
    if certificate_key:
        join["controlPlane"] = {
            "localAPIEndpoint": {"advertiseAddress": advertise_address, "bindPort": 6443},
            "certificateKey": certificate_key,
        }
    return _documents(join)


# --------------------------------------------------------------------------- kube-vip

# kube-vip announces the control-plane virtual IP (ARP) from whichever control plane
# holds the leader lease. Pinned; the manifest mirrors `kube-vip manifest pod --arp
# --controlplane --leaderElection` from this release.
KUBE_VIP_IMAGE = "ghcr.io/kube-vip/kube-vip:v1.3.1"
# Image used by the post-install smoke test (same registry as the Kubernetes images).
SMOKE_TEST_IMAGE = "registry.k8s.io/e2e-test-images/busybox:1.38.0-1"
SUPER_ADMIN_MINOR = 29  # kubeadm >= 1.29 creates super-admin.conf; admin.conf has no rights until init ends


def vip_of(spec: ClusterSpec) -> str | None:
    """The kube-vip address (endpoint host, IPv4) for clusters using kube-vip, else None."""
    from app.modules.cluster_lifecycle.distributions.spec import LoadBalancerMode

    if spec.control_plane.load_balancer is not LoadBalancerMode.KUBE_VIP or not spec.control_plane.endpoint:
        return None
    return spec.control_plane.endpoint.split(":", 1)[0]


def kube_vip_kubeconfig(version: str, *, bootstrapping: bool) -> str:
    """The kubeconfig kube-vip mounts. On the first control plane during `kubeadm init`,
    admin.conf has no RBAC binding yet (1.29+), so kube-vip uses super-admin.conf until init
    finishes and is then switched back."""
    if bootstrapping and parse_version(version)[1] >= SUPER_ADMIN_MINOR:
        return "/etc/kubernetes/super-admin.conf"
    return "/etc/kubernetes/admin.conf"


def render_kube_vip_manifest(*, vip: str, interface: str, kubeconfig: str) -> str:
    env = [
        {"name": "vip_arp", "value": "true"},
        {"name": "port", "value": "6443"},
        {"name": "vip_nodename", "valueFrom": {"fieldRef": {"fieldPath": "spec.nodeName"}}},
        {"name": "vip_interface", "value": interface},
        {"name": "vip_subnet", "value": "32"},
        {"name": "cp_enable", "value": "true"},
        {"name": "cp_namespace", "value": "kube-system"},
        {"name": "vip_leaderelection", "value": "true"},
        {"name": "vip_leasename", "value": "plndr-cp-lock"},
        {"name": "vip_leaseduration", "value": "5"},
        {"name": "vip_renewdeadline", "value": "3"},
        {"name": "vip_retryperiod", "value": "1"},
        {"name": "address", "value": vip},
        {"name": "prometheus_server", "value": ":2112"},
    ]
    pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": "kube-vip", "namespace": "kube-system"},
        "spec": {
            "containers": [
                {
                    "name": "kube-vip",
                    "image": KUBE_VIP_IMAGE,
                    "imagePullPolicy": "IfNotPresent",
                    "args": ["manager"],
                    "env": env,
                    "securityContext": {"capabilities": {"add": ["NET_ADMIN", "NET_RAW"], "drop": ["ALL"]}},
                    "volumeMounts": [{"name": "kubeconfig", "mountPath": "/etc/kubernetes/admin.conf"}],
                }
            ],
            # kube-vip talks to the local API server as "kubernetes:6443" (a name in the
            # API server certificate) so it works before the VIP exists.
            "hostAliases": [{"ip": "127.0.0.1", "hostnames": ["kubernetes"]}],
            "hostNetwork": True,
            "volumes": [{"name": "kubeconfig", "hostPath": {"path": kubeconfig}}],
        },
    }
    return json.dumps(pod, indent=2) + "\n"
