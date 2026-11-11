"""Real Kind/k3d lifecycle on the configured local Docker daemon."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import tempfile
import time
import urllib.request
from contextlib import suppress
from pathlib import Path
from typing import Any

import yaml

from app.modules.cluster_lifecycle.distributions import access
from app.modules.cluster_lifecycle.distributions.base import ClusterPlanInput
from app.modules.infrastructure.provider_workflows import provider_for
from app.modules.kubernetes import client as kc
from app.modules.kubernetes.phase6 import workflow_api
from app.orchestration.context import StepContext
from app.orchestration.workflow import RetryableStepError, StepFailedError

VERSIONS = {"kind": "v0.33.0", "k3d": "v5.8.3"}
REPOS = {"kind": "kubernetes-sigs/kind", "k3d": "k3d-io/k3d"}


def binary(tool: str) -> str:
    """Cache pinned upstream executables after checking their published SHA256 sums."""
    system = platform.system().lower()
    arch = {"amd64": "amd64", "x86_64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine().lower())
    if system not in {"windows", "linux", "darwin"} or arch is None or (system == "windows" and arch != "amd64"):
        raise StepFailedError(f"{tool} automatic installation does not support {system}/{platform.machine()}")
    asset = f"{tool}-{system}-{arch}" + (".exe" if tool == "k3d" and system == "windows" else "")
    root = Path.home() / ".cache" / "kubernetes-platform" / "tools" / tool / VERSIONS[tool]
    root.mkdir(parents=True, exist_ok=True)
    destination = root / (f"{tool}.exe" if system == "windows" else tool)
    digest_file = destination.with_suffix(".sha256")
    if (
        destination.is_file()
        and digest_file.is_file()
        and hashlib.sha256(destination.read_bytes()).hexdigest() == digest_file.read_text().strip()
    ):
        return str(destination)
    base = f"https://github.com/{REPOS[tool]}/releases/download/{VERSIONS[tool]}"
    sums_asset = f"{asset}.sha256sum" if tool == "kind" else "checksums.txt"
    try:
        with urllib.request.urlopen(f"{base}/{sums_asset}", timeout=30) as response:
            sums = response.read(256 * 1024).decode()
        expected = next(
            (
                line.split()[0]
                for line in sums.splitlines()
                if len(line.split()) >= 2 and line.split()[-1].lstrip("*") == asset
            ),
            None,
        )
        if expected is None:
            raise StepFailedError(f"Upstream did not publish a checksum for {asset}")
        with urllib.request.urlopen(f"{base}/{asset}", timeout=120) as response:
            data = response.read(150 * 1024 * 1024 + 1)
        if hashlib.sha256(data).hexdigest() != expected:
            raise StepFailedError(f"Integrity verification failed for {asset}")
        with tempfile.NamedTemporaryFile(dir=root, delete=False) as temporary:
            temporary.write(data)
            temporary_path = Path(temporary.name)
        temporary_path.chmod(0o700)
        os.replace(temporary_path, destination)
        digest_file.write_text(expected)
    except OSError as exc:
        raise RetryableStepError(f"Could not install pinned {tool}: {exc}") from exc
    return str(destination)


def run(ctx: StepContext, tool: str, args: list[str], env: dict[str, str], *, timeout: int = 900) -> str:
    ctx.check_cancelled()
    ctx.info(f"Running {tool} {' '.join(args[:2])}")
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(
            [binary(tool), *args],
            stdout=output,
            stderr=errors,
            env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )
        deadline = time.monotonic() + timeout
        try:
            while process.poll() is None:
                ctx.check_cancelled()
                if time.monotonic() > deadline:
                    raise StepFailedError(f"{tool} timed out; retry to resume or delete the partial cluster")
                time.sleep(1)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
        output.seek(0)
        result = output.read(4 * 1024 * 1024).decode(errors="replace")
        errors.seek(0)
        diagnostic = errors.read(2 * 1024 * 1024).decode(errors="replace")
        if process.returncode:
            raise StepFailedError(f"{tool} {' '.join(args[:2])} failed: {diagnostic[-1500:]}")
        if "create" in args[:2] or "delete" in args[:2]:
            for line in (result + diagnostic).splitlines()[-40:]:
                ctx.info(line)
        return result


def identity(plan: ClusterPlanInput) -> str:
    # A UUID namespace keeps clusters from different organizations disjoint, and
    # reserves ownership before the external create command can be interrupted.
    return f"kp-{plan.cluster_id.hex}"


def environment(ctx: StepContext, plan: ClusterPlanInput) -> dict[str, str]:
    if plan.spec.provider_connection_id is None:
        raise StepFailedError("Local clusters need a Local Docker connection")
    env = os.environ.copy()
    with provider_for(ctx, plan.spec.provider_connection_id) as provider:
        provider.validate_connection()
        config = provider.config.model_dump()
    if config.get("access") != "published_ports":
        raise StepFailedError("Kind/k3d currently require local Docker with published_ports access")
    docker_host = config.get("docker_host") or env.get("DOCKER_HOST")
    if docker_host and not docker_host.startswith(("unix://", "npipe://")):
        raise StepFailedError("Kind/k3d require a local Docker socket, not a remote TCP daemon")
    env["DOCKER_HOST"] = docker_host or (
        "npipe:////./pipe/docker_engine" if os.name == "nt" else "unix:///var/run/docker.sock"
    )
    # The SDK does not use Docker CLI contexts; the CLI must use the same daemon.
    env.pop("DOCKER_CONTEXT", None)
    env["KIND_EXPERIMENTAL_PROVIDER"] = "docker"
    return env


def create(ctx: StepContext) -> dict[str, Any]:
    plan: ClusterPlanInput = ctx.params  # type: ignore[assignment]
    tool = plan.spec.distribution.value
    name = identity(plan)
    env = environment(ctx, plan)
    version = plan.spec.kubernetes_version.removeprefix("v")
    if version.count(".") != 2:
        raise StepFailedError("Local distributions require an exact Kubernetes patch version")
    with tempfile.TemporaryDirectory(prefix="kplatform-local-") as directory:
        config_path = Path(directory) / "cluster.yaml"
        kubeconfig_path = Path(directory) / "access.yaml"
        kubeconfig_path.touch(mode=0o600)
        if tool == "kind":
            clusters = run(ctx, tool, ["get", "clusters"], env).splitlines()
            if name not in clusters:
                config: dict[str, Any] = {
                    "kind": "Cluster",
                    "apiVersion": "kind.x-k8s.io/v1alpha4",
                    "networking": {
                        "apiServerAddress": "127.0.0.1",
                        "podSubnet": plan.spec.network.pod_cidr,
                        "serviceSubnet": plan.spec.network.service_cidr,
                        "disableDefaultCNI": plan.spec.network.cni.value != "provider-default",
                    },
                    "kubeadmConfigPatches": [
                        yaml.safe_dump(
                            {"kind": "ClusterConfiguration", "networking": {"dnsDomain": plan.spec.network.dns_domain}}
                        )
                    ],
                    "nodes": [
                        {
                            "role": "control-plane",
                            "labels": {
                                **plan.spec.control_plane.labels,
                                "kubernetes-platform.io/cluster-id": str(plan.cluster_id),
                                "kplatform.io/pool": plan.spec.control_plane.name,
                            },
                        }
                        for _ in range(plan.spec.control_plane.count)
                    ]
                    + [
                        {
                            "role": "worker",
                            "labels": {
                                **pool.labels,
                                "kplatform.io/pool": pool.name,
                                "kubernetes-platform.io/cluster-id": str(plan.cluster_id),
                            },
                        }
                        for pool in plan.spec.worker_pools
                        for _ in range(pool.count)
                    ],
                }
                config_path.write_text(yaml.safe_dump(config))
                run(
                    ctx,
                    tool,
                    [
                        "create",
                        "cluster",
                        "--name",
                        name,
                        "--image",
                        f"kindest/node:v{version}",
                        "--config",
                        str(config_path),
                        "--kubeconfig",
                        str(kubeconfig_path),
                        "--wait",
                        "0s",
                        "--retain",
                    ],
                    env,
                )
            document = run(ctx, tool, ["get", "kubeconfig", "--name", name], env)
        else:
            clusters = json.loads(run(ctx, tool, ["cluster", "list", "--output", "json"], env))
            if not any(cluster.get("name") == name for cluster in clusters):
                config = {
                    "apiVersion": "k3d.io/v1alpha5",
                    "kind": "Simple",
                    "metadata": {"name": name},
                    "servers": plan.spec.control_plane.count,
                    "agents": sum(pool.count for pool in plan.spec.worker_pools),
                    "image": f"rancher/k3s:v{version}-k3s{int(plan.spec.options.get('release_revision', 1))}",
                    "kubeAPI": {"hostIP": "127.0.0.1"},
                    "options": {
                        "kubeconfig": {"updateDefaultKubeconfig": False, "switchCurrentContext": False},
                        "k3d": {"wait": True, "timeout": "300s"},
                        "k3s": {
                            "extraArgs": [
                                {"arg": f"--cluster-cidr={plan.spec.network.pod_cidr}", "nodeFilters": ["server:*"]},
                                {
                                    "arg": f"--service-cidr={plan.spec.network.service_cidr}",
                                    "nodeFilters": ["server:*"],
                                },
                                {
                                    "arg": f"--cluster-domain={plan.spec.network.dns_domain}",
                                    "nodeFilters": ["server:*"],
                                },
                            ]
                        },
                    },
                }
                agent = 0
                for key, value in plan.spec.control_plane.labels.items():
                    config["options"]["k3s"]["extraArgs"].append(
                        {"arg": f"--node-label={key}={value}", "nodeFilters": ["server:*"]}
                    )
                for taint in plan.spec.control_plane.taints:
                    config["options"]["k3s"]["extraArgs"].append(
                        {
                            "arg": f"--node-taint={taint.key}={taint.value or ''}:{taint.effect.value}",
                            "nodeFilters": ["server:*"],
                        }
                    )
                for pool in plan.spec.worker_pools:
                    for _ in range(pool.count):
                        config["options"]["k3s"]["extraArgs"].append(
                            {"arg": f"--node-label=kplatform.io/pool={pool.name}", "nodeFilters": [f"agent:{agent}"]}
                        )
                        for key, value in pool.labels.items():
                            config["options"]["k3s"]["extraArgs"].append(
                                {"arg": f"--node-label={key}={value}", "nodeFilters": [f"agent:{agent}"]}
                            )
                        for taint in pool.taints:
                            config["options"]["k3s"]["extraArgs"].append(
                                {
                                    "arg": f"--node-taint={taint.key}={taint.value or ''}:{taint.effect.value}",
                                    "nodeFilters": [f"agent:{agent}"],
                                }
                            )
                        agent += 1
                if plan.spec.storage.nfs_server:
                    config["options"]["k3s"]["extraArgs"].append(
                        {"arg": "--disable=local-storage", "nodeFilters": ["server:*"]}
                    )
                config_path.write_text(yaml.safe_dump(config))
                run(ctx, tool, ["cluster", "create", "--config", str(config_path)], env)
            document = run(ctx, tool, ["kubeconfig", "get", name], env)
        ctx.redact(document)
        return access.save(ctx, plan, document, f"v{version}")


def ready(ctx: StepContext) -> dict[str, Any]:
    plan: ClusterPlanInput = ctx.params  # type: ignore[assignment]
    api = workflow_api(ctx, plan.cluster_id)
    try:
        deadline = time.monotonic() + 300
        expected = plan.spec.control_plane.count + sum(p.count for p in plan.spec.worker_pools)
        configured: set[str] = set()
        while time.monotonic() < deadline:
            ctx.check_cancelled()
            nodes = kc.get_json(api, "/api/v1/nodes").get("items", [])
            if plan.spec.distribution.value == "kind":
                for node in nodes:
                    meta = node["metadata"]
                    pool = next(
                        (
                            pool
                            for pool in (plan.spec.control_plane, *plan.spec.worker_pools)
                            if meta.get("labels", {}).get("kplatform.io/pool") == pool.name
                        ),
                        None,
                    )
                    if pool and pool.taints and meta["name"] not in configured:
                        existing = node.get("spec", {}).get("taints", [])
                        desired = {(t.key, t.effect.value) for t in pool.taints}
                        taints = [t for t in existing if (t["key"], t["effect"]) not in desired]
                        taints.extend(t.model_dump(mode="json", exclude_none=True) for t in pool.taints)
                        api.request(
                            "PATCH",
                            f"/api/v1/nodes/{meta['name']}",
                            body={"metadata": {"resourceVersion": meta["resourceVersion"]}, "spec": {"taints": taints}},
                            content_type="application/merge-patch+json",
                        )
                        configured.add(meta["name"])
            dns = kc.get_json(
                api, "/api/v1/namespaces/kube-system/pods", params={"labelSelector": "k8s-app=kube-dns"}
            ).get("items", [])
            if (
                dns
                and all(
                    any(
                        c.get("type") == "Ready" and c.get("status") == "True"
                        for c in d.get("status", {}).get("conditions", [])
                    )
                    for d in dns
                )
                and len(nodes) >= expected
                and all(
                    any(
                        c.get("type") == "Ready" and c.get("status") == "True"
                        for c in node.get("status", {}).get("conditions", [])
                    )
                    for node in nodes
                )
            ):
                return {"nodes": [node["metadata"]["name"] for node in nodes]}
            time.sleep(5)
        raise StepFailedError("Timed out waiting for local nodes to become Ready")
    finally:
        api.close()


def delete(ctx: StepContext) -> dict[str, Any]:
    plan: ClusterPlanInput = ctx.params  # type: ignore[assignment]
    tool = plan.spec.distribution.value
    args = ["delete", "cluster", "--name", identity(plan)] if tool == "kind" else ["cluster", "delete", identity(plan)]
    run(ctx, tool, args, environment(ctx, plan))
    return {"deleted": True}


def resize(ctx: StepContext) -> dict[str, Any]:
    from urllib.parse import quote

    from app.modules.cluster_lifecycle.models import Cluster

    plan: ClusterPlanInput = ctx.params  # type: ignore[assignment]
    pool = next((p for p in plan.spec.worker_pools if p.name == plan.pool_name), None)
    if pool is None or plan.pool_count is None:
        raise StepFailedError("A pool and desired count are required")
    env = environment(ctx, plan)
    api = workflow_api(ctx, plan.cluster_id)
    try:

        def members():
            return kc.get_json(api, "/api/v1/nodes", params={"labelSelector": f"kplatform.io/pool={pool.name}"}).get(
                "items", []
            )

        nodes = sorted(members(), key=lambda node: node["metadata"]["name"])
        while len(nodes) < plan.pool_count:
            ctx.check_cancelled()
            suffix = hashlib.sha256(pool.name.encode()).hexdigest()[:8]
            name = f"{identity(plan)}-{suffix}-{len(nodes)}"
            args = [
                "node",
                "create",
                name,
                "--cluster",
                identity(plan),
                "--role",
                "agent",
                "--wait",
                "--timeout",
                "300s",
                "--k3s-node-label",
                f"kplatform.io/pool={pool.name}",
            ]
            for key, value in pool.labels.items():
                args.extend(["--k3s-node-label", f"{key}={value}"])
            for taint in pool.taints:
                args.extend(["--k3s-arg", f"--node-taint={taint.key}={taint.value or ''}:{taint.effect.value}"])
            run(ctx, "k3d", args, env)
            updated = sorted(members(), key=lambda node: node["metadata"]["name"])
            if len(updated) <= len(nodes):
                raise StepFailedError("k3d created no registered worker; inspect the partial node before retrying")
            nodes = updated
        for node in reversed(nodes[plan.pool_count :]):
            ctx.check_cancelled()
            name = node["metadata"]["name"]
            if not name.startswith(f"k3d-{identity(plan)}-"):
                raise StepFailedError("Refusing to remove a node outside this platform cluster")
            # Scale-down is explicitly confirmed by cluster name at submission.
            # k3d's native delete removes the container; this local development
            # operation is not a production workload migration mechanism.
            run(ctx, "k3d", ["node", "delete", name], env)
            with suppress(kc.NotFoundInClusterError):
                api.request("DELETE", f"/api/v1/nodes/{quote(name, safe='')}")
    finally:
        api.close()
    with ctx.db() as session:
        cluster = session.get(Cluster, plan.cluster_id)
        if cluster is None:
            raise StepFailedError("Cluster registration no longer exists")
        spec = dict(cluster.spec)
        pools = [dict(p) for p in spec.get("worker_pools", [])]
        for item in pools:
            if item["name"] == plan.pool_name:
                item["count"] = plan.pool_count
        spec["worker_pools"] = pools
        cluster.spec = spec
    return {"pool": plan.pool_name, "count": plan.pool_count}
