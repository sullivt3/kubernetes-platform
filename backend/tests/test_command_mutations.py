"""Real reviewed operation execution: drift, dry runs, reauthorization and recovery."""

from __future__ import annotations

import copy
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.errors import ValidationFailedError
from app.core.secrets import SealedSecret, get_secret_store
from app.db.session import get_session_factory
from app.modules.command_center import service as commands
from app.modules.helm import service as hs
from app.modules.kubernetes.custom_resource_models import CustomResourceReview
from app.orchestration.models import OperationLog
from tests.test_advanced_automation import DEPLOY, SENTINEL, env  # noqa: F401
from tests.test_helm import helm_environment, plan, submit  # noqa: F401
from tests.test_identity import identity  # noqa: F401


def base(env):
    return "/api/v1/clusters/" + env[1] + "/commands"


def review(env, command="kubectl scale deployment proof --replicas=2"):
    result = env[0].post(base(env) + "/reviews", json={"command": command})
    assert result.status_code == 201, result.text
    assert SENTINEL not in result.text
    return result.json()


def run(env, reviewed, confirm="proof"):
    result = env[0].post(base(env) + "/run", json={"review_id": reviewed["id"], "confirm": confirm})
    assert result.status_code == 202, result.text
    return env[0].get("/api/v1/operations/" + result.json()["id"]).json()


@pytest.mark.parametrize(
    "text",
    [
        "kubectl scale deployment proof --replicas=2",
        "kubectl scale sts db --replicas 0 -n apps",
        "kubectl rollout restart deploy proof -n apps",
        "helm rollback release 1 -n apps",
    ],
)
def test_mutations_canonicalize(text):
    value = commands.parse(text)
    assert commands.parse(value["command"]) == value


@pytest.mark.parametrize(
    "text",
    [
        "kubectl scale pods proof --replicas=1",
        "kubectl scale deployment --replicas=1",
        "kubectl scale deployment proof",
        "kubectl scale deployment proof --replicas=-1",
        "kubectl scale deployment proof --replicas=1001",
        "kubectl scale deployment proof --replicas=1 --replicas=2",
        "kubectl scale deployment proof --replicas=1 -A",
        "kubectl rollout undo deployment proof",
        "kubectl rollout restart statefulset db",
        "kubectl rollout restart deployment proof --force",
        "helm rollback release 0",
        "helm rollback release",
        "helm rollback release 1 --force-replace",
        "helm rollback release 1 --no-hooks",
        "helm uninstall release",
        "helm upgrade release chart",
        "kubectl scale deployment proof --replicas=1; id",
    ],
)
def test_other_mutations_and_flags_refused(text):
    with pytest.raises(ValidationFailedError):
        commands.parse(text)


@pytest.mark.parametrize(
    "command", ["kubectl scale deployment proof --replicas=2", "kubectl rollout restart deployment proof"]
)
def test_mutation_review_is_nonmutating_encrypted_and_requires_target(env, command):
    client, _, store = env
    before = copy.deepcopy(store.objects[DEPLOY])
    before["spec"]["template"]["spec"]["containers"][0]["env"] = [{"name": "PRIVATE", "value": SENTINEL}]
    store.objects[DEPLOY] = copy.deepcopy(before)
    reviewed = review(env, command)
    assert reviewed["read_only"] is False and reviewed["uid"] == before["metadata"]["uid"]
    assert store.objects[DEPLOY] == before
    with get_session_factory()() as session:
        row = session.get(CustomResourceReview, uuid.UUID(reviewed["id"]))
        assert SENTINEL.encode() not in row.ciphertext and SENTINEL not in json.dumps(row.specification)
        assert SENTINEL.encode() in get_secret_store().unseal(SealedSecret(row.key_id, row.ciphertext))
    assert (
        client.post(base(env) + "/run", json={"review_id": reviewed["id"], "confirm": "advanced-test"}).status_code
        == 428
    )
    operation = run(env, reviewed)
    assert operation["type"] == "cluster.command.mutate" and operation["status"] == "succeeded", operation
    live = store.objects[DEPLOY]
    assert live["spec"]["template"]["spec"] == before["spec"]["template"]["spec"]
    if "scale" in command:
        assert live["spec"]["replicas"] == 2
    else:
        assert live["spec"]["replicas"] == before["spec"]["replicas"]
        assert (
            live["spec"]["template"]["metadata"]["annotations"]["kubectl.kubernetes.io/restartedAt"]
            == reviewed["restart_at"]
        )
    result = client.get(base(env) + "/" + reviewed["id"] + "/output")
    assert SENTINEL not in result.text and json.loads(result.json()["result"]["output"])[0]["rollout_verified"] is False
    assert SENTINEL not in json.dumps(operation)
    assert run(env, reviewed)["id"] == operation["id"]


