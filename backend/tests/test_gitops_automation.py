"""Persistent approval, periodic reconciliation and destructive pruning boundaries."""

from __future__ import annotations

import copy
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.errors import InvalidStateError
from app.db.session import get_session_factory
from app.modules.gitops import automation, pruning, resources
from app.modules.gitops import service as gs
from app.modules.gitops.models import GitOpsApplication, GitOpsReview
from app.modules.gitops.source import Configuration, GitSource
from app.orchestration.dispatch import InlineDispatcher
from app.orchestration.models import Operation, OperationLog
from tests.conftest import RecordingDispatcher
from tests.fake_kube import kubeconfig_text
from tests.test_gitops import env, review, sync  # noqa: F401
from tests.test_identity import IdentityHarness, identity  # noqa: F401

PATH = "/api/v1/namespaces/default/configmaps/proof"


def configure(env, **changes):
    client, base, app, *_ = env
    response = client.put(
        base + "/" + app["id"] + "/automation",
        json={
            "enabled": True,
            "confirm": "app",
            "generation": current(env)["generation"],
            "interval_seconds": 60,
            **changes,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def reviewed_prune(env, **changes):
    response = env[0].post(env[1] + "/" + env[2]["id"] + "/review", json={"prune": True, **changes})
    assert response.status_code == 200, response.text
    return response.json()


def tick(executor, publisher, *, when=None, dispatcher=None):
    return automation.tick(
        get_session_factory(),
        dispatcher=dispatcher or InlineDispatcher(lambda identifier: executor.execute(identifier)),
        publisher=publisher,
        now=when,
    )


def current(env):
    return env[0].get(env[1]).json()[0]


def test_automatic_reconcile_updates_and_unchanged_checks_do_not_write(env, executor, publisher):
    configure(env)
    when = datetime.now(UTC)
    assert tick(executor, publisher, when=when)["queued"] == 1
    assert env[4].objects[PATH]["data"]["message"] == "one"
    before = copy.deepcopy(env[4].objects[PATH])
    assert tick(executor, publisher, when=when + timedelta(seconds=61))["queued"] == 1
    assert env[4].objects[PATH] == before
    assert current(env)["observation"]["status"] == "InSync"
    env[-1]["objects"][0]["data"]["message"] = "two"
    env[-1]["commit"] = "b" * 40
    assert tick(executor, publisher, when=when + timedelta(seconds=122))["queued"] == 1
    assert env[4].objects[PATH]["data"]["message"] == "two"
    with get_session_factory()() as session:
        assert all(r.summary["automatic"] and r.approved_at for r in session.scalars(select(GitOpsReview)))
        assert all(
            o.status.value == "succeeded"
            for o in session.scalars(select(Operation).where(Operation.type == "cluster.gitops.reconcile"))
        )


def test_disabled_default_and_interval_are_respected(env, executor, publisher):
    when = datetime.now(UTC)
    assert tick(executor, publisher, when=when)["queued"] == 0
    configure(env, interval_seconds=300)
    assert tick(executor, publisher, when=when + timedelta(seconds=1))["queued"] == 1
    assert tick(executor, publisher, when=when + timedelta(seconds=2))["queued"] == 0


def test_pause_invalidates_already_queued_work(env, executor, publisher):
    configure(env)
    dispatcher = RecordingDispatcher()
    assert tick(executor, publisher, dispatcher=dispatcher)["queued"] == 1
    response = env[0].post(env[1] + "/" + env[2]["id"] + "/automation/pause")
    assert response.status_code == 200
    assert executor.execute(dispatcher.dispatched[0]).value == "failed"
    assert PATH not in env[4].objects and not current(env)["automation"]["enabled"]
    assert tick(executor, publisher, when=datetime.now(UTC) + timedelta(hours=1))["queued"] == 0


def test_busy_automatic_operation_is_not_duplicated_and_blocks_source_edits(env, executor, publisher):
    configure(env)
    dispatcher = RecordingDispatcher()
    when = datetime.now(UTC)
    assert tick(executor, publisher, when=when, dispatcher=dispatcher)["queued"] == 1
    assert tick(executor, publisher, when=when + timedelta(seconds=61), dispatcher=dispatcher)["skipped"] == 1
    assert len(dispatcher.dispatched) == 1
    assert env[0].put(env[1] + "/" + env[2]["id"], json=env[3]).status_code == 409


def test_source_changes_pause_automation_and_require_new_approval(env, executor, publisher):
    configure(env)
    response = env[0].put(env[1] + "/" + env[2]["id"], json={**env[3], "branch": "release"})
    assert response.status_code == 200
    assert not response.json()["automation"]["enabled"] and response.json()["next_reconcile_at"] is None
    assert tick(executor, publisher)["queued"] == 0


def test_pruning_is_opt_in_and_manual_review_is_explicit(env):
    env[-1]["objects"].append({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "obsolete"}})
    assert sync(env, review(env))["status"] == "succeeded"
    env[-1]["objects"].pop()
    assert sync(env, review(env))["status"] == "succeeded"
    old = "/api/v1/namespaces/default/configmaps/obsolete"
    assert old in env[4].objects
    planned = reviewed_prune(env)
    assert planned["prune"][0]["action"] == "delete"
    result = sync(env, planned)
    assert result["status"] == "succeeded", result
    assert result["result"]["pruned"] == 1 and old not in env[4].objects
    assert len(current(env)["inventory"]) == 1


def test_empty_source_pruning_requires_independent_opt_in(env):
    assert sync(env, review(env))["status"] == "succeeded"
    env[-1]["objects"] = []
    assert env[0].post(env[1] + "/" + env[2]["id"] + "/review", json={"prune": True}).status_code == 409
    assert PATH in env[4].objects
    planned = reviewed_prune(env, allow_empty=True)
    result = sync(env, planned)
    assert result["status"] == "succeeded", result
    assert PATH not in env[4].objects


def test_automatic_pruning_and_empty_source_guard(env, executor, publisher):
    configure(env, prune=True)
    when = datetime.now(UTC)
    assert tick(executor, publisher, when=when)["queued"] == 1
    env[-1]["objects"] = []
    assert tick(executor, publisher, when=when + timedelta(seconds=61))["queued"] == 1
    assert PATH in env[4].objects
    assert current(env)["observation"]["status"] == "Error"
    configure(env, prune=True, allow_empty=True)
    assert tick(executor, publisher, when=when + timedelta(seconds=122))["queued"] == 1
    assert PATH not in env[4].objects


def test_uid_changed_after_review_never_deletes_replacement(env):
    assert sync(env, review(env))["status"] == "succeeded"
    env[-1]["objects"] = []
    planned = reviewed_prune(env, allow_empty=True)
    live = env[4].objects[PATH]
    live["metadata"]["uid"] = "replacement-uid"
    result = sync(env, planned)
    assert result["status"] == "failed"
    assert PATH in env[4].objects and env[4].objects[PATH]["metadata"]["uid"] == "replacement-uid"


def test_owner_removed_after_review_never_deletes_target(env):
    assert sync(env, review(env))["status"] == "succeeded"
    env[-1]["objects"] = []
    planned = reviewed_prune(env, allow_empty=True)
    env[4].objects[PATH]["metadata"]["annotations"].pop(resources.OWNER)
    assert sync(env, planned)["status"] == "failed"
    assert PATH in env[4].objects


@pytest.mark.parametrize("annotation", [pruning.RETAIN, "helm.sh/resource-policy"])
def test_keep_annotations_are_retained_even_with_pruning(env, annotation):
    env[-1]["objects"][0]["metadata"]["annotations"] = {annotation: "keep"}
    assert sync(env, review(env))["status"] == "succeeded"
    env[-1]["objects"] = []
    planned = reviewed_prune(env, allow_empty=True)
    assert planned["prune"] == [] and planned["retained"][0]["name"] == "proof"
    assert sync(env, planned)["status"] == "succeeded" and PATH in env[4].objects


@pytest.mark.parametrize(
    "kind,version,spec",
    [("PersistentVolumeClaim", "v1", {"accessModes": ["ReadWriteOnce"]}), ("StatefulSet", "apps/v1", {"replicas": 1})],
)
def test_data_resources_are_retained(env, kind, version, spec):
    env[-1]["objects"] = [{"apiVersion": version, "kind": kind, "metadata": {"name": "data"}, "spec": spec}]
    assert sync(env, review(env))["status"] == "succeeded"
    env[-1]["objects"] = []
    planned = reviewed_prune(env, allow_empty=True)
    assert planned["prune"] == [] and planned["retained"][0]["kind"] == kind
    assert sync(env, planned)["status"] == "succeeded"


def test_failures_and_pause_mid_apply_do_not_prune(env, monkeypatch):
    env[-1]["objects"].append({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "obsolete"}})
    assert sync(env, review(env))["status"] == "succeeded"
    env[-1]["objects"].pop()
    env[-1]["objects"][0]["data"]["message"] = "new"
    planned = reviewed_prune(env)
    original = resources.apply_reviewed

    def apply(*args):
        result = original(*args)
        with get_session_factory()() as session:
            row = session.get(GitOpsApplication, uuid.UUID(env[2]["id"]))
            row.generation += 1
            session.commit()
        return result

    monkeypatch.setattr(resources, "apply_reviewed", apply)
    assert sync(env, planned)["status"] == "failed"
    assert "/api/v1/namespaces/default/configmaps/obsolete" in env[4].objects


