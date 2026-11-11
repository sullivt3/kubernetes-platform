"""Run the packaged Bash installers against isolated filesystems and command stubs.

The OS/package-manager commands are simulated, not the Bash branching/configuration.
No script under test can write to the host's /etc, /proc, /run or /var trees.
Real Fedora package verification is provided by scripts/check-fedora.sh separately.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.modules.cluster_lifecycle.distributions import rancher_steps
from app.modules.cluster_lifecycle.distributions.kubeadm.steps import _script
from app.modules.cluster_lifecycle.distributions.linux import swap_script
from app.modules.infrastructure.facts import CheckStatus, evaluate, os_supported
from app.modules.infrastructure.host_access import CommandResult

STUB = r"""
import json, os, pathlib, sys
command = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
root = pathlib.Path(os.environ['TEST_ROOT'])
with (root / 'calls.jsonl').open('a') as stream:
    stream.write(json.dumps([command, args]) + '\n')
if command == 'id':
    print('0')
elif command in {'dnf', 'dnf5'}:
    if command == 'dnf5' and any('--disableexcludes' in a for a in args):
        sys.exit(2)
    if command == 'dnf' and '--setopt=disable_excludes=kubernetes' in args:
        sys.exit(2)
    if 'repoquery' in args:
        if os.environ.get('QUERY_FAIL'):
            print('Repository unavailable', file=sys.stderr)
            sys.exit(1)
        if args[-1] == 'kubeadm-1.34.2-*':
            print('1.34.2-1.1')
        elif args[-1] == 'kubeadm':
            print('1.34.12-1.1')
    elif 'list' in args:
        print('Available Packages\nkubeadm.x86_64 1.34.2-1.1 kubernetes\nkubeadm.x86_64 1.34.12-1.1 kubernetes')
elif command == 'sort':
    # macOS sort has no -V; emulate the Linux command on the DNF4 test fixture.
    import re
    print('\n'.join(sorted(sys.stdin.read().splitlines(), key=lambda v: [int(n) for n in re.findall(r'\d+', v)])))
elif command == 'curl':
    if os.environ.get('CURL_FAIL'):
        sys.exit(22)
    output = pathlib.Path(args[args.index('-o') + 1])
    output.write_text('downloaded-key-or-repository\n')
elif command == 'dpkg':
    print('amd64')
elif command == 'containerd':
    if args == ['config', 'default']:
        print('version = ' + os.environ.get('CONTAINERD_CONFIG_VERSION', '3'))
        print('SystemdCgroup = false')
    elif args == ['--version']:
        print('containerd containerd.io v2.1.0 test')
elif command == 'ctr':
    if os.environ.get('CTR_FAIL'):
        sys.exit(1)
elif command == 'kubeadm':
    print('v1.34.2')
elif command == 'kubelet':
    print('Kubernetes v1.34.2')
elif command == 'systemctl':
    if 'list-units' in args:
        print('dev-zram0.swap loaded active active /dev/zram0')
    elif 'is-active' in args:
        sys.exit(0 if args[-1] == 'firewalld' else 1)
elif command == 'swapon':
    if os.environ.get('SWAP_STUCK'):
        print('/dev/zram0 partition 1G 0B 100')
elif command == 'getenforce':
    print(os.environ.get('SELINUX_STATE', 'Enforcing'))
elif command == 'sysctl':
    pass
elif command == 'rpm' and os.environ.get('POLICY_MISSING'):
    sys.exit(1)
elif command == 'sed':
    # BSD sed differs from GNU sed; apply the real expressions with a portable shim.
    args = [a.replace('-ri', '-Ei') for a in args]
    if '-Ei' in args:
        index = args.index('-Ei')
        args[index:index + 1] = ['-E', '-i', '']
    sys.exit(__import__('subprocess').call(['/usr/bin/sed', *args]))
