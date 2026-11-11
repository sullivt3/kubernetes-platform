"""Pinned Cilium/Calico installation for clusters without a native CNI installer."""

import hashlib
import io
import os
import platform
import subprocess
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any

import yaml

from app.modules.cluster_lifecycle.distributions.base import ClusterPlanInput
from app.modules.kubernetes import apply as kapply
from app.modules.kubernetes import client as kc
from app.modules.kubernetes.phase6 import workflow_api
from app.orchestration.context import StepContext
from app.orchestration.workflow import RetryableStepError, StepFailedError

HELM_VERSION = "v3.19.0"
CILIUM_VERSION = "1.20.4"
OWNER = "kubernetes-platform.io/cluster-id"


def download(url: str, limit: int = 100 * 1024 * 1024) -> bytes:
    try:
        with urllib.request.urlopen(url, timeout=120) as response:
            content = response.read(limit + 1)
        if len(content) > limit:
            raise StepFailedError("Upstream artifact exceeds the size limit")
        return content
    except OSError as exc:
        raise RetryableStepError(f"Could not fetch installer artifact: {exc}") from exc


def helm() -> str:
    system = platform.system().lower()
    arch = {"amd64": "amd64", "x86_64": "amd64", "arm64": "arm64", "aarch64": "arm64"}.get(platform.machine().lower())
    if system not in {"windows", "linux", "darwin"} or not arch:
        raise StepFailedError("Helm automatic installation does not support this host")
    root = Path.home() / ".cache" / "kubernetes-platform" / "tools" / "helm" / HELM_VERSION
    root.mkdir(parents=True, exist_ok=True)
    target = root / ("helm.exe" if system == "windows" else "helm")
    stamp = root / "sha256"
    if target.is_file() and stamp.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == stamp.read_text():
        return str(target)
    # Helm publishes tar.gz archives for Windows as well as Unix.
    url = f"https://get.helm.sh/helm-{HELM_VERSION}-{system}-{arch}.tar.gz"
    expected = download(url + ".sha256sum", 4096).decode().split()[0]
    archive = download(url)
    if hashlib.sha256(archive).hexdigest() != expected:
        raise StepFailedError("Helm archive checksum verification failed")
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        member = tar.extractfile(f"{system}-{arch}/{'helm.exe' if system == 'windows' else 'helm'}")
        if member is None:
            raise StepFailedError("Helm archive is missing its executable")
        data = member.read()
    with tempfile.NamedTemporaryFile(dir=root, delete=False) as temporary:
        temporary.write(data)
        path = Path(temporary.name)
    path.chmod(0o700)
    os.replace(path, target)
    stamp.write_text(hashlib.sha256(data).hexdigest())
    return str(target)


def cilium_objects(plan: ClusterPlanInput) -> list[dict[str, Any]]:
    # The chart digest is published in the upstream repository index.
    index = yaml.safe_load(download("https://helm.cilium.io/index.yaml", 10 * 1024 * 1024))
    entry = next((item for item in index["entries"]["cilium"] if item["version"] == CILIUM_VERSION), None)
    if entry is None:
        raise StepFailedError("The pinned Cilium chart is missing from its upstream index")
    chart = download(f"https://helm.cilium.io/cilium-{CILIUM_VERSION}.tgz")
    if hashlib.sha256(chart).hexdigest() != entry["digest"]:
        raise StepFailedError("Cilium chart checksum verification failed")
    values: dict[str, Any] = {"kubeProxyReplacement": False, "operator": {"replicas": 1}}
    if plan.spec.distribution.value == "eks":
        values.update(
            {
                "cni": {"chainingMode": "aws-cni", "exclusive": False},
                "enableIPv4Masquerade": False,
                "routingMode": "native",
            }
        )
    else:
        values.update({"ipam": {"mode": "kubernetes"}, "routingMode": "tunnel"})
    if plan.spec.distribution.value == "kind":
        values.update({"cgroup": {"autoMount": {"enabled": False}, "hostRoot": "/sys/fs/cgroup"}})
    with tempfile.TemporaryDirectory(prefix="kplatform-cni-") as directory:
        root = Path(directory)
        (root / "cilium.tgz").write_bytes(chart)
        (root / "values.yaml").write_text(yaml.safe_dump(values))
        version = plan.spec.kubernetes_version.removeprefix("v")
        if version.count(".") == 1:
            version += ".0"
        result = subprocess.run(
            [
                helm(),
                "template",
                "cilium",
                str(root / "cilium.tgz"),
                "--namespace",
                "kube-system",
                "--kube-version",
                version,
                "--include-crds",
                "--values",
                str(root / "values.yaml"),
            ],
            capture_output=True,
            text=True,
            timeout=180,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )
        if result.returncode:
            raise StepFailedError(f"Could not render Cilium chart: {result.stderr[-1000:]}")
        return kapply.parse_manifest(result.stdout)


def install(ctx: StepContext) -> dict[str, Any]:
    plan: ClusterPlanInput = ctx.params  # type: ignore[assignment]
    cni = plan.spec.network.cni.value
    ctx.check_cancelled()
    if cni == "cilium":
        objects = cilium_objects(plan)
    else:
        from app.modules.cluster_lifecycle.distributions.kubeadm.config import CNI_MANIFESTS

        version, url = CNI_MANIFESTS["calico"]
        objects = kapply.parse_manifest(download(url.format(version=version)).decode())
        for obj in objects:
            if obj.get("kind") == "DaemonSet":
                for container in obj.get("spec", {}).get("template", {}).get("spec", {}).get("containers", []):
                    if container.get("name") == "calico-node":
                        env = container.setdefault("env", [])
                        env[:] = [value for value in env if value.get("name") != "CALICO_IPV4POOL_CIDR"]
                        env.append({"name": "CALICO_IPV4POOL_CIDR", "value": plan.spec.network.pod_cidr})
    api = workflow_api(ctx, plan.cluster_id)
    try:
        discovery = kapply.Discovery(api)
        for obj in objects:
            ctx.check_cancelled()
            meta = obj.setdefault("metadata", {})
            meta.setdefault("namespace", "kube-system") if obj["kind"] in {
                "ConfigMap",
                "Secret",
                "ServiceAccount",
                "Service",
                "Deployment",
                "DaemonSet",
                "Role",
                "RoleBinding",
            } else None
            path = discovery.object_path(obj)
            try:
                existing = kc.get_json(api, path)
            except kc.NotFoundInClusterError:
                existing = None
            if existing and existing.get("metadata", {}).get("labels", {}).get(OWNER) != str(plan.cluster_id):
                raise StepFailedError(f"Refusing to replace existing unowned CNI resource {obj['kind']}/{meta['name']}")
            meta.setdefault("labels", {})[OWNER] = str(plan.cluster_id)
            kapply.apply_objects(api, [obj])
        workload = "cilium" if cni == "cilium" else "calico-node"
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            ctx.check_cancelled()
            status = kc.get_json(api, f"/apis/apps/v1/namespaces/kube-system/daemonsets/{workload}").get("status", {})
            wanted = status.get("desiredNumberScheduled", 0)
            if wanted and status.get("numberReady", 0) >= wanted:
                return {"cni": cni, "version": CILIUM_VERSION if cni == "cilium" else version}
            time.sleep(5)
        raise StepFailedError(f"Timed out waiting for {cni} agents")
    finally:
        api.close()
