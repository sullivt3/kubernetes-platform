"""Namespace access is explicit, identity-bound, revocable and enforced in RBAC."""

import uuid

import pytest

from app.core.errors import PermissionDeniedError
from app.db.session import get_session_factory
from app.modules.command_center import access, sessions
from app.modules.command_center.session_models import CommandSession
from app.modules.organizations.identity import resolve_principal
from app.modules.organizations.models import User
from tests.test_command_sessions import get_row, oidc_env, revoke  # noqa: F401
from tests.test_identity import identity  # noqa: F401


def setup(env):
    actor, cid, store = env
    store.put_namespace("application")
    base = f"/api/v1/clusters/{cid}/command-sessions"
    response = actor.client.post(
        base + "/namespace-assignments",
        headers=actor.bearer(),
        json={"user_id": str(actor.user_id), "namespace": "application"},
    )
    assert response.status_code == 201, response.text
    revoke(actor)
    return actor, base, store, response.json()["id"]


def start(actor, base, namespace="application"):
    return actor.client.post(
        base,
        headers=actor.bearer(),
        json={"confirm": "session-test", "profile": "namespace-readonly", "target_namespace": namespace},
    )


def test_operator_gets_only_assigned_namespace_role_and_no_cluster_grant(oidc_env):
    actor, base, store, _ = setup(oidc_env)
    response = start(actor, base)
    assert response.status_code == 202, response.text
    row = response.json()
    assert row["status"] == "ready"
    assert row["profile"] == "namespace-readonly" and row["target_namespace"] == "application"
    ns = row["namespace"]
    assert not any("/clusterrolebindings/" in path for path in store.objects)
    role = store.objects[f"/apis/rbac.authorization.k8s.io/v1/namespaces/application/roles/{ns}"]
    assert role["rules"] == access.RULES
    for rule in role["rules"]:
        assert set(rule["verbs"]) <= {"get", "list", "watch"}
        assert not {
            "*",
            "secrets",
            "configmaps",
            "pods/log",
            "pods/exec",
            "pods/attach",
            "pods/portforward",
            "services/proxy",
            "roles",
            "rolebindings",
            "serviceaccounts/token",
        }.intersection(rule["resources"])
    assert start(actor, base, "default").status_code == 409
    assert (
        actor.client.post(
            base, headers=actor.bearer(), json={"confirm": "session-test", "acknowledge_cluster_admin": True}
        ).status_code
        == 403
    )
    assert actor.client.get(base + "/namespace-assignments", headers=actor.bearer()).status_code == 403
    response = actor.client.post(base + "/" + row["id"] + "/close", headers=actor.bearer())
    assert response.status_code == 202
    assert get_row(row).status == "closed"
    assert not any(ns in path for path in store.objects)


def test_revocation_closes_session_and_deletes_namespaced_grants(oidc_env):
    actor, base, store, grant_id = setup(oidc_env)
    row = start(actor, base).json()
    with get_session_factory()() as db:
        record = db.get(CommandSession, uuid.UUID(row["id"]))
        user = db.get(User, actor.user_id)
        principal = resolve_principal(db, user, record.organization_id, record.project_id)
    with get_session_factory()() as db:
        user = db.get(User, actor.user_id)
        user.is_platform_admin = True
        db.commit()
    response = actor.client.delete(base + "/namespace-assignments/" + grant_id, headers=actor.bearer())
    assert response.status_code == 204
    with pytest.raises(PermissionDeniedError), get_session_factory()() as db:
        access.authorize(db, principal, db.get(CommandSession, uuid.UUID(row["id"])))
    assert get_row(row).status == "closed"
    assert not any(row["namespace"] in path for path in store.objects)


@pytest.mark.parametrize("namespace", ["kube-system", "kp-shell-" + "a" * 32, "../other", "Invalid"])
def test_unsafe_namespace_assignment_refused(oidc_env, namespace):
    actor, cid, _ = oidc_env
    result = actor.client.post(
        f"/api/v1/clusters/{cid}/command-sessions/namespace-assignments",
        headers=actor.bearer(),
        json={"user_id": str(actor.user_id), "namespace": namespace},
    )
    assert result.status_code == 409


def test_recreated_namespace_cannot_reuse_old_assignment(oidc_env):
    actor, base, store, _ = setup(oidc_env)
    store.objects["/api/v1/namespaces/application"]["metadata"]["uid"] = str(uuid.uuid4())
    result = start(actor, base)
    assert result.status_code == 202
    assert result.json()["status"] == "closed"
    assert not store.tokens


def test_viewer_cannot_use_an_existing_assignment(oidc_env):
    actor, base, _, _ = setup(oidc_env)
    actor.role("viewer")
    assert start(actor, base).status_code == 403


def test_namespaced_binding_finalizer_revokes_subjects_before_cleanup_retry(oidc_env, monkeypatch):
    actor, base, store, _ = setup(oidc_env)
    row = start(actor, base).json()
    binding = f"/apis/rbac.authorization.k8s.io/v1/namespaces/application/rolebindings/{row['namespace']}"
    original = store.request

    def blocked(method, path, **kwargs):
        if method == "DELETE" and path == binding:
            return store._ok({"status": "Success"})
        return original(method, path, **kwargs)

    monkeypatch.setattr(store, "request", blocked)
    response = actor.client.post(base + "/" + row["id"] + "/close", headers=actor.bearer())
    assert response.status_code == 202 and get_row(row).status == "closing"
    assert store.objects[binding]["subjects"] == []
    monkeypatch.setattr(store, "request", original)
    assert sessions.cleanup(uuid.UUID(row["id"]))
    assert not any(row["namespace"] in path for path in store.objects)


def test_replaced_namespaced_role_is_preserved_and_cleanup_stays_pending(oidc_env):
    actor, base, store, _ = setup(oidc_env)
    row = start(actor, base).json()
    role_path = f"/apis/rbac.authorization.k8s.io/v1/namespaces/application/roles/{row['namespace']}"
    original_uid = store.objects[role_path]["metadata"]["uid"]
    store.objects[role_path]["metadata"]["uid"] = str(uuid.uuid4())
    actor.client.post(base + "/" + row["id"] + "/close", headers=actor.bearer())
    assert get_row(row).status == "closing"
    assert role_path in store.objects
    binding = role_path.replace("/roles/", "/rolebindings/")
    assert binding not in store.objects
    store.objects[role_path]["metadata"]["uid"] = original_uid
    assert sessions.cleanup(uuid.UUID(row["id"]))
