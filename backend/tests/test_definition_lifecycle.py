"""Definition ownership, non-mutating previews and schema/storage/delete guards."""

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
from app.modules.kubernetes import definition_lifecycle as dl
from app.modules.kubernetes.custom_resource_models import CustomResourceReview
from tests import fake_store
from tests.fake_kube import kubeconfig_text
from tests.test_custom_resources import DEFINITION, GROUP, PATH, SENTINEL, CustomStore, install_definition
from tests.test_identity import IdentityHarness, identity  # noqa: F401

DEF_PATH = cr.BASE + "/" + DEFINITION


class DefinitionStore(CustomStore):
    def __init__(self):
        super().__init__()
        self.inventory_error = None
        self.inventory_extra = None

    def request(self, method, path, *, body=None, params=None, **kwargs):
        params = params or {}
        payload = json.loads(body) if isinstance(body, bytes) else body
        if method == "GET" and path == "/apis/" + GROUP + "/v1/widgets":
            if self.inventory_error:
                return self._err(self.inventory_error, SENTINEL)
            if self.inventory_extra:
                return self._ok(self.inventory_extra)
        if path.startswith(cr.BASE) and method in {"POST", "PUT", "DELETE"}:
            target = cr.BASE + "/" + payload["metadata"]["name"] if method == "POST" else path
            current = self.objects.get(target)
            if method == "POST" and current:
                return self._err(409, SENTINEL)
            if method != "POST" and not current:
                return self._err(404, SENTINEL)
            if method == "DELETE":
                if payload["preconditions"] != {k: current["metadata"][k] for k in ["uid", "resourceVersion"]}:
                    return self._err(409, SENTINEL)
                if payload.get("dryRun") != ["All"]:
                    if self.pending:
                        current["metadata"]["deletionTimestamp"] = "2026-11-23T00:00:00Z"
                    else:
                        self.objects.pop(target)
                return self._ok(current)
            if method == "PUT" and payload["metadata"].get("resourceVersion") != current["metadata"]["resourceVersion"]:
                return self._err(409, SENTINEL)
            # A schema error with sensitive raw server text must be sanitized.
            if not payload["spec"]["versions"][0].get("schema"):
                return self._err(422, SENTINEL, [{"field": "spec.versions[0].schema", "message": SENTINEL}])
            if params.get("dryRun") == "All":
                return self._ok(payload)
            obj = copy.deepcopy(payload)
            obj["status"] = {"storedVersions": ["v1"], "conditions": [{"type": "Established", "status": "True"}]}
            return self._ok(self.put(obj), 201 if method == "POST" else 200)
        return super().request(method, path, body=body, params=params, **kwargs)


@pytest.fixture
def env(client, monkeypatch):
    monkeypatch.setitem(
        fake_store.RESOURCES,
        "apiextensions.k8s.io/v1",
        [("customresourcedefinitions", "CustomResourceDefinition", False)],
    )
    monkeypatch.setitem(fake_store.RESOURCES, GROUP + "/v1", [("widgets", "Widget", True)])
    store = DefinitionStore()
    obj = install_definition(store)
    manifest = dl.editor_definition(obj)
    store.objects.pop(DEF_PATH)
    kc.set_client_factory(store.factory)
    try:
        imported = client.post(
            "/api/v1/clusters/import", json={"name": "definition-test", "kubeconfig": kubeconfig_text()}
        )
        assert imported.status_code == 202
        cluster_id = imported.json()["cluster_id"]
        yield client, f"/api/v1/clusters/{cluster_id}/custom-resources", store, manifest, cluster_id
    finally:
        kc.set_client_factory(None)


def plan(env, action="install", manifest=None, **extra):
    body = {"name": DEFINITION, "action": action, "manifest": yaml.safe_dump(manifest or env[3])}
    live = env[2].objects.get(DEF_PATH)
    if action != "install" and live:
        body.update(expected_uid=live["metadata"]["uid"], expected_hash=cr.fingerprint(live))
    body.update(extra)
    return env[0].post(env[1] + "/definition-reviews", json=body)


