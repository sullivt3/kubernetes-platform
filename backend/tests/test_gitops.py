"""Reviewed commit delivery, encrypted state, drift and non-adoption boundaries."""

from __future__ import annotations

import copy
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.errors import InvalidStateError
from app.db.session import get_session_factory
from app.modules.gitops import resources
from app.modules.gitops import service as gs
from app.modules.gitops.models import GitOpsApplication, GitOpsReview
from app.modules.gitops.source import Configuration, GitSource
from app.modules.kubernetes import client as kc
from app.orchestration.models import OperationLog
from tests.fake_kube import kubeconfig_text
from tests.fake_store import FakeStore
from tests.test_identity import IdentityHarness, identity  # noqa: F401

SENTINEL = "test-only-gitops-secret-sentinel"


class VersionedStore(FakeStore):
    def put(self, obj):
        result = super().put(obj)
        result["metadata"]["resourceVersion"] = str(self.uid)
        return result


@pytest.fixture
def env(client, monkeypatch):
    store = VersionedStore()
    state = {
        "commit": "a" * 40,
        "objects": [
            {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "proof"}, "data": {"message": "one"}}
        ],
    }

    class Source:
        def __init__(self, *args):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def fetch(self, *, allow_empty=False):
            if not state["objects"] and not allow_empty:
                raise InvalidStateError("Empty source rejected")
            return state["commit"], copy.deepcopy(state["objects"])

    monkeypatch.setattr(gs, "GitSource", Source)
    kc.set_client_factory(store.factory)
    try:
        created = client.post("/api/v1/clusters/import", json={"name": "gitops-test", "kubeconfig": kubeconfig_text()})
        assert created.status_code == 202, created.text
        base = f"/api/v1/clusters/{created.json()['cluster_id']}/gitops/applications"
        body = {
            "name": "app",
            "url": "https://git.example.test/team/repo.git",
            "branch": "main",
            "path": "manifests",
            "namespace": "default",
        }
        response = client.post(base, json=body)
        assert response.status_code == 201, response.text
        yield client, base, response.json(), body, store, state
    finally:
        kc.set_client_factory(None)


def review(env):
    client, base, app, *_ = env
    response = client.post(base + "/" + app["id"] + "/review")
    assert response.status_code == 200, response.text
    assert SENTINEL not in response.text
    return response.json()


def sync(env, reviewed):
    client, base, app, *_ = env
    response = client.post(base + "/" + app["id"] + "/sync", json={"review_id": reviewed["id"], "confirm": "app"})
    assert response.status_code == 202, response.text
    return response.json()


def test_commit_pinning_drift_and_repeated_submission(env):
    _client, _base, _app, _body, store, state = env
    reviewed = review(env)
    state["commit"] = "b" * 40
    state["objects"][0]["data"]["message"] = "two"
    result = sync(env, reviewed)
    assert result["status"] == "succeeded", result
    assert result["result"]["commit"] == "a" * 40
    assert store.objects["/api/v1/namespaces/default/configmaps/proof"]["data"]["message"] == "one"
    assert sync(env, reviewed)["id"] == result["id"]
    assert review(env)["resources"][0]["action"] == "update"
    assert sync(env, review(env))["status"] == "succeeded"
    assert review(env)["resources"][0]["action"] == "unchanged"
    live = store.objects["/api/v1/namespaces/default/configmaps/proof"]
    live["data"]["message"] = "drift"
    assert review(env)["resources"][0]["action"] == "update"


def test_credential_and_manifest_encryption_without_payload_or_log_leaks(env):
    client, base, app, body, _store, state = env
    password = uuid.uuid4().hex
    response = client.put(
        base + "/" + app["id"], json={**body, "authentication": {"username": "git-user", "password": password}}
    )
    assert response.status_code == 200 and password not in response.text
    state["objects"] = [
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": "proof-secret"},
            "stringData": {"password": SENTINEL},
        }
    ]
    reviewed = review(env)
    result = sync(env, reviewed)
    assert result["status"] == "succeeded", result
    history = client.get(base + "/" + app["id"] + "/history")
    assert SENTINEL not in history.text and password not in history.text and SENTINEL not in json.dumps(result)
    with get_session_factory()() as session:
        row = session.get(GitOpsApplication, uuid.UUID(app["id"]))
        plan = session.get(GitOpsReview, uuid.UUID(reviewed["id"]))
        assert password.encode() not in row.ciphertext and SENTINEL.encode() not in plan.ciphertext
        assert all(
            password not in log.message and SENTINEL not in log.message for log in session.scalars(select(OperationLog))
        )


def test_existing_unowned_resource_is_not_adopted(env):
    *_, store, state = env
    store.put(state["objects"][0])
    response = env[0].post(env[1] + "/" + env[2]["id"] + "/review")
    assert response.status_code == 409
    assert resources.OWNER not in store.objects["/api/v1/namespaces/default/configmaps/proof"]["metadata"].get(
        "annotations", {}
    )