@pytest.mark.parametrize("change", ["uid", "spec", "deleting", "admission", "race", "last_applied"])
def test_drift_or_changed_admission_stops_mutation(env, monkeypatch, change):
    reviewed = review(env)
    store = env[2]
    if change == "uid":
        store.objects[DEPLOY]["metadata"]["uid"] = str(uuid.uuid4())
    elif change == "spec":
        store.objects[DEPLOY]["spec"]["replicas"] = 7
    elif change == "deleting":
        store.objects[DEPLOY]["metadata"]["deletionTimestamp"] = datetime.now(UTC).isoformat()
    elif change == "last_applied":
        store.objects[DEPLOY]["metadata"].setdefault("annotations", {})[
            "kubectl.kubernetes.io/last-applied-configuration"
        ] = "externally updated"
    elif change == "admission":
        store.mutate_workload = True
    else:
        original = store.request

        def request(method, path, **kwargs):
            result = original(method, path, **kwargs)
            if method == "PUT" and path == DEPLOY and (kwargs.get("params") or {}).get("dryRun") == "All":
                store.objects[DEPLOY]["metadata"]["resourceVersion"] = "999999"
            return result

        monkeypatch.setattr(store, "request", request)
    assert run(env, reviewed)["status"] == "failed"
    assert store.objects[DEPLOY]["spec"]["replicas"] == (7 if change == "spec" else 0)


def test_status_only_change_does_not_revoke_review(env):
    reviewed = review(env)
    env[2].objects[DEPLOY]["status"]["availableReplicas"] = 42
    env[2].objects[DEPLOY]["metadata"]["resourceVersion"] = "9999"
    assert run(env, reviewed)["status"] == "succeeded"


def test_retry_after_write_reuses_owned_action(env, monkeypatch):
    reviewed = review(env, "kubectl rollout restart deployment proof")
    original_request = env[2].request
    original_access = commands.tasks.access
    writes = []

    def request(method, path, **kwargs):
        result = original_request(method, path, **kwargs)
        if method == "PUT" and path == DEPLOY and (kwargs.get("params") or {}).get("dryRun") != "All":
            writes.append(path)
        return result

    def access(*args, **kwargs):
        if writes:
            raise RuntimeError("test-only interruption after acceptance")
        return original_access(*args, **kwargs)

    monkeypatch.setattr(env[2], "request", request)
    monkeypatch.setattr(commands.tasks, "access", access)
    operation = run(env, reviewed)
    assert operation["status"] == "failed" and len(writes) == 1
    accepted = copy.deepcopy(env[2].objects[DEPLOY])
    monkeypatch.setattr(commands.tasks, "access", original_access)
    retry = env[0].post("/api/v1/operations/" + operation["id"] + "/retry")
    assert retry.status_code == 202, retry.text
    final = env[0].get("/api/v1/operations/" + operation["id"]).json()
    assert final["status"] == "succeeded" and len(writes) == 1
    assert env[2].objects[DEPLOY] == accepted


@pytest.fixture
def rollback_env(helm_environment, monkeypatch):
    e = helm_environment
    assert submit(e, plan(e))["status"] == "succeeded"
    assert submit(e, plan(e, "upgrade"), "fixture")["status"] == "succeeded"
    fake = hs.Runner
    simulations = []

    class Runner(fake):
        def run(self, args, timeout=30):
            if "--dry-run=server" in args:
                simulations.append(args)
                return b"test-only simulated raw data must not escape"
            if args[:2] == ["get", "manifest"]:
                return (
                    b"apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: chart-config\n"
                    b"data:\n  private: test-only-secret\n"
                )
            return super().run(args, timeout)

    monkeypatch.setattr(hs, "Runner", Runner)
    yield e, simulations


def helm_review(rollback_env):
    e, _ = rollback_env
    response = e[0].post("/api/v1/clusters/" + e[2] + "/commands/reviews", json={"command": "helm rollback fixture 1"})
    assert response.status_code == 201, response.text
    assert "test-only-secret" not in response.text
    return response.json()


def helm_run(e, reviewed):
    base = "/api/v1/clusters/" + e[2] + "/commands"
    response = e[0].post(base + "/run", json={"review_id": reviewed["id"], "confirm": "fixture"})
    assert response.status_code == 202, response.text
    return e[0].get("/api/v1/operations/" + response.json()["id"]).json()


def test_helm_rollback_simulation_confirmation_and_existing_executor(rollback_env):
    e, simulations = rollback_env
    reviewed = helm_review(rollback_env)
    assert e[3][("default", "fixture")]["revision"] == 2 and len(simulations) == 1
    assert reviewed["revision"] == 1 and reviewed["expected_revision"] == 2
    operation = helm_run(e, reviewed)
    assert operation["status"] == "succeeded", operation
    assert e[3][("default", "fixture")]["revision"] == 3 and len(simulations) == 2
    actual = [a for a in e[5] if a[0] == "rollback"]
    assert len(actual) == 1 and "--no-hooks" in actual[0] and "--wait" in actual[0]
    with get_session_factory()() as session:
        assert all("test-only-secret" not in row.message for row in session.scalars(select(OperationLog)))