def test_replacement_readiness_blocks_pruning(env, monkeypatch):
    env[-1]["objects"].append({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "obsolete"}})
    assert sync(env, review(env))["status"] == "succeeded"
    env[-1]["objects"] = [
        {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "replacement"}, "spec": {"replicas": 1}}
    ]
    monkeypatch.setattr(pruning, "WAIT_SECONDS", 0)
    result = sync(env, reviewed_prune(env))
    assert result["status"] == "failed"
    assert "/api/v1/namespaces/default/configmaps/obsolete" in env[4].objects and PATH in env[4].objects


def test_pending_finalizers_are_not_removed_and_inventory_is_retained(env, monkeypatch):
    assert sync(env, review(env))["status"] == "succeeded"
    env[-1]["objects"] = []
    planned = reviewed_prune(env, allow_empty=True)
    original = env[4].request

    def request(method, path, **kwargs):
        if method == "DELETE":
            options = json.loads(kwargs["body"])
            assert options["preconditions"]["uid"] == env[4].objects[PATH]["metadata"]["uid"]
            assert options["preconditions"]["resourceVersion"] == env[4].objects[PATH]["metadata"]["resourceVersion"]
            env[4].objects[PATH]["metadata"]["deletionTimestamp"] = "now"
            return 202, b"{}"
        return original(method, path, **kwargs)

    monkeypatch.setattr(env[4], "request", request)
    monkeypatch.setattr(pruning, "WAIT_SECONDS", 0)
    assert sync(env, planned)["status"] == "failed"
    assert PATH in env[4].objects and current(env)["inventory"][0]["name"] == "proof"


