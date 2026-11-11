"""SSH lifecycle for K3s and RKE2 using their versioned upstream installers."""

from __future__ import annotations

import base64
import json
import secrets
import shlex
import time
from typing import Any

import yaml

from app.modules.cluster_lifecycle.distributions import access
from app.modules.cluster_lifecycle.distributions.base import ClusterPlanInput, PlannedNode
from app.modules.cluster_lifecycle.distributions.linux import swap_script
from app.modules.infrastructure.access import connect, run_script, tail
from app.modules.infrastructure.host_access import CommandOptions, HostExecutor
from app.orchestration.context import StepContext
from app.orchestration.workflow import StepFailedError


def release(plan: ClusterPlanInput, target: str | None = None) -> str:
    version = (target or plan.spec.kubernetes_version).removeprefix("v")
    if version.count(".") != 2:
        raise StepFailedError("K3s/RKE2 require an exact Kubernetes patch version")
    revision = plan.target_release_revision if target else int(plan.spec.options.get("release_revision", 1))
    if revision < 1 or revision > 100:
        raise StepFailedError("release_revision must be between 1 and 100")
    suffix = "rke2r" if plan.spec.distribution.value == "rke2" else "k3s"
    return f"v{version}+{suffix}{revision}"


def command(executor: HostExecutor, value: str, timeout: float = 180) -> str:
    result = executor.run(value, CommandOptions(sudo=True, timeout_seconds=timeout))
    if not result.ok:
        raise StepFailedError(f"Node command failed: {tail(result)}")
    return result.stdout


def prepare(ctx: StepContext) -> dict[str, Any]:
    plan: ClusterPlanInput = ctx.params  # type: ignore[assignment]
    distro = plan.spec.distribution.value
    for node in plan.nodes:
        ctx.check_cancelled()
        with connect(ctx, node.machine_id) as executor:
            result = run_script(
                executor,
                "set -eu\n"
                + swap_script()
                + """
. /etc/os-release
if command -v getenforce >/dev/null && [ "$(getenforce)" = Enforcing ]; then
  rpm -q "$DISTRO-selinux" >/dev/null || {
    echo "Install the distribution's signed SELinux policy package before provisioning this enforcing host" >&2
    exit 1
  }
fi
if command -v apt-get >/dev/null; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq ca-certificates curl tar iptables conntrack socat
else
  iptables_package=iptables
  if [ "$ID" = fedora ]; then iptables_package=iptables-nft; fi
  dnf install -y ca-certificates /usr/bin/curl tar "$iptables_package" conntrack-tools socat
fi
modprobe overlay || true
modprobe br_netfilter || true
install -d /etc/modules-load.d /etc/sysctl.d
printf 'overlay\nbr_netfilter\n' > /etc/modules-load.d/kplatform-k8s.conf
printf 'net.ipv4.ip_forward=1\nnet.bridge.bridge-nf-call-iptables=1\n' > /etc/sysctl.d/99-kplatform-k8s.conf
sysctl -p /etc/sysctl.d/99-kplatform-k8s.conf
disable_swap
if command -v firewall-cmd >/dev/null && firewall-cmd --state >/dev/null 2>&1; then
  for port in 6443/tcp 9345/tcp 2379-2380/tcp 10250/tcp 8472/udp 4789/udp 4240/tcp; do
    firewall-cmd --permanent --add-port="$port"
  done
  firewall-cmd --reload
fi
if command -v ufw >/dev/null && ufw status | grep -q 'Status: active'; then
  for port in 6443/tcp 9345/tcp 2379:2380/tcp 10250/tcp 8472/udp 4789/udp 4240/tcp; do ufw allow "$port"; done
fi
""",
                env={"DISTRO": distro},
                timeout_seconds=900,
            )
            if not result.ok:
                raise StepFailedError(f"OS preparation failed on {node.name}: {tail(result)}")
    return {"prepared": [n.name for n in plan.nodes]}


