"""Real persistence, approval routing and worker actions with simulated Kubernetes."""

from __future__ import annotations

import copy
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.errors import InvalidStateError, PermissionDeniedError
from app.db.session import get_session_factory
from app.modules.kubernetes import client as kc
from app.modules.kubernetes import remediation as r
from app.modules.kubernetes.remediation_models import RemediationRequest
from tests.fake_kube import kubeconfig_text
from tests.fake_store import FakeStore
from tests.test_identity import IdentityHarness, identity  # noqa: F401


def workload(store: FakeStore) -> dict:
    template = {
        "metadata": {"labels": {"app": "flaky"}},
        "spec": {"containers": [{"name": "app", "image": "example.test/app:v1"}]},
    }
    store.put(
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "app", "namespace": "default", "uid": "deploy", "generation": 1},
            "spec": {"replicas": 1, "template": template},
            "status": {"observedGeneration": 2, "availableReplicas": 1},
        }
    )
    store.put(
        {
            "apiVersion": "apps/v1",
            "kind": "ReplicaSet",
            "metadata": {
                "name": "app-rs",
                "namespace": "default",
                "uid": "rs",
                "ownerReferences": [{"kind": "Deployment", "name": "app", "uid": "deploy", "controller": True}],
            },
            "spec": {"replicas": 1, "template": template},
        }
    )
    pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": "flaky",
            "namespace": "default",
            "uid": "original",
            "resourceVersion": "1",
            "ownerReferences": [{"kind": "ReplicaSet", "name": "app-rs", "uid": "rs", "controller": True}],
        },
        "spec": template["spec"],
        "status": {
            "containerStatuses": [
                {"name": "app", "restartCount": 4, "ready": False, "state": {"waiting": {"reason": "CrashLoopBackOff"}}}
            ]
        },
    }
    store.put(pod)
    return pod


@pytest.fixture
def environment(client):
    store = FakeStore()
    kc.set_client_factory(store.factory)
    try:
        response = client.post(
            "/api/v1/clusters/import", json={"name": "remediation-test", "kubeconfig": kubeconfig_text()}
        )
        assert response.status_code == 202, response.text
        cluster_id = uuid.UUID(response.json()["cluster_id"])
        base = f"/api/v1/clusters/{cluster_id}/monitoring/remediation"
        pod = workload(store)
        yield client, store, cluster_id, base, pod
    finally:
        kc.set_client_factory(None)


def pending(env) -> uuid.UUID:
    client, _store, _cluster_id, base, _pod = env
    assert client.put(base, json={"enabled": True, "namespaces": ["default"], "grace_seconds": 60}).status_code == 200
    assert client.post(base + "/scan").status_code == 200
    rows = client.get(base).json()["requests"]
    assert len(rows) == 1 and rows[0]["status"] == "observing"
    with get_session_factory()() as session:
        row = session.get(RemediationRequest, uuid.UUID(rows[0]["id"]))
        row.first_seen_at = datetime.now(UTC) - timedelta(seconds=61)
        session.commit()
    assert client.post(base + "/scan").status_code == 200
    assert client.get(base).json()["requests"][0]["status"] == "pending"
    return uuid.UUID(rows[0]["id"])


def test_detection_never_mutates_and_approval_eviction_is_idempotent(environment, monkeypatch):
    client, store, _cluster_id, base, pod = environment
    assert client.get(base).json()["policy"]["enabled"] is False
    assert client.post(base + "/scan").json() == {"skipped": True}
    assert client.get(base).json()["requests"] == []
    ident = pending(environment)
    assert not any(path.endswith("/eviction") for method, path, _ in store.requests)
    assert client.post(f"{base}/{ident}/approve", json={"confirm": "wrong"}).status_code == 428
    replacement = copy.deepcopy(pod)
    replacement["metadata"].update(name="replacement", uid="new")
    replacement["status"] = {"conditions": [{"type": "Ready", "status": "True"}]}
    request = store.request

    def transport(method, path, **kwargs):
        response = request(method, path, **kwargs)
        if path.endswith("/eviction") and method == "POST" and response[0] < 300:
            assert kwargs["body"]["deleteOptions"]["preconditions"] == {"uid": "original", "resourceVersion": "1"}
            store.put(replacement)
        return response

    monkeypatch.setattr(store, "request", transport)
    response = client.post(f"{base}/{ident}/approve", json={"confirm": "remediation-test"})
    assert response.status_code == 202, response.text
    result = response.json()
    assert result["status"] == "succeeded", result
    assert result["result"]["replacement_ready"] is True
    assert client.post(f"{base}/{ident}/approve", json={"confirm": "remediation-test"}).json()["id"] == result["id"]
    assert len([path for method, path, _ in store.requests if path.endswith("/eviction") and method == "POST"]) == 1
    assert client.get(base).json()["requests"][0]["operation_status"] == "succeeded"