def test_resource_change_between_review_and_sync_stops_before_any_write(env):
    _client, _base, _app, _body, store, state = env
    reviewed = review(env)
    store.put(state["objects"][0])
    assert sync(env, reviewed)["status"] == "failed"
    assert not any(method == "POST" and path.endswith("/configmaps") for method, path, _ in store.requests)


@pytest.mark.parametrize(
    "change",
    [
        {"namespace": "other"},
        {"kind": "Namespace", "apiVersion": "v1"},
        {"metadata": {"name": "../unsafe"}},
        {"metadata": {"name": "x", "annotations": {resources.OWNER: "other"}}},
    ],
)
def test_scope_cluster_resources_and_reserved_metadata_rejected(env, change):
    *_, state = env
    if "namespace" in change:
        state["objects"][0]["metadata"]["namespace"] = change["namespace"]
    else:
        state["objects"][0].update(change)
    assert env[0].post(env[1] + "/" + env[2]["id"] + "/review").status_code == 409


@pytest.mark.parametrize("expire", [True, False])
def test_expiry_and_configuration_changes_invalidate_review(env, expire):
    client, base, app, body, *_ = env
    reviewed = review(env)
    if expire:
        with get_session_factory()() as session:
            row = session.get(GitOpsReview, uuid.UUID(reviewed["id"]))
            row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            session.commit()
    else:
        assert client.put(base + "/" + app["id"], json={**body, "branch": "release"}).status_code == 200
    assert (
        client.post(base + "/" + app["id"] + "/sync", json={"review_id": reviewed["id"], "confirm": "app"}).status_code
        == 409
    )


def test_typed_confirmation_and_registration_removal_preserve_resources(env):
    client, base, app, *_ = env
    reviewed = review(env)
    assert (
        client.post(
            base + "/" + app["id"] + "/sync", json={"review_id": reviewed["id"], "confirm": "wrong"}
        ).status_code
        == 428
    )
    assert sync(env, reviewed)["status"] == "succeeded"
    assert client.post(base + "/" + app["id"] + "/forget", json={"confirm": "app"}).status_code == 200
    assert "/api/v1/namespaces/default/configmaps/proof" in env[4].objects
    assert client.get(base).json() == []


def test_removed_manifest_is_retained(env):
    state = env[-1]
    state["objects"].append(
        {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "retained"}, "data": {"message": "retained"}}
    )
    assert sync(env, review(env))["status"] == "succeeded"
    state["objects"].pop()
    assert review(env)["retained"][0]["name"] == "retained"
    assert "/api/v1/namespaces/default/configmaps/retained" in env[4].objects


@pytest.mark.parametrize(
    "overrides",
    [
        {"branch": "a/../b"},
        {"path": "../secret"},
        {"path": "/etc"},
        {"url": "file:///etc"},
        {"url": "https://user:token@example.test/repo"},
        {"url": "https://example.test/repo?token=secret"},
    ],
)
def test_source_validation(overrides):
    with pytest.raises(ValueError):
        Configuration.model_validate({"url": "https://example.test/repo", "namespace": "default", **overrides})


def test_endpoint_policy_and_viewer_permissions(identity: IdentityHarness):
    store = VersionedStore()
    kc.set_client_factory(store.factory)
    try:
        headers = identity.bearer()
        created = identity.client.post(
            "/api/v1/clusters/import", headers=headers, json={"name": "gitops-roles", "kubeconfig": kubeconfig_text()}
        )
        base = f"/api/v1/clusters/{created.json()['cluster_id']}/gitops/applications"
        body = {
            "name": "roles",
            "url": "https://192.168.18.11:5443/repo",
            "namespace": "default",
            "allow_private_network": True,
        }
        assert identity.client.post(base, headers=headers, json=body).status_code == 403
        identity.role("org-admin")
        created = identity.client.post(base, headers=headers, json=body)
        assert created.status_code == 201
        identity.role("viewer")
        assert identity.client.get(base, headers=headers).status_code == 200
        assert identity.client.post(base + "/" + created.json()["id"] + "/review", headers=headers).status_code == 403
        assert identity.client.put(base + "/" + created.json()["id"], headers=headers, json=body).status_code == 403
    finally:
        kc.set_client_factory(None)


def test_direct_unapproved_workflow_is_rejected(env, deferred_service, executor, principal):
    reviewed = review(env)
    cluster = env[1].split("/")[4]
    operation = deferred_service.submit(
        principal,
        "cluster.gitops.sync",
        {
            "cluster_id": cluster,
            "cluster_name": "gitops-test",
            "application_id": env[2]["id"],
            "review_id": reviewed["id"],
        },
        confirm="gitops-test",
    )
    assert executor.execute(operation.id).value == "failed"
    assert "/api/v1/namespaces/default/configmaps/proof" not in env[4].objects