def apply(env, review, **extra):
    return env[0].post(env[1] + "/definition-execute", json={"review_id": review["id"], "confirm": DEFINITION, **extra})


def installed(env):
    planned = plan(env)
    assert planned.status_code == 200, planned.text
    result = apply(env, planned.json())
    assert result.status_code == 200, result.text
    return env[2].objects[DEF_PATH]


def test_install_reveal_update_remove_encryption_and_audit(env):
    preview = plan(env)
    assert preview.status_code == 200 and DEF_PATH not in env[2].objects
    assert SENTINEL not in preview.text
    with get_session_factory()() as session:
        row = session.get(CustomResourceReview, uuid.UUID(preview.json()["id"]))
        assert SENTINEL.encode() not in row.ciphertext and SENTINEL not in json.dumps(row.specification)
    result = apply(env, preview.json())
    assert result.status_code == 200 and result.json()["status"] == "installed"
    assert apply(env, preview.json()).json() == result.json()
    live = env[2].objects[DEF_PATH]
    assert dl.managed(live, env[4])
    listing = env[0].get(env[1] + "/definitions")
    assert listing.json()["items"][0]["managed"] is True and SENTINEL not in listing.text
    revealed = env[0].post(
        env[1] + "/definitions/" + DEFINITION + "/manifest/reveal",
        json={"name": DEFINITION, "expected_uid": live["metadata"]["uid"]},
    )
    assert revealed.status_code == 200 and SENTINEL in revealed.json()["yaml"]
    assert revealed.json()["content_hash"] == cr.fingerprint(live)
    assert revealed.headers["cache-control"] == "no-store"
    desired = yaml.safe_load(revealed.json()["edit_yaml"])
    desired["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["spec"]["properties"]["replicas"][
        "maximum"
    ] = 3
    changed = plan(env, "update", desired)
    assert changed.status_code == 200 and changed.json()["requires_empty"]
    assert apply(env, changed.json()).json()["status"] == "updated"
    before = copy.deepcopy(env[2].objects[DEF_PATH])
    deletion = plan(env, "remove")
    assert deletion.status_code == 200 and env[2].objects[DEF_PATH] == before
    assert apply(env, deletion.json()).json()["status"] == "deletion_requested"
    assert DEF_PATH not in env[2].objects
    with get_session_factory()() as session:
        events = list(session.scalars(select(AuditEvent)))
        assert any(e.action == "custom_definition.reveal" for e in events)
        assert any(e.action == "custom_definition.execute" for e in events)
        assert all(SENTINEL not in json.dumps(e.details) for e in events)


