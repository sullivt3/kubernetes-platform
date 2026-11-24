"""Real operation engine coverage for runbooks, migration and conversion rollback."""

from __future__ import annotations

import copy
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from sqlalchemy import select

from app.db.session import get_session_factory
from app.modules.custom_workflows.models import CustomWorkflow
from app.modules.kubernetes import client as kc
from app.modules.kubernetes import crd_operations as advanced
from app.modules.kubernetes.custom_resource_models import CustomResourceReview
from app.modules.kubernetes.custom_resources import fingerprint
from app.modules.kubernetes.definition_lifecycle import MANAGER, MANAGER_VALUE, OWNER, editor_definition
from app.orchestration.models import OperationLog
from tests import fake_store
from tests.fake_kube import kubeconfig_text
from tests.test_custom_resources import SENTINEL, install_definition
from tests.test_definition_lifecycle import DEF_PATH, DEFINITION, GROUP, DefinitionStore
from tests.test_identity import IdentityHarness, identity  # noqa: F401

ROOT = "/apis/" + GROUP + "/v1"
RESOURCE = ROOT + "/namespaces/default/widgets/proof"
DEPLOY = "/apis/apps/v1/namespaces/default/deployments/proof"


class AutomationStore(DefinitionStore):
    def __init__(self):
        super().__init__()
        self.stored = {}
        self.bad_conversion = False
        self.fail_rewrite = False
        self.rewrite_writes = 0
        self.native_available = False
        self.native_failed = False
        self.native_pending = False
        self.discovery_lag = 0
        self.mutate_workload = False
        self.put(
            {
                "apiVersion": "v1",
                "kind": "Node",
                "metadata": {"name": "node1", "labels": {"node-role.kubernetes.io/control-plane": ""}},
                "status": {"conditions": [{"type": "Ready", "status": "True"}]},
            }
        )
        self.put(
            {
                "apiVersion": "v1",
                "kind": "Pod",
                "metadata": {
                    "name": "kube-apiserver-node1",
                    "namespace": "kube-system",
                    "labels": {"component": "kube-apiserver"},
                    "annotations": {"kubernetes.io/config.mirror": "test-only"},
                },
                "spec": {"nodeName": "node1"},
                "status": {"phase": "Running"},
            }
        )
        self.put(
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": "proof", "namespace": "default"},
                "spec": {
                    "replicas": 0,
                    "template": {
                        "metadata": {},
                        "spec": {"containers": [{"name": "app", "image": "test-only-never-pulled"}]},
                    },
                },
                "status": {
                    "observedGeneration": 99999,
                    "readyReplicas": 3,
                    "availableReplicas": 3,
                    "updatedReplicas": 3,
                },
            }
        )

    def request(self, method, path, *, body=None, params=None, **kwargs):
        params = params or {}
        payload = json.loads(body) if isinstance(body, bytes) else body
        if path == "/apis/storagemigration.k8s.io/v1":
            return (
                self._ok(
                    {
                        "resources": [
                            {"name": "storageversionmigrations", "kind": "StorageVersionMigration", "namespaced": False}
                        ]
                    }
                )
                if self.native_available
                else self._err(404, SENTINEL)
            )
        if path == advanced.NATIVE and method == "POST":
            if params.get("dryRun") == "All":
                return self._ok(payload)
            result = copy.deepcopy(payload)
            result["status"] = {
                "conditions": [
                    {"type": "Failed" if self.native_failed else "Succeeded", "status": "True", "message": SENTINEL}
                ]
            }
            if self.native_pending:
                result["status"] = {"conditions": []}
            if not self.native_failed and not self.native_pending:
                for key in self.stored:
                    self.stored[key] = advanced.storage_version(self.objects[DEF_PATH])
                self.objects[DEF_PATH]["status"]["storedVersions"] = [advanced.storage_version(self.objects[DEF_PATH])]
            return self._ok(self.put(result))
        if path == "/readyz":
            return 200, b"ok"
        if path == DEF_PATH + "/status" and method == "PUT":
            live = self.objects[DEF_PATH]
            if (
                payload["metadata"]["uid"] != live["metadata"]["uid"]
                or payload["metadata"]["resourceVersion"] != live["metadata"]["resourceVersion"]
            ):
                return self._err(409, SENTINEL)
            result = copy.deepcopy(live)
            result["status"] = payload["status"]
            result["metadata"] = copy.deepcopy(payload["metadata"])
            return self._ok(self.put(result))
        if path == DEPLOY and method == "PUT":
            current = self.objects[DEPLOY]
            if payload["metadata"]["resourceVersion"] != current["metadata"]["resourceVersion"]:
                return self._err(409, SENTINEL)
            result = copy.deepcopy(payload)
            result["status"] = current["status"]
            if self.mutate_workload:
                result["spec"]["replicas"] = 7
            if params.get("dryRun") == "All":
                return self._ok(result)
            return self._ok(self.put(result))
        if path.startswith("/apis/" + GROUP + "/"):
            parts = path.split("/")
            version = parts[3]
            if len(parts) == 4:
                aliases = self.objects.get(DEF_PATH, {}).get("spec", {}).get("names", {}).get("shortNames", [])
                if self.discovery_lag and aliases:
                    self.discovery_lag -= 1
                    aliases = []
                return self._ok(
                    {"resources": [{"name": "widgets", "kind": "Widget", "namespaced": True, "shortNames": aliases}]}
                )
            canonical = path.replace("/" + version + "/", "/v1/", 1)
            if (
                method == "GET"
                and self.bad_conversion
                and version != "v1"
                and self.objects.get(DEF_PATH, {}).get("spec", {}).get("conversion", {}).get("strategy") == "Webhook"
            ):
                return self._err(500, SENTINEL)
            if method == "GET":
                status, data = super().request(method, canonical, body=body, params=params, **kwargs)
                value = json.loads(data)
                for obj in value.get("items", []) if "items" in value else [value]:
                    if obj.get("kind") == "Widget":
                        obj["apiVersion"] = GROUP + "/" + version
                return status, json.dumps(value).encode()
            if method == "PUT":
                current = self.objects.get(canonical)
                if not current:
                    return self._err(404, SENTINEL)
                if payload["metadata"]["resourceVersion"] != current["metadata"]["resourceVersion"]:
                    return self._err(409, SENTINEL)
                if params.get("dryRun") == "All":
                    return self._ok(payload)
                if self.fail_rewrite:
                    return self._err(500, SENTINEL)
                rewritten = copy.deepcopy(payload)
                rewritten["apiVersion"] = GROUP + "/v1"
                result = self.put(rewritten)
                self.stored[canonical] = advanced.storage_version(self.objects[DEF_PATH])
                self.rewrite_writes += 1
                returned = copy.deepcopy(result)
                returned["apiVersion"] = GROUP + "/" + version
                return self._ok(returned)
        before = copy.deepcopy(self.objects.get(DEF_PATH, {}).get("status", {}))
        result = super().request(method, path, body=body, params=params, **kwargs)
        if method == "PUT" and path == DEF_PATH and params.get("dryRun") != "All" and result[0] < 400:
            self.objects[DEF_PATH]["status"] = before
            value = json.loads(result[1])
            value["status"] = before
            return self._ok(value)
        return result


