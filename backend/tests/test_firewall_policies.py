"""Execute the packaged policy manager against a stateful firewalld client simulator."""

from __future__ import annotations

import base64
import copy
import json
import sys
import types

import pytest

from app.modules.cluster_lifecycle.distributions.kubeadm.steps import _script

CLUSTER = "12345678-1234-4567-8123-123456789abc"


class Settings:
    def __init__(self, settings=None):
        self.settings = {
            "description": "",
            "target": "CONTINUE",
            "priority": -1,
            "ingress_zones": [],
            "egress_zones": [],
            "ports": [],
            "rich_rules": [],
        }
        self.settings.update(settings or {})

    def getSettingsDict(self):
        return self.settings

    def getDescription(self):
        return self.settings["description"]


class Policy:
    def __init__(self, client, name):
        self.client, self.name = client, name

    def getSettings(self):
        return copy.deepcopy(self.client.saved[self.name])

    def update(self, settings):
        self.client.saved[self.name] = copy.deepcopy(settings)

    def remove(self):
        del self.client.saved[self.name]


class Firewall:
    def __init__(self):
        self.saved = {"unrelated": Settings({"description": "administrator", "rich_rules": ["existing-rule"]})}
        self.runtime = copy.deepcopy(self.saved)
        self.zones = {
            "public": Settings({"interfaces": ["br0"], "ports": [("2222", "tcp")]}),
            "libvirt": Settings({"interfaces": ["virbr0"], "services": ["dns"]}),
        }
        self.saved_zones = {z: Settings({"interfaces": []}) for z in self.zones}
        self.reloads = 0
        self.fail_reload = False

    def config(self):
        return self

    def getZones(self):
        return list(self.zones)

    def getZoneNames(self):
        return list(self.saved_zones)

    def listServices(self):
        return []

    def getServiceNames(self):
        return []

    def getIPSets(self):
        return []

    def getIPSetNames(self):
        return []

    def getZoneByName(self, zone):
        settings = copy.deepcopy(self.saved_zones[zone])
        return types.SimpleNamespace(getSettings=lambda: settings)

    def getPolicies(self):
        return list(self.runtime)

    def getPolicyNames(self):
        return list(self.saved)

    def getZoneSettings(self, zone):
        return copy.deepcopy(self.zones[zone])

    def getPolicySettings(self, policy):
        return copy.deepcopy(self.runtime[policy])

    def getPolicyByName(self, policy):
        return Policy(self, policy)

    def addPolicy(self, policy, settings):
        self.saved[policy] = copy.deepcopy(settings)

    def setZoneSettings(self, zone, settings):
        self.zones[zone] = copy.deepcopy(settings)

    def setPolicySettings(self, policy, settings):
        self.runtime[policy] = copy.deepcopy(settings)

    def reload(self):
        self.reloads += 1
        self.zones = copy.deepcopy(self.saved_zones)
        self.runtime = copy.deepcopy(self.saved)
        if self.fail_reload:
            self.fail_reload = False
            raise RuntimeError("reload failed")


@pytest.fixture
def manager(monkeypatch, tmp_path):
    fw = Firewall()
    module = types.ModuleType("firewall.client")
    module.FirewallClient = lambda: fw
    module.FirewallClientPolicySettings = Settings
    monkeypatch.setitem(sys.modules, "firewall", types.ModuleType("firewall"))
    monkeypatch.setitem(sys.modules, "firewall.client", module)
    import subprocess

    monkeypatch.setattr(subprocess, "check_output", lambda *a, **kw: "")
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: None)
    source = _script("firewall.sh").split("<<'KPLATFORM_FIREWALL_PY'\n", 1)[1].rsplit("KPLATFORM_FIREWALL_PY", 1)[0]
    source = source.replace("/run/lock/kplatform-firewall.lock", str(tmp_path / "firewall.lock"))

    def run(mode="apply", **overrides):
        config = {
            "cluster_id": CLUSTER,
            "pod_cidr": "10.244.0.0/16",
            "role": "worker",
            "cni": "calico",
            "peers": ["192.168.18.11"],
            **overrides,
        }
        monkeypatch.setenv("FIREWALL_CONFIG_B64", base64.b64encode(json.dumps(config).encode()).decode())
        monkeypatch.setenv("MODE", mode)
        exec(compile(source, "firewall.sh:policy-manager", "exec"), {})

    return fw, run


def test_scoped_rules_and_retry_preserve_runtime_vm_networking(manager):
    fw, run = manager
    before = copy.deepcopy(fw.zones)
    run()
    host = fw.saved["kh-123456781234"].settings
    pods = fw.saved["kp-123456781234"].settings
    assert host["rich_rules"] == ['rule family="ipv4" source address="192.168.18.11/32" protocol value="4" accept']
    assert host["egress_zones"] == ["HOST"]
    assert pods["egress_zones"] == ["ANY"] and pods["target"] == "CONTINUE"
    assert pods["rich_rules"] == [
        'rule family="ipv4" source address="10.244.0.0/16" destination address="10.244.0.0/16" accept'
    ]
    assert {z: v.settings for z, v in before.items()} == {z: v.settings for z, v in fw.zones.items()}
    assert fw.runtime["unrelated"].settings == fw.saved["unrelated"].settings
    run()
    assert fw.reloads == 1
    run(peers=["192.168.18.11", "192.168.18.12"])
    assert len(fw.runtime["kh-123456781234"].settings["rich_rules"]) == 2


def test_owned_policies_removed_without_deleting_unrelated_rules(manager):
    fw, run = manager
    run()
    run("remove")
    assert set(fw.saved) == set(fw.runtime) == {"unrelated"}
    run("remove")
    assert fw.reloads == 2