@pytest.mark.parametrize("case", ["budget", "changed", "recovered", "expired", "disabled", "replaced"])
def test_guarded_approval(environment, case):
    client, store, _cluster_id, base, _pod = environment
    ident = pending(environment)
    if case == "budget":
        store.pdb_blocked.add("default/flaky")
    elif case == "changed":
        store.objects["/apis/apps/v1/namespaces/default/deployments/app"]["spec"]["template"]["metadata"]["labels"][
            "new"
        ] = "revision"
    elif case == "recovered":
        store.objects["/api/v1/namespaces/default/pods/flaky"]["status"]["containerStatuses"][0]["ready"] = True
    elif case == "replaced":
        current = store.objects["/api/v1/namespaces/default/pods/flaky"]
        current["metadata"]["uid"] = "different-pod"
        current["status"]["conditions"] = [{"type": "Ready", "status": "True"}]
    elif case == "expired":
        with get_session_factory()() as session:
            session.get(RemediationRequest, ident).expires_at = datetime.now(UTC) - timedelta(seconds=1)
            session.commit()
    else:
        assert client.put(base, json={"enabled": False}).status_code == 200
    response = client.post(f"{base}/{ident}/approve", json={"confirm": "remediation-test"})
    if case in {"expired", "disabled"}:
        assert response.status_code == 409
    elif case == "recovered":
        assert response.json()["status"] == "succeeded"
        assert response.json()["result"]["action"] == "skipped_recovered"
    elif case == "replaced":
        assert response.json()["status"] == "succeeded"
        assert response.json()["result"]["action"] == "already_gone"
        assert not any(path.endswith("/eviction") for method, path, _ in store.requests)
    else:
        assert response.status_code == 202, response.text
        assert response.json()["status"] == "failed"
    assert "/api/v1/namespaces/default/pods/flaky" in store.objects


def test_scan_dedup_dismissal_recovery_and_cooldown(environment):
    client, store, _cluster_id, base, pod = environment
    ident = pending(environment)
    assert client.post(base + "/scan").status_code == 200
    assert len(client.get(base).json()["requests"]) == 1
    assert client.post(f"{base}/{ident}/dismiss").status_code == 200
    assert client.post(f"{base}/{ident}/approve", json={"confirm": "remediation-test"}).status_code == 409
    with get_session_factory()() as session:
        row = session.get(RemediationRequest, ident)
        row.approved_at = datetime.now(UTC)
        session.commit()
    pod2 = copy.deepcopy(pod)
    pod2["metadata"].update(name="flaky-new", uid="second")
    store.put(pod2)
    client.post(base + "/scan")
    assert len(client.get(base).json()["requests"]) == 1


@pytest.mark.parametrize("kind", ["standalone", "stateful", "host", "volume", "critical", "system"])
def test_unsafe_pods_are_rejected(kind):
    store = FakeStore()
    pod = workload(store)
    if kind == "standalone":
        pod["metadata"]["ownerReferences"] = []
    if kind == "stateful":
        pod["metadata"]["ownerReferences"][0]["kind"] = "StatefulSet"
    if kind == "host":
        pod["spec"]["hostNetwork"] = True
    if kind == "volume":
        pod["spec"]["volumes"] = [{"name": "data", "emptyDir": {}}]
    if kind == "critical":
        pod["spec"]["priority"] = 2_000_000_000
    if kind == "system":
        pod["metadata"]["namespace"] = "kube-system"
    with pytest.raises(InvalidStateError):
        r.snapshot(store, pod)


def test_transient_failures_and_system_policy_validation():
    assert (
        r.failing_reason(
            {
                "status": {
                    "containerStatuses": [{"restartCount": 2, "state": {"waiting": {"reason": "CrashLoopBackOff"}}}]
                }
            }
        )
        is None
    )
    for namespace in ["kube-system", "kplatform-monitoring"]:
        with pytest.raises(ValueError):
            r.RemediationPolicy(enabled=True, namespaces=[namespace])


