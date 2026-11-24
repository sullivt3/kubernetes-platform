"""Real lease DB/operation/WebSocket boundaries with a Kubernetes API simulator."""

from __future__ import annotations

import asyncio
import base64
import copy
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import yaml
from sqlalchemy import select
from starlette.websockets import WebSocketDisconnect

from app.core.config import get_settings
from app.core.errors import InvalidStateError
from app.core.secrets import get_secret_store
from app.db.session import get_session_factory
from app.modules.cluster_lifecycle.models import Cluster
from app.modules.command_center import sessions
from app.modules.command_center.session_models import CommandSession
from app.modules.kubernetes import client as kc
from app.modules.organizations.models import User
from app.orchestration.models import Operation, OperationLog
from tests import fake_store
from tests.fake_kube import kubeconfig_text
from tests.fake_store import FakeStore
from tests.test_identity import identity  # noqa: F401

IMAGE = "docker.io/example/runtime@sha256:" + "a" * 64
TOKEN = "test-only-bound-session-token-sentinel"


class SessionStore(FakeStore):
    def __init__(self):
        super().__init__()
        self.put_namespace("kube-system")
        self.long_token = False
        self.fail_cleanup = False
        self.stuck_namespace = False
        self.tokens = []
        self.put(
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {"name": "kubernetes", "namespace": "default"},
                "spec": {"clusterIP": "10.96.0.1"},
            }
        )
        self.put(
            {
                "apiVersion": "v1",
                "kind": "Endpoints",
                "metadata": {"name": "kubernetes", "namespace": "default"},
                "subsets": [{"addresses": [{"ip": "192.0.2.10"}], "ports": [{"port": 6443}]}],
            }
        )

    def put(self, obj):
        result = super().put(obj)
        result["metadata"]["resourceVersion"] = str(self.uid)
        return result

    def request(self, method, path, *, body=None, params=None, **kwargs):
        payload = json.loads(body) if isinstance(body, bytes) else copy.deepcopy(body)
        if self.fail_cleanup and "/clusterrolebindings/" in path:
            return 503, b"raw-private-error"
        if path.endswith("/serviceaccounts/session/token"):
            self.tokens.append(payload)
            return self._ok(
                {
                    "status": {
                        "token": TOKEN,
                        "expirationTimestamp": (
                            datetime.now(UTC) + timedelta(seconds=3600 if self.long_token else 600)
                        ).isoformat(),
                    }
                }
            )
        if method == "POST" and "/namespaces/" in path and payload:
            payload.setdefault("metadata", {}).setdefault("namespace", path.split("/namespaces/")[1].split("/")[0])
        if method == "PUT":
            if path not in self.objects:
                return self._err(404, "missing")
            if payload["metadata"].get("resourceVersion") != self.objects[path]["metadata"]["resourceVersion"]:
                return self._err(409, "changed")
            return self._ok(self.put(payload))
        if method == "DELETE" and path.startswith("/api/v1/namespaces/kp-shell-"):
            if self.stuck_namespace:
                return self._ok({"status": "Success"})
            ns = path.rsplit("/", 1)[1]
            for key in list(self.objects):
                if "/namespaces/" + ns + "/" in key:
                    del self.objects[key]
        result = super().request(method, path, body=payload, params=params, **kwargs)
        if method == "POST" and path.endswith("/pods") and result[0] < 400:
            pod = json.loads(result[1])
            pod["status"] = {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}]}
            self.objects[path + "/" + pod["metadata"]["name"]] = pod
            return self._ok(pod, 201)
        return result


@pytest.fixture
def env(client, monkeypatch):
    monkeypatch.setitem(fake_store.RESOURCES, "v1", [*fake_store.RESOURCES["v1"], ("endpoints", "Endpoints", True)])
    monkeypatch.setenv("COMMAND_CENTER_RUNTIME_IMAGE", IMAGE)
    get_settings.cache_clear()
    store = SessionStore()
    kc.set_client_factory(store.factory)
    try:
        imported = client.post(
            "/api/v1/clusters/import", json={"name": "session-test", "kubeconfig": kubeconfig_text()}
        )
        assert imported.status_code == 202
        cid = imported.json()["cluster_id"]
        with get_session_factory()() as session:
            cluster = session.get(Cluster, uuid.UUID(cid))
            cluster.kubernetes_version = "v1.37.2"
            session.commit()
        yield client, cid, store
    finally:
        kc.set_client_factory(None)
        get_settings.cache_clear()


