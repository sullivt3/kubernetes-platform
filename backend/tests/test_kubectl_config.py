"""Opt-in user kubeconfig provisioning, including real kubectl config merging."""

from __future__ import annotations

import base64
import json
import os
import shutil
import stat
import subprocess
import sys
from importlib import resources
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.modules.cluster_lifecycle.distributions.kubeadm.config import KubeadmOptions
from tests.fake_hosts import FakeCluster
from tests.test_cluster_lifecycle import API, all_logs, spec
from tests.test_cluster_lifecycle import fleet as fleet
from tests.test_cluster_lifecycle import machines as machines


@pytest.mark.parametrize("username", ["", "../root", "alice;id", "--help", "alice\nroot", "a" * 33])
def test_invalid_username_rejected(username: str) -> None:
    with pytest.raises(ValidationError):
        KubeadmOptions(kubectl_username=username)


def test_opt_in_runs_after_smoke_test_and_only_on_first_control_plane(
    client: TestClient, machines: dict[str, str], fleet: FakeCluster
) -> None:
    created = client.post(
        f"{API}/clusters", json={"spec": spec(machines, options={"kubectl_username": "alice"})}
    ).json()
    op = created["operation"]
    assert op["status"] == "succeeded", op["error"]
    keys = [s["key"] for s in op["steps"]]
    assert keys.index("smoke-test") < keys.index("kubectl-config") < keys.index("register")
    access = op["result"]["kubectl_config"]
    assert access["username"] == "alice" and access["machine"] == "cp-1"
    assert created["cluster"]["id"] in access["context"]
    writes = [env for name, env in fleet.hosts["10.0.0.11:22"].calls if name == "kubectl_config"]
    assert len(writes) == 1
    payload = json.loads(base64.b64decode(writes[0]["CONFIG_B64"]))
    assert payload["current-context"] == access["context"]
    assert payload["users"][0]["name"] == payload["clusters"][0]["name"] == access["context"]
    assert all(not any(name == "kubectl_config" for name, _ in h.calls) for h in list(fleet.hosts.values())[1:])
    logs = all_logs(client, op["id"])
    assert "SECRETKEYDATA" not in logs and writes[0]["CONFIG_B64"] not in logs


def test_missing_account_fails_before_installation(
    client: TestClient, machines: dict[str, str], fleet: FakeCluster
) -> None:
    created = client.post(
        f"{API}/clusters", json={"spec": spec(machines, options={"kubectl_username": "missing"})}
    ).json()
    op = created["operation"]
    assert op["status"] == "failed" and op["error"]["step"] == "validate"
    assert all(not any(name == "prepare" for name, _ in h.calls) for h in fleet.hosts.values())


def test_config_retry_does_not_reinstall_cluster(
    client: TestClient, machines: dict[str, str], fleet: FakeCluster
) -> None:
    lead = fleet.hosts["10.0.0.11:22"]
    lead.fail_next["kubectl_config"] = 1
    created = client.post(f"{API}/clusters", json={"spec": spec(machines, options={"kubectl_username": "root"})}).json()
    op = created["operation"]
    assert op["status"] == "failed" and op["error"]["step"] == "kubectl-config"
    assert sum(name == "init" for name, _ in lead.calls) == 1
    retry = client.post(f"{API}/operations/{op['id']}/retry").json()
    assert retry["status"] == "succeeded", retry
    assert sum(name == "init" for name, _ in lead.calls) == 1