@pytest.mark.parametrize("action", ["update", "remove"])
def test_nonempty_blocks_schema_update_and_removal(env, action):
    installed(env)
    env[2].put(
        {
            "apiVersion": GROUP + "/v1",
            "kind": "Widget",
            "metadata": {"name": "proof", "namespace": "default"},
            "spec": {"replicas": 1},
        }
    )
    desired = copy.deepcopy(env[3])
    desired["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["description"] = "changed"
    response = plan(env, action, desired)
    assert response.status_code == 409 and "instances still exist" in response.text


def test_metadata_updates_preserve_instances_status_owners_and_finalizers(env):
    live = installed(env)
    live["metadata"]["finalizers"] = ["customresourcecleanup.apiextensions.k8s.io", "example.com/hold"]
    env[2].put(
        {
            "apiVersion": GROUP + "/v1",
            "kind": "Widget",
            "metadata": {"name": "proof", "namespace": "default"},
            "spec": {"replicas": 1},
        }
    )
    obj = dl.editor_definition(live)
    obj["metadata"]["labels"] = {"example.com/team": "platform"}
    preview = plan(env, "update", obj)
    assert preview.status_code == 200 and not preview.json()["requires_empty"]
    assert apply(env, preview.json()).status_code == 200
    assert env[2].objects[DEF_PATH]["metadata"]["finalizers"] == live["metadata"]["finalizers"]
    assert PATH in env[2].objects


@pytest.mark.parametrize("action", ["update", "remove"])
def test_new_instances_after_review_block_execution(env, action):
    live = installed(env)
    obj = dl.editor_definition(live)
    obj["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["description"] = "changed"
    preview = plan(env, action, obj)
    assert preview.status_code == 200
    env[2].put(
        {
            "apiVersion": GROUP + "/v1",
            "kind": "Widget",
            "metadata": {"name": "proof", "namespace": "default"},
            "spec": {"replicas": 1},
        }
    )
    assert apply(env, preview.json()).status_code == 409
    assert env[2].objects[DEF_PATH] == live and PATH in env[2].objects


@pytest.mark.parametrize("change", ["external", "helm", "gitops", "owner", "other-cluster"])
def test_nonadoption_and_foreign_ownership(env, change):
    live = installed(env)
    if change == "external":
        live["metadata"]["labels"].pop(dl.OWNER)
    if change == "helm":
        live["metadata"]["annotations"]["meta.helm.sh/release-name"] = "other"
    if change == "gitops":
        live["metadata"]["labels"]["kplatform.io/gitops-application"] = "other"
    if change == "owner":
        live["metadata"]["ownerReferences"] = [{"uid": "owner"}]
    if change == "other-cluster":
        live["metadata"]["labels"][dl.OWNER] = str(uuid.uuid4())
    assert plan(env).status_code == 409
    assert plan(env, "remove").status_code == 409
    assert plan(env, "update", dl.editor_definition(live)).status_code == 409


@pytest.mark.parametrize("change", ["storage", "remove", "unserve", "group", "scope", "names", "conversion"])
def test_version_and_conversion_guards(env, change):
    live = installed(env)
    desired = dl.editor_definition(live)
    if change == "storage":
        desired["spec"]["versions"][0]["storage"] = False
        desired["spec"]["versions"][1].update(storage=True, served=True)
    if change == "remove":
        desired["spec"]["versions"].pop()
    if change == "unserve":
        desired["spec"]["versions"].append(
            {**copy.deepcopy(desired["spec"]["versions"][0]), "name": "v2", "storage": False}
        )
        live["spec"]["versions"].append({**copy.deepcopy(live["spec"]["versions"][0]), "name": "v2", "storage": False})
        desired["spec"]["versions"][-1]["served"] = False
    if change == "group":
        desired["spec"]["group"] = "other.example.com"
    if change == "scope":
        desired["spec"]["scope"] = "Cluster"
    if change == "names":
        desired["spec"]["names"]["kind"] = "Different"
    if change == "conversion":
        desired["spec"]["conversion"] = {"strategy": "Webhook", "webhook": {}}
    assert plan(env, "update", desired).status_code in {409, 422}


@pytest.mark.parametrize("error", [403, 404, 500])
def test_unverifiable_instance_inventory_fails_closed(env, error):
    installed(env)
    env[2].inventory_error = error
    response = plan(env, "remove")
    assert response.status_code >= 400 and SENTINEL not in response.text
    assert DEF_PATH in env[2].objects


@pytest.mark.parametrize(
    "page",
    [
        {"metadata": {"continue": "more"}, "items": []},
        {"metadata": {"remainingItemCount": 1}, "items": []},
        {"items": None},
    ],
)
def test_uncertain_or_paginated_inventory_fails_closed(env, page):
    installed(env)
    env[2].inventory_extra = page
    assert plan(env, "remove").status_code == 409


def test_stale_snapshot_review_and_execute_and_stored_version_race(env):
    live = installed(env)
    old_hash = cr.fingerprint(live)
    live["metadata"]["labels"]["example.com/changed"] = "yes"
    assert plan(env, "update", dl.editor_definition(live), expected_hash=old_hash).status_code == 409
    preview = plan(env, "remove").json()
    live["status"]["storedVersions"].append("v0")
    assert apply(env, preview).status_code == 409
    live["status"]["storedVersions"] = ["v1"]
    live["metadata"]["uid"] = "replacement"
    assert apply(env, preview).status_code == 409


def test_confirmation_expiry_author_and_cross_endpoint_guards(env):
    preview = plan(env).json()
    assert apply(env, preview, confirm="wrong").status_code == 428
    assert env[0].post(env[1] + "/execute", json={"review_id": preview["id"], "confirm": DEFINITION}).status_code == 404
    with get_session_factory()() as session:
        row = session.get(CustomResourceReview, uuid.UUID(preview["id"]))
        row.created_by = "someone-else"
        session.commit()
    assert apply(env, preview).status_code == 404
    preview = plan(env).json()
    with get_session_factory()() as session:
        row = session.get(CustomResourceReview, uuid.UUID(preview["id"]))
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()
    assert apply(env, preview).status_code == 409


def test_remove_pending_finalizers_and_interrupted_write_retry(env):
    preview = plan(env).json()
    assert apply(env, preview).status_code == 200
    with get_session_factory()() as session:
        row = session.get(CustomResourceReview, uuid.UUID(preview["id"]))
        row.result = None
        session.commit()
    assert apply(env, preview).json()["status"] == "already_applied"
    env[2].pending = True
    env[2].objects[DEF_PATH]["metadata"]["finalizers"] = ["example.com/hold"]
    deletion = plan(env, "remove").json()
    assert apply(env, deletion).json()["status"] == "deletion_requested"
    assert env[2].objects[DEF_PATH]["metadata"]["finalizers"] == ["example.com/hold"]
    with get_session_factory()() as session:
        row = session.get(CustomResourceReview, uuid.UUID(deletion["id"]))
        row.result = None
        session.commit()
    assert apply(env, deletion).json()["status"] == "deletion_requested"


def test_raw_schema_validation_error_values_are_withheld(env):
    obj = copy.deepcopy(env[3])
    obj["spec"]["versions"][0].pop("schema")
    response = plan(env, manifest=obj)
    assert response.status_code == 422 and SENTINEL not in response.text
    assert response.json()["error"]["details"]["fields"] == ["spec.versions[0].schema"]


def test_definition_lifecycle_administrator_and_scope_checks(identity: IdentityHarness, monkeypatch):
    monkeypatch.setitem(
        fake_store.RESOURCES,
        "apiextensions.k8s.io/v1",
        [("customresourcedefinitions", "CustomResourceDefinition", False)],
    )
    store = DefinitionStore()
    obj = install_definition(store)
    kc.set_client_factory(store.factory)
    try:
        headers = identity.bearer()
        imported = identity.client.post(
            "/api/v1/clusters/import",
            headers=headers,
            json={"name": "definition-roles", "kubeconfig": kubeconfig_text()},
        )
        base = f"/api/v1/clusters/{imported.json()['cluster_id']}/custom-resources"
        for role in ["operator", "viewer"]:
            identity.role(role)
            assert (
                identity.client.post(
                    base + "/definition-reviews", headers=headers, json={"name": DEFINITION, "action": "remove"}
                ).status_code
                == 403
            )
            assert (
                identity.client.post(
                    base + "/definition-execute",
                    headers=headers,
                    json={"review_id": str(uuid.uuid4()), "confirm": DEFINITION},
                ).status_code
                == 403
            )
        reveal_path = base + "/definitions/" + DEFINITION + "/manifest/reveal"
        assert identity.client.post(reveal_path, headers=headers, json={"name": DEFINITION}).status_code == 403
        identity.role("org-admin")
        reveal = identity.client.post(
            reveal_path, headers=headers, json={"name": DEFINITION, "expected_uid": obj["metadata"]["uid"]}
        )
        assert reveal.status_code == 200
        identity.client.headers["X-Project-ID"] = str(uuid.uuid4())
        assert identity.client.post(reveal_path, headers=headers, json={"name": DEFINITION}).status_code in {403, 404}
    finally:
        kc.set_client_factory(None)


@pytest.mark.parametrize("text", ["", "---", "[]", "metadata: [", "true", "a: &a [*a]"])
def test_invalid_or_empty_yaml_is_sanitized(env, text):
    response = env[0].post(
        env[1] + "/definition-reviews", json={"name": DEFINITION, "action": "install", "manifest": text}
    )
    assert response.status_code == 422