def test_request_now_only_works_under_an_active_approved_policy(env):
    path = env[1] + "/" + env[2]["id"] + "/automation/reconcile"
    assert env[0].post(path).status_code == 409
    configure(env)
    assert env[0].post(path).json()["scheduled"] is True


def test_operator_can_pause_but_cannot_enable_automation(identity: IdentityHarness):
    from tests.test_gitops import VersionedStore

    store = VersionedStore()
    kc = __import__("app.modules.kubernetes.client", fromlist=[""])
    kc.set_client_factory(store.factory)
    try:
        headers = identity.bearer()
        cluster = identity.client.post(
            "/api/v1/clusters/import", headers=headers, json={"name": "auto-roles", "kubeconfig": kubeconfig_text()}
        ).json()["cluster_id"]
        base = f"/api/v1/clusters/{cluster}/gitops/applications"
        app = identity.client.post(
            base,
            headers=headers,
            json={"name": "roles", "url": "https://git.example.test/repo", "namespace": "default"},
        ).json()
        path = base + "/" + app["id"] + "/automation"
        body = {"enabled": True, "prune": True, "confirm": "roles", "generation": app["generation"]}
        assert identity.client.put(path, headers=headers, json=body).status_code == 403
        identity.role("org-admin")
        assert identity.client.put(path, headers=headers, json=body).status_code == 200
        identity.role("operator")
        assert identity.client.post(path + "/pause", headers=headers).status_code == 200
        identity.role("viewer")
        assert identity.client.post(path + "/pause", headers=headers).status_code == 403
    finally:
        kc.set_client_factory(None)