@pytest.fixture
def env(client, monkeypatch):
    monkeypatch.setitem(
        fake_store.RESOURCES,
        "apiextensions.k8s.io/v1",
        [("customresourcedefinitions", "CustomResourceDefinition", False)],
    )
    for version in ["v1", "v2"]:
        monkeypatch.setitem(fake_store.RESOURCES, GROUP + "/" + version, [("widgets", "Widget", True)])
    monkeypatch.setitem(
        fake_store.RESOURCES,
        "storagemigration.k8s.io/v1",
        [("storageversionmigrations", "StorageVersionMigration", False)],
    )
    store = AutomationStore()
    store.native_available = True
    kc.set_client_factory(store.factory)
    try:
        imported = client.post(
            "/api/v1/clusters/import", json={"name": "advanced-test", "kubeconfig": kubeconfig_text()}
        )
        assert imported.status_code == 202
        cid = imported.json()["cluster_id"]
        definition = install_definition(store)
        definition["spec"]["versions"] = [
            definition["spec"]["versions"][0],
            {**copy.deepcopy(definition["spec"]["versions"][0]), "name": "v2", "storage": False},
        ]
        definition["metadata"]["labels"] = {OWNER: cid, MANAGER: MANAGER_VALUE}
        definition["status"]["storedVersions"] = ["v1"]
        yield client, cid, store
    finally:
        kc.set_client_factory(None)