class ConfigHost:
    def __init__(self, root: Path):
        kubectl = shutil.which("kubectl")
        if kubectl is None:
            pytest.skip("Install kubectl for the real kubeconfig-merge shell tests")
        self.root = root
        self.home = root / "home"
        self.home.mkdir()
        self.bin = root / "bin"
        self.bin.mkdir()
        for name, body in {
            "getent": (
                "import os,sys; "
                "print(f'{sys.argv[2]}:x:{os.getuid()}:{os.getgid()}::'+os.environ['TEST_HOME']+':/bin/bash')"
            ),
            "chown": "pass",
            "runuser": "import os,sys; os.execvp(sys.argv[4],sys.argv[4:])",
        }.items():
            path = self.bin / name
            path.write_text(f"#!{sys.executable}\n{body}\n")
            path.chmod(0o755)
        (self.bin / "kubectl").symlink_to(kubectl)
        self.script = (
            'set -euo pipefail\nlog() { printf \'%s\\n\' "$*"; }\nfail() { echo "ERROR: $*" >&2; exit 1; }\n'
            + (
                resources.files("app.modules.cluster_lifecycle.distributions.kubeadm") / "scripts/kubectl_config.sh"
            ).read_text()
        )

    def run(self, **env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", "-s"],
            input=self.script,
            text=True,
            capture_output=True,
            env={
                **os.environ,
                "PATH": f"{self.bin}:{os.environ['PATH']}",
                "TEST_HOME": str(self.home),
                "TARGET_USER": "alice",
                "CONTEXT_NAME": "new-cluster",
                "CONFIG_B64": base64.b64encode(json.dumps(config("new-cluster")).encode()).decode(),
                **env,
            },
            timeout=30,
        )


def config(name: str) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [{"name": name, "cluster": {"server": "https://192.0.2.1:6443"}}],
        "users": [{"name": name, "user": {"token": "disposable-" + name}}],
        "contexts": [{"name": name, "context": {"cluster": name, "user": name}}],
        "current-context": name,
    }


def test_shell_creates_private_config_and_preserves_it_on_retry(tmp_path: Path) -> None:
    host = ConfigHost(tmp_path)
    result = host.run()
    assert result.returncode == 0, result.stderr
    target = host.home / ".kube/config"
    assert yaml.safe_load(target.read_text())["current-context"] == "new-cluster"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700
    before = target.read_bytes()
    assert host.run().returncode == 0
    assert target.read_bytes() == before
    assert list(target.parent.glob("config.backup.*")) == []
    assert "disposable-" not in result.stdout + result.stderr


def test_shell_merges_backs_up_and_keeps_current_context(tmp_path: Path) -> None:
    host = ConfigHost(tmp_path)
    directory = host.home / ".kube"
    directory.mkdir()
    target = directory / "config"
    original = json.dumps(config("old-cluster"))
    target.write_text(original)
    result = host.run()
    assert result.returncode == 0, result.stderr
    merged = yaml.safe_load(target.read_text())
    assert merged["current-context"] == "old-cluster"
    assert {c["name"] for c in merged["clusters"]} == {"old-cluster", "new-cluster"}
    backup = next(directory.glob("config.backup.*"))
    assert backup.read_text() == original and stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert host.run().returncode == 0
    assert len(list(directory.glob("config.backup.*"))) == 1


@pytest.mark.parametrize("mode", ["symlink-config", "symlink-directory", "malformed"])
def test_shell_does_not_overwrite_unsafe_or_invalid_configs(tmp_path: Path, mode: str) -> None:
    host = ConfigHost(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "config"
    sentinel.write_text("do-not-change")
    directory = host.home / ".kube"
    if mode == "symlink-directory":
        directory.symlink_to(outside, target_is_directory=True)
    else:
        directory.mkdir()
        if mode == "symlink-config":
            (directory / "config").symlink_to(sentinel)
        else:
            (directory / "config").write_text("contexts: [invalid")
    result = host.run()
    assert result.returncode != 0
    assert sentinel.read_text() == "do-not-change"
    if mode == "malformed":
        assert (directory / "config").read_text() == "contexts: [invalid"


def test_shell_preflight_does_not_create_config(tmp_path: Path) -> None:
    host = ConfigHost(tmp_path)
    assert host.run(CHECK_ONLY="1").returncode == 0
    assert not (host.home / ".kube").exists()