def test_empty_source_cannot_use_missing_directory(monkeypatch):
    source = GitSource(Configuration(url="https://git.example.test/repo", path="missing", namespace="default"))

    def run(args, *unused):
        if args[0] == "fetch":
            return b""
        if args[0] == "rev-parse":
            return b"a" * 40
        if args[0] == "ls-tree":
            return b""
        raise InvalidStateError("Missing Git directory")

    monkeypatch.setattr(source, "run", run)
    try:
        with pytest.raises(InvalidStateError):
            source.fetch(allow_empty=True)
    finally:
        source.temp.cleanup()


def test_stale_policy_confirmation_is_rejected(env):
    generation = current(env)["generation"]
    assert env[0].put(env[1] + "/" + env[2]["id"], json={**env[3], "branch": "new-source"}).status_code == 200
    response = env[0].put(
        env[1] + "/" + env[2]["id"] + "/automation", json={"enabled": True, "confirm": "app", "generation": generation}
    )
    assert response.status_code == 409 and not current(env)["automation"]["enabled"]


def test_source_failure_never_prunes_and_raw_errors_are_withheld(env, executor, publisher, monkeypatch):
    configure(env, prune=True)
    when = datetime.now(UTC)
    assert tick(executor, publisher, when=when)["queued"] == 1
    secret = uuid.uuid4().hex

    class FailingSource:
        def __exit__(self, *args):
            pass

        def __init__(self, *args):
            pass

        def __enter__(self):
            raise InvalidStateError(secret)

    monkeypatch.setattr(gs, "GitSource", FailingSource)
    assert tick(executor, publisher, when=when + timedelta(seconds=61))["queued"] == 1
    assert PATH in env[4].objects and current(env)["observation"]["status"] == "Error"
    with get_session_factory()() as session:
        assert all(secret not in log.message for log in session.scalars(select(OperationLog)))
        assert all(secret not in json.dumps(op.error or {}) for op in session.scalars(select(Operation)))


def test_cluster_exclusivity_defers_reconciliation_without_fetch(env, executor, publisher, deferred_service, principal):
    configure(env)
    cluster = env[1].split("/")[4]
    deferred_service.submit(principal, "cluster.connect", {"cluster_id": cluster, "cluster_name": "gitops-test"})
    result = tick(executor, publisher)
    assert result["queued"] == 0 and result["skipped"] == 1 and PATH not in env[4].objects


def test_unknown_api_group_cannot_inherit_builtin_pruning_permission():
    assert pruning.protected({"apiVersion": "external.example/v1", "kind": "ConfigMap", "metadata": {}})


def test_pre_automation_encrypted_review_still_executes(env):
    planned = review(env)
    from app.core.secrets import SealedSecret, get_secret_store

    with get_session_factory()() as session:
        row = session.get(GitOpsReview, uuid.UUID(planned["id"]))
        store = get_secret_store()
        payload = json.loads(store.unseal(SealedSecret(row.key_id, row.ciphertext)))
        sealed = store.seal(json.dumps(payload["apply"]).encode())
        row.key_id = sealed.key_id
        row.ciphertext = sealed.ciphertext
        row.summary = {k: v for k, v in row.summary.items() if k not in {"prune", "automatic", "prune_enabled"}}
        session.commit()
    assert sync(env, planned)["status"] == "succeeded"


def test_auto_policy_confirmation_and_invalid_allow_empty_are_enforced(env):
    path = env[1] + "/" + env[2]["id"] + "/automation"
    generation = current(env)["generation"]
    assert env[0].put(path, json={"enabled": True, "confirm": "wrong", "generation": generation}).status_code == 428
    assert (
        env[0]
        .put(path, json={"enabled": True, "confirm": "app", "generation": generation, "allow_empty": True})
        .status_code
        == 422
    )
    assert env[0].post(env[1] + "/" + env[2]["id"] + "/review", json={"allow_empty": True}).status_code == 422
