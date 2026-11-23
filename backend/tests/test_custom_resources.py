"""Dynamic discovery, safe read/reveal and encrypted CRD-bound mutation reviews."""

from __future__ import annotations

import copy
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import yaml
from sqlalchemy import select

from app.db.session import get_session_factory
from app.modules.auditing.models import AuditEvent
from app.modules.kubernetes import client as kc
from app.modules.kubernetes import custom_resources as cr
from app.modules.kubernetes.custom_resource_models import CustomResourceReview
from tests import fake_store
from tests.fake_kube import kubeconfig_text
from tests.test_gitops import VersionedStore
from tests.test_identity import IdentityHarness, identity  # noqa: F401

GROUP = "validation.example.com"
DEFINITION = "widgets." + GROUP
SENTINEL = "test-only-custom-resource-value-sentinel"
ROOT = "/apis/" + GROUP + "/v1"
PATH = ROOT + "/namespaces/default/widgets/proof"


class CustomStore(VersionedStore):
    def __init__(self):
        super().__init__()
        self.denied = None
        self.pending = False

    def request(self, method, path, *, body=None, params=None, **kwargs):
        params = params or {}
        obj = json.loads(body) if isinstance(body, bytes) else body
        if self.denied and path.startswith(cr.BASE):
            return self._err(self.denied, SENTINEL)
        if method == "GET" and path == ROOT:
            return self._ok(
                {
                    "resources": [
                        {"name": p, "kind": k, "namespaced": n, "verbs": ["get", "list", "create", "update", "delete"]}
                        for p, k, n in fake_store.RESOURCES[GROUP + "/v1"]
                    ]
                }
            )
        if method in {"POST", "PUT", "DELETE"} and path.startswith(ROOT + "/"):
            target = (path + "/" + obj["metadata"]["name"]) if method == "POST" else path
            current = self.objects.get(target)
            if method == "POST" and current:
                return self._err(409, SENTINEL)
            if method != "POST" and current is None:
                return self._err(404, SENTINEL)
            if method in {"POST", "PUT"}:
                replicas = (obj.get("spec") or {}).get("replicas")
                if not isinstance(replicas, int) or replicas < 1:
                    return self._err(422, SENTINEL, [{"field": "spec.replicas", "message": SENTINEL}])
                if method == "PUT" and obj["metadata"].get("resourceVersion") != current["metadata"]["resourceVersion"]:
                    return self._err(409, SENTINEL)
                result = copy.deepcopy(obj)
                result.setdefault("spec", {}).setdefault("password", SENTINEL)
                if params.get("dryRun") == "All":
                    return self._ok(result)
                return self._ok(self.put(result), 201 if method == "POST" else 200)
            pre = obj["preconditions"]
            if (
                pre["uid"] != current["metadata"]["uid"]
                or pre["resourceVersion"] != current["metadata"]["resourceVersion"]
            ):
                return self._err(409, SENTINEL)
            if obj.get("dryRun") != ["All"]:
                if self.pending:
                    current["metadata"]["deletionTimestamp"] = "2026-11-23T00:00:00Z"
                else:
                    self.objects.pop(target)
            return self._ok(current)
        return super().request(method, path, body=body, params=params, **kwargs)


def install_definition(store):
    schema = {
        "type": "object",
        "properties": {
            "spec": {
                "type": "object",
                "required": ["replicas"],
                "properties": {
                    "replicas": {"type": "integer", "minimum": 1},
                    "password": {"type": "string", "default": SENTINEL, "enum": [SENTINEL]},
                    "message": {"type": "string", "description": SENTINEL},
                },
            },
            "status": {"type": "object"},
        },
    }
    return store.put(
        {
            "apiVersion": "apiextensions.k8s.io/v1",
            "kind": "CustomResourceDefinition",
            "metadata": {"name": DEFINITION},
            "spec": {
                "group": GROUP,
                "scope": "Namespaced",
                "names": {"kind": "Widget", "plural": "widgets"},
                "versions": [
                    {
                        "name": "v1",
                        "served": True,
                        "storage": True,
                        "schema": {"openAPIV3Schema": schema},
                        "subresources": {"status": {}},
                    },
                    {"name": "v1alpha1", "served": False, "storage": False, "schema": {"openAPIV3Schema": schema}},
                ],
            },
            "status": {
                "conditions": [{"type": "Established", "status": "True"}, {"type": "NamesAccepted", "status": "True"}]
            },
        }
    )