def target(env):
    live = env[2].objects[DEF_PATH]
    return {"name": DEFINITION, "expected_uid": live["metadata"]["uid"], "expected_hash": fingerprint(live)}


def base(env):
    return "/api/v1/clusters/" + env[1] + "/custom-resources"


def workflow_base(env):
    return "/api/v1/clusters/" + env[1] + "/custom-workflows"


def instance(env):
    env[2].put(
        {
            "apiVersion": GROUP + "/v1",
            "kind": "Widget",
            "metadata": {"name": "proof", "namespace": "default"},
            "spec": {"replicas": 1, "message": SENTINEL},
        }
    )
    env[2].stored[RESOURCE] = "v1"


def run(env, review, category):
    response = env[0].post(
        base(env) + "/automation-run", json={"review_id": review["id"], "confirm": DEFINITION, "category": category}
    )
    assert response.status_code == 202, response.text
    assert SENTINEL not in response.text
    return env[0].get("/api/v1/operations/" + response.json()["id"]).json()


def certificate():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-only-conversion-ca")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM).decode()


def endpoints(env):
    service = env[2].put(
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": "conversion", "namespace": "default"},
            "spec": {"ports": [{"name": "https", "port": 443, "targetPort": 9443}]},
        }
    )
    env[2].put(
        {
            "apiVersion": "discovery.k8s.io/v1",
            "kind": "EndpointSlice",
            "metadata": {
                "name": "conversion",
                "namespace": "default",
                "labels": {"kubernetes.io/service-name": "conversion"},
                "ownerReferences": [{"uid": service["metadata"]["uid"]}],
            },
            "ports": [{"name": "https", "port": 9443}],
            "endpoints": [{"addresses": ["192.0.2.1"], "conditions": {"ready": True}}],
        }
    )


def conversion(env, **extra):
    return env[0].post(
        base(env) + "/conversion-reviews",
        json={**target(env), "strategy": "Webhook", "service_name": "conversion", "ca_pem": certificate(), **extra},
    )