def test_foreign_policy_is_never_adopted(manager):
    fw, run = manager
    fw.saved["kh-123456781234"] = Settings({"description": "someone else"})
    with pytest.raises(RuntimeError, match="owned by someone else"):
        run()
    assert fw.reloads == 0 and fw.saved["kh-123456781234"].getDescription() == "someone else"


def test_reload_failure_rolls_back_and_restores_interfaces(manager):
    fw, run = manager
    original = {z: copy.deepcopy(v.settings) for z, v in fw.zones.items()}
    fw.fail_reload = True
    with pytest.raises(RuntimeError, match="reload failed"):
        run()
    assert set(fw.saved) == set(fw.runtime) == {"unrelated"}
    assert {z: v.settings for z, v in fw.zones.items()} == original
    run()
    assert "kp-123456781234" in fw.runtime


def test_runtime_only_policy_blocks_reload_before_mutation(manager):
    fw, run = manager
    fw.runtime["temporary"] = Settings()
    with pytest.raises(RuntimeError, match="Runtime-only zones/policies"):
        run()
    assert fw.reloads == 0 and set(fw.saved) == {"unrelated"}


@pytest.mark.parametrize(
    "overrides",
    [{"pod_cidr": "0.0.0.0/0"}, {"peers": ["1.2.3.4; false"]}, {"cluster_id": "../foreign"}, {"cni": "unknown"}],
)
def test_bad_inputs_do_not_change_firewall(manager, overrides):
    fw, run = manager
    with pytest.raises((ValueError, RuntimeError)):
        run(**overrides)
    assert fw.reloads == 0 and set(fw.saved) == {"unrelated"}


def test_flannel_opens_vxlan_without_ipip(manager):
    fw, run = manager
    run(cni="flannel", role="control_plane")
    host = fw.saved["kh-123456781234"].settings
    assert ("8472", "udp") in host["ports"] and ("6443", "tcp") in host["ports"]
    assert host["rich_rules"] == []


def test_node_add_and_remove_reconcile_peers_without_repreparing_existing_hosts(client, machines, fleet):
    from tests.test_cluster_lifecycle import API
    from tests.test_node_lifecycle import create, worker_spec

    cluster, op = create(client, worker_spec(machines))
    assert op["status"] == "succeeded", op["error"]
    added = client.post(
        f"{API}/clusters/{cluster['id']}/nodes",
        json={"pool": "general", "existing_machine_ids": [machines["worker-2"]]},
    ).json()
    assert added["status"] == "succeeded", added["error"]

    def config(host):
        env = [env for name, env in host.calls if name == "firewall"][-1]
        return env["MODE"], json.loads(base64.b64decode(env["FIREWALL_CONFIG_B64"]))

    cp = fleet.hosts["10.0.0.11:22"]
    assert config(cp)[1]["peers"] == ["10.0.0.12", "10.0.0.13"]
    assert sum(name == "prepare" for name, _ in cp.calls) == 1
    assert config(fleet.hosts["10.0.0.13:22"])[1]["peers"] == ["10.0.0.11", "10.0.0.12"]
    removed = client.delete(f"{API}/clusters/{cluster['id']}/nodes/worker-2?confirm=worker-2").json()
    assert removed["status"] == "succeeded", removed["error"]
    assert config(cp)[1]["peers"] == ["10.0.0.12"]
    assert config(fleet.hosts["10.0.0.13:22"])[0] == "remove"


# Real workflow fixtures, imported explicitly for pytest discovery.
from tests.test_cluster_lifecycle import fleet as fleet  # noqa: E402
from tests.test_cluster_lifecycle import machines as machines  # noqa: E402


def test_failed_peer_update_restores_previous_owned_policy(manager):
    fw, run = manager
    run()
    original = {p: copy.deepcopy(v.settings) for p, v in fw.saved.items()}
    fw.fail_reload = True
    with pytest.raises(RuntimeError, match="reload failed"):
        run(peers=["192.168.18.99"])
    assert {p: v.settings for p, v in fw.saved.items()} == original
    assert {p: v.settings for p, v in fw.runtime.items()} == original


def test_runtime_only_direct_rules_block_reload(manager, monkeypatch):
    fw, run = manager
    import subprocess

    monkeypatch.setattr(
        subprocess, "check_output", lambda args, **kw: "saved" if "--permanent" in args else "temporary"
    )
    with pytest.raises(RuntimeError, match="direct firewall rules"):
        run()
    assert fw.reloads == 0 and set(fw.saved) == {"unrelated"}


def test_unrelated_pending_policy_cannot_be_activated_as_a_side_effect(manager):
    fw, run = manager
    fw.saved["pending"] = Settings()
    with pytest.raises(RuntimeError, match="pending permanent policies"):
        run()
    assert fw.reloads == 0 and len(fw.saved) == 2


def test_already_correct_policies_do_not_reload_unrelated_temporary_objects(manager):
    fw, run = manager
    run()
    fw.runtime["temporary"] = Settings()
    run()
    assert fw.reloads == 1 and "temporary" in fw.runtime


def test_force_remove_defers_peer_cleanup_when_a_survivor_is_unreachable(client, machines, fleet):
    from tests.test_cluster_lifecycle import API, all_logs, spec
    from tests.test_node_lifecycle import create

    cluster, op = create(client, spec(machines))
    assert op["status"] == "succeeded", op["error"]
    fleet.hosts["10.0.0.12:22"].reachable = False
    removed = client.delete(f"{API}/clusters/{cluster['id']}/nodes/worker-2?confirm=worker-2&force=true").json()
    assert removed["status"] == "succeeded", removed["error"]
    assert "firewall peer cleanup is deferred" in all_logs(client, removed["id"])