def base(env):
    return "/api/v1/clusters/" + env[1] + "/command-sessions"


def start(env):
    response = env[0].post(base(env), json={"confirm": "session-test", "acknowledge_cluster_admin": True})
    assert response.status_code == 202, response.text
    assert TOKEN not in response.text
    return response.json()


def get_row(row):
    with get_session_factory()() as session:
        result = session.get(CommandSession, uuid.UUID(row["id"]))
        session.expunge(result)
        return result


def test_preparation_pod_hardening_bound_credential_and_no_parent_secret(env):
    row = start(env)
    assert row["status"] == "ready", row
    ns = row["namespace"]
    store = env[2]
    pod = store.objects["/api/v1/namespaces/" + ns + "/pods/runtime"]
    spec = pod["spec"]
    container = spec["containers"][0]
    assert container["image"] == IMAGE and spec["activeDeadlineSeconds"] == 600
    assert spec["securityContext"]["runAsNonRoot"] is True and spec["automountServiceAccountToken"] is False
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert container["securityContext"]["capabilities"]["drop"] == ["ALL"]
    assert store.tokens[0]["spec"]["boundObjectRef"]["uid"] == pod["metadata"]["uid"]
    assert store.tokens[0]["spec"]["expirationSeconds"] == 600
    secret = store.objects["/api/v1/namespaces/" + ns + "/secrets/credentials"]
    config = yaml.safe_load(base64.b64decode(secret["data"]["kubeconfig"]))
    assert config["users"][0]["user"] == {"token": TOKEN}
    assert config["clusters"][0]["cluster"]["server"] == "https://10.96.0.1:443"
    assert "client-key-data" not in json.dumps(config)
    assert TOKEN not in json.dumps(get_row(row).specification)
    with get_session_factory()() as session:
        op = session.get(Operation, uuid.UUID(row["operation_id"]))
        assert TOKEN not in json.dumps(op.params) and TOKEN not in json.dumps(op.result)
        assert all(TOKEN not in log.message for log in session.scalars(select(OperationLog)))
    policy = store.objects["/apis/networking.k8s.io/v1/namespaces/" + ns + "/networkpolicies/api-only"]["spec"]
    assert policy["ingress"] == [] and len(policy["egress"]) == 2


def test_confirmation_disabled_feature_version_skew_and_capacity(env, monkeypatch):
    client, cid, _ = env
    assert client.post(base(env), json={"confirm": "wrong", "acknowledge_cluster_admin": True}).status_code == 428
    assert (
        client.post(base(env), json={"confirm": "session-test", "acknowledge_cluster_admin": False}).status_code == 428
    )
    assert (
        client.post(base(env), json={"confirm": "session-test", "acknowledge_cluster_admin": "yes"}).status_code == 422
    )
    monkeypatch.setattr(get_settings(), "command_center_runtime_image", None)
    assert (
        client.post(base(env), json={"confirm": "session-test", "acknowledge_cluster_admin": True}).status_code == 409
    )
    monkeypatch.setenv("COMMAND_CENTER_RUNTIME_IMAGE", IMAGE)
    get_settings.cache_clear()
    with get_session_factory()() as session:
        cluster = session.get(Cluster, uuid.UUID(cid))
        cluster.kubernetes_version = "v1.30.1"
        session.commit()
    assert (
        client.post(base(env), json={"confirm": "session-test", "acknowledge_cluster_admin": True}).status_code == 409
    )
    with get_session_factory()() as session:
        cluster = session.get(Cluster, uuid.UUID(cid))
        cluster.kubernetes_version = "v1.37.2"
        session.commit()
    start(env)
    assert (
        client.post(base(env), json={"confirm": "session-test", "acknowledge_cluster_admin": True}).status_code == 409
    )


def test_close_revokes_subjects_and_deletes_only_owned_resources(env):
    row = start(env)
    result = env[0].post(base(env) + "/" + row["id"] + "/close")
    assert result.status_code == 202 and result.json()["status"] == "closed"
    assert not any(row["namespace"] in path for path in env[2].objects)
    assert env[0].post(base(env) + "/" + row["id"] + "/close").json()["status"] == "closed"


def test_unreachable_cleanup_is_durable_and_retried(env):
    row = start(env)
    env[2].fail_cleanup = True
    assert env[0].post(base(env) + "/" + row["id"] + "/close").json()["status"] == "closing"
    env[2].fail_cleanup = False
    sessions.reap()
    assert get_row(row).status == "closed"