def install_node(
    ctx: StepContext, plan: ClusterPlanInput, node: PlannedNode, *, token: str | None, server: str | None
) -> None:
    distro = plan.spec.distribution.value
    path = f"/etc/rancher/{distro}"
    with connect(ctx, node.machine_id) as executor:
        marker = executor.run(f"cat {path}/platform-cluster-id", CommandOptions(sudo=True))
        if marker.ok and marker.stdout.strip() != str(plan.cluster_id):
            raise StepFailedError(f"{node.name} belongs to another platform cluster")
        if not marker.ok:
            probe = executor.run(
                f"test ! -e {path}/config.yaml && test ! -d /var/lib/rancher/{distro}/server/db",
                CommandOptions(sudo=True),
            )
            if not probe.ok:
                raise StepFailedError(f"{node.name} already has unmanaged {distro} state")
        elif token is None:
            document = yaml.safe_load(executor.get_file(f"{path}/config.yaml", sudo=True))
            token = document.get("token")
        token = token or secrets.token_hex(32)
        ctx.redact(token)
        pool = next(p for p in (plan.spec.control_plane, *plan.spec.worker_pools) if p.name == node.pool)
        config: dict[str, Any] = {
            "node-name": node.name,
            "token": token,
            "node-label": [f"{key}={value}" for key, value in {"kplatform.io/pool": pool.name, **pool.labels}.items()],
            "node-taint": [f"{t.key}={t.value or ''}:{t.effect.value}" for t in pool.taints],
        }
        if server:
            config["server"] = server
        if node.role == "control_plane":
            config.update(
                {
                    "cluster-cidr": plan.spec.network.pod_cidr,
                    "service-cidr": plan.spec.network.service_cidr,
                    "cluster-domain": plan.spec.network.dns_domain,
                    "write-kubeconfig-mode": "0600",
                }
            )
            endpoint = plan.spec.control_plane.endpoint
            if endpoint:
                config["tls-san"] = [endpoint.split(":")[0]]
            if distro == "k3s":
                if server is None:
                    config["cluster-init"] = True
                config["disable"] = ["traefik", "servicelb"]
                if plan.spec.storage.nfs_server:
                    config["disable"].append("local-storage")
                if plan.spec.network.cni.value != "flannel":
                    config["flannel-backend"] = "none"
                    config["disable-network-policy"] = True
            else:
                config["cni"] = plan.spec.network.cni.value
        encoded = base64.b64encode(yaml.safe_dump(config).encode()).decode()
        version = release(plan)
        repository = "k3s-io/k3s" if distro == "k3s" else "rancher/rke2"
        installer = f"https://raw.githubusercontent.com/{repository}/{version}/install.sh"
        role = "server" if node.role == "control_plane" else "agent"
        env = (
            {"INSTALL_K3S_VERSION": version, "INSTALL_K3S_EXEC": role}
            if distro == "k3s"
            else {
                "INSTALL_RKE2_VERSION": version,
                "INSTALL_RKE2_TYPE": role,
                "INSTALL_RKE2_METHOD": "tar",
            }
        )
        service = ("k3s" if role == "server" else "k3s-agent") if distro == "k3s" else f"rke2-{role}"
        script = f"""set -eu
install -d -m 700 {path}
printf '%s' '{encoded}' | base64 -d > {path}/config.yaml
chmod 600 {path}/config.yaml
printf '%s' '{plan.cluster_id}' > {path}/platform-cluster-id
installer=$(mktemp)
trap 'rm -f "$installer"' EXIT
curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 {shlex.quote(installer)} -o "$installer"
sh "$installer"
systemctl enable --now {service}
systemctl restart {service}
"""
        if distro == "rke2":
            script += "ln -sf /var/lib/rancher/rke2/bin/kubectl /usr/local/bin/kubectl\n"
        if node.role == "control_plane":
            script += f"""for i in $(seq 1 120); do test ! -f {path}/{distro}.yaml || break; sleep 2; done
install -d -m 700 /etc/kubernetes
ln -sf {path}/{distro}.yaml /etc/kubernetes/admin.conf
"""
        result = run_script(executor, script, env=env, timeout_seconds=1800)
        if not result.ok:
            raise StepFailedError(f"{distro} installation failed on {node.name}: {tail(result)}")
        ctx.info(f"[{node.name}] {version} {role} installed")


def bootstrap(ctx: StepContext) -> dict[str, Any]:
    plan: ClusterPlanInput = ctx.params  # type: ignore[assignment]
    install_node(ctx, plan, plan.lead, token=None, server=None)
    return {"version": release(plan)}


