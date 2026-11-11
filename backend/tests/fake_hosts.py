"""In-memory fake Linux servers for testing machine and cluster workflows without SSH.

Each FakeHost recognises the platform's scripts by their header comment, records what
ran (with the exported environment), and simulates the effects that later steps rely
on (installed version, kubeadm init/join state, node readiness).
"""

from __future__ import annotations

import base64
import json
import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass, field

from app.modules.infrastructure.host_access import (
    CommandOptions,
    CommandResult,
    HostEndpoint,
    HostExecutor,
    HostKey,
    HostKeyMismatchError,
    HostUnreachableError,
)
from app.modules.infrastructure.ssh import SshAuth

SCRIPT_MARKERS = {
    "Prepare the operating system": "prepare",
    "Install and configure containerd": "containerd",
    "Configure the pkgs.k8s.io repository": "packages",
    "Initialize the first control-plane node": "init",
    "Install the pod network": "cni",
    "Join this node to the cluster": "join",
    "Remove Kubernetes state": "reset",
    "Install or update the kube-vip static pod": "kube_vip",
}


@dataclass
class FakeHost:
    name: str
    ip: str
    host_key: HostKey
    cpus: int = 4
    memory_mb: int = 4096
    os_id: str = "ubuntu"
    os_version: str = "24.04"
    reachable: bool = True
    sudo_ok: bool = True
    joined: bool = False
    initialized: bool = False
    installed_version: str | None = None
    calls: list[tuple[str, dict[str, str]]] = field(default_factory=list)
    fail_next: dict[str, int] = field(default_factory=dict)  # script -> remaining failures
    secrets_seen: list[str] = field(default_factory=list)
    kube_vip_manifest: str | None = None
    control_plane: bool = False


class FakeCluster:
    """Shared view of which nodes have joined (what `kubectl get nodes` reports)."""

    def __init__(self) -> None:
        self.hosts: dict[str, FakeHost] = {}  # "host:port" -> FakeHost
        self.dns_works = True
        self.control_plane_workloads = False  # whether control planes carry the NoSchedule taint
        self.etcd_removed: list[str] = []
        self.deleted_nodes: list[str] = []
        self.drained: list[str] = []
        self.smoke_pods: list[str] = []

    def add(self, address: str, host: FakeHost) -> FakeHost:
        self.hosts[address] = host
        return host

    def members(self) -> list[FakeHost]:
        return [h for h in self.hosts.values() if h.joined or h.initialized]

    def connector(self) -> Callable[[HostEndpoint, SshAuth], HostExecutor]:
        def connect(endpoint: HostEndpoint, auth: SshAuth) -> HostExecutor:
            host = self.hosts.get(f"{endpoint.host}:{endpoint.port}")
            if host is None or not host.reachable:
                raise HostUnreachableError(f"Cannot connect to {endpoint.host}:{endpoint.port}: timed out")
            if endpoint.host_key is not None and endpoint.host_key.data != host.host_key.data:
                raise HostKeyMismatchError("Host key changed", observed=host.host_key)
            return FakeExecutor(self, host, endpoint)

        return connect


def _env(script: str) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in script.splitlines():
        if line.startswith("export ") and "=" in line:
            key, _, value = line[len("export ") :].partition("=")
            env[key] = shlex.split(value)[0] if value else ""
    return env


