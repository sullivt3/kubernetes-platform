"""Real plan encryption, authorization and orchestration with simulated Helm/Kubernetes."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import tarfile
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import yaml
from sqlalchemy import select

from app.db.session import get_session_factory
from app.modules.helm import catalog, transport
from app.modules.helm import service as hs
from app.modules.helm.models import HelmPlan
from app.modules.helm.runtime import HelmError, Runner
from app.modules.kubernetes import client as kc
from app.orchestration.models import Operation, OperationLog, OperationStatus, StepStatus
from tests.fake_kube import kubeconfig_text
from tests.fake_store import FakeStore
from tests.test_identity import IdentityHarness, identity  # noqa: F401

SECRET = "test-only-helm-values-sentinel"


@pytest.fixture
def helm_environment(client, monkeypatch, tmp_path):
    store = FakeStore()
    current = {}
    revisions = {}
    commands = []
    fail = {"enabled": False}

    class FakeHelm:
        def __init__(self, *args):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def write(self, name, data):
            path = tmp_path / name
            path.write_bytes(data)
            return path

        def releases(self, namespace=None, name=None):
            return [
                copy.deepcopy(row)
                for (ns, release), row in current.items()
                if (not namespace or namespace == ns) and (not name or name == release)
            ]

        def release(self, namespace, name):
            return copy.deepcopy(current.get((namespace, name)))

        def history(self, namespace, name):
            return copy.deepcopy(revisions.get((namespace, name), []))

        def run(self, args, timeout=30):
            commands.append(args)
            if fail["enabled"]:
                raise HelmError("Helm failed; raw output withheld")
            if args[0] == "show":
                return b"replicaCount: 1\n"
            if args[0] == "template":
                return (
                    f"apiVersion: v1\nkind: Secret\nmetadata:\n  name: fixture-secret\nstringData:\n  token: {SECRET}\n"
                ).encode()
            action, release = args[:2]
            namespace = args[args.index("--namespace") + 1]
            key = namespace, release
            if action == "uninstall":
                current.pop(key, None)
                return b""
            revision = int(current[key]["revision"]) + 1 if key in current else 1
            row = {
                "name": release,
                "namespace": namespace,
                "revision": revision,
                "status": "deployed",
                "chart": "fixture-1.0.0",
                "app_version": "1",
                "updated": "now",
            }
            current[key] = row
            revisions.setdefault(key, []).append({**row, "description": args[args.index("--description") + 1]})
            store.put(
                {
                    "apiVersion": "v1",
                    "kind": "Secret",
                    "metadata": {"name": f"sh.helm.release.v1.{release}.v{revision}", "namespace": namespace},
                    "data": {},
                }
            )
            return b"private render output intentionally ignored"

    content = io.BytesIO()
    with tarfile.open(fileobj=content, mode="w:gz") as archive:
        info = tarfile.TarInfo("fixture/Chart.yaml")
        data = b"apiVersion: v2\nname: fixture\nversion: 1.0.0\n"
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    bundle = content.getvalue()
    monkeypatch.setattr(hs, "Runner", FakeHelm)
    monkeypatch.setattr("app.api.routes.helm.Runner", FakeHelm)
    monkeypatch.setattr(
        catalog,
        "index",
        lambda _, *_auth: {
            "fixture": [{"version": "1.0.0", "digest": hashlib.sha256(bundle).hexdigest(), "urls": ["fixture.tgz"]}]
        },
    )
    monkeypatch.setattr(catalog, "download", lambda *args: bundle)
    monkeypatch.setattr(
        hs,
        "head_uid",
        lambda api, ns, name, revision: kc.get_json(
            store, f"/api/v1/namespaces/{ns}/secrets/sh.helm.release.v1.{name}.v{revision}"
        )["metadata"]["uid"],
    )
    kc.set_client_factory(store.factory)
    try:
        created = client.post("/api/v1/clusters/import", json={"name": "helm-test", "kubeconfig": kubeconfig_text()})
        assert created.status_code == 202, created.text
        cluster_id = created.json()["cluster_id"]
        base = f"/api/v1/clusters/{cluster_id}/helm"
        assert (
            client.put(
                base + "/repositories",
                json={"repositories": [{"name": "public", "url": "https://charts.example.test"}]},
            ).status_code
            == 200
        )
        yield client, base, cluster_id, current, revisions, commands, fail
    finally:
        kc.set_client_factory(None)


def plan(env, action="install", **changes):
    client, base, *_ = env
    body = {
        "action": action,
        "release": "fixture",
        "namespace": "default",
        "repository": "public",
        "chart": "fixture",
        "version": "1.0.0",
        "values_yaml": f"token: {SECRET}\n",
    }
    body.update(changes)
    response = client.post(base + "/preview", json=body)
    assert response.status_code == 200, response.text
    assert SECRET not in response.text
    return response.json()


def submit(env, reviewed, confirm=None):
    client, base, *_ = env
    response = client.post(base + "/execute", json={"plan_id": reviewed["id"], "confirm": confirm})
    assert response.status_code == 202, response.text
    assert SECRET not in response.text
    return response.json()


def test_lifecycle_encryption_confirmation_and_history(helm_environment):
    env = helm_environment
    client, base, _cluster, _current, _history, commands, _fail = env
    reviewed = plan(env)
    assert reviewed["resources"] == [{"kind": "Secret", "name": "fixture-secret", "namespace": "default"}]
    with get_session_factory()() as session:
        row = session.get(HelmPlan, uuid.UUID(reviewed["id"]))
        assert SECRET.encode() not in row.ciphertext
        assert SECRET not in json.dumps(row.specification)
    installed = submit(env, reviewed)
    assert installed["status"] == "succeeded", installed
    assert submit(env, reviewed)["id"] == installed["id"]
    assert len([a for a in commands if a[0] == "install"]) == 1
    upgraded = plan(env, "upgrade")
    assert client.post(base + "/execute", json={"plan_id": upgraded["id"], "confirm": "wrong"}).status_code == 428
    assert submit(env, upgraded, "fixture")["status"] == "succeeded"
    assert submit(env, plan(env, "rollback", revision=1), "fixture")["status"] == "succeeded"
    assert submit(env, plan(env, "uninstall"), "fixture")["status"] == "succeeded"
    assert client.get(base + "/releases").json()["items"] == []
    assert all(
        "--no-hooks" in a and "--wait" in a for a in commands if a[0] in {"install", "upgrade", "rollback", "uninstall"}
    )
    with get_session_factory()() as session:
        assert all(SECRET not in log.message for log in session.scalars(select(OperationLog)))


@pytest.mark.parametrize("case", ["expired", "changed", "identity", "failure", "changed_after_download"])
def test_stale_plan_and_failed_execution(helm_environment, case, monkeypatch):
    env = helm_environment
    client, base, _cluster, current, _history, commands, fail = env
    submit(env, plan(env))
    reviewed = plan(env, "upgrade")
    if case == "expired":
        with get_session_factory()() as session:
            session.get(HelmPlan, uuid.UUID(reviewed["id"])).expires_at = datetime.now(UTC) - timedelta(seconds=1)
            session.commit()
        assert client.post(base + "/execute", json={"plan_id": reviewed["id"], "confirm": "fixture"}).status_code == 409
        return
    if case == "changed":
        current[("default", "fixture")]["revision"] = 8
    elif case == "identity":
        monkeypatch.setattr(hs, "head_uid", lambda *args: "another-release-secret-uid")
    elif case == "changed_after_download":
        original = catalog.archive

        def changed(spec):
            data = original(spec)
            current[("default", "fixture")]["revision"] = 8
            return data

        monkeypatch.setattr(catalog, "archive", changed)
    else:
        fail["enabled"] = True
    result = submit(env, reviewed, "fixture")
    assert result["status"] == "failed"
    if case in {"changed", "identity", "changed_after_download"}:
        assert not any(a[0] == "upgrade" for a in commands)


def test_catalog_and_values_defaults(helm_environment):
    client, base, *_ = helm_environment
    response = client.get(base + "/charts", params={"repository": "public", "search": "fix"})
    assert response.status_code == 200 and response.json()["items"][0]["name"] == "fixture"
    response = client.get(
        base + "/charts/values", params={"repository": "public", "chart": "fixture", "version": "1.0.0"}
    )
    assert response.json()["values_yaml"] == "replicaCount: 1\n"
    assert client.get(base + "/releases/default/does-not-exist/history").status_code == 404


def test_values_parsing_and_repository_validation():
    for text in ["[1,2]", "a: &a [*a]", "secret: [unterminated", "x: !!python/object:bad {}"]:
        with pytest.raises(Exception) as exc:
            catalog.values(text)
        assert "unterminated" not in str(exc.value)
    for url in [
        "http://charts.example.com",
        "https://user:pass@example.com",
        "https://example.com:1234",
        "https://example.com?token=test",
    ]:
        with pytest.raises(ValueError):
            catalog.Repository(name="repo", url=url)


def test_history_filters_secret_fields():
    from app.modules.helm.runtime import public_history

    assert "description" not in public_history({"revision": 1, "description": SECRET})


def test_review_flattens_lists_and_marks_cluster_scoped_objects():
    rendered = yaml.safe_dump(
        {
            "apiVersion": "v1",
            "kind": "List",
            "items": [
                {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRole", "metadata": {"name": "reader"}},
                {
                    "apiVersion": "v1",
                    "kind": "Secret",
                    "metadata": {"name": "private"},
                    "stringData": {"password": SECRET},
                },
            ],
        }
    )
    reviewed = hs.inventory(rendered.encode(), "default")
    assert reviewed == [
        {"kind": "ClusterRole", "name": "reader", "namespace": "(cluster)"},
        {"kind": "Secret", "name": "private", "namespace": "default"},
    ]
    assert SECRET not in json.dumps(reviewed)


def test_runtime_output_redaction_and_private_files(monkeypatch, tmp_path):
    script = tmp_path / "helm"
    script.write_text(
        '#!/bin/sh\nif [ "$1" = version ]; then printf v4.3.2; else printf "'
        + SECRET
        + '"; printf "'
        + SECRET
        + '" >&2; exit 1; fi\n'
    )
    script.chmod(0o755)
    monkeypatch.setattr("app.modules.helm.runtime.shutil.which", lambda _: str(script))
    root = None
    with Runner() as runtime:
        root = runtime.root
        assert (root / "kubeconfig").stat().st_mode & 0o777 == 0o600
        with pytest.raises(HelmError) as exc:
            runtime.run(["install", "fixture"])
        assert SECRET not in str(exc.value)
    assert not root.exists()


def test_viewer_and_project_isolation(identity: IdentityHarness, monkeypatch):
    store = FakeStore()
    kc.set_client_factory(store.factory)
    try:
        headers = {**identity.bearer(), "X-Project-ID": str(identity.project_id)}
        response = identity.client.post(
            "/api/v1/clusters/import", headers=headers, json={"name": "scoped-helm", "kubeconfig": kubeconfig_text()}
        )
        assert response.status_code == 202, response.text
        base = f"/api/v1/clusters/{response.json()['cluster_id']}/helm"
        identity.role("viewer")
        assert identity.client.get(base + "/repositories", headers=headers).status_code == 200
        assert (
            identity.client.put(base + "/repositories", headers=headers, json={"repositories": []}).status_code == 403
        )
        assert (
            identity.client.post(
                base + "/preview",
                headers=headers,
                json={"action": "install", "namespace": "default", "release": "fixture"},
            ).status_code
            == 403
        )
        assert (
            identity.client.post(base + "/execute", headers=headers, json={"plan_id": str(uuid.uuid4())}).status_code
            == 403
        )
        identity.role("org-admin")
        from app.modules.organizations.models import Project

        with get_session_factory()() as session:
            p = Project(organization_id=identity.organization_id, name="Other", slug="other-helm")
            session.add(p)
            session.commit()
            pid = p.id
        hidden = {**identity.bearer(), "X-Project-ID": str(pid)}
        assert identity.client.get(base + "/repositories", headers=hidden).status_code == 404
    finally:
        kc.set_client_factory(None)


def test_recovery_after_committed_action_does_not_repeat_upgrade(
    helm_environment, deferred_service, executor, principal
):
    env = helm_environment
    _client, _base, _cluster, _current, _history, commands, _fail = env
    submit(env, plan(env))
    changed = submit(env, plan(env, "upgrade"), "fixture")
    ident = uuid.UUID(changed["id"])
    with get_session_factory()() as session:
        op = session.get(Operation, ident)
        op.status = OperationStatus.FAILED
        op.steps[0].status = StepStatus.FAILED
        op.steps[0].output = {}
        session.commit()
    deferred_service.retry(principal, ident)
    assert executor.execute(ident).value == "succeeded"
    assert len([a for a in commands if a[0] == "upgrade"]) == 1


def test_unapproved_plan_and_expired_queued_plan_cannot_execute(
    helm_environment, deferred_service, executor, principal
):
    env = helm_environment
    _client, _base, cluster, _current, _history, commands, _fail = env
    reviewed = plan(env)
    op = deferred_service.submit(
        principal,
        "cluster.helm.install",
        {
            "cluster_id": cluster,
            "cluster_name": "helm-test",
            "plan_id": reviewed["id"],
            "action": "install",
            "release": "fixture",
        },
    )
    assert executor.execute(op.id).value == "failed"
    assert not any(a[0] == "install" for a in commands)
    # Simulate an approved review expiring while its operation is still queued.
    with get_session_factory()() as session:
        row = session.get(HelmPlan, uuid.UUID(reviewed["id"]))
        row.approved_at = datetime.now(UTC) - timedelta(minutes=16)
        row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        session.commit()
    queued = deferred_service.submit(
        principal,
        "cluster.helm.install",
        {
            "cluster_id": cluster,
            "cluster_name": "helm-test",
            "plan_id": reviewed["id"],
            "action": "install",
            "release": "fixture",
        },
    )
    assert executor.execute(queued.id).value == "failed"
    assert not any(a[0] == "install" for a in commands)


def test_private_download_target_is_never_connected(monkeypatch):
    connected = []
    monkeypatch.setattr(transport.socket, "getaddrinfo", lambda *args, **kwargs: [(2, 1, 6, "", ("127.0.0.1", 443))])
    monkeypatch.setattr(transport.urllib3, "HTTPSConnectionPool", lambda *args, **kwargs: connected.append(args))
    with pytest.raises(catalog.CatalogError):
        catalog.download("https://private.example/chart.tgz", 100)
    assert connected == []


def test_redirect_to_metadata_address_is_blocked(monkeypatch):
    from unittest.mock import Mock

    calls = []

    def resolve(host, *args, **kwargs):
        return [(2, 1, 6, "", ("93.184.216.34" if host == "public.example" else "169.254.169.254", 443))]

    def pool(address, **kwargs):
        calls.append((address, kwargs["server_hostname"]))
        response = Mock(status=302, headers={"Location": "https://169.254.169.254/latest/meta-data"})
        return Mock(urlopen=Mock(return_value=response))

    monkeypatch.setattr(transport.socket, "getaddrinfo", resolve)
    monkeypatch.setattr(transport.urllib3, "HTTPSConnectionPool", pool)
    with pytest.raises(catalog.CatalogError):
        catalog.download("https://public.example/chart.tgz", 100)
    assert calls == [("93.184.216.34", "public.example")]


def test_archive_traversal_is_rejected(monkeypatch):
    content = io.BytesIO()
    with tarfile.open(fileobj=content, mode="w:gz") as archive:
        member = tarfile.TarInfo("../../escape")
        member.size = 1
        archive.addfile(member, io.BytesIO(b"x"))
    data = content.getvalue()
    monkeypatch.setattr(catalog, "download", lambda *args: data)
    with pytest.raises(catalog.CatalogError):
        catalog.archive({"url": "https://public.example/chart.tgz", "digest": hashlib.sha256(data).hexdigest()})


def test_cancellation_terminates_the_helm_process(monkeypatch, tmp_path):
    from unittest.mock import Mock

    from app.orchestration.workflow import OperationCancelledError

    script = tmp_path / "helm"
    script.write_text('#!/bin/sh\nif [ "$1" = version ]; then printf v4.3.2; else exec sleep 60; fi\n')
    script.chmod(0o755)
    monkeypatch.setattr("app.modules.helm.runtime.shutil.which", lambda _: str(script))
    with Runner() as runtime:
        runtime.ctx = Mock(check_cancelled=Mock(side_effect=OperationCancelledError))
        with pytest.raises(OperationCancelledError):
            runtime.run(["upgrade", "fixture"])