def test_partial_sync_retry_recognizes_only_its_own_completed_writes(env, monkeypatch):
    state = env[-1]
    state["objects"].append(
        {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "second"}, "data": {"message": "second"}}
    )
    reviewed = review(env)
    # The real write helper recognizes the review marker after a crash, preserving UID.
    with get_session_factory()() as session:
        row = session.get(GitOpsReview, uuid.UUID(reviewed["id"]))
        from app.core.secrets import SealedSecret, get_secret_store

        payload = json.loads(get_secret_store().unseal(SealedSecret(row.key_id, row.ciphertext)))
        objects = payload if isinstance(payload, list) else payload["apply"]
    first = resources.apply_reviewed(env[4], objects[0], uuid.UUID(env[2]["id"]), uuid.UUID(reviewed["id"]))
    uid = first["metadata"]["uid"]
    result = sync(env, reviewed)
    assert result["status"] == "succeeded", result
    assert env[4].objects["/api/v1/namespaces/default/configmaps/proof"]["metadata"]["uid"] == uid


def test_credential_rotation_invalidates_review_and_clear_is_explicit(env):
    client, base, app, body, *_ = env
    assert (
        client.put(
            base + "/" + app["id"], json={**body, "authentication": {"username": "user", "password": uuid.uuid4().hex}}
        ).status_code
        == 200
    )
    reviewed = review(env)
    response = client.put(base + "/" + app["id"], json=body)
    assert response.json()["authenticated"] is True
    assert (
        client.post(base + "/" + app["id"] + "/sync", json={"review_id": reviewed["id"], "confirm": "app"}).status_code
        == 409
    )
    assert client.put(base + "/" + app["id"], json={**body, "clear_credentials": True}).json()["authenticated"] is False


def test_source_symlinks_and_submodules_rejected(monkeypatch):
    source = GitSource(Configuration(url="https://git.example.test/repo", namespace="default"))

    def run(args, *unused):
        if args[0] == "fetch":
            return b""
        if args[0] == "rev-parse":
            return b"a" * 40
        return b"120000 blob " + b"b" * 40 + b"\tproof.yaml\0"

    monkeypatch.setattr(source, "run", run)
    try:
        with pytest.raises(InvalidStateError, match="symlinks"):
            source.fetch()
    finally:
        source.temp.cleanup()


def test_git_tunnel_refuses_other_origins_and_plain_http():
    import socket
    import time
    from urllib.parse import urlsplit

    from app.modules.gitops.tunnel import PinnedTunnel

    tunnel = PinnedTunnel("git.example.test", 443, "93.184.216.34", time.monotonic() + 10)
    try:
        for request in [
            b"CONNECT 169.254.169.254:443 HTTP/1.1\r\n\r\n",
            b"GET http://git.example.test/ HTTP/1.1\r\n\r\n",
        ]:
            with socket.create_connection(("127.0.0.1", urlsplit(tunnel.url).port), timeout=2) as client:
                client.sendall(request)
                assert b"403" in client.recv(4096)
    finally:
        tunnel.close()


def test_bootstrap_handoff_removes_only_own_update_entry(monkeypatch):
    manager = "gitops-" + uuid.uuid4().hex
    own = {
        "manager": manager,
        "operation": "Update",
        "fieldsType": "FieldsV1",
        "fieldsV1": {"f:data": {"f:message": {}}},
    }
    other = {
        "manager": "controller",
        "operation": "Update",
        "fieldsType": "FieldsV1",
        "fieldsV1": {"f:metadata": {"f:labels": {}}},
    }
    live = {"metadata": {"uid": "owned-uid", "resourceVersion": "1", "managedFields": [other, own]}}
    shared = {
        "metadata": {
            "uid": "owned-uid",
            "resourceVersion": "2",
            "managedFields": [other, own, {**own, "operation": "Apply"}],
        }
    }

    def apply(api, path, obj, identity, *, dry_run):
        assert not dry_run and identity == manager
        assert obj["metadata"]["resourceVersion"] == "1"
        return shared

    monkeypatch.setattr(resources, "patch", apply)

    class API:
        def request(self, method, path, **kwargs):
            operations = json.loads(kwargs["body"])
            assert operations[0] == {"op": "test", "path": "/metadata/uid", "value": "owned-uid"}
            assert operations[1] == {"op": "test", "path": "/metadata/resourceVersion", "value": "2"}
            assert operations[2] == {"op": "test", "path": "/metadata/managedFields/1", "value": own}
            assert operations[3] == {"op": "remove", "path": "/metadata/managedFields/1"}
            assert len(operations) == 4
            return 200, json.dumps(shared).encode()

    resources.finish_bootstrap(API(), "/owned", {"metadata": {}}, live, manager)


def test_foreign_field_conflicts_are_visible_and_block_sync(env, monkeypatch):
    assert sync(env, review(env))["status"] == "succeeded"
    store = env[4]
    original = store.request

    def request(method, path, **kwargs):
        if method == "PATCH" and kwargs.get("content_type") == "application/apply-patch+yaml":
            return 409, b'{"message":"sensitive conflict detail withheld"}'
        return original(method, path, **kwargs)

    monkeypatch.setattr(store, "request", request)
    reviewed = review(env)
    assert reviewed["resources"][0]["action"] == "conflict"
    assert env[0].get(env[1]).json()[0]["observation"]["status"] == "Conflict"
    response = env[0].post(env[1] + "/" + env[2]["id"] + "/sync", json={"review_id": reviewed["id"], "confirm": "app"})
    assert response.status_code == 409 and "sensitive" not in response.text