"""


class ShellHost:
    def __init__(self, root: Path, os_id: str = "fedora", dnf5: bool = True):
        self.root = root
        self.os_id = os_id
        self.dnf5 = dnf5
        self.bin = root / "bin"
        self.bin.mkdir()
        for directory in (
            "etc/yum.repos.d",
            "etc/apt/keyrings",
            "etc/apt/sources.list.d",
            "etc/selinux",
            "proc/sys/net/bridge",
            "proc/sys/net/ipv4",
            "run",
            "var",
            "usr/lib/systemd/system-generators",
        ):
            (root / directory).mkdir(parents=True, exist_ok=True)
        (root / "etc/os-release").write_text(
            f'ID={os_id}\nVERSION_ID=45\nVERSION_CODENAME=noble\nID_LIKE="rhel fedora"\n'
        )
        (root / "etc/fstab").write_text("/dev/vda1 / ext4 defaults 0 1\n/swapfile none swap defaults 0 0\n")
        (root / "etc/selinux/config").write_text("SELINUX=enforcing\n")
        (root / "proc/sys/net/bridge/bridge-nf-call-iptables").write_text("1\n")
        (root / "proc/sys/net/ipv4/ip_forward").write_text("1\n")
        (root / "usr/lib/systemd/system-generators/zram-generator").write_text("vendor generator")
        (root / "usr/lib/systemd/system-generators/zram-generator").chmod(0o755)
        # Do not stub file utilities: the actual installer creates configs/symlinks.
        commands = [
            "id",
            "dnf",
            "sort",
            "curl",
            "dpkg",
            "containerd",
            "ctr",
            "kubeadm",
            "kubelet",
            "systemctl",
            "swapon",
            "swapoff",
            "getenforce",
            "setenforce",
            "sysctl",
            "modprobe",
            "firewall-cmd",
            "apt-get",
            "apt-mark",
            "rpm",
            "ufw",
        ]
        if dnf5:
            commands.append("dnf5")
        if sys.platform == "darwin":
            commands.append("sed")
        for command in commands:
            path = self.bin / command
            path.write_text(f"#!{sys.executable}\n" + STUB)
            path.chmod(0o755)

    def run(self, script: str, **env: str) -> subprocess.CompletedProcess[str]:
        # Retarget every absolute host path before executing any code.
        script = re.sub(r"/(?:etc|proc|run|var|usr/lib)/", lambda match: str(self.root) + match.group(), script)
        script = "\n".join(line for line in script.splitlines() if not line.startswith("export PATH="))
        script = (
            """command() {
  if [ "$1" = -v ]; then
    case "$2" in
      apt-get) [ "$TEST_APT" = 1 ] || return 1 ;;
      dnf5) [ "$TEST_DNF5" = 1 ] || return 1 ;;
      ufw) return 1 ;;
    esac
  fi
  builtin command "$@"
}
"""
            + script
        )
        return subprocess.run(
            [shutil.which("bash") or "bash", "-c", script],
            env={
                **os.environ,
                "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
                "TEST_ROOT": str(self.root),
                "TEST_APT": "1" if self.os_id in {"ubuntu", "debian"} else "0",
                "TEST_DNF5": "1" if self.dnf5 else "0",
                **env,
            },
            capture_output=True,
            text=True,
            timeout=30,
        )

    def calls(self, name: str) -> list[list[str]]:
        path = self.root / "calls.jsonl"
        return [
            args for command, args in (json.loads(line) for line in path.read_text().splitlines()) if command == name
        ]


@pytest.mark.parametrize("version", ["43", "44", "45"])
def test_fedora_inspection_accepts_maintained_versions(version: str) -> None:
    facts = {"os_id": "fedora", "os_version_id": version, "selinux": "Enforcing"}
    assert os_supported(facts)
    checks = {check.key: check for check in evaluate(facts, sudo_ok=True)}
    assert checks["os"].status is CheckStatus.PASS
    assert checks["selinux"].status is CheckStatus.WARN


@pytest.mark.parametrize(
    "facts",
    [
        {"os_id": "fedora", "os_version_id": "42"},
        {"os_id": "fedora", "os_version_id": "46"},
        {"os_id": "fedora", "os_version_id": "45", "immutable_os": "yes"},
    ],
)
def test_unvalidated_or_immutable_fedora_is_not_accepted(facts: dict[str, str]) -> None:
    assert not os_supported(facts)


@pytest.mark.parametrize(
    ("os_id", "repository"),
    [
        ("fedora", "fedora"),
        ("rhel", "rhel"),
        ("rocky", "centos"),
        ("almalinux", "centos"),
        ("centos", "centos"),
        ("ubuntu", "ubuntu"),
        ("debian", "debian"),
    ],
)
@pytest.mark.parametrize("config_version", ["2", "3"])
def test_containerd_installer_uses_os_repository_and_systemd_cgroups(
    tmp_path: Path,
    os_id: str,
    repository: str,
    config_version: str,
) -> None:
    host = ShellHost(tmp_path, os_id)
    result = host.run(_script("containerd.sh"), CONTAINERD_CONFIG_VERSION=config_version)
    assert result.returncode == 0, result.stderr
    assert any(f"https://download.docker.com/linux/{repository}/" in arg for args in host.calls("curl") for arg in args)
    config = (tmp_path / "etc/containerd/config.toml").read_text()
    assert f"version = {config_version}" in config and "SystemdCgroup = true" in config
    assert ["restart", "containerd"] in host.calls("systemctl")
    assert not any(arg == "docker-ce" for args in host.calls("dnf5") for arg in args)
    if repository in {"fedora", "rhel", "centos"}:
        assert any("containerd.io" in args for args in host.calls("dnf5"))


def test_repository_download_failure_stops_before_install(tmp_path: Path) -> None:
    host = ShellHost(tmp_path)
    result = host.run(_script("containerd.sh"), CURL_FAIL="1")
    assert result.returncode != 0
    assert host.calls("curl")
    assert host.calls("dnf5") == []


def test_unrecognised_rpm_flavor_does_not_fall_back_to_centos(tmp_path: Path) -> None:
    host = ShellHost(tmp_path, os_id="unknown-rhel-derivative")
    result = host.run(_script("containerd.sh"))
    assert result.returncode != 0 and "no containerd repository configured" in result.stderr
    assert host.calls("dnf5") == []


def test_containerd_retry_preserves_original_configuration_backup(tmp_path: Path) -> None:
    host = ShellHost(tmp_path)
    config = tmp_path / "etc/containerd/config.toml"
    config.parent.mkdir()
    config.write_text("# administrator configuration\nversion = 2\n")
    for _ in range(2):
        result = host.run(_script("containerd.sh"))
        assert result.returncode == 0, result.stderr
    backup = config.with_name("config.toml.before-kplatform")
    assert backup.read_text() == "# administrator configuration\nversion = 2\n"
    assert "managed by kubernetes-platform" in config.read_text()


@pytest.mark.parametrize("dnf5", [True, False])
@pytest.mark.parametrize("mode", ["install", "upgrade-kubeadm", "upgrade-kubelet"])
def test_rpm_install_and_upgrade_pin_exact_patch(tmp_path: Path, dnf5: bool, mode: str) -> None:
    host = ShellHost(tmp_path, dnf5=dnf5)
    result = host.run(_script("packages.sh"), K8S_MINOR="1.34", K8S_VERSION="1.34.2", MODE=mode)
    assert result.returncode == 0, result.stderr
    calls = host.calls("dnf5" if dnf5 else "dnf")
    expected_option = "--setopt=disable_excludes=kubernetes" if dnf5 else "--disableexcludes=kubernetes"
    assert all(expected_option in args for args in calls)
    installed = next(args for args in calls if "install" in args)
    expected = {
        "install": ["kubeadm", "kubelet", "kubectl"],
        "upgrade-kubeadm": ["kubeadm"],
        "upgrade-kubelet": ["kubelet", "kubectl"],
    }[mode]
    assert [arg for arg in installed if arg.endswith("-1.34.2-1.1")] == [f"{name}-1.34.2-1.1" for name in expected]
    assert ("cri-tools" in installed) is (mode == "install")


def test_dnf5_resolves_latest_patch_and_preserves_repository_errors(tmp_path: Path) -> None:
    host = ShellHost(tmp_path)
    result = host.run(_script("packages.sh"), K8S_MINOR="1.34", MODE="resolve")
    assert result.returncode == 0 and "RESOLVED_VERSION=1.34.12" in result.stdout
    assert "--repo=kubernetes" in host.calls("dnf5")[0]
    failed = host.run(_script("packages.sh"), K8S_MINOR="1.34", MODE="resolve", QUERY_FAIL="1")
    assert failed.returncode != 0 and "Repository unavailable" in failed.stderr


@pytest.mark.parametrize("allow_swap", [True, False])
@pytest.mark.parametrize("selinux", ["Enforcing", "Permissive"])
def test_fedora_preparation_persists_swap_policy_and_reconciles_selinux(
    tmp_path: Path,
    allow_swap: bool,
    selinux: str,
) -> None:
    host = ShellHost(tmp_path)
    existing = tmp_path / "etc/systemd/zram-generator.conf"
    existing.parent.mkdir(exist_ok=True)
    existing.write_text("[zram0]\nzram-size=ram / 2\n")
    env = {
        "DISABLE_SWAP": "0" if allow_swap else "1",
        "NODE_ROLE": "control_plane",
        "CNI": "flannel",
        "SELINUX_STATE": selinux,
    }
    for _ in range(2):
        result = host.run(_script("prepare.sh"), **env)
        assert result.returncode == 0, result.stderr
    mask = tmp_path / "etc/systemd/system-generators/zram-generator"
    assert mask.is_symlink() is (not allow_swap)
    assert existing.read_text() == "[zram0]\nzram-size=ram / 2\n"
    assert (tmp_path / "etc/selinux/config").read_text() == "SELINUX=permissive\n"
    if not allow_swap:
        assert os.readlink(mask) == "/dev/null"
        assert ["mask", "--now", "dev-zram0.swap"] in host.calls("systemctl")
        assert "# disabled by kubernetes-platform: /swapfile" in (tmp_path / "etc/fstab").read_text()
    else:
        assert host.calls("swapoff") == []
    assert any("iptables-nft" in args for args in host.calls("dnf5"))
    assert ["-q", "--permanent", "--add-port=6443/tcp"] in host.calls("firewall-cmd")
    assert ["-q", "--permanent", "--add-port=8472/udp"] in host.calls("firewall-cmd")


def test_swap_failure_is_fatal_for_every_installer(tmp_path: Path) -> None:
    host = ShellHost(tmp_path)
    result = host.run("set -e\n" + swap_script() + "\ndisable_swap", SWAP_STUCK="1")
    assert result.returncode != 0 and "swap is still active" in result.stderr


def test_shell_prelude_rejects_immutable_hosts(tmp_path: Path) -> None:
    host = ShellHost(tmp_path)
    (tmp_path / "run/ostree-booted").touch()
    result = host.run(_script("containerd.sh"))
    assert result.returncode != 0 and "immutable images" in result.stderr


@pytest.mark.parametrize("distribution", ["k3s", "rke2"])
@pytest.mark.parametrize("policy_installed", [True, False])
def test_native_fedora_preparation_and_selinux_policy_requirement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    distribution: str,
    policy_installed: bool,
) -> None:
    captured = []

    def capture(_executor, script, **kwargs):
        captured.append((script, kwargs["env"]))
        return CommandResult("bash -s", 0, "", "", 0)

    monkeypatch.setattr(rancher_steps, "connect", lambda *_: nullcontext(object()))
    monkeypatch.setattr(rancher_steps, "run_script", capture)
    node = SimpleNamespace(machine_id="test", name="fedora")
    context = SimpleNamespace(
        params=SimpleNamespace(spec=SimpleNamespace(distribution=SimpleNamespace(value=distribution)), nodes=[node]),
        check_cancelled=lambda: None,
    )
    rancher_steps.prepare(context)
    script, env = captured[0]
    host = ShellHost(tmp_path)
    result = host.run(script, **env, **({} if policy_installed else {"POLICY_MISSING": "1"}))
    if not policy_installed:
        assert result.returncode != 0 and "signed SELinux policy" in result.stdout + result.stderr
        assert host.calls("dnf") == []
        return
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "etc/modules-load.d/kplatform-k8s.conf").read_text() == "overlay\nbr_netfilter\n"
    assert (tmp_path / "etc/sysctl.d/99-kplatform-k8s.conf").read_text() == (
        "net.ipv4.ip_forward=1\nnet.bridge.bridge-nf-call-iptables=1\n"
    )
    assert (tmp_path / "etc/systemd/system-generators/zram-generator").is_symlink()
    assert host.calls("setenforce") == []
    assert any("iptables-nft" in args for args in host.calls("dnf"))