@pytest.mark.parametrize("change", ["target", "head", "revision", "expiry", "target_uid"])
def test_helm_rollback_refuses_changed_history(rollback_env, change):
    e, _ = rollback_env
    reviewed = helm_review(rollback_env)
    if change == "revision":
        e[3][("default", "fixture")]["revision"] = 4
    else:
        with get_session_factory()() as session:
            row = session.get(CustomResourceReview, uuid.UUID(reviewed["id"]))
            if change == "expiry":
                row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            session.commit()
        if change != "expiry":
            # The existing pooled fake retains the imported cluster's Secret inventory.
            from app.modules.cluster_lifecycle.models import Cluster
            from app.modules.kubernetes import client as kc
            from app.modules.kubernetes.service import api_for

            with get_session_factory()() as session:
                cluster = session.get(Cluster, uuid.UUID(e[2]))
                api = api_for(session, get_secret_store(), cluster)
                rev = 1 if change in {"target", "target_uid"} else 2
                path = f"/api/v1/namespaces/default/secrets/sh.helm.release.v1.fixture.v{rev}"
                obj = kc.get_json(api, path)
                if change == "target_uid":
                    obj["metadata"]["uid"] = str(uuid.uuid4())
                else:
                    obj["data"] = {"release": "changed-private-data"}
                api.put(obj)
    if change == "expiry":
        response = e[0].post(
            "/api/v1/clusters/" + e[2] + "/commands/run", json={"review_id": reviewed["id"], "confirm": "fixture"}
        )
        assert response.status_code == 409
    else:
        assert helm_run(e, reviewed)["status"] == "failed"
    assert not any(a[0] == "rollback" for a in e[5])


@pytest.mark.parametrize("moment", ["queued", "after-dry-run"])
def test_mutation_rechecks_original_human_before_write(identity, monkeypatch, executor, recording_dispatcher, moment):
    from app.api import deps
    from app.modules.kubernetes import client as kc
    from tests.fake_kube import kubeconfig_text
    from tests.test_advanced_automation import AutomationStore

    store = AutomationStore()
    kc.set_client_factory(store.factory)
    try:
        client = identity.client
        headers = identity.bearer()
        imported = client.post(
            "/api/v1/clusters/import", headers=headers, json={"name": "actor-change", "kubeconfig": kubeconfig_text()}
        )
        assert imported.status_code == 202
        path = "/api/v1/clusters/" + imported.json()["cluster_id"] + "/commands"
        reviewed = client.post(
            path + "/reviews", headers=headers, json={"command": "kubectl scale deployment proof --replicas=2"}
        ).json()
        monkeypatch.setattr(deps, "get_dispatcher", lambda: recording_dispatcher)
        queued = client.post(path + "/run", headers=headers, json={"review_id": reviewed["id"], "confirm": "proof"})
        assert queued.status_code == 202
        if moment == "queued":
            identity.role("viewer")
        else:
            original = store.request

            def request(method, url, **kwargs):
                result = original(method, url, **kwargs)
                if method == "PUT" and url == DEPLOY and (kwargs.get("params") or {}).get("dryRun") == "All":
                    identity.role("viewer")
                return result

            monkeypatch.setattr(store, "request", request)
        executor.execute(uuid.UUID(queued.json()["id"]))
        result = client.get("/api/v1/operations/" + queued.json()["id"], headers=headers).json()
        assert result["status"] == "failed" and store.objects[DEPLOY]["spec"]["replicas"] == 0
        assert (
            client.post(
                path + "/run", headers=headers, json={"review_id": reviewed["id"], "confirm": "proof"}
            ).status_code
            == 403
        )
    finally:
        kc.set_client_factory(None)


def test_statefulset_scaling_uses_its_own_identity(env):
    stateful = copy.deepcopy(env[2].objects[DEPLOY])
    stateful["kind"] = "StatefulSet"
    stateful["metadata"].update(name="db", uid=str(uuid.uuid4()))
    env[2].put(stateful)
    reviewed = review(env, "kubectl scale sts db --replicas=1")
    assert run(env, reviewed, "db")["status"] == "succeeded"
    assert env[2].objects["/apis/apps/v1/namespaces/default/statefulsets/db"]["spec"]["replicas"] == 1
    assert env[2].objects[DEPLOY]["spec"]["replicas"] == 0


def test_review_withholds_raw_helm_api_errors(env, monkeypatch):
    from app.modules.kubernetes.client import KubernetesAPIError

    def fail(*args, **kwargs):
        raise KubernetesAPIError(SENTINEL, http_status=500)

    monkeypatch.setattr(hs, "preview", fail)
    response = env[0].post(base(env) + "/reviews", json={"command": "helm rollback test 1"})
    assert response.status_code == 502 and SENTINEL not in response.text
    assert response.json()["error"]["code"] == "command_review_failed"


def test_legacy_mutation_review_requires_fresh_approval(env):
    reviewed = review(env)
    with get_session_factory()() as session:
        row = session.get(CustomResourceReview, uuid.UUID(reviewed["id"]))
        store = get_secret_store()
        payload = json.loads(store.unseal(SealedSecret(row.key_id, row.ciphertext)))
        payload["mutation"].pop("hash_version")
        sealed = store.seal(json.dumps(payload).encode())
        row.key_id, row.ciphertext = sealed.key_id, sealed.ciphertext
        session.commit()
    assert run(env, reviewed)["status"] == "failed"
    assert env[2].objects[DEPLOY]["spec"]["replicas"] == 0