def test_namespace_finalizer_does_not_retain_access_or_get_stripped(env):
    row = start(env)
    env[2].stuck_namespace = True
    assert env[0].post(base(env) + "/" + row["id"] + "/close").json()["status"] == "closing"
    assert sessions.binding_path(get_row(row)) not in env[2].objects
    env[2].stuck_namespace = False
    sessions.reap()
    assert get_row(row).status == "closed"


@pytest.mark.parametrize("resource", ["namespace", "binding"])
def test_foreign_or_replaced_resource_is_never_deleted(env, resource):
    row = start(env)
    record = get_row(row)
    path = sessions.ns_path(record) if resource == "namespace" else sessions.binding_path(record)
    env[2].objects[path]["metadata"]["uid"] = "foreign-replacement"
    env[0].post(base(env) + "/" + row["id"] + "/close")
    assert path in env[2].objects and get_row(row).status == "closing"


def test_long_lived_token_response_fails_closed_and_is_cleaned(env):
    env[2].long_token = True
    row = start(env)
    assert row["status"] == "closed"
    assert not any("kp-shell-" in path for path in env[2].objects)
    op = env[0].get("/api/v1/operations/" + row["operation_id"]).json()
    assert op["status"] == "failed" and TOKEN not in json.dumps(op)


def test_cleanup_cannot_race_an_inflight_provisioner(env):
    row = start(env)
    record = get_row(row)
    sessions.mark_closing(record.id, "test")
    with sessions.resource_lock(record.id) as acquired:
        assert acquired and sessions.cleanup(record.id) is False
        assert sessions.binding_path(record) in env[2].objects
    assert sessions.cleanup(record.id) is True and get_row(row).status == "closed"


def test_lease_unattached_and_abandoned_sessions_are_reaped(env):
    for status in ["ready", "active", "starting"]:
        row = start(env)
        with get_session_factory()() as session:
            record = session.get(CommandSession, uuid.UUID(row["id"]))
            record.status = status
            record.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            session.commit()
        sessions.reap()
        assert get_row(row).status == "closed"


def test_active_session_blocks_cluster_unregister(env):
    start(env)
    # Exercise the actual service method: API unregister route uses DELETE /clusters/{id}.
    from app.core.errors import InvalidStateError
    from app.core.principal import Principal
    from app.modules.kubernetes.service import KubernetesService

    with get_session_factory()() as session:
        cluster = session.get(Cluster, uuid.UUID(env[1]))
        service = KubernetesService(session, get_secret_store(), None)
        with pytest.raises(InvalidStateError, match="End interactive"):
            service.unregister(Principal.system(cluster.organization_id, "test"), cluster.id, confirm=cluster.name)


@pytest.mark.parametrize("origin", [None, "https://untrusted.example"])
def test_websocket_requires_browser_origin(env, origin):
    row = start(env)
    headers = {} if origin is None else {"Origin": origin}
    with (
        pytest.raises(WebSocketDisconnect),
        env[0].websocket_connect(base(env) + "/" + row["id"] + "/attach", headers=headers),
    ):
        pass
    assert get_row(row).status == "ready"


def test_single_attachment_disconnect_cleanup_and_no_transcript(env, monkeypatch):
    from app.api.routes import command_sessions

    async def bridge(browser, *args, **kwargs):
        await browser.send_json({"type": "output", "data": TOKEN})
        await browser.receive_json()
        return "closed"

    monkeypatch.setattr(command_sessions.terminal, "bridge", bridge)
    row = start(env)
    url = base(env) + "/" + row["id"] + "/attach"
    with env[0].websocket_connect(url, headers={"Origin": "http://localhost:3000"}) as ws:
        assert ws.receive_json()["data"] == TOKEN
        with (
            pytest.raises(WebSocketDisconnect),
            env[0].websocket_connect(url, headers={"Origin": "http://localhost:3000"}),
        ):
            pass
        ws.send_json({"type": "input", "data": "exit\n"})
        assert ws.receive()["type"] == "websocket.close"
    assert get_row(row).status == "closed" and TOKEN not in json.dumps(get_row(row).specification)


def test_operator_cannot_start_interactive_sessions(identity, monkeypatch):
    response = identity.client.post(
        "/api/v1/clusters/" + str(uuid.uuid4()) + "/command-sessions",
        headers=identity.bearer(),
        json={"confirm": "x", "acknowledge_cluster_admin": True},
    )
    assert response.status_code == 403