def save_workflow(env, steps):
    response = env[0].post(
        workflow_base(env), json={"name": "proof-workflow", "document": yaml.safe_dump({"steps": steps})}
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_custom_workflow_real_engine_encrypted_definition_and_review(env):
    client, _cid, store = env
    store.objects[DEPLOY]["spec"]["template"]["spec"]["containers"][0]["env"] = [
        {"name": "TEST_ONLY", "value": SENTINEL}
    ]
    row = save_workflow(
        env,
        [
            {"action": "cluster.check"},
            {"action": "resource.scale", "name": "proof", "replicas": 2},
            {"action": "resource.restart", "name": "proof"},
            {"action": "resource.wait", "name": "proof"},
        ],
    )
    before = copy.deepcopy(store.objects[DEPLOY])
    path = workflow_base(env) + "/" + row["id"]
    preview = client.post(path + "/review")
    assert preview.status_code == 200, preview.text
    assert store.objects[DEPLOY] == before
    assert client.post(path + "/run", json={"review_id": preview.json()["id"], "confirm": "wrong"}).status_code == 428
    result = client.post(path + "/run", json={"review_id": preview.json()["id"], "confirm": row["name"]})
    assert result.status_code == 202, result.text
    operation = client.get("/api/v1/operations/" + result.json()["id"]).json()
    assert operation["status"] == "succeeded", operation
    assert SENTINEL not in json.dumps(operation)
    with get_session_factory()() as session:
        review_row = session.get(CustomResourceReview, uuid.UUID(preview.json()["id"]))
        assert SENTINEL.encode() not in review_row.ciphertext and SENTINEL not in json.dumps(review_row.specification)
    assert operation["result"]["completed_steps"] == 4 and store.objects[DEPLOY]["spec"]["replicas"] == 2
    assert "kubectl.kubernetes.io/restartedAt" in store.objects[DEPLOY]["spec"]["template"]["metadata"]["annotations"]
    assert len(client.get(path + "/history").json()) == 1
    assert (
        client.post(path + "/run", json={"review_id": preview.json()["id"], "confirm": row["name"]}).json()["id"]
        == result.json()["id"]
    )
    with get_session_factory()() as session:
        stored = session.get(CustomWorkflow, uuid.UUID(row["id"]))
        assert b"resource.scale" not in stored.ciphertext
    assert client.post(path + "/reveal").status_code == 200


def test_workflow_generation_and_target_drift_block_runs(env):
    row = save_workflow(env, [{"action": "resource.scale", "name": "proof", "replicas": 2}])
    path = workflow_base(env) + "/" + row["id"]
    planned = env[0].post(path + "/review").json()
    env[2].objects[DEPLOY]["spec"]["replicas"] = 5
    result = env[0].post(path + "/run", json={"review_id": planned["id"], "confirm": row["name"]}).json()
    operation = env[0].get("/api/v1/operations/" + result["id"]).json()
    assert operation["status"] == "failed"
    assert env[2].objects[DEPLOY]["spec"]["replicas"] == 5
    planned = env[0].post(path + "/review").json()
    assert (
        env[0]
        .put(path, json={"name": row["name"], "generation": 1, "document": "steps:\n- action: cluster.check"})
        .status_code
        == 200
    )
    result = env[0].post(path + "/run", json={"review_id": planned["id"], "confirm": row["name"]}).json()
    assert env[0].get("/api/v1/operations/" + result["id"]).json()["status"] == "failed"


@pytest.mark.parametrize(
    "document",
    [
        "",
        "steps: []",
        "steps:\n- action: shell\n  command: secret",
        "steps:\n- action: resource.scale\n  name: proof",
        "steps:\n- action: resource.wait\n  name: ../escape",
        "steps:\n- action: resource.scale\n  name: proof\n  replicas: -1",
    ],
)
def test_workflow_dsl_rejects_unsafe_or_malformed_steps(env, document):
    response = env[0].post(workflow_base(env), json={"name": "invalid", "document": document})
    assert response.status_code == 422


def test_migration_switches_storage_rewrites_then_certifies(env):
    instance(env)
    store = env[2]
    response = env[0].post(base(env) + "/migration-reviews", json={**target(env), "target_version": "v2"})
    assert response.status_code == 200, response.text
    assert response.json()["reviewed_instances"] == 1 and advanced.storage_version(store.objects[DEF_PATH]) == "v1"
    assert store.stored[RESOURCE] == "v1"
    operation = run(env, response.json(), "storage_migration")
    assert operation["status"] == "succeeded", operation
    assert store.stored[RESOURCE] == "v2" and advanced.storage_version(store.objects[DEF_PATH]) == "v2"
    assert store.objects[DEF_PATH]["status"]["storedVersions"] == ["v2"]
    assert store.objects[DEF_PATH]["metadata"]["labels"][OWNER] == env[1]
    assert store.objects[DEF_PATH]["metadata"]["labels"][MANAGER] == MANAGER_VALUE
    assert store.objects[RESOURCE]["spec"]["message"] == SENTINEL
    assert operation["result"]["backend"] == "native"
    assert operation["result"]["reviewed_instances"] == 1
    assert operation["result"]["native_migration_uid"]
    assert SENTINEL not in json.dumps(operation)


def test_partial_migration_retains_old_version_metadata_then_retry(env):
    instance(env)
    store = env[2]
    store.fail_rewrite = True
    planned = (
        env[0]
        .post(base(env) + "/migration-reviews", json={**target(env), "target_version": "v1", "mode": "rewrite_only"})
        .json()
    )
    operation = run(env, planned, "storage_migration")
    assert operation["status"] == "failed"
    assert advanced.storage_version(store.objects[DEF_PATH]) == "v1"
    assert store.objects[DEF_PATH]["status"]["storedVersions"] == ["v1"]
    store.fail_rewrite = False
    retried = env[0].post("/api/v1/operations/" + operation["id"] + "/retry")
    assert retried.status_code == 202
    assert env[0].get("/api/v1/operations/" + operation["id"]).json()["status"] == "succeeded"
    assert store.objects[DEF_PATH]["status"]["storedVersions"] == ["v1"]


def test_none_migration_rejects_different_schema_and_missing_target(env):
    env[2].objects[DEF_PATH]["spec"]["versions"][1]["schema"]["openAPIV3Schema"]["description"] = "different"
    assert (
        env[0].post(base(env) + "/migration-reviews", json={**target(env), "target_version": "v2"}).status_code == 409
    )
    assert (
        env[0].post(base(env) + "/migration-reviews", json={**target(env), "target_version": "missing"}).status_code
        == 409
    )


def test_definition_change_after_migration_review_cannot_switch_storage(env):
    planned = env[0].post(base(env) + "/migration-reviews", json={**target(env), "target_version": "v2"}).json()
    env[2].objects[DEF_PATH]["metadata"]["labels"]["example.com/changed"] = "yes"
    assert run(env, planned, "storage_migration")["status"] == "failed"
    assert advanced.storage_version(env[2].objects[DEF_PATH]) == "v1"


def test_webhook_configuration_probes_and_rolls_back_on_protocol_failure(env):
    instance(env)
    endpoints(env)
    before = copy.deepcopy(env[2].objects[DEF_PATH]["spec"])
    planned = conversion(env)
    assert planned.status_code == 200, planned.text
    env[2].bad_conversion = True
    operation = run(env, planned.json(), "conversion_webhook")
    assert operation["status"] == "failed", operation
    assert env[2].objects[DEF_PATH]["spec"] == before
    history = env[0].get(base(env) + "/definition-operations", params={"name": DEFINITION}).json()
    assert history[0]["progress"]["rolled_back"] is True
    assert SENTINEL not in json.dumps(operation) and SENTINEL not in json.dumps(history)
    with get_session_factory()() as session:
        assert all(SENTINEL not in log.message for log in session.scalars(select(OperationLog)))


def test_webhook_success_probe_and_existing_configuration_edit(env):
    instance(env)
    endpoints(env)
    preview = conversion(env)
    assert preview.status_code == 200, preview.text
    result = run(env, preview.json(), "conversion_webhook")
    assert result["status"] == "succeeded", result
    assert result["result"]["probed_instances"] == 1 and result["result"]["status"] == "Verified"
    live = env[2].objects[DEF_PATH]
    assert live["spec"]["conversion"]["strategy"] == "Webhook"
    desired = editor_definition(live)
    desired["metadata"]["labels"] = {"example.com/team": "test"}
    review = env[0].post(
        base(env) + "/definition-reviews",
        json={
            "action": "update",
            "name": DEFINITION,
            "expected_uid": live["metadata"]["uid"],
            "expected_hash": fingerprint(live),
            "manifest": yaml.safe_dump(desired),
        },
    )
    assert review.status_code == 200, review.text
    assert conversion(env, action="probe").status_code == 200


def test_webhook_without_instances_does_not_claim_protocol_verified(env):
    endpoints(env)
    preview = conversion(env)
    assert preview.status_code == 200, preview.text
    operation = run(env, preview.json(), "conversion_webhook")
    assert operation["status"] == "succeeded"
    assert operation["result"]["status"] == "ConfiguredWithoutInstanceProbe"


@pytest.mark.parametrize("change", ["unready", "foreign-endpoint", "external", "missing-port", "invalid-ca"])
def test_webhook_readiness_and_trust_fail_closed(env, change):
    endpoints(env)
    kwargs = {}
    if change == "unready":
        env[2].objects["/apis/discovery.k8s.io/v1/namespaces/default/endpointslices/conversion"]["endpoints"][0][
            "conditions"
        ]["ready"] = False
    if change == "foreign-endpoint":
        env[2].objects["/apis/discovery.k8s.io/v1/namespaces/default/endpointslices/conversion"]["metadata"][
            "ownerReferences"
        ] = [{"uid": "foreign"}]
    if change == "external":
        env[2].objects["/api/v1/namespaces/default/services/conversion"]["spec"]["type"] = "ExternalName"
    if change == "missing-port":
        kwargs["port"] = 8443
    if change == "invalid-ca":
        kwargs["ca_pem"] = "invalid-" + SENTINEL
    response = conversion(env, **kwargs)
    assert response.status_code in {409, 422} and SENTINEL not in response.text
    assert env[2].objects[DEF_PATH]["spec"].get("conversion", {}).get("strategy") != "Webhook"


def test_service_replacement_after_review_blocks_webhook_change(env):
    endpoints(env)
    planned = conversion(env).json()
    env[2].objects["/api/v1/namespaces/default/services/conversion"]["metadata"]["uid"] = "replacement"
    assert run(env, planned, "conversion_webhook")["status"] == "failed"
    assert env[2].objects[DEF_PATH]["spec"].get("conversion", {}).get("strategy") != "Webhook"


def test_cross_category_execution_and_existing_instance_endpoint_cannot_consume_task(env):
    planned = env[0].post(base(env) + "/migration-reviews", json={**target(env), "target_version": "v2"}).json()
    assert (
        env[0].post(base(env) + "/execute", json={"review_id": planned["id"], "confirm": DEFINITION}).status_code == 404
    )
    assert (
        env[0]
        .post(base(env) + "/definition-execute", json={"review_id": planned["id"], "confirm": DEFINITION})
        .status_code
        == 404
    )
    assert (
        env[0]
        .post(
            base(env) + "/automation-run",
            json={"review_id": planned["id"], "confirm": DEFINITION, "category": "conversion_webhook"},
        )
        .status_code
        == 404
    )


def test_review_expiry_and_typed_confirmation(env):
    planned = env[0].post(base(env) + "/migration-reviews", json={**target(env), "target_version": "v2"}).json()
    assert (
        env[0]
        .post(
            base(env) + "/automation-run",
            json={"review_id": planned["id"], "confirm": "wrong", "category": "storage_migration"},
        )
        .status_code
        == 428
    )
    with get_session_factory()() as session:
        row = session.get(CustomResourceReview, uuid.UUID(planned["id"]))
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()
    assert (
        env[0]
        .post(
            base(env) + "/automation-run",
            json={"review_id": planned["id"], "confirm": DEFINITION, "category": "storage_migration"},
        )
        .status_code
        == 409
    )


@pytest.mark.parametrize("failure", [False, True])
def test_native_migration_controller_is_used_and_failure_is_not_certified(env, monkeypatch, failure):
    monkeypatch.setitem(
        fake_store.RESOURCES,
        "storagemigration.k8s.io/v1",
        [("storageversionmigrations", "StorageVersionMigration", False)],
    )
    env[2].native_available = True
    env[2].native_failed = failure
    instance(env)
    response = env[0].post(base(env) + "/migration-reviews", json={**target(env), "target_version": "v2"})
    assert response.status_code == 200 and response.json()["backend"] == "native"
    operation = run(env, response.json(), "storage_migration")
    assert operation["status"] == ("failed" if failure else "succeeded"), operation
    assert env[2].objects[DEF_PATH]["status"]["storedVersions"] == (["v1"] if failure else ["v2"])
    assert SENTINEL not in json.dumps(operation)


def test_unobservable_or_changed_manual_control_plane_is_refused(env):
    env[2].objects["/api/v1/nodes/node1"]["metadata"]["labels"] = {}
    assert (
        env[0]
        .post(base(env) + "/migration-reviews", json={**target(env), "target_version": "v1", "mode": "rewrite_only"})
        .status_code
        == 409
    )
    env[2].objects["/api/v1/nodes/node1"]["metadata"]["labels"] = {"node-role.kubernetes.io/control-plane": ""}
    planned = (
        env[0]
        .post(base(env) + "/migration-reviews", json={**target(env), "target_version": "v1", "mode": "rewrite_only"})
        .json()
    )
    env[2].put(
        {
            "apiVersion": "v1",
            "kind": "Node",
            "metadata": {"name": "node2", "labels": {"node-role.kubernetes.io/control-plane": ""}},
        }
    )
    assert run(env, planned, "storage_migration")["status"] == "failed"
    assert advanced.storage_version(env[2].objects[DEF_PATH]) == "v1"


def test_definition_change_after_scan_prevents_certification(env, monkeypatch):
    instance(env)
    old = advanced.tasks.progress

    def progress(ctx, **values):
        old(ctx, **values)
        if values.get("scan_complete"):
            env[2].objects[DEF_PATH]["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["description"] = "changed"

    monkeypatch.setattr(advanced.tasks, "progress", progress)
    preview = (
        env[0]
        .post(base(env) + "/migration-reviews", json={**target(env), "target_version": "v1", "mode": "rewrite_only"})
        .json()
    )
    assert run(env, preview, "storage_migration")["status"] == "failed"
    assert env[2].objects[DEF_PATH]["status"]["storedVersions"] == ["v1"]


def test_worker_revoked_permissions_prevent_queued_workflow_actions(
    identity: IdentityHarness, monkeypatch, executor, recording_dispatcher
):
    from app.api import deps

    store = AutomationStore()
    kc.set_client_factory(store.factory)
    try:
        headers = identity.bearer()
        identity.role("org-admin")
        imported = identity.client.post(
            "/api/v1/clusters/import", headers=headers, json={"name": "actor-test", "kubeconfig": kubeconfig_text()}
        )
        base_path = "/api/v1/clusters/" + imported.json()["cluster_id"] + "/custom-workflows"
        created = identity.client.post(
            base_path,
            headers=headers,
            json={
                "name": "actor-runbook",
                "document": "steps:\n- action: resource.scale\n  name: proof\n  replicas: 2",
            },
        )
        assert created.status_code == 201, created.text
        path = base_path + "/" + created.json()["id"]
        identity.role("operator")
        review = identity.client.post(path + "/review", headers=headers)
        assert review.status_code == 200
        monkeypatch.setattr(deps, "get_dispatcher", lambda: recording_dispatcher)
        queued = identity.client.post(
            path + "/run", headers=headers, json={"review_id": review.json()["id"], "confirm": "actor-runbook"}
        )
        assert queued.status_code == 202
        identity.role("viewer")
        executor.execute(uuid.UUID(queued.json()["id"]))
        operation = identity.client.get("/api/v1/operations/" + queued.json()["id"], headers=headers).json()
        assert operation["status"] == "failed" and store.objects[DEPLOY]["spec"]["replicas"] == 0
        assert identity.client.post(path + "/review", headers=headers).status_code == 403
        assert identity.client.post(path + "/reveal", headers=headers).status_code == 403
        assert (
            identity.client.post(
                base_path, headers=headers, json={"name": "blocked", "document": "steps: []"}
            ).status_code
            == 403
        )
    finally:
        kc.set_client_factory(None)


def test_ca_bundle_rejects_private_keys_and_path_traversal(env):
    endpoints(env)
    mixed = certificate() + "\n-----BEGIN PRIVATE KEY-----\n" + SENTINEL + "\n-----END PRIVATE KEY-----"
    response = conversion(env, ca_pem=mixed)
    assert response.status_code == 422 and SENTINEL not in response.text
    assert conversion(env, path="/../convert").status_code == 422


def test_active_native_migration_blocks_another_review(env, monkeypatch):
    monkeypatch.setitem(
        fake_store.RESOURCES,
        "storagemigration.k8s.io/v1",
        [("storageversionmigrations", "StorageVersionMigration", False)],
    )
    env[2].native_available = True
    env[2].put(
        {
            "apiVersion": "storagemigration.k8s.io/v1",
            "kind": "StorageVersionMigration",
            "metadata": {"name": "foreign"},
            "spec": {"resource": {"group": GROUP, "resource": "widgets"}},
            "status": {"conditions": []},
        }
    )
    response = env[0].post(base(env) + "/migration-reviews", json={**target(env), "target_version": "v2"})
    assert response.status_code == 409 and advanced.storage_version(env[2].objects[DEF_PATH]) == "v1"


def test_native_cancel_removes_only_owned_job_and_retry_can_resume(env, monkeypatch):
    import time

    from app.orchestration.models import Operation

    monkeypatch.setitem(
        fake_store.RESOURCES,
        "storagemigration.k8s.io/v1",
        [("storageversionmigrations", "StorageVersionMigration", False)],
    )
    env[2].native_available = True
    env[2].native_pending = True
    instance(env)

    def cancel(_seconds):
        with get_session_factory()() as session:
            operation = session.scalar(select(Operation).where(Operation.type == "cluster.crd.migrate"))
            operation.cancel_requested_at = datetime.now(UTC)
            session.commit()

    monkeypatch.setattr(time, "sleep", cancel)
    preview = env[0].post(base(env) + "/migration-reviews", json={**target(env), "target_version": "v2"}).json()
    result = run(env, preview, "storage_migration")
    assert result["status"] == "cancelled"
    assert not any(o["kind"] == "StorageVersionMigration" for o in env[2].objects.values())
    assert env[2].objects[DEF_PATH]["status"]["storedVersions"] == ["v1"]
    env[2].native_pending = False
    assert env[0].post("/api/v1/operations/" + result["id"] + "/retry").status_code == 202
    assert env[0].get("/api/v1/operations/" + result["id"]).json()["status"] == "succeeded"


@pytest.mark.parametrize("mode", ["endless", "incomplete"])
def test_instance_pagination_is_bounded_and_incomplete_inventory_refused(env, monkeypatch, mode):
    from app.core.errors import InvalidStateError

    store = env[2]
    original = store.request
    calls = []

    def request(method, path, **kwargs):
        if method == "GET" and path == ROOT + "/widgets":
            calls.append(path)
            return store._ok(
                {
                    "items": [],
                    "metadata": {"continue": "token-" + str(len(calls))}
                    if mode == "endless"
                    else {"remainingItemCount": 1},
                }
            )
        return original(method, path, **kwargs)

    monkeypatch.setattr(store, "request", request)
    with pytest.raises(InvalidStateError):
        list(advanced.pages(store, store.objects[DEF_PATH], "v1", advanced.MAX_OBJECTS))
    assert len(calls) <= 100


def test_configuration_waits_for_discovery_and_removes_temporary_alias(env, monkeypatch):
    monkeypatch.setattr(advanced, "DISCOVERY_POLL_SECONDS", 0)
    instance(env)
    endpoints(env)
    env[2].discovery_lag = 5
    original_names = copy.deepcopy(env[2].objects[DEF_PATH]["spec"]["names"])
    preview = conversion(env)
    assert preview.status_code == 200
    result = run(env, preview.json(), "conversion_webhook")
    assert result["status"] == "succeeded" and env[2].discovery_lag == 0
    assert env[2].objects[DEF_PATH]["spec"]["names"] == original_names


def test_unobserved_configuration_rolls_back_instead_of_claiming_verification(env, monkeypatch):
    monkeypatch.setattr(advanced, "DISCOVERY_WAIT_SECONDS", 0)
    instance(env)
    endpoints(env)
    env[2].discovery_lag = 100
    original = copy.deepcopy(env[2].objects[DEF_PATH]["spec"])
    preview = conversion(env)
    assert preview.status_code == 200
    assert run(env, preview.json(), "conversion_webhook")["status"] == "failed"
    assert env[2].objects[DEF_PATH]["spec"] == original


def test_missing_native_api_refuses_certification_and_rewrite_retains_history(env):
    env[2].native_available = False
    instance(env)
    assert (
        env[0].post(base(env) + "/migration-reviews", json={**target(env), "target_version": "v2"}).status_code == 409
    )
    assert advanced.storage_version(env[2].objects[DEF_PATH]) == "v1"
    env[2].objects[DEF_PATH]["status"]["storedVersions"] = ["v1", "v2"]
    preview = env[0].post(
        base(env) + "/migration-reviews", json={**target(env), "target_version": "v1", "mode": "rewrite_only"}
    )
    assert preview.status_code == 200, preview.text
    result = run(env, preview.json(), "storage_migration")
    assert result["status"] == "succeeded" and result["result"]["status"] == "RewrittenUncertified"
    assert result["type"] == "cluster.crd.rewrite"
    assert env[2].objects[DEF_PATH]["status"]["storedVersions"] == ["v1", "v2"]


def test_unreviewed_admission_mutations_block_bounded_actions(env):
    row = save_workflow(env, [{"action": "resource.scale", "name": "proof", "replicas": 2}])
    env[2].mutate_workload = True
    response = env[0].post(workflow_base(env) + "/" + row["id"] + "/review")
    assert response.status_code == 409 and env[2].objects[DEPLOY]["spec"]["replicas"] == 0


def test_changed_admission_after_review_stops_later_workflow_steps(env):
    row = save_workflow(
        env,
        [{"action": "resource.scale", "name": "proof", "replicas": 2}, {"action": "resource.restart", "name": "proof"}],
    )
    path = workflow_base(env) + "/" + row["id"]
    preview = env[0].post(path + "/review")
    assert preview.status_code == 200
    env[2].mutate_workload = True
    submitted = env[0].post(path + "/run", json={"review_id": preview.json()["id"], "confirm": row["name"]})
    operation = env[0].get("/api/v1/operations/" + submitted.json()["id"]).json()
    assert operation["status"] == "failed" and env[2].objects[DEPLOY]["spec"]["replicas"] == 7
    assert (
        not env[2]
        .objects[DEPLOY]["spec"]["template"]["metadata"]
        .get("annotations", {})
        .get("kubectl.kubernetes.io/restartedAt")
    )