class FakeExecutor(HostExecutor):
    def __init__(self, cluster: FakeCluster, host: FakeHost, endpoint: HostEndpoint) -> None:
        self.cluster = cluster
        self.host = host
        self.endpoint = endpoint
        self.observed_host_key = host.host_key

    def _result(self, command: str, out: str = "", err: str = "", code: int = 0) -> CommandResult:
        return CommandResult(command=command, exit_code=code, stdout=out, stderr=err, duration_seconds=0.01)

    def run(
        self,
        command: str,
        options: CommandOptions | None = None,
        on_output: Callable[[str], None] | None = None,
    ) -> CommandResult:
        options = options or CommandOptions()
        h = self.host
        if options.sudo and not h.sudo_ok:
            return self._result(command, err="sudo: a password is required", code=1)
        if command == "true":
            return self._result(command)
        if command == "bash -s":
            return self._script(options.stdin or "", on_output)
        if command.startswith("kubeadm token create"):
            token = command.split()[3]
            h.secrets_seen.append(token)
            if on_output:
                on_output(f"created token {token}")
            return self._result(command, out=token)
        if command.startswith("kubeadm init phase upload-certs"):
            return self._result(command, out="[upload-certs] Using certificate key:\n" + command.split()[-1])
        if command.startswith("kubectl"):
            return self._kubectl(command)
        if command == "ip -o -4 addr show":
            return self._result(
                command,
                out=f"1: lo    inet 127.0.0.1/8 scope host lo\n2: eth0    inet {h.ip}/24 brd x scope global eth0\n",
            )
        if command.startswith("openssl x509 -pubkey"):
            return self._result(command, out="ab" * 32 + "\n") if h.initialized else self._result(command, code=1)
        if command.startswith("journalctl -u "):
            return self._result(command, out=f"{h.name} {command.split()[2]}: started\n")
        return self._result(command, err=f"fake: unknown command {command}", code=127)

    def _script(self, stdin: str, on_output: Callable[[str], None] | None) -> CommandResult:
        h = self.host
        if "kv os_id" in stdin:  # detect.sh
            facts = {
                "os_id": h.os_id,
                "os_version_id": h.os_version,
                "os_pretty_name": f"{h.os_id} {h.os_version}",
                "hostname": h.name,
                "kernel": "6.8.0",
                "arch": "x86_64",
                "cpu_cores": str(h.cpus),
                "memory_mb": str(h.memory_mb),
                "swap_mb": "0",
                "all_ipv4": h.ip,
                "primary_ip": h.ip,
                "systemd": "yes",
                "package_manager": "dnf5" if h.os_id == "fedora" else "apt",
                "root_disk_gb": "100",
                "root_disk_free_gb": "80",
                "cgroup_version": "2",
                "virtualization": "kvm",
                "selinux": "Enforcing" if h.os_id == "fedora" else "absent",
                "nameservers": "10.0.0.2",
                "systemd_resolved": "inactive",
                "kubeadm_version": f"v{h.installed_version}" if h.installed_version else "",
                "has_admin_conf": "yes" if h.initialized else "no",
                "has_kubelet_conf": "yes" if (h.joined or h.initialized) else "no",
                "internet": "302",
            }
            return self._result("bash -s", out="\n".join(f"{k}={v}" for k, v in facts.items()))

        script = next((name for marker, name in SCRIPT_MARKERS.items() if marker in stdin), None)
        if script is None:
            return self._result("bash -s", err="fake: unknown script", code=2)
        env = _env(stdin)
        h.calls.append((script, env))
        if h.fail_next.get(script, 0) > 0:
            h.fail_next[script] -= 1
            return self._result("bash -s", err=f"{script}: simulated failure", code=1)

        out: list[str] = []
        if "CONFIG_B64" in env:
            config = base64.b64decode(env["CONFIG_B64"]).decode()
            for token in re.findall(r'"token": "([a-z0-9]{6}\.[a-z0-9]{16})"', config):
                h.secrets_seen.append(token)
                out.append(f"kubeadm join {h.ip}:6443 --token {token}")  # what kubeadm really prints
        if script == "packages":
            if env.get("MODE") == "resolve":
                out.append(f"RESOLVED_VERSION={env.get('K8S_VERSION') or env['K8S_MINOR'] + '.7'}")
            else:
                h.installed_version = env["K8S_VERSION"]
                out.append(f"Installed Kubernetes components v{h.installed_version}")
        elif script == "init":
            h.initialized = h.control_plane = True
            out.append("CA_CERT_HASH=sha256:" + "ab" * 32)
        elif script == "join":
            h.joined = True
            h.control_plane = '"controlPlane"' in base64.b64decode(env["CONFIG_B64"]).decode()
        elif script == "reset":
            h.joined = h.initialized = h.control_plane = False
            h.kube_vip_manifest = None
        elif script == "kube_vip":
            h.kube_vip_manifest = base64.b64decode(env["MANIFEST_B64"]).decode()
        for line in out:
            if on_output:
                on_output(line)
        return self._result("bash -s", out="\n".join(out))

    def _kubectl(self, command: str) -> CommandResult:
        if "get nodes -o json" in command:
            items = [
                {
                    "metadata": {"name": node_name},
                    "status": {
                        "conditions": [{"type": "Ready", "status": "True"}],
                        "nodeInfo": {
                            "kubeletVersion": f"v{h.installed_version}",
                            "osImage": h.os_id,
                            "containerRuntimeVersion": "containerd://2.0.0",
                        },
                    },
                }
                for h in self.cluster.members()
                for node_name in [h.name]
            ]
            return self._result(command, out=json.dumps({"items": items}))
        if "get deployment coredns -o json" in command:
            return self._result(command, out=json.dumps({"status": {"replicas": 2, "availableReplicas": 2}}))
        words = command.split()
        if " run kplatform-smoke-" in command:
            self.cluster.smoke_pods.append(words[words.index("run") + 1])
            return self._result(command, out="pod created")
        if "get pod kplatform-smoke-" in command:
            phase = "Succeeded" if self.cluster.dns_works else "Failed"
            return self._result(
                command, out=json.dumps({"status": {"phase": phase}, "spec": {"nodeName": self.host.name}})
            )
        if " logs kplatform-smoke-" in command:
            if not self.cluster.dns_works:
                return self._result(command, out=";; connection timed out; no servers could be reached")
            return self._result(
                command,
                out="Server: 10.96.0.10\nName: kubernetes.default.svc.cluster.local\nAddress: 10.96.0.1\n",
            )
        if " drain " in command:
            self.cluster.drained.append(words[words.index("drain") + 1])
            return self._result(command, out="node drained")
        if " get node " in command and "-o json" in command:
            name = words[words.index("node") + 1]
            taints = (
                []
                if self.cluster.control_plane_workloads
                else [{"key": "node-role.kubernetes.io/control-plane", "effect": "NoSchedule"}]
            )
            return self._result(command, out=json.dumps({"metadata": {"name": name}, "spec": {"taints": taints}}))
        if "member list -w json" in command:
            members = [
                {"ID": 1000 + i, "name": x.name}
                for i, x in enumerate(self.cluster.hosts.values())
                if x.control_plane and (x.initialized or x.joined)
            ]
            return self._result(command, out=json.dumps({"members": members}))
        if " member remove " in command:
            self.cluster.etcd_removed.append(words[-1])
            return self._result(command, out="Member removed")
        if " delete node " in command:
            self.cluster.deleted_nodes.append(words[words.index("node") + 1])
            return self._result(command, out="node deleted")
        return self._result(command, out="ok")

    def put_file(self, content: bytes, remote_path: str, *, mode: int = 0o600, sudo: bool = False) -> None:
        return None

    def get_file(self, remote_path: str, *, sudo: bool = False) -> bytes:
        if remote_path == "/etc/kubernetes/admin.conf" and self.host.initialized:
            return (
                "apiVersion: v1\nclusters:\n- cluster: {server: https://" + self.host.ip + ":6443}\n  name: lab\n"
                "contexts:\n- context: {cluster: lab, user: admin}\n  name: admin@lab\n"
                "users:\n- name: admin\n  user:\n    client-key-data: SECRETKEYDATA\n"
            ).encode()
        raise FileNotFoundError(remote_path)

    def close(self) -> None:
        return None


def host_key(seed: str) -> HostKey:
    data = base64.b64encode(f"fake-key-{seed}".encode()).decode()
    return HostKey(key_type="ssh-ed25519", data=data, fingerprint=f"SHA256:{seed}")
