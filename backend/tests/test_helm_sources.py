"""OCI protocol integrity, credential boundaries and encrypted private-source configuration."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import tarfile
import uuid
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import select

from app.db.session import get_session_factory
from app.modules.helm import catalog, transport
from app.modules.helm.models import HelmPlan, HelmRepositoryCredential
from app.modules.helm.oci import OCIClient
from app.orchestration.models import OperationLog
from tests.fake_kube import kubeconfig_text
from tests.test_helm import helm_environment, plan, submit  # noqa: F401
from tests.test_identity import IdentityHarness, identity  # noqa: F401


def artifact(version="1.2.3+build.1"):
    config = json.dumps({"name": "demo", "version": version}).encode()
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as bundle:
        entry = tarfile.TarInfo("demo/Chart.yaml")
        entry.size = len(config)
        bundle.addfile(entry, io.BytesIO(config))
    chart = stream.getvalue()

    def descriptor(data, media):
        return {
            "mediaType": media,
            "size": len(data),
            "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
        }

    manifest = json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": descriptor(config, "application/vnd.cncf.helm.config.v1+json"),
            "layers": [descriptor(chart, "application/vnd.cncf.helm.chart.content.v1.tar+gzip")],
        }
    ).encode()
    return manifest, config, chart


@pytest.fixture
def registry(monkeypatch):
    manifest, config, chart = artifact()
    blobs = {"sha256:" + hashlib.sha256(data).hexdigest(): data for data in [config, chart]}
    manifest_digest = "sha256:" + hashlib.sha256(manifest).hexdigest()
    calls = []
    settings = {"realm": "https://registry.example.test/token", "corrupt": False}

    def request(url, limit, **kwargs):
        parsed = urlsplit(url)
        calls.append((url, kwargs.get("authorization")))
        if parsed.path == "/token":
            assert parse_qs(parsed.query)["scope"] == ["repository:team/demo:pull"]
            return transport.Response(200, {}, b'{"token":"test-registry-token"}')
        if kwargs.get("authorization") != "Bearer test-registry-token":
            return transport.Response(
                401,
                {
                    "www-authenticate": 'Bearer realm="'
                    + settings["realm"]
                    + '",service="registry.example.test",scope="repository:other:pull,push"'
                },
                b"",
            )
        if parsed.path.endswith("/tags/list"):
            return transport.Response(200, {}, b'{"tags":["1.2.3_build.1","2.0.0","latest","1.2.3-alpha.2"]}')
        if "/manifests/" in parsed.path:
            return transport.Response(200, {"docker-content-digest": manifest_digest}, manifest)
        data = blobs[parsed.path.rsplit("/", 1)[-1]]
        return transport.Response(200, {}, b"corrupt" if settings["corrupt"] else data)

    monkeypatch.setattr(transport, "request", request)
    return calls, settings, chart


def test_oci_tags_manifest_pinning_and_pull_only_scope(registry):
    calls, _settings, chart = registry
    repo = catalog.Repository(name="oci", url="oci://registry.example.test/team/demo")
    indexed = catalog.index(repo)["demo"]
    assert [e["version"] for e in indexed] == ["2.0.0", "1.2.3+build.1", "1.2.3-alpha.2"]
    reviewed = catalog.chart(repo, "demo", "1.2.3+build.1")
    assert reviewed["manifest_digest"].startswith("sha256:")
    assert catalog.archive(reviewed) == chart
    assert any("/manifests/sha256:" in url for url, _ in calls)
    assert any("/manifests/1.2.3_build.1" in url for url, _ in calls)


def test_oci_digest_corruption_is_rejected(registry):
    _calls, settings, _chart = registry
    repo = catalog.Repository(name="oci", url="oci://registry.example.test/team/demo")
    reviewed = catalog.chart(repo, "demo", "1.2.3+build.1")
    settings["corrupt"] = True
    with pytest.raises(catalog.CatalogError):
        catalog.archive(reviewed)
    reviewed["manifest_digest"] = "sha256:" + "f" * 64
    with pytest.raises(catalog.CatalogError):
        catalog.archive(reviewed)


def test_authenticated_cross_origin_realm_requires_explicit_trust(registry):
    calls, settings, _chart = registry
    settings["realm"] = "https://login.example.test/token"
    repo = catalog.Repository(name="oci", url="oci://registry.example.test/team/demo")
    basic = "Basic " + base64.b64encode(b"user:fixture-password").decode()
    with pytest.raises(catalog.CatalogError, match="trusted token service"):
        OCIClient(repo, basic).index()
    assert not any(url.startswith("https://login.example.test") for url, _ in calls)
    repo.token_service_url = settings["realm"]
    assert OCIClient(repo, basic).index()["demo"]
    assert any(url.startswith("https://login.example.test/token") and auth == basic for url, auth in calls)


def test_private_policy_never_allows_metadata_or_loopback():
    for address in ["127.0.0.1", "169.254.169.254", "::1", "fe80::1", "224.0.0.1", "::ffff:127.0.0.1"]:
        assert not transport.permitted_address(address, True)
    for address in ["192.168.18.11", "10.1.2.3", "172.20.1.1", "fd00::1"]:
        assert transport.permitted_address(address, True)
        assert not transport.permitted_address(address, False)


def test_authorization_is_stripped_on_cross_origin_redirect(monkeypatch):
    seen = []
    monkeypatch.setattr(
        transport.socket, "getaddrinfo", lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))]
    )

    def pool(address, **kwargs):
        host = kwargs["server_hostname"]

        def get(*args, **request):
            seen.append((host, request["headers"].get("Authorization")))
            return (
                Mock(status=302, headers={"Location": "https://cdn.example.test/chart.tgz"})
                if host == "charts.example.test"
                else Mock(status=200, headers={}, read=Mock(return_value=b"chart"))
            )

        return Mock(urlopen=get)

    monkeypatch.setattr(transport.urllib3, "HTTPSConnectionPool", pool)
    repo = catalog.Repository(name="private", url="https://charts.example.test")
    assert transport.request(repo.url + "/chart.tgz", 100, policy=repo, authorization="Basic fixture").data == b"chart"
    assert seen == [("charts.example.test", "Basic fixture"), ("cdn.example.test", None)]


def save_auth(env, password):
    client, base, *_ = env
    response = client.put(
        base + "/repositories",
        json={
            "repositories": [
                {
                    "name": "public",
                    "url": "https://charts.example.test",
                    "authentication": {"username": "fixture-user", "password": password},
                }
            ]
        },
    )
    assert response.status_code == 200, response.text
    assert password not in response.text
    return response.json()[0]


def test_private_credentials_encrypted_and_omitted_from_all_payloads(helm_environment):
    env = helm_environment
    client, base, *_ = env
    password = uuid.uuid4().hex
    repository = save_auth(env, password)
    assert repository["auth_id"]
    assert password not in client.get(base + "/repositories").text
    reviewed = plan(env)
    assert password not in json.dumps(reviewed)
    result = submit(env, reviewed)
    assert result["status"] == "succeeded", result
    with get_session_factory()() as session:
        credential = session.get(HelmRepositoryCredential, uuid.UUID(repository["auth_id"]))
        assert password.encode() not in credential.ciphertext
        assert all(password not in r.message for r in session.scalars(select(OperationLog)))
        row = session.get(HelmPlan, uuid.UUID(reviewed["id"]))
        assert password not in json.dumps(row.specification)


def test_rotation_and_removal_revoke_old_plans(helm_environment):
    env = helm_environment
    client, base, _cluster, _current, _history, commands, _fail = env
    original = save_auth(env, uuid.uuid4().hex)
    reviewed = plan(env)
    replacement = save_auth(env, uuid.uuid4().hex)
    assert replacement["auth_id"] != original["auth_id"]
    result = submit(env, reviewed)
    assert result["status"] == "failed"
    assert not any(args[0] == "install" for args in commands)
    assert client.put(base + "/repositories", json={"repositories": []}).status_code == 200
    with get_session_factory()() as session:
        assert all(r.revoked for r in session.scalars(select(HelmRepositoryCredential)))


def test_credential_reference_cannot_cross_clusters(helm_environment):
    env = helm_environment
    client, _base, *_ = env
    repository = save_auth(env, uuid.uuid4().hex)
    created = client.post(
        "/api/v1/clusters/import", json={"name": "other-private-cluster", "kubeconfig": kubeconfig_text()}
    )
    base = f"/api/v1/clusters/{created.json()['cluster_id']}/helm"
    assert client.put(base + "/repositories", json={"repositories": [repository]}).status_code == 409


def test_invalid_credentials_do_not_echo_secrets(helm_environment):
    client, base, *_ = helm_environment
    password = uuid.uuid4().hex
    response = client.put(
        base + "/repositories",
        json={
            "repositories": [
                {
                    "name": "invalid",
                    "url": "https://charts.example.test",
                    "authentication": {"username": "", "password": password},
                }
            ]
        },
    )
    assert response.status_code == 422
    assert password not in response.text


def test_endpoint_trust_is_administrator_controlled(identity: IdentityHarness):
    from app.modules.kubernetes import client as kc
    from tests.fake_store import FakeStore

    store = FakeStore()
    kc.set_client_factory(store.factory)
    try:
        headers = identity.bearer()
        response = identity.client.post(
            "/api/v1/clusters/import", headers=headers, json={"name": "private-policy", "kubeconfig": kubeconfig_text()}
        )
        base = f"/api/v1/clusters/{response.json()['cluster_id']}/helm/repositories"
        private = {
            "repositories": [
                {"name": "internal", "url": "https://192.168.18.11:5443/charts", "allow_private_network": True}
            ]
        }
        assert identity.client.put(base, headers=headers, json=private).status_code == 403
        identity.role("org-admin")
        assert identity.client.put(base, headers=headers, json=private).status_code == 200
        identity.role("operator")
        private["repositories"][0]["url"] = "https://192.168.18.12:5443/charts"
        assert identity.client.put(base, headers=headers, json=private).status_code == 403
    finally:
        kc.set_client_factory(None)
