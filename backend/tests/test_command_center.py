"""Command parsing and real DB/API/operation boundaries; no arbitrary process mocks."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.core.errors import ValidationFailedError
from app.core.secrets import SealedSecret, get_secret_store
from app.db.session import get_session_factory
from app.modules.command_center import service as commands
from app.modules.kubernetes import client as kc
from app.modules.kubernetes.custom_resource_models import CustomResourceReview
from app.orchestration.models import OperationLog
from tests.fake_kube import FakeKube, kubeconfig_text
from tests.test_identity import IdentityHarness, identity  # noqa: F401

SENTINEL = "test-only-command-sensitive-payload"


@pytest.fixture
def env(client):
    store = FakeKube()
    kc.set_client_factory(store.factory)
    try:
        response = client.post(
            "/api/v1/clusters/import", json={"name": "command-test", "kubeconfig": kubeconfig_text()}
        )
        assert response.status_code == 202, response.text
        cid = response.json()["cluster_id"]
        store.requests.clear()
        yield client, "/api/v1/clusters/" + cid + "/commands", store
    finally:
        kc.set_client_factory(None)


def review(env, command="kubectl get pods -A"):
    response = env[0].post(env[1] + "/reviews", json={"command": command})
    assert response.status_code == 201, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()


def run(env, planned):
    response = env[0].post(env[1] + "/run", json={"review_id": planned["id"], "confirm": "command-test"})
    assert response.status_code == 202, response.text
    return env[0].get("/api/v1/operations/" + response.json()["id"]).json()


@pytest.mark.parametrize(
    "command",
    [
        "kubectl get pods; whoami",
        "kubectl get pods | cat",
        "kubectl get pods\nhelm list",
        "kubectl get pods && id",
        "kubectl get pods > x",
        "kubectl get pods $(id)",
        "kubectl get pods `id`",
        "kubectl get pods --kubeconfig=/tmp/private",
        "kubectl get pods --server=https://other",
        "kubectl get pods --token=private",
        "kubectl get --raw=/api/v1/secrets",
        "kubectl get pods --as=admin",
        "kubectl get secrets",
        "kubectl get configmaps",
        "kubectl get pods -o yaml",
        "kubectl describe pods",
        "kubectl exec test -- id",
        "kubectl delete pods test",
        "kubectl get pods/test",
        "kubectl get pods ../test",
        "kubectl get pods -A -n default",
        "kubectl get pods -n default -n other",
        "kubectl get pods -n",
        "kubectl get nodes -n default",
        "kubectl get pods test -A",
        "helm get values test",
        "helm install x y",
        "helm list --post-renderer=/tmp/private",
        "helm history test -A",
        "helm plugin list",
        "",
        "kubectl",
        "kubectl get pods " + "a" * 513,
    ],
)
def test_unsupported_input_fails_closed_without_echo(command):
    with pytest.raises(ValidationFailedError) as error:
        commands.parse(command)
    assert "private" not in str(error.value)
    assert "whoami" not in str(error.value)


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("kubectl get po -n apps", "kubectl get pods -n apps"),
        ("kubectl get nodes", "kubectl get nodes"),
        ("kubectl get pods web-1 --namespace=apps", "kubectl get pods web-1 -n apps"),
        ("helm list", "helm list -n default"),
        ("helm list --all-namespaces", "helm list -A"),
        ("helm history my-release -n apps", "helm history my-release -n apps"),
    ],
)
def test_supported_commands_are_canonical(command, expected):
    plan = commands.parse(command)
    assert plan["command"] == expected
    assert commands.parse(expected) == plan


def test_review_does_not_contact_cluster_and_output_is_private_encrypted(env):
    planned = review(env)
    assert env[2].requests == []
    env[2].pods[0]["spec"]["containers"][0]["env"] = [{"name": "PRIVATE", "value": SENTINEL}]
    env[2].pods[0]["status"]["message"] = SENTINEL
    operation = run(env, planned)
    assert operation["status"] == "succeeded", operation
    assert "coredns-1" not in json.dumps(operation)
    result = env[0].get(env[1] + "/" + planned["id"] + "/output")
    assert result.status_code == 200 and result.headers["cache-control"] == "no-store"
    assert SENTINEL not in result.text
    assert "coredns-1" in result.json()["result"]["output"]
    assert set(json.loads(result.json()["result"]["output"])[0]) <= {
        "name",
        "namespace",
        "kind",
        "uid",
        "phase",
        "ready",
    }
    assert all(method == "GET" for method, _ in env[2].requests)
    with get_session_factory()() as session:
        row = session.get(CustomResourceReview, uuid.UUID(planned["id"]))
        assert b"coredns-1" not in row.ciphertext
        decrypted = get_secret_store().unseal(SealedSecret(row.key_id, row.ciphertext))
        assert b"coredns-1" in decrypted
        assert "coredns-1" not in json.dumps(row.specification)
        assert all(
            "coredns-1" not in item.message and SENTINEL not in item.message
            for item in session.scalars(select(OperationLog))
        )
    duplicate = run(env, planned)
    assert duplicate["id"] == operation["id"]


def test_list_is_bounded_and_truncation_visible(env, monkeypatch):
    original = env[2].request

    def request(method, path, **kwargs):
        if path == "/api/v1/pods":
            assert kwargs["params"]["limit"] == 100
            assert kwargs["timeout"] == 10
            return 200, json.dumps({"items": env[2].pods * 40, "metadata": {"continue": "more"}}).encode()
        return original(method, path, **kwargs)

    monkeypatch.setattr(env[2], "request", request)
    planned = review(env)
    assert run(env, planned)["status"] == "succeeded"
    result = env[0].get(env[1] + "/" + planned["id"] + "/output").json()["result"]
    assert result["truncated"] is True and result["row_count"] <= 100
    assert len(result["output"].encode()) <= commands.OUTPUT_LIMIT


def test_named_resource_uses_selected_namespace(env, monkeypatch):
    original = env[2].request

    def request(method, path, **kwargs):
        if path == "/api/v1/namespaces/apps/pods/web-1":
            env[2].requests.append((method, path))
            return env[2]._ok(env[2].pods[1])
        return original(method, path, **kwargs)

    monkeypatch.setattr(env[2], "request", request)
    planned = review(env, "kubectl get pods web-1 -n apps")
    assert run(env, planned)["status"] == "succeeded"
    assert ("GET", "/api/v1/namespaces/apps/pods/web-1") in env[2].requests


def test_expiry_confirmation_and_actor_binding(env):
    planned = review(env)
    assert env[0].post(env[1] + "/run", json={"review_id": planned["id"], "confirm": "wrong"}).status_code == 428
    with get_session_factory()() as session:
        row = session.get(CustomResourceReview, uuid.UUID(planned["id"]))
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()
    assert env[0].post(env[1] + "/run", json={"review_id": planned["id"], "confirm": "command-test"}).status_code == 409
    with get_session_factory()() as session:
        row = session.get(CustomResourceReview, uuid.UUID(planned["id"]))
        row.created_by = "another-human"
        session.commit()
    assert env[0].get(env[1] + "/" + planned["id"] + "/output").status_code == 404
    assert env[0].get(env[1] + "/history").json() == []
    assert env[0].post(env[1] + "/run", json={"review_id": planned["id"], "confirm": "command-test"}).status_code == 404


def test_cluster_binding(env):
    planned = review(env)
    other = (
        env[0]
        .post("/api/v1/clusters/import", json={"name": "other-cluster", "kubeconfig": kubeconfig_text()})
        .json()["cluster_id"]
    )
    base = "/api/v1/clusters/" + other + "/commands"
    assert env[0].get(base + "/" + planned["id"] + "/output").status_code == 404
    assert env[0].post(base + "/run", json={"review_id": planned["id"], "confirm": "command-test"}).status_code == 404


@pytest.mark.parametrize("failure", ["http", "size", "malformed"])
def test_failure_raw_output_is_withheld(env, monkeypatch, failure):
    original = env[2].request

    def request(method, path, **kwargs):
        if path == "/api/v1/pods":
            return {
                "http": (500, SENTINEL.encode()),
                "size": (200, b"x" * (commands.RAW_LIMIT + 1)),
                "malformed": (200, SENTINEL.encode()),
            }[failure]
        return original(method, path, **kwargs)

    monkeypatch.setattr(env[2], "request", request)
    planned = review(env)
    operation = run(env, planned)
    assert operation["status"] == "failed" and SENTINEL not in json.dumps(operation)
    assert env[0].get(env[1] + "/" + planned["id"] + "/output").json()["result"] is None


def test_helm_uses_fixed_arguments_and_projects_metadata(env, monkeypatch):
    seen = []

    class Runner:
        def __init__(self, target, ctx):
            assert target.server == "https://10.0.0.11:6443"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def run(self, args, timeout):
            seen.append((args, timeout))
            return json.dumps(
                [
                    {
                        "name": "release",
                        "namespace": "apps",
                        "revision": 2,
                        "notes": SENTINEL,
                        "description": SENTINEL,
                        "values": {"private": SENTINEL},
                    }
                ]
            ).encode()

    monkeypatch.setattr(commands, "Runner", Runner)
    for command in ["helm list -A", "helm history release -n apps"]:
        planned = review(env, command)
        assert run(env, planned)["status"] == "succeeded"
        result = env[0].get(env[1] + "/" + planned["id"] + "/output")
        assert SENTINEL not in result.text
    assert seen[0] == (["list", "--output", "json", "--all-namespaces", "--max", "101"], 20)
    assert seen[1] == (
        ["history", "--output", "json", "release", "--namespace", "apps", "--max", "50"],
        20,
    )


@pytest.mark.parametrize("change", ["revoke", "expire", "cancel"])
def test_worker_rechecks_queued_command(identity: IdentityHarness, monkeypatch, executor, recording_dispatcher, change):
    from app.api import deps

    store = FakeKube()
    kc.set_client_factory(store.factory)
    try:
        client = identity.client
        headers = identity.bearer()
        cid = client.post(
            "/api/v1/clusters/import", headers=headers, json={"name": "actor-command", "kubeconfig": kubeconfig_text()}
        ).json()["cluster_id"]
        base = "/api/v1/clusters/" + cid + "/commands"
        planned = client.post(base + "/reviews", headers=headers, json={"command": "kubectl get pods -A"}).json()
        monkeypatch.setattr(deps, "get_dispatcher", lambda: recording_dispatcher)
        queued = client.post(
            base + "/run", headers=headers, json={"review_id": planned["id"], "confirm": "actor-command"}
        )
        assert queued.status_code == 202, queued.text
        oid = queued.json()["id"]
        store.requests.clear()
        if change == "revoke":
            identity.role("viewer")
        elif change == "expire":
            with get_session_factory()() as session:
                row = session.get(CustomResourceReview, uuid.UUID(planned["id"]))
                row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
                session.commit()
        else:
            assert client.post("/api/v1/operations/" + oid + "/cancel", headers=headers).status_code == 200
        executor.execute(uuid.UUID(oid))
        operation = client.get("/api/v1/operations/" + oid, headers=headers).json()
        assert operation["status"] == ("cancelled" if change == "cancel" else "failed"), operation
        assert store.requests == []
        identity.role("viewer")
        assert client.post(base + "/reviews", headers=headers, json={"command": "kubectl get nodes"}).status_code == 403
        assert client.get(base + "/history", headers=headers).status_code == 403
        assert client.get(base + "/" + planned["id"] + "/output", headers=headers).status_code == 403
    finally:
        kc.set_client_factory(None)


def test_bounded_transport_closes_response_without_loading_full_body():
    class Response:
        status = 200
        closed = released = False

        def read1(self, amount, **kwargs):
            return b"x" * amount

        def close(self):
            self.closed = True

        def release_conn(self):
            self.released = True

    response = Response()

    def request(*args, **kwargs):
        assert kwargs["preload_content"] is False
        return response

    client = kc.KubeClient.__new__(kc.KubeClient)
    client._base = "https://test.invalid"
    client._token = None
    client.target = SimpleNamespace(token_provider=None)
    client._timeout = SimpleNamespace(connect_timeout=5)
    client._pool = SimpleNamespace(request=request)
    with pytest.raises(kc.KubernetesAPIError, match="size limit"):
        client.request("GET", "/api/v1/pods", max_response_bytes=100)
    assert response.closed and response.released


def test_display_limit_keeps_complete_rows(env, monkeypatch):
    original = env[2].request

    def request(method, path, **kwargs):
        if path == "/api/v1/pods":
            obj = {"kind": "k" * 253, "metadata": {"name": "n" * 253, "namespace": "s" * 253, "uid": "u" * 253}}
            return 200, json.dumps({"items": [obj] * 100}).encode()
        return original(method, path, **kwargs)

    monkeypatch.setattr(env[2], "request", request)
    planned = review(env)
    assert run(env, planned)["status"] == "succeeded"
    result = env[0].get(env[1] + "/" + planned["id"] + "/output").json()["result"]
    rows = json.loads(result["output"])
    assert 0 < len(rows) < 100
    assert result["truncated"] is True
    assert len(result["output"].encode()) <= commands.OUTPUT_LIMIT


def test_bounded_transport_deadline_closes_slow_response(monkeypatch):
    closed = []
    clock = iter([0, 0, 11])
    monkeypatch.setattr(kc, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    client = kc.KubeClient.__new__(kc.KubeClient)
    client._base = "https://test.invalid"
    client._token = None
    client.target = SimpleNamespace(token_provider=None)
    client._timeout = SimpleNamespace(connect_timeout=5)
    response = SimpleNamespace(
        status=200,
        read1=lambda *a, **k: b"x",
        close=lambda: closed.append("closed"),
        release_conn=lambda: closed.append("released"),
    )
    client._pool = SimpleNamespace(request=lambda *a, **k: response)
    with pytest.raises(kc.KubernetesAPIError, match="time limit"):
        client.request("GET", "/api/v1/pods", max_response_bytes=100, timeout=10)
    assert closed == ["closed", "released"]