def test_session_transport_limits():
    from app.api.routes.command_sessions import SessionSocket

    class Browser:
        async def send_json(self, message):
            raise AssertionError("Oversized output was forwarded")

        async def receive_text(self):
            return "x" * 20000

    adapter = SessionSocket(Browser())
    with pytest.raises(InvalidStateError):
        asyncio.run(adapter.receive_json())
    assert adapter.outcome == "input_limit"
    with pytest.raises(InvalidStateError):
        asyncio.run(adapter.send_json({"type": "output", "data": "x" * (2 * 1024 * 1024 + 1)}))
    assert adapter.outcome == "output_limit"


@pytest.fixture
def oidc_env(identity, monkeypatch):
    monkeypatch.setitem(fake_store.RESOURCES, "v1", [*fake_store.RESOURCES["v1"], ("endpoints", "Endpoints", True)])
    monkeypatch.setenv("COMMAND_CENTER_RUNTIME_IMAGE", IMAGE)
    get_settings.cache_clear()
    store = SessionStore()
    kc.set_client_factory(store.factory)
    with get_session_factory()() as session:
        user = session.get(User, identity.user_id)
        user.is_platform_admin = True
        session.commit()
    try:
        result = identity.client.post(
            "/api/v1/clusters/import",
            headers=identity.bearer(),
            json={"name": "session-test", "kubeconfig": kubeconfig_text()},
        )
        assert result.status_code == 202
        cid = result.json()["cluster_id"]
        with get_session_factory()() as session:
            cluster = session.get(Cluster, uuid.UUID(cid))
            cluster.kubernetes_version = "v1.37.2"
            session.commit()
        yield identity, cid, store
    finally:
        kc.set_client_factory(None)
        get_settings.cache_clear()


def oidc_start(env):
    actor, cid, _ = env
    response = actor.client.post(
        "/api/v1/clusters/" + cid + "/command-sessions",
        headers=actor.bearer(),
        json={"confirm": "session-test", "acknowledge_cluster_admin": True},
    )
    assert response.status_code == 202, response.text
    return response.json()


def revoke(actor):
    with get_session_factory()() as session:
        user = session.get(User, actor.user_id)
        user.is_platform_admin = False
        session.commit()


@pytest.mark.parametrize("when", ["queued", "before-credential"])
def test_original_admin_is_reauthorized_during_prepare(oidc_env, monkeypatch, executor, recording_dispatcher, when):
    from app.api import deps

    actor, _, store = oidc_env
    if when == "queued":
        monkeypatch.setattr(deps, "get_dispatcher", lambda: recording_dispatcher)
        row = oidc_start(oidc_env)
        revoke(actor)
        executor.execute(uuid.UUID(row["operation_id"]))
        sessions.reap()
    else:
        original = store.request

        def request(method, path, **kwargs):
            result = original(method, path, **kwargs)
            if path.endswith("/serviceaccounts/session/token"):
                revoke(actor)
            return result

        monkeypatch.setattr(store, "request", request)
        row = oidc_start(oidc_env)
    assert get_row(row).status == "closed"
    assert not any("kp-shell-" in key for key in store.objects)


def test_active_socket_revocation_closes_and_cleans(oidc_env, monkeypatch):
    from app.api.routes import command_sessions

    actor, cid, store = oidc_env

    async def bridge(browser, *args, **kwargs):
        await browser.send_json({"type": "output", "data": "ready"})
        await browser.receive_json()
        return "closed"

    monkeypatch.setattr(command_sessions.terminal, "bridge", bridge)
    row = oidc_start(oidc_env)
    url = "/api/v1/clusters/" + cid + "/command-sessions/" + row["id"] + "/attach"
    with actor.client.websocket_connect(url, headers={**actor.bearer(), "Origin": "http://localhost:3000"}) as ws:
        assert ws.receive_json()["data"] == "ready"
        revoke(actor)
        assert ws.receive()["type"] == "websocket.close"
    assert get_row(row).status == "closed" and not any("kp-shell-" in p for p in store.objects)