def join(ctx: StepContext) -> dict[str, Any]:
    plan: ClusterPlanInput = ctx.params  # type: ignore[assignment]
    distro = plan.spec.distribution.value
    with connect(ctx, plan.lead.machine_id) as executor:
        token = executor.get_file(f"/var/lib/rancher/{distro}/server/token", sudo=True).decode().strip()
    ctx.redact(token)
    endpoint = plan.spec.control_plane.endpoint
    if endpoint:
        host = endpoint.split(":")[0]
    elif plan.anchor:
        with connect(ctx, plan.anchor.machine_id) as executor:
            # A native kubeconfig's localhost endpoint cannot be used by joining nodes.
            nodes = json.loads(command(executor, "kubectl --kubeconfig /etc/kubernetes/admin.conf get nodes -o json"))[
                "items"
            ]
        host = next(
            address["address"]
            for n in nodes
            if n["metadata"]["name"] == plan.anchor.name
            for address in n["status"]["addresses"]
            if address["type"] == "InternalIP"
        )
    else:
        host = ctx.outputs["validate"]["nodes"][str(plan.lead.machine_id)]["ip"]
    port = "9345" if distro == "rke2" else endpoint.split(":", 1)[1] if endpoint and ":" in endpoint else "6443"
    server = f"https://{host}:{port}"
    for node in [*plan.control_planes, *plan.workers]:
        if not plan.anchor and node.machine_id == plan.lead.machine_id:
            continue
        ctx.check_cancelled()
        install_node(ctx, plan, node, token=token, server=server)
    return {"joined": [n.name for n in plan.nodes]}


def register(ctx: StepContext) -> dict[str, Any]:
    plan: ClusterPlanInput = ctx.params  # type: ignore[assignment]
    distro = plan.spec.distribution.value
    with connect(ctx, plan.lead.machine_id) as executor:
        document = yaml.safe_load(executor.get_file(f"/etc/rancher/{distro}/{distro}.yaml", sudo=True))
    endpoint = (
        plan.spec.control_plane.endpoint or f"{ctx.outputs['validate']['nodes'][str(plan.lead.machine_id)]['ip']}:6443"
    )
    if ":" not in endpoint:
        endpoint += ":6443"
    document["clusters"][0]["cluster"]["server"] = f"https://{endpoint}"
    return access.save(ctx, plan, yaml.safe_dump(document), "v" + plan.spec.kubernetes_version.removeprefix("v"))


def ready(ctx: StepContext) -> dict[str, Any]:
    plan: ClusterPlanInput = ctx.params  # type: ignore[assignment]
    expected = {n.name for n in plan.nodes}
    deadline = time.monotonic() + 600
    with connect(ctx, plan.lead.machine_id) as executor:
        while time.monotonic() < deadline:
            ctx.check_cancelled()
            nodes = json.loads(command(executor, "kubectl --kubeconfig /etc/kubernetes/admin.conf get nodes -o json"))[
                "items"
            ]
            ready_nodes = {
                n["metadata"]["name"]
                for n in nodes
                if any(c["type"] == "Ready" and c["status"] == "True" for c in n["status"].get("conditions", []))
            }
            dns = json.loads(
                command(
                    executor,
                    "kubectl --kubeconfig /etc/kubernetes/admin.conf -n kube-system "
                    "get pods -l k8s-app=kube-dns -o json",
                )
            ).get("items", [])
            if (
                expected <= ready_nodes
                and dns
                and all(
                    any(
                        c.get("type") == "Ready" and c.get("status") == "True"
                        for c in d.get("status", {}).get("conditions", [])
                    )
                    for d in dns
                )
            ):
                return {"nodes": sorted(ready_nodes), "dns": "available"}
            time.sleep(5)
    raise StepFailedError("Timed out waiting for nodes to become Ready")