@pytest.fixture
def environment(client, monkeypatch):
    monkeypatch.setitem(
        fake_store.RESOURCES,
        "apiextensions.k8s.io/v1",
        [("customresourcedefinitions", "CustomResourceDefinition", False)],
    )
    monkeypatch.setitem(fake_store.RESOURCES, GROUP + "/v1", [("widgets", "Widget", True)])
    store = CustomStore()
    definition = install_definition(store)
    kc.set_client_factory(store.factory)
    try:
        created = client.post("/api/v1/clusters/import", json={"name": "crd-test", "kubeconfig": kubeconfig_text()})
        assert created.status_code == 202
        base = f"/api/v1/clusters/{created.json()['cluster_id']}/custom-resources"
        yield client, base, store, definition
    finally:
        kc.set_client_factory(None)


def target(env, **changes):
    return {
        "definition": DEFINITION,
        "definition_uid": env[3]["metadata"]["uid"] if env else None,
        "version": "v1",
        "namespace": "default",
        "name": "proof",
        **changes,
    }


def review(env, action="create", **changes):
    client, base, *_ = env
    obj = {
        "apiVersion": GROUP + "/v1",
        "kind": "Widget",
        "metadata": {"name": "proof", "namespace": "default"},
        "spec": {"replicas": 1, "message": SENTINEL},
    }
    body = target(env, action=action, manifest=yaml.safe_dump(obj))
    if action != "create":
        body["expected_uid"] = env[2].objects[PATH]["metadata"]["uid"]
    if action == "update":
        body["expected_hash"] = cr.fingerprint(env[2].objects[PATH])
    body.update(changes)
    response = client.post(base + "/review", json=body)
    assert response.status_code == 200, response.text
    assert SENTINEL not in response.text
    return response.json()


def execute(env, reviewed):
    response = env[0].post(env[1] + "/execute", json={"review_id": reviewed["id"], "confirm": "proof"})
    assert response.status_code == 200, response.text
    assert SENTINEL not in response.text
    return response.json()


def test_inventory_schema_redaction_and_unserved_version(environment):
    client, base, *_ = environment
    inventory = client.get(base + "/definitions")
    assert inventory.status_code == 200 and inventory.json()["available"]
    assert SENTINEL not in inventory.text
    schema = client.get(base + "/definitions/" + DEFINITION, params={"version": "v1"})
    assert schema.status_code == 200 and SENTINEL not in schema.text
    assert any(field["path"] == "$.spec.replicas" and field["required"] for field in schema.json()["schema"]["fields"])
    assert client.get(base + "/definitions/" + DEFINITION, params={"version": "v1alpha1"}).status_code == 200
    assert (
        client.get(base + "/definitions/" + DEFINITION + "/resources", params={"version": "v1alpha1"}).status_code
        == 409
    )


def test_create_update_delete_encryption_audit_and_idempotence(environment):
    env = environment
    client, base, store, _ = env
    planned = review(env)
    assert PATH not in store.objects
    with get_session_factory()() as session:
        row = session.get(CustomResourceReview, uuid.UUID(planned["id"]))
        assert SENTINEL.encode() not in row.ciphertext and SENTINEL not in json.dumps(row.specification)
    created = execute(env, planned)
    assert created["status"] == "created"
    assert execute(env, planned) == created
    assert SENTINEL not in client.get(base + "/definitions/" + DEFINITION + "/resources", params={"version": "v1"}).text
    assert SENTINEL not in client.get(base + "/resource", params=target(env)).text
    revealed = client.post(base + "/reveal", json=target(env, expected_uid=created["uid"]))
    assert revealed.status_code == 200 and SENTINEL in revealed.text
    obj = yaml.safe_load(revealed.json()["edit_yaml"])
    obj["spec"]["replicas"] = 2
    updated = execute(env, review(env, "update", manifest=yaml.safe_dump(obj)))
    assert updated["uid"] == created["uid"] and store.objects[PATH]["spec"]["replicas"] == 2
    removed = execute(env, review(env, "delete"))
    assert removed["status"] == "deletion_requested" and PATH not in store.objects
    with get_session_factory()() as session:
        events = list(session.scalars(select(AuditEvent)))
        assert any(event.action == "custom_resource.reveal" for event in events)
        assert all(SENTINEL not in json.dumps(event.details) for event in events)


def test_validation_errors_do_not_return_values(environment):
    client, base, *_ = environment
    obj = {
        "apiVersion": GROUP + "/v1",
        "kind": "Widget",
        "metadata": {"name": "proof", "namespace": "default"},
        "spec": {"replicas": 0, "password": SENTINEL},
    }
    response = client.post(base + "/review", json=target(environment, action="create", manifest=yaml.safe_dump(obj)))
    assert response.status_code == 422 and SENTINEL not in response.text
    assert response.json()["error"]["details"]["fields"] == ["spec.replicas"]