def test_author_isolation_and_closed_history_does_not_block_unregister(oidc_env):
    actor, cid, _ = oidc_env
    row = oidc_start(oidc_env)
    path = "/api/v1/clusters/" + cid + "/command-sessions/" + row["id"] + "/close"
    with get_session_factory()() as session:
        record = session.get(CommandSession, uuid.UUID(row["id"]))
        record.created_by = "another-person"
        session.commit()
    assert actor.client.post(path, headers=actor.bearer()).status_code == 404
    with get_session_factory()() as session:
        record = session.get(CommandSession, uuid.UUID(row["id"]))
        record.created_by = actor.settings.oidc_issuer_url + "|alice"
        session.commit()
    assert (
        actor.client.post(
            "/api/v1/clusters/" + cid + "/unregister", params={"confirm": "session-test"}, headers=actor.bearer()
        ).status_code
        == 409
    )
    assert actor.client.post(path, headers=actor.bearer()).json()["status"] == "closed"
    assert (
        actor.client.post(
            "/api/v1/clusters/" + cid + "/unregister", params={"confirm": "session-test"}, headers=actor.bearer()
        ).status_code
        == 204
    )


def test_close_during_preparation_cannot_leave_a_late_grant(env, monkeypatch):
    original = env[2].request

    def request(method, path, **kwargs):
        result = original(method, path, **kwargs)
        if method == "POST" and path.endswith("/clusterrolebindings"):
            obj = json.loads(result[1])
            sid = uuid.UUID(obj["metadata"]["labels"][sessions.LABEL])
            sessions.mark_closing(sid, "closed_during_prepare")
            assert sessions.cleanup(sid) is False
        return result

    monkeypatch.setattr(env[2], "request", request)
    row = start(env)
    assert get_row(row).status == "closed" and not any("kp-shell-" in path for path in env[2].objects)


def test_cancel_before_worker_never_creates_resources(env, monkeypatch, executor, recording_dispatcher):
    from app.api import deps

    monkeypatch.setattr(deps, "get_dispatcher", lambda: recording_dispatcher)
    row = start(env)
    assert row["status"] == "starting"
    assert env[0].post(base(env) + "/" + row["id"] + "/close").json()["status"] == "closed"
    executor.execute(uuid.UUID(row["operation_id"]))
    assert not env[2].tokens and not any("kp-shell-" in path for path in env[2].objects)


def test_cleanup_cannot_mistake_a_different_cluster_for_absent_resources(env):
    row = start(env)
    system = env[2].objects["/api/v1/namespaces/kube-system"]
    original = system["metadata"]["uid"]
    system["metadata"]["uid"] = "different-cluster-uid"
    assert env[0].post(base(env) + "/" + row["id"] + "/close").json()["status"] == "closing"
    assert sessions.binding_path(get_row(row)) in env[2].objects
    system["metadata"]["uid"] = original
    sessions.reap()
    assert get_row(row).status == "closed"


def test_connection_changes_are_blocked_until_session_cleanup(env):
    row = start(env)
    from app.core.principal import Principal
    from app.modules.kubernetes.service import KubernetesService

    with get_session_factory()() as session:
        cluster = session.get(Cluster, uuid.UUID(env[1]))
        service = KubernetesService(session, get_secret_store(), None)
        with pytest.raises(InvalidStateError, match="End interactive"):
            service.update_connection(
                Principal.system(cluster.organization_id, "test"),
                cluster.id,
                server_override="https://192.0.2.100:6443",
                tls_server_name=None,
            )
    env[0].post(base(env) + "/" + row["id"] + "/close")


def test_ordinary_resource_routes_cannot_access_runtime_credentials_or_shell(env):
    row = start(env)
    ns = row["namespace"]
    client = env[0]
    root = "/api/v1/clusters/" + env[1]
    response = client.post(root + "/secrets/" + ns + "/credentials/reveal")
    assert response.status_code == 403 and TOKEN not in response.text
    response = client.get(root + "/resources/pods/" + ns + "/runtime")
    assert response.status_code == 403
    listing = client.get(root + "/resources/pods").json()
    assert ns not in json.dumps(listing)
    with (
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect(root + "/pods/" + ns + "/runtime/exec", headers={"Origin": "http://localhost:3000"}),
    ):
        pass
    from app.core.errors import PermissionDeniedError
    from app.modules.kubernetes.reserved import ReservedAPI

    api = ReservedAPI(env[2])
    with pytest.raises(PermissionDeniedError):
        api.request(
            "POST", "/api/v1/namespaces", body={"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": ns}}
        )
    with pytest.raises(PermissionDeniedError):
        api.request("DELETE", "/api/v1/namespaces/" + ns)
    from app.modules.helm.service import inventory

    with pytest.raises(PermissionDeniedError):
        inventory(
            yaml.safe_dump(
                {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "steal", "namespace": ns}}
            ).encode(),
            "default",
        )
    assert get_row(row).status == "ready"
    client.post(base(env) + "/" + row["id"] + "/close")