def test_administrator_boundary_and_project_scope(identity: IdentityHarness):
    store = FakeStore()
    kc.set_client_factory(store.factory)
    try:
        headers = {**identity.bearer(), "X-Project-ID": str(identity.project_id)}
        response = identity.client.post(
            "/api/v1/clusters/import", headers=headers, json={"name": "guarded", "kubeconfig": kubeconfig_text()}
        )
        assert response.status_code == 202, response.text
        cluster = response.json()["cluster_id"]
        base = f"/api/v1/clusters/{cluster}/monitoring/remediation"
        assert identity.client.get(base, headers=headers).status_code == 200
        for role in ["operator", "viewer"]:
            identity.role(role)
            assert (
                identity.client.put(
                    base, headers=headers, json={"enabled": True, "namespaces": ["default"]}
                ).status_code
                == 403
            )
            assert identity.client.post(base + "/scan", headers=headers).status_code == 403
            assert (
                identity.client.post(
                    base + "/" + str(uuid.uuid4()) + "/approve", headers=headers, json={"confirm": "guarded"}
                ).status_code
                == 403
            )
        identity.role("org-admin")
        assert (
            identity.client.put(base, headers=headers, json={"enabled": True, "namespaces": ["default"]}).status_code
            == 200
        )
        from app.modules.organizations.models import Project

        with get_session_factory()() as session:
            sibling = Project(organization_id=identity.organization_id, name="Sibling", slug="remediation-sibling")
            session.add(sibling)
            session.commit()
            sibling_id = sibling.id
        hidden = {**identity.bearer(), "X-Project-ID": str(sibling_id)}
        assert identity.client.get(base, headers=hidden).status_code == 404
        assert identity.client.post(base + "/scan", headers=hidden).status_code == 404
    finally:
        kc.set_client_factory(None)


def test_unapproved_workflow_and_operator_retry_cannot_bypass_review(
    environment, deferred_service, executor, principal
):
    from dataclasses import replace

    _client, store, cluster_id, _base, _pod = environment
    ident = pending(environment)
    operation = deferred_service.submit(
        principal,
        r.WORKFLOW,
        {
            "cluster_id": str(cluster_id),
            "cluster_name": "remediation-test",
            "request_id": str(ident),
        },
        confirm="remediation-test",
    )
    assert executor.execute(operation.id).value == "failed"
    assert not any(path.endswith("/eviction") for method, path, _ in store.requests)
    operator = replace(principal, roles=frozenset({"operator"}))
    with pytest.raises(PermissionDeniedError):
        deferred_service.retry(operator, operation.id)


def test_failed_replacement_reports_failure_without_evicting_again(environment, monkeypatch):
    client, store, _cluster_id, base, _pod = environment
    ident = pending(environment)
    monkeypatch.setattr(r.time, "sleep", lambda _: None)
    response = client.post(f"{base}/{ident}/approve", json={"confirm": "remediation-test"})
    assert response.status_code == 202
    assert response.json()["status"] == "failed"
    assert "three minutes" in response.json()["error"]["message"]
    assert len([path for method, path, _ in store.requests if method == "POST" and path.endswith("/eviction")]) == 1


def test_observation_gap_resets_grace_and_recovery_resolves_proposal(environment):
    client, store, cluster_id, base, _pod = environment
    assert client.put(base, json={"enabled": True, "namespaces": ["default"]}).status_code == 200
    client.post(base + "/scan")
    with get_session_factory()() as session:
        row = session.scalars(select(RemediationRequest).where(RemediationRequest.cluster_id == cluster_id)).one()
        row.first_seen_at = datetime.now(UTC) - timedelta(minutes=10)
        row.last_seen_at = datetime.now(UTC) - timedelta(minutes=4)
        session.commit()
    client.post(base + "/scan")
    assert client.get(base).json()["requests"][0]["status"] == "observing"
    store.objects["/api/v1/namespaces/default/pods/flaky"]["status"]["containerStatuses"][0]["ready"] = True
    client.post(base + "/scan")
    assert client.get(base).json()["requests"][0]["status"] == "resolved"