@pytest.mark.parametrize("change", ["uid", "spec", "definition", "expiry"])
def test_changes_after_review_stop_mutation(environment, change):
    env = environment
    execute(env, review(env))
    planned = review(env, "delete")
    if change == "uid":
        env[2].objects[PATH]["metadata"]["uid"] = "replacement"
    elif change == "spec":
        env[2].objects[PATH]["spec"]["replicas"] = 3
    elif change == "definition":
        env[3]["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["extra"] = {"type": "string"}
    else:
        with get_session_factory()() as session:
            row = session.get(CustomResourceReview, uuid.UUID(planned["id"]))
            row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            session.commit()
    response = env[0].post(env[1] + "/execute", json={"review_id": planned["id"], "confirm": "proof"})
    assert response.status_code == 409 and PATH in env[2].objects


def test_delete_keeps_finalizers_and_reports_pending(environment):
    env = environment
    execute(env, review(env))
    env[2].objects[PATH]["metadata"]["finalizers"] = ["validation.example.com/hold"]
    env[2].pending = True
    result = execute(env, review(env, "delete"))
    assert result["status"] == "deletion_requested" and env[2].objects[PATH]["metadata"]["finalizers"] == [
        "validation.example.com/hold"
    ]


@pytest.mark.parametrize(
    "manifest",
    [
        "apiVersion: " + GROUP + "/v1\nkind: Widget\nmetadata:\n  name: other\n  namespace: default\nspec: {}",
        "apiVersion: v1\nkind: Secret\nmetadata:\n  name: proof\n  namespace: default\ndata: {}",
        "apiVersion: " + GROUP + "/v1\nkind: Widget\nmetadata:\n  name: proof\n  namespace: other\nspec: {}",
        "apiVersion: "
        + GROUP
        + "/v1\nkind: Widget\nmetadata:\n  name: proof\n  namespace: default\n  finalizers: []\nspec: {}",
    ],
)
def test_manifest_cannot_escape_selected_identity_or_edit_finalizers(environment, manifest):
    assert (
        environment[0]
        .post(environment[1] + "/review", json=target(environment, action="create", manifest=manifest))
        .status_code
        == 422
    )


@pytest.mark.parametrize("code", [403, 404])
def test_unavailable_definition_inventory_is_not_an_empty_success(environment, code):
    environment[2].denied = code
    response = environment[0].get(environment[1] + "/definitions")
    assert response.status_code == 200 and not response.json()["available"] and SENTINEL not in response.text


def test_confirmation_and_cross_cluster_plan_binding(environment):
    env = environment
    planned = review(env)
    assert env[0].post(env[1] + "/execute", json={"review_id": planned["id"], "confirm": "wrong"}).status_code == 428
    cluster = (
        env[0]
        .post("/api/v1/clusters/import", json={"name": "other-crd-cluster", "kubeconfig": kubeconfig_text()})
        .json()["cluster_id"]
    )
    assert (
        env[0]
        .post(
            f"/api/v1/clusters/{cluster}/custom-resources/execute",
            json={"review_id": planned["id"], "confirm": "proof"},
        )
        .status_code
        == 404
    )


def test_status_only_change_does_not_overwrite_new_status(environment):
    env = environment
    execute(env, review(env))
    planned = review(env, "update")
    env[2].objects[PATH]["status"] = {"message": SENTINEL, "observedGeneration": 2}
    env[2].objects[PATH]["metadata"]["resourceVersion"] = "new-status-version"
    assert execute(env, planned)["status"] == "updated"
    assert env[2].objects[PATH]["status"]["message"] == SENTINEL


def test_recovery_recognizes_applied_marker_without_second_write(environment):
    env = environment
    planned = review(env)
    created = execute(env, planned)
    with get_session_factory()() as session:
        row = session.get(CustomResourceReview, uuid.UUID(planned["id"]))
        row.result = None
        session.commit()
    recovered = execute(env, planned)
    assert recovered["status"] == "already_applied" and recovered["uid"] == created["uid"]


def test_schema_reveal_is_explicit_and_audited(environment):
    response = environment[0].post(environment[1] + "/definitions/" + DEFINITION + "/reveal", json={"version": "v1"})
    assert response.status_code == 200 and SENTINEL in response.text and response.headers["cache-control"] == "no-store"
    with get_session_factory()() as session:
        assert session.scalar(select(AuditEvent).where(AuditEvent.action == "custom_resource.schema.reveal"))


def test_viewer_metadata_only_and_project_scope(identity: IdentityHarness, monkeypatch):
    monkeypatch.setitem(
        fake_store.RESOURCES,
        "apiextensions.k8s.io/v1",
        [("customresourcedefinitions", "CustomResourceDefinition", False)],
    )
    monkeypatch.setitem(fake_store.RESOURCES, GROUP + "/v1", [("widgets", "Widget", True)])
    store = CustomStore()
    install_definition(store)
    kc.set_client_factory(store.factory)
    try:
        headers = identity.bearer()
        created = identity.client.post(
            "/api/v1/clusters/import", headers=headers, json={"name": "crd-role-test", "kubeconfig": kubeconfig_text()}
        )
        base = f"/api/v1/clusters/{created.json()['cluster_id']}/custom-resources"
        obj = {
            "apiVersion": GROUP + "/v1",
            "kind": "Widget",
            "metadata": {"name": "proof", "namespace": "default"},
            "spec": {"password": SENTINEL},
        }
        store.put(obj)
        identity.role("viewer")
        assert identity.client.get(base + "/definitions", headers=headers).status_code == 200
        assert SENTINEL not in identity.client.get(base + "/resource", headers=headers, params=target(None)).text
        assert identity.client.post(base + "/reveal", headers=headers, json=target(None)).status_code == 403
        assert (
            identity.client.post(
                base + "/definitions/" + DEFINITION + "/reveal", headers=headers, json={"version": "v1"}
            ).status_code
            == 403
        )
        assert (
            identity.client.post(base + "/review", headers=headers, json=target(None, action="delete")).status_code
            == 403
        )
    finally:
        kc.set_client_factory(None)


def test_cluster_scoped_resource_uses_cluster_path_and_rejects_namespace(environment, monkeypatch):
    env = environment
    store = env[2]
    monkeypatch.setitem(
        fake_store.RESOURCES, GROUP + "/v1", [("widgets", "Widget", True), ("gadgets", "Gadget", False)]
    )
    definition = copy.deepcopy(env[3])
    definition["metadata"] = {"name": "gadgets." + GROUP}
    definition["spec"]["scope"] = "Cluster"
    definition["spec"]["names"] = {"kind": "Gadget", "plural": "gadgets"}
    definition = store.put(definition)
    obj = {"apiVersion": GROUP + "/v1", "kind": "Gadget", "metadata": {"name": "proof"}, "spec": {"replicas": 1}}
    body = target(
        env,
        definition="gadgets." + GROUP,
        definition_uid=definition["metadata"]["uid"],
        namespace=None,
        action="create",
        manifest=yaml.safe_dump(obj),
    )
    assert env[0].post(env[1] + "/review", json={**body, "namespace": "default"}).status_code == 422
    response = env[0].post(env[1] + "/review", json=body)
    assert response.status_code == 200, response.text
    assert execute(env, response.json())["status"] == "created"
    assert ROOT + "/gadgets/proof" in store.objects


def test_resource_pagination_is_forwarded_and_values_are_not_returned(environment, monkeypatch):
    store = environment[2]
    original = store.request

    def request(method, path, **kwargs):
        if method == "GET" and path == ROOT + "/widgets":
            token = (kwargs.get("params") or {}).get("continue")
            obj = {
                "apiVersion": GROUP + "/v1",
                "kind": "Widget",
                "metadata": {"name": "second" if token else "first", "namespace": "default", "uid": "uid"},
                "spec": {"password": SENTINEL},
            }
            return store._ok({"items": [obj], "metadata": {"continue": "" if token else "next-page"}})
        return original(method, path, **kwargs)

    monkeypatch.setattr(store, "request", request)
    path = environment[1] + "/definitions/" + DEFINITION + "/resources"
    first = environment[0].get(path, params={"version": "v1"})
    assert (
        first.json()["items"][0]["name"] == "first"
        and first.json()["continue_token"] == "next-page"
        and SENTINEL not in first.text
    )
    second = environment[0].get(path, params={"version": "v1", "continue_token": "next-page"})
    assert second.json()["items"][0]["name"] == "second" and not second.json()["continue_token"]


def test_definition_pagination_cannot_loop_forever(environment, monkeypatch):
    store = environment[2]
    original = store.request
    calls = []

    def request(method, path, **kwargs):
        if method == "GET" and path == cr.BASE:
            calls.append(path)
            return store._ok({"items": [], "metadata": {"continue": "same-token"}})
        return original(method, path, **kwargs)

    monkeypatch.setattr(store, "request", request)
    response = environment[0].get(environment[1] + "/definitions")
    assert response.status_code == 200 and response.json()["truncated"] and len(calls) == 2


def test_arbitrary_status_shape_is_not_exposed(environment):
    env = environment
    execute(env, review(env))
    env[2].objects[PATH]["status"] = SENTINEL
    response = env[0].get(env[1] + "/resource", params=target(env))
    assert (
        response.status_code == 200 and SENTINEL not in response.text and response.json()["summary"]["conditions"] == []
    )


def test_discovery_scope_mismatch_stops_resource_requests(environment, monkeypatch):
    store = environment[2]
    original = store.request

    def request(method, path, **kwargs):
        if path == ROOT:
            return store._ok({"resources": [{"name": "widgets", "kind": "Widget", "namespaced": False}]})
        return original(method, path, **kwargs)

    monkeypatch.setattr(store, "request", request)
    response = environment[0].get(
        environment[1] + "/definitions/" + DEFINITION + "/resources", params={"version": "v1"}
    )
    assert response.status_code == 409


def test_replaced_definition_cannot_be_silently_approved(environment):
    env = environment
    body = target(env, action="create", manifest="{}")
    env[3]["metadata"]["uid"] = "replaced-definition"
    assert env[0].post(env[1] + "/review", json=body).status_code == 409


def test_edit_preserves_owner_references_and_finalizers(environment):
    env = environment
    execute(env, review(env))
    live = env[2].objects[PATH]
    live["metadata"]["finalizers"] = ["validation.example.com/hold"]
    live["metadata"]["ownerReferences"] = [
        {"apiVersion": "v1", "kind": "ConfigMap", "name": "parent", "uid": "parent-uid"}
    ]
    data = env[0].post(env[1] + "/reveal", json=target(env)).json()
    assert "finalizers" not in yaml.safe_load(data["edit_yaml"])["metadata"]
    planned = review(env, "update", manifest=data["edit_yaml"])
    execute(env, planned)
    assert env[2].objects[PATH]["metadata"]["ownerReferences"] == live["metadata"]["ownerReferences"]
    assert env[2].objects[PATH]["metadata"]["finalizers"] == ["validation.example.com/hold"]


def test_malformed_yaml_errors_do_not_echo_sensitive_input(environment):
    response = environment[0].post(
        environment[1] + "/review", json=target(environment, action="create", manifest='spec: ["' + SENTINEL)
    )
    assert response.status_code == 422 and SENTINEL not in response.text


def test_edit_snapshot_change_before_review_requires_reveal_again(environment):
    env = environment
    execute(env, review(env))
    response = env[0].post(env[1] + "/reveal", json=target(env)).json()
    env[2].objects[PATH]["spec"]["replicas"] = 3
    body = target(
        env,
        action="update",
        expected_uid=env[2].objects[PATH]["metadata"]["uid"],
        expected_hash=response["content_hash"],
        manifest=response["edit_yaml"],
    )
    assert env[0].post(env[1] + "/review", json=body).status_code == 409
    assert env[2].objects[PATH]["spec"]["replicas"] == 3


def test_review_belongs_to_the_user_who_created_it(environment):
    env = environment
    planned = review(env)
    with get_session_factory()() as session:
        row = session.get(CustomResourceReview, uuid.UUID(planned["id"]))
        row.created_by = "another-user"
        session.commit()
    assert env[0].post(env[1] + "/execute", json={"review_id": planned["id"], "confirm": "proof"}).status_code == 404
    assert PATH not in env[2].objects


def test_establishment_and_live_names_are_rechecked(environment):
    env = environment
    env[3]["status"]["conditions"].append({"type": "NamesAccepted", "status": "False"})
    assert env[0].get(env[1] + "/definitions/" + DEFINITION + "/resources", params={"version": "v1"}).status_code == 409


def test_editable_does_not_mutate_revealed_snapshot():
    original = {
        "metadata": {"name": "proof", "annotations": {cr.MARKER: "prior-review", "example.com/keep": "yes"}},
        "spec": {"replicas": 1},
    }
    snapshot = copy.deepcopy(original)
    result = cr.editable(original)
    assert original == snapshot
    assert cr.MARKER not in result["metadata"]["annotations"]
    assert cr.fingerprint(original) == cr.fingerprint(snapshot)


def test_delete_review_does_not_delete_or_mark_resource(environment):
    env = environment
    execute(env, review(env))
    before = copy.deepcopy(env[2].objects[PATH])
    env[2].pending = True
    planned = review(env, "delete")
    assert env[2].objects[PATH] == before
    assert execute(env, planned)["status"] == "deletion_requested"
    assert env[2].objects[PATH]["metadata"]["deletionTimestamp"]