def uninstall(ctx: StepContext) -> dict[str, Any]:
    plan: ClusterPlanInput = ctx.params  # type: ignore[assignment]
    distro = plan.spec.distribution.value
    for node in [*plan.workers, *reversed(plan.control_planes)]:
        ctx.check_cancelled()
        try:
            with connect(ctx, node.machine_id) as executor:
                marker = executor.run(f"cat /etc/rancher/{distro}/platform-cluster-id", CommandOptions(sudo=True))
                if not marker.ok:
                    continue
                if marker.stdout.strip() != str(plan.cluster_id):
                    raise StepFailedError(f"Refusing to uninstall {distro} owned by a different cluster")
                suffix = "-agent" if distro == "k3s" and node.role == "worker" else ""
                script = f"/usr/local/bin/{distro}{suffix}-uninstall.sh"
                command(executor, f"{script} && rm -f /etc/kubernetes/admin.conf", 600)
        except Exception:
            if not plan.force:
                raise
            ctx.warning(f"[{node.name}] uninstall failed; registration will be removed because force was selected")
    return {"uninstalled": [n.name for n in plan.nodes]}


def upgrade(ctx: StepContext) -> dict[str, Any]:
    plan: ClusterPlanInput = ctx.params  # type: ignore[assignment]
    target = plan.target_version
    if not target:
        raise StepFailedError("An exact target version is required")
    distro = plan.spec.distribution.value
    for node in [*plan.control_planes, *plan.workers]:
        ctx.check_cancelled()
        version = release(plan, target)
        with connect(ctx, plan.lead.machine_id) as control:
            drain = (
                f"kubectl --kubeconfig /etc/kubernetes/admin.conf drain {shlex.quote(node.name)} "
                "--ignore-daemonsets --timeout=300s"
            )
            if plan.allow_emptydir_data:
                drain += " --delete-emptydir-data"
            command(control, drain, 360)
        # Preserve the exact installed config and role; never bootstrap a fresh datastore.
        with connect(ctx, node.machine_id) as executor:
            marker = command(executor, f"cat /etc/rancher/{distro}/platform-cluster-id").strip()
            if marker != str(plan.cluster_id):
                raise StepFailedError(f"{node.name} does not belong to this cluster")
            repository = "k3s-io/k3s" if distro == "k3s" else "rancher/rke2"
            installer = f"https://raw.githubusercontent.com/{repository}/{version}/install.sh"
            role = "server" if node.role == "control_plane" else "agent"
            env = (
                {"INSTALL_K3S_VERSION": version, "INSTALL_K3S_EXEC": role}
                if distro == "k3s"
                else {"INSTALL_RKE2_VERSION": version, "INSTALL_RKE2_TYPE": role, "INSTALL_RKE2_METHOD": "tar"}
            )
            service = ("k3s" if role == "server" else "k3s-agent") if distro == "k3s" else f"rke2-{role}"
            result = run_script(
                executor,
                "set -eu\ninstaller=$(mktemp)\ntrap 'rm -f \"$installer\"' EXIT\n"
                f"curl -fsSL --proto '=https' {shlex.quote(installer)} -o \"$installer\"\n"
                f'sh "$installer"\nsystemctl restart {service}\n',
                env=env,
            )
            if not result.ok:
                raise StepFailedError(f"Upgrade failed on {node.name}: {tail(result)}; node remains cordoned")
        with connect(ctx, plan.lead.machine_id) as control:
            deadline = time.monotonic() + 600
            while time.monotonic() < deadline:
                ctx.check_cancelled()
                try:
                    observed = json.loads(
                        command(
                            control,
                            "kubectl --kubeconfig /etc/kubernetes/admin.conf get node "
                            f"{shlex.quote(node.name)} -o json",
                        )
                    )
                except StepFailedError:
                    # An API server restarting on the anchor temporarily rejects kubectl.
                    time.sleep(5)
                    continue
                actual = observed["status"]["nodeInfo"]["kubeletVersion"]
                ready = any(
                    c.get("type") == "Ready" and c.get("status") == "True"
                    for c in observed["status"].get("conditions", [])
                )
                if actual == version and ready:
                    break
                time.sleep(5)
            else:
                raise StepFailedError(f"{node.name} did not become Ready on {version}; node remains cordoned")
            command(control, f"kubectl --kubeconfig /etc/kubernetes/admin.conf uncordon {shlex.quote(node.name)}")
    return {"version": "v" + target.removeprefix("v")}
