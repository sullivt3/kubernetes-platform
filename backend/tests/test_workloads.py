"""Phase 2: workload summaries, YAML plan/apply, actions, secrets, logs and terminal framing."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.modules.kubernetes import apply as kapply
from app.modules.kubernetes import client as kclient
from app.modules.workloads import summaries, terminal
from tests.fake_kube import kubeconfig_text
from tests.fake_store import FakeStore

API = "/api/v1"

DEPLOYMENT = {
    "apiVersion": "apps/v1",
    "kind": "Deployment",
    "metadata": {"name": "web", "namespace": "default", "labels": {"app": "web"}},
    "spec": {
        "replicas": 2,
        "selector": {"matchLabels": {"app": "web"}},
        "template": {
            "metadata": {"labels": {"app": "web"}},
            "spec": {"containers": [{"name": "nginx", "image": "nginx:1.27"}]},
        },
    },
}


# ----------------------------------------------------------------------------- pure logic


class TestSummaries:
    def test_deployment_rollout_states(self) -> None:
        base = {"metadata": {"generation": 3}, "spec": {"replicas": 3}}
        assert summaries.deployment_rollout({**base, "status": {"observedGeneration": 2}})["state"] == "progressing"
        assert (
            "1 of 3 replicas updated"
            in summaries.deployment_rollout(
                {**base, "status": {"observedGeneration": 3, "updatedReplicas": 1, "replicas": 3}}
            )["message"]
        )
        assert summaries.deployment_rollout(
            {**base, "status": {"observedGeneration": 3, "updatedReplicas": 3, "replicas": 4, "availableReplicas": 3}}
        )["message"].startswith("1 old replica")
        done = {"observedGeneration": 3, "updatedReplicas": 3, "replicas": 3, "availableReplicas": 3}
        assert summaries.deployment_rollout({**base, "status": done})["state"] == "complete"
        failed = {
            **done,
            "conditions": [{"type": "Progressing", "reason": "ProgressDeadlineExceeded", "message": "timed out"}],
        }
        assert summaries.deployment_rollout({**base, "status": failed})["state"] == "failed"
        assert summaries.deployment_rollout({**base, "spec": {"paused": True}, "status": done})["state"] == "paused"

    def test_rows(self) -> None:
        job = {
            "metadata": {"name": "j"},
            "spec": {"completions": 1},
            "status": {"conditions": [{"type": "Failed", "status": "True"}]},
        }
        assert summaries.summarize("jobs", job)["status"] == "Failed"
        svc = {
            "metadata": {"name": "s"},
            "spec": {
                "type": "NodePort",
                "clusterIP": "10.96.0.5",
                "ports": [{"port": 80, "nodePort": 30080, "protocol": "TCP"}],
            },
            "status": {},
        }
        assert summaries.summarize("services", svc)["ports"] == ["80:30080/TCP"]
        legacy_class = {
            "metadata": {"name": "legacy", "annotations": {"storageclass.beta.kubernetes.io/is-default-class": "true"}},
            "provisioner": "nfs.csi.k8s.io",
        }
        assert summaries.summarize("storageclasses", legacy_class)["default"] is True
        secret = {"metadata": {"name": "x"}, "type": "Opaque", "data": {"b": "eA==", "a": "eQ=="}}
        row = summaries.summarize("secrets", secret)
        assert row["keys"] == ["a", "b"] and "eA==" not in json.dumps(row)

    def test_edit_cleaning_and_redaction(self) -> None:
        obj = {
            "kind": "Secret",
            "metadata": {
                "name": "x",
                "uid": "u",
                "resourceVersion": "9",
                "managedFields": [{}],
                "annotations": {summaries.LAST_APPLIED: "{}"},
            },
            "data": {"k": "c2VjcmV0"},
            "status": {},
        }
        cleaned = summaries.clean_for_edit(summaries.redact_secret(obj))
        assert cleaned["data"] == {"k": summaries.REDACTED}
        assert "uid" not in cleaned["metadata"] and "annotations" not in cleaned["metadata"] and "status" not in cleaned
        assert obj["data"]["k"] == "c2VjcmV0"  # original untouched


class TestTerminalFraming:
    def test_exec_url(self) -> None:
        from app.modules.kubernetes.kubeconfig import ResolvedKubeconfig

        target = kclient.ConnectionTarget(ResolvedKubeconfig("c", "https://k8s:6443", "", None, "t", None, None))
        url = terminal.exec_url(target, "demo", "web-1", "nginx", ["/bin/sh", "-c", "exec sh"])
        assert url.startswith("wss://k8s:6443/api/v1/namespaces/demo/pods/web-1/exec?")
        assert "tty=true" in url and "container=nginx" in url and url.count("command=") == 3

    def test_frames(self) -> None:
        assert terminal.encode_input("ls\n") == b"\x00ls\n"
        assert json.loads(terminal.encode_resize(120, 30)[1:]) == {"Width": 120, "Height": 30}
        assert terminal.decode_frame(b"\x01hello") == {"type": "output", "data": "hello"}
        assert terminal.decode_frame(b"\x02oops") == {"type": "output", "data": "oops"}
        assert terminal.decode_frame(b'\x03{"status":"Success"}')["status"] == "Success"
        assert terminal.decode_frame(b"\x01") is None


class TestPlan:
    def test_create_update_unchanged_and_errors(self) -> None:
        store = FakeStore()
        created = kapply.plan_objects(store, [DEPLOYMENT], apply=True)
        assert created[0]["action"] == "create" and created[0]["applied"]
        assert kapply.plan_objects(store, [DEPLOYMENT])[0]["action"] == "unchanged"
        changed = json.loads(json.dumps(DEPLOYMENT))
        changed["spec"]["template"]["spec"]["containers"][0]["image"] = "nginx:1.28"
        plan = kapply.plan_objects(store, [changed])[0]
        assert (
            plan["action"] == "update"
            and "-      - image: nginx:1.27" in plan["diff"]
            and "+      - image: nginx:1.28" in plan["diff"]
        )
        assert (
            store.objects["/apis/apps/v1/namespaces/default/deployments/web"]["spec"]["template"]["spec"]["containers"][
                0
            ]["image"]
            == "nginx:1.27"
        )
        invalid = json.loads(json.dumps(DEPLOYMENT))
        invalid["spec"]["replicas"] = -1
        error = kapply.plan_objects(store, [invalid])[0]
        assert error["action"] == "error" and error["causes"][0]["field"] == "spec.replicas"

    def test_namespace_created_by_manifest(self) -> None:
        store = FakeStore()
        namespace = {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "demo"}}
        dep = json.loads(json.dumps(DEPLOYMENT))
        dep["metadata"]["namespace"] = "demo"
        plan = kapply.plan_objects(store, [dep, namespace])  # order is fixed by the planner
        assert [p["kind"] for p in plan] == ["Namespace", "Deployment"]
        assert plan[1]["action"] == "create" and "created by this manifest" in plan[1]["note"]
        applied = kapply.plan_objects(store, [dep, namespace], apply=True)
        assert all(p["applied"] for p in applied)

    def test_unknown_kind(self) -> None:
        widget = {"apiVersion": "example.com/v1", "kind": "Widget", "metadata": {"name": "w"}}
        result = kapply.plan_objects(FakeStore(), [widget])[0]
        assert result["action"] == "error" and "does not serve" in result["error"]


# ----------------------------------------------------------------------------- API


@pytest.fixture
def store() -> Iterator[FakeStore]:
    fake = FakeStore()
    kclient.set_client_factory(fake.factory)
    yield fake
    kclient.set_client_factory(None)


@pytest.fixture
def cluster_id(client: TestClient, store: FakeStore) -> str:
    response = client.post(f"{API}/clusters/import", json={"name": "prod", "kubeconfig": kubeconfig_text()})
    assert response.status_code == 202, response.text
    return response.json()["cluster_id"]


def apply(
    client: TestClient, cid: str, manifest: str, dry_run: bool = False, namespace: str = "default"
) -> dict[str, Any]:
    response = client.post(
        f"{API}/clusters/{cid}/apply", json={"manifest": manifest, "dry_run": dry_run, "namespace": namespace}
    )
    assert response.status_code == 200, response.text
    return response.json()


def audit_actions(client: TestClient) -> list[str]:
    return [e["action"] for e in client.get(f"{API}/audit-events", params={"limit": 50}).json()["items"]]


class TestWorkloadApi:
    def test_deployment_scaling_policy(self, client: TestClient, store: FakeStore, cluster_id: str) -> None:
        apply(client, cluster_id, json.dumps(DEPLOYMENT))
        response = client.put(
            f"{API}/clusters/{cluster_id}/deployments/default/web/scaling-policy",
            json={
                "minimum": 2,
                "maximum": 8,
                "metric": "cpu",
                "target_utilization": 65,
                "scale_down_stabilization_seconds": 420,
            },
        )
        assert response.status_code == 200, response.text
        hpa = next(value for key, value in store.objects.items() if "/horizontalpodautoscalers/" in key)
        assert hpa["apiVersion"] == "autoscaling/v2"
        assert hpa["spec"]["minReplicas"] == 2 and hpa["spec"]["maxReplicas"] == 8
        assert hpa["spec"]["metrics"][0]["resource"]["target"]["averageUtilization"] == 65
        assert hpa["spec"]["behavior"]["scaleDown"]["stabilizationWindowSeconds"] == 420
        assert "deployment.scaling-policy.create" in audit_actions(client)

        detail = client.get(f"{API}/clusters/{cluster_id}/resources/deployments/default/web")
        assert (
            detail.status_code == 200 and detail.json()["scaling_policy"]["metadata"]["name"] == hpa["metadata"]["name"]
        )

        removed = client.delete(f"{API}/clusters/{cluster_id}/deployments/default/web/scaling-policy")
        assert removed.status_code == 200 and removed.json()["deleted"]
        assert "deployment.scaling-policy.delete" in audit_actions(client)

    def test_apply_dry_run_then_apply(self, client: TestClient, store: FakeStore, cluster_id: str) -> None:
        manifest = json.dumps(DEPLOYMENT)
        plan = apply(client, cluster_id, manifest, dry_run=True)
        assert plan["dry_run"] and plan["results"][0]["action"] == "create" and not plan["results"][0]["applied"]
        assert "/apis/apps/v1/namespaces/default/deployments/web" not in store.objects
        assert "kubernetes.apply" not in audit_actions(client)
        result = apply(client, cluster_id, manifest)
        assert result["ok"] and result["results"][0]["applied"]
        assert "kubernetes.apply" in audit_actions(client)

    def test_list_get_and_history(self, client: TestClient, store: FakeStore, cluster_id: str) -> None:
        apply(client, cluster_id, json.dumps(DEPLOYMENT))
        dep_uid = store.objects["/apis/apps/v1/namespaces/default/deployments/web"]["metadata"]["uid"]
        for revision, image in ((1, "nginx:1.26"), (2, "nginx:1.27")):
            store.put(
                {
                    "apiVersion": "apps/v1",
                    "kind": "ReplicaSet",
                    "metadata": {
                        "name": f"web-r{revision}",
                        "namespace": "default",
                        "annotations": {"deployment.kubernetes.io/revision": str(revision)},
                        "ownerReferences": [{"kind": "Deployment", "name": "web", "uid": dep_uid}],
                    },
                    "spec": {
                        "template": {
                            "metadata": {"labels": {"app": "web", "pod-template-hash": f"h{revision}"}},
                            "spec": {"containers": [{"name": "nginx", "image": image}]},
                        }
                    },
                    "status": {"replicas": 0},
                }
            )
        store.objects["/apis/apps/v1/namespaces/default/deployments/web"]["metadata"]["annotations"] = {
            "deployment.kubernetes.io/revision": "2"
        }
        store.put(
            {
                "apiVersion": "v1",
                "kind": "Pod",
                "metadata": {"name": "web-1", "namespace": "default", "labels": {"app": "web"}},
                "spec": {"containers": [{"name": "nginx", "image": "nginx:1.27"}]},
                "status": {"phase": "Running"},
            }
        )

        rows = client.get(f"{API}/clusters/{cluster_id}/resources/deployments").json()["items"]
        assert [(r["name"], r["replicas"], r["images"]) for r in rows] == [("web", 2, ["nginx:1.27"])]
        detail = client.get(f"{API}/clusters/{cluster_id}/resources/deployments/default/web").json()
        assert [(h["revision"], h["current"]) for h in detail["history"]] == [(2, True), (1, False)]
        assert [p["name"] for p in detail["pods"]] == ["web-1"]
        assert "uid:" not in detail["yaml"] and "kind: Deployment" in detail["yaml"]
        assert detail["capabilities"] == {"scale": True, "restart": True, "delete": True, "namespaced": True}

        rollback = client.post(f"{API}/clusters/{cluster_id}/deployments/default/web/rollback", json={"revision": 1})
        assert rollback.status_code == 200 and rollback.json()["images"] == ["nginx:1.26"]
        template = store.objects["/apis/apps/v1/namespaces/default/deployments/web"]["spec"]["template"]
        assert template["spec"]["containers"][0]["image"] == "nginx:1.26"
        assert "pod-template-hash" not in template["metadata"]["labels"]
        current = client.post(f"{API}/clusters/{cluster_id}/deployments/default/web/rollback", json={"revision": 2})
        assert current.status_code == 409
        assert (
            client.post(
                f"{API}/clusters/{cluster_id}/deployments/default/web/rollback", json={"revision": 9}
            ).status_code
            == 422
        )

    def test_scale_restart_delete(self, client: TestClient, store: FakeStore, cluster_id: str) -> None:
        apply(client, cluster_id, json.dumps(DEPLOYMENT))
        path = "/apis/apps/v1/namespaces/default/deployments/web"
        scaled = client.post(
            f"{API}/clusters/{cluster_id}/resources/deployments/default/web/scale", json={"replicas": 5}
        ).json()
        assert scaled == {"replicas": 5, "previous": 2} and store.objects[path]["spec"]["replicas"] == 5
        assert (
            client.post(
                f"{API}/clusters/{cluster_id}/resources/deployments/default/web/scale", json={"replicas": -1}
            ).status_code
            == 422
        )
        assert (
            client.post(
                f"{API}/clusters/{cluster_id}/resources/configmaps/default/x/scale", json={"replicas": 1}
            ).status_code
            == 422
        )
        client.post(f"{API}/clusters/{cluster_id}/resources/deployments/default/web/restart")
        assert "kubectl.kubernetes.io/restartedAt" in store.objects[path]["spec"]["template"]["metadata"]["annotations"]
        url = f"{API}/clusters/{cluster_id}/resources/deployments/default/web"
        assert client.delete(url).status_code == 428
        assert client.delete(url, params={"confirm": "web"}).status_code == 204
        assert path not in store.objects
        actions = audit_actions(client)
        assert {"deployment.scale", "deployment.restart", "deployment.delete"} <= set(actions)

    def test_phase6_network_and_storage_resource_views(
        self, client: TestClient, store: FakeStore, cluster_id: str
    ) -> None:
        store.put(
            {
                "apiVersion": "networking.k8s.io/v1",
                "kind": "Ingress",
                "metadata": {"name": "web", "namespace": "default"},
                "spec": {
                    "ingressClassName": "nginx",
                    "rules": [{"host": "app.example.test", "http": {"paths": [{"path": "/"}]}}],
                },
                "status": {"loadBalancer": {"ingress": [{"ip": "192.0.2.10"}]}},
            }
        )
        store.put(
            {
                "apiVersion": "networking.k8s.io/v1",
                "kind": "NetworkPolicy",
                "metadata": {"name": "allow-web", "namespace": "default"},
                "spec": {
                    "podSelector": {"matchLabels": {"app": "web"}},
                    "policyTypes": ["Ingress"],
                    "ingress": [{}],
                },
            }
        )
        store.put(
            {
                "apiVersion": "discovery.k8s.io/v1",
                "kind": "EndpointSlice",
                "metadata": {
                    "name": "web-abc",
                    "namespace": "default",
                    "labels": {"kubernetes.io/service-name": "web"},
                },
                "addressType": "IPv4",
                "ports": [{"name": "http", "port": 8080}],
                "endpoints": [
                    {"addresses": ["10.0.0.8"], "conditions": {"ready": True}},
                    {"addresses": ["10.0.0.9"], "conditions": {"ready": False}},
                ],
            }
        )
        store.put(
            {
                "apiVersion": "v1",
                "kind": "PersistentVolume",
                "metadata": {"name": "pv-data"},
                "spec": {
                    "capacity": {"storage": "10Gi"},
                    "accessModes": ["ReadWriteOnce"],
                    "persistentVolumeReclaimPolicy": "Retain",
                    "storageClassName": "fast",
                },
                "status": {"phase": "Available"},
            }
        )
        store.put(
            {
                "apiVersion": "storage.k8s.io/v1",
                "kind": "StorageClass",
                "metadata": {
                    "name": "fast",
                    "annotations": {"storageclass.kubernetes.io/is-default-class": "true"},
                },
                "provisioner": "csi.example.test",
                "allowVolumeExpansion": True,
                "volumeBindingMode": "WaitForFirstConsumer",
            }
        )
        store.put(
            {
                "apiVersion": "storage.k8s.io/v1",
                "kind": "CSIDriver",
                "metadata": {"name": "csi.example.test"},
                "spec": {"attachRequired": True, "volumeLifecycleModes": ["Persistent"]},
            }
        )
        store.put(
            {
                "apiVersion": "gateway.networking.k8s.io/v1",
                "kind": "GatewayClass",
                "metadata": {"name": "public"},
                "spec": {"controllerName": "example.test/gateway-controller"},
                "status": {"conditions": [{"type": "Accepted", "status": "True"}]},
            }
        )
        store.put(
            {
                "apiVersion": "gateway.networking.k8s.io/v1",
                "kind": "Gateway",
                "metadata": {"name": "public", "namespace": "default"},
                "spec": {"gatewayClassName": "public", "listeners": [{"name": "http", "port": 80}]},
                "status": {
                    "addresses": [{"value": "192.0.2.20"}],
                    "conditions": [{"type": "Programmed", "status": "True"}],
                },
            }
        )
        store.put(
            {
                "apiVersion": "gateway.networking.k8s.io/v1",
                "kind": "HTTPRoute",
                "metadata": {"name": "web", "namespace": "default"},
                "spec": {"hostnames": ["app.example.test"], "parentRefs": [{"name": "public"}], "rules": [{}]},
                "status": {
                    "parents": [
                        {
                            "conditions": [
                                {"type": "Accepted", "status": "True"},
                                {"type": "ResolvedRefs", "status": "True"},
                            ]
                        }
                    ]
                },
            }
        )
        store.put(
            {
                "apiVersion": "gateway.networking.k8s.io/v1",
                "kind": "ReferenceGrant",
                "metadata": {"name": "allow-route", "namespace": "services"},
                "spec": {"from": [{}], "to": [{}]},
            }
        )
        store.put(
            {
                "apiVersion": "v1",
                "kind": "PersistentVolumeClaim",
                "metadata": {"name": "app-data", "namespace": "default"},
                "spec": {
                    "accessModes": ["ReadWriteOnce"],
                    "resources": {"requests": {"storage": "1Gi"}},
                    "storageClassName": "fast",
                },
                "status": {"phase": "Bound", "capacity": {"storage": "1Gi"}},
            }
        )
        store.put(
            {
                "apiVersion": "storage.k8s.io/v1",
                "kind": "VolumeAttachment",
                "metadata": {"name": "attach-pv-data"},
                "spec": {
                    "attacher": "csi.example.test",
                    "nodeName": "worker-1",
                    "source": {"persistentVolumeName": "pv-data"},
                },
                "status": {"attached": True},
            }
        )
        store.put(
            {
                "apiVersion": "v1",
                "kind": "ResourceQuota",
                "metadata": {"name": "app-budget", "namespace": "default"},
                "spec": {"hard": {"pods": "10"}},
                "status": {"hard": {"pods": "10"}, "used": {"pods": "3"}},
            }
        )
        store.put(
            {
                "apiVersion": "v1",
                "kind": "LimitRange",
                "metadata": {"name": "app-defaults", "namespace": "default"},
                "spec": {"limits": [{"type": "Container"}]},
            }
        )

        ingress = client.get(f"{API}/clusters/{cluster_id}/resources/ingresses").json()["items"][0]
        assert ingress["hosts"] == ["app.example.test"] and ingress["addresses"] == ["192.0.2.10"]
        policy = client.get(f"{API}/clusters/{cluster_id}/resources/networkpolicies").json()["items"][0]
        assert policy["pod_selector"] == {"app": "web"} and policy["ingress_rules"] == 1
        endpoint = client.get(f"{API}/clusters/{cluster_id}/resources/endpointslices").json()["items"][0]
        assert endpoint["service"] == "web" and endpoint["ready_addresses"] == ["10.0.0.8"]

        volumes = client.get(f"{API}/clusters/{cluster_id}/resources/persistentvolumes").json()["items"]
        assert volumes[0]["namespace"] is None and volumes[0]["capacity"] == "10Gi"
        pv = client.get(f"{API}/clusters/{cluster_id}/resources/persistentvolumes/cluster/pv-data").json()
        assert pv["capabilities"]["namespaced"] is False and pv["summary"]["phase"] == "Available"
        storage_class = client.get(f"{API}/clusters/{cluster_id}/resources/storageclasses/cluster/fast").json()
        assert storage_class["summary"]["default"] and storage_class["summary"]["provisioner"] == "csi.example.test"
        driver = client.get(f"{API}/clusters/{cluster_id}/resources/csidrivers/cluster/csi.example.test").json()
        assert driver["summary"]["attach_required"] is True and driver["capabilities"]["delete"] is False
        gateway_class = client.get(f"{API}/clusters/{cluster_id}/resources/gatewayclasses/cluster/public").json()
        assert gateway_class["summary"]["controller"] == "example.test/gateway-controller"
        gateway = client.get(f"{API}/clusters/{cluster_id}/resources/gateways/default/public").json()
        assert gateway["summary"]["listeners"] == 1 and gateway["summary"]["programmed"] == "True"
        route = client.get(f"{API}/clusters/{cluster_id}/resources/httproutes/default/web").json()
        assert route["summary"]["accepted"] == "True" and route["summary"]["resolved_refs"] == "True"
        grant = client.get(f"{API}/clusters/{cluster_id}/resources/referencegrants/services/allow-route").json()
        assert grant["summary"]["from_count"] == 1 and grant["summary"]["to_count"] == 1
        claim = client.get(f"{API}/clusters/{cluster_id}/resources/persistentvolumeclaims/default/app-data").json()
        assert claim["summary"]["phase"] == "Bound" and claim["summary"]["requested"] == "1Gi"
        attachment = client.get(
            f"{API}/clusters/{cluster_id}/resources/volumeattachments/cluster/attach-pv-data"
        ).json()
        assert attachment["summary"]["attached"] and attachment["capabilities"]["delete"] is False
        quota = client.get(f"{API}/clusters/{cluster_id}/resources/resourcequotas/default/app-budget").json()
        assert quota["summary"]["used"] == ["pods: 3"]
        limit_range = client.get(f"{API}/clusters/{cluster_id}/resources/limitranges/default/app-defaults").json()
        assert limit_range["summary"]["limit_types"] == ["Container"]
        blocked = client.delete(
            f"{API}/clusters/{cluster_id}/resources/csidrivers/cluster/csi.example.test",
            params={"confirm": "csi.example.test"},
        )
        assert blocked.status_code == 422

    def test_secrets_redacted_and_reveal_audited(self, client: TestClient, store: FakeStore, cluster_id: str) -> None:
        store.put(
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"name": "db", "namespace": "default"},
                "type": "Opaque",
                "data": {"password": "aHVudGVyMg==", "cert": "/w=="},
            }
        )
        detail = client.get(f"{API}/clusters/{cluster_id}/resources/secrets/default/db")
        assert "aHVudGVyMg==" not in detail.text and "hunter2" not in detail.text
        assert client.get(f"{API}/clusters/{cluster_id}/resources/secrets").json()["items"][0]["keys"] == [
            "cert",
            "password",
        ]
        blocked = client.post(
            f"{API}/clusters/{cluster_id}/apply", json={"manifest": detail.json()["yaml"], "dry_run": True}
        )
        assert blocked.status_code == 422 and "redacted" in blocked.json()["error"]["message"]
        revealed = client.post(f"{API}/clusters/{cluster_id}/secrets/default/db/reveal").json()["values"]
        assert revealed["password"] == {"text": "hunter2", "binary": False} and revealed["cert"]["binary"] is True
        events = client.get(f"{API}/audit-events", params={"action": "secret.reveal"}).json()["items"]
        assert events and events[0]["details"]["keys"] == ["cert", "password"] and "hunter2" not in json.dumps(events)

    def test_cronjob_trigger_and_suspend(self, client: TestClient, store: FakeStore, cluster_id: str) -> None:
        apply(
            client,
            cluster_id,
            json.dumps(
                {
                    "apiVersion": "batch/v1",
                    "kind": "CronJob",
                    "metadata": {"name": "nightly", "namespace": "default"},
                    "spec": {
                        "schedule": "0 3 * * *",
                        "jobTemplate": {
                            "spec": {
                                "template": {
                                    "spec": {
                                        "restartPolicy": "Never",
                                        "containers": [{"name": "c", "image": "busybox"}],
                                    }
                                }
                            }
                        },
                    },
                }
            ),
        )
        job = client.post(f"{API}/clusters/{cluster_id}/cronjobs/default/nightly/trigger").json()["job"]
        created = store.objects[f"/apis/batch/v1/namespaces/default/jobs/{job}"]
        assert created["metadata"]["ownerReferences"][0]["kind"] == "CronJob"
        assert created["spec"]["template"]["spec"]["containers"][0]["image"] == "busybox"
        detail = client.get(f"{API}/clusters/{cluster_id}/resources/cronjobs/default/nightly").json()
        assert [j["name"] for j in detail["jobs"]] == [job]
        client.post(f"{API}/clusters/{cluster_id}/cronjobs/default/nightly/suspend", json={"suspend": True})
        assert store.objects["/apis/batch/v1/namespaces/default/cronjobs/nightly"]["spec"]["suspend"] is True

    def test_logs(self, client: TestClient, store: FakeStore, cluster_id: str) -> None:
        store.logs["/api/v1/namespaces/default/pods/web-1/log"] = "line one\nline two\n"
        text = client.get(
            f"{API}/clusters/{cluster_id}/pods/default/web-1/logs", params={"container": "nginx", "tail": 50}
        )
        assert text.status_code == 200 and text.text == "line one\nline two\n"
        _method, _path, params = store.requests[-1]
        assert params["container"] == "nginx" and params["tailLines"] == 50
        with client.stream("GET", f"{API}/clusters/{cluster_id}/pods/default/web-1/logs/stream") as response:
            body = "".join(response.iter_text())
        assert body.count("event: line") == 2 and body.rstrip().endswith("event: end\ndata: {}")

    def test_input_validation(self, client: TestClient, store: FakeStore, cluster_id: str) -> None:
        assert client.get(f"{API}/clusters/{cluster_id}/resources/widgets").status_code == 422
        assert client.get(f"{API}/clusters/{cluster_id}/resources/pods/Bad_NS/x").status_code == 422
        assert client.get(f"{API}/clusters/{cluster_id}/resources/pods/default/missing").status_code == 404
        assert (
            client.post(
                f"{API}/clusters/{cluster_id}/apply", json={"manifest": "not: [yaml", "dry_run": True}
            ).status_code
            == 422
        )

    def test_terminal_rejects_foreign_origin(self, client: TestClient, store: FakeStore, cluster_id: str) -> None:
        from starlette.websockets import WebSocketDisconnect

        with (
            pytest.raises(WebSocketDisconnect) as closed,
            client.websocket_connect(
                f"{API}/clusters/{cluster_id}/pods/default/web-1/exec", headers={"origin": "https://evil.example"}
            ),
        ):
            pass
        assert closed.value.code == 4403
