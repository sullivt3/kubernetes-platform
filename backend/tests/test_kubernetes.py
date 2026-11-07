"""Phase 1: kubeconfig handling, the HTTPS client, live views, import and health observation."""

from __future__ import annotations

import datetime as dt
import http.server
import ipaddress
import json
import ssl
import threading
from collections.abc import Iterator
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient

from app.modules.cluster_lifecycle.distributions.types import DistributionType
from app.modules.kubernetes import client as kclient
from app.modules.kubernetes import kubeconfig, views
from app.modules.kubernetes.quantities import cpu_millicores, memory_bytes
from tests.fake_kube import FakeKube, kubeconfig_text

API = "/api/v1"


# ----------------------------------------------------------------------------- kubeconfig


class TestKubeconfig:
    def test_contexts_and_auth_types(self) -> None:
        contexts = {c.name: c for c in kubeconfig.list_contexts(kubeconfig.parse(kubeconfig_text()))}
        assert contexts["admin@prod"].auth_type == "client-certificate" and contexts["admin@prod"].supported
        assert contexts["other"].auth_type == "token" and contexts["other"].supported

    @pytest.mark.parametrize(
        ("text", "fragment"),
        [
            (kubeconfig_text(auth="exec"), "external authentication plugin"),
            (kubeconfig_text(server="http://10.0.0.11:6443"), "https:// URL"),
            (
                kubeconfig_text(extra_cluster="    insecure-skip-tls-verify: true\n"),
                "Disables TLS verification",
            ),
            (
                kubeconfig_text(extra_cluster="    certificate-authority: /home/me/ca.crt\n"),
                "References a CA file",
            ),
        ],
    )
    def test_unsupported_contexts_explain_why(self, text: str, fragment: str) -> None:
        context = kubeconfig.list_contexts(kubeconfig.parse(text))[0]
        assert not context.supported and fragment in (context.problem or "")
        with pytest.raises(kubeconfig.KubeconfigError, match="cannot be used"):
            kubeconfig.minify(kubeconfig.parse(text), "admin@prod")

    def test_minify_keeps_only_selected_context(self) -> None:
        minified, name = kubeconfig.minify(kubeconfig.parse(kubeconfig_text()), "other")
        assert name == "other"
        assert [c["name"] for c in minified["clusters"]] == ["other"]
        assert [u["name"] for u in minified["users"]] == ["other"]
        assert minified["current-context"] == "other"

    def test_resolve_decodes_material(self) -> None:
        resolved = kubeconfig.resolve(kubeconfig_text())
        assert resolved.server == "https://10.0.0.11:6443"
        assert "BEGIN CERTIFICATE" in resolved.ca_pem and "BEGIN PRIVATE KEY" in (resolved.client_key_pem or "")

    @pytest.mark.parametrize("text", ["not: [valid", "just a string", "apiVersion: v1\nkind: Config\ncontexts: []\n"])
    def test_invalid_documents(self, text: str) -> None:
        with pytest.raises(kubeconfig.KubeconfigError):
            kubeconfig.parse(text)


def test_quantities() -> None:
    assert cpu_millicores("250m") == 250 and cpu_millicores("2") == 2000 and cpu_millicores("1.5") == 1500
    assert cpu_millicores("500000000n") == 500 and cpu_millicores(None) == 0
    assert memory_bytes("128Mi") == 134217728 and memory_bytes("1G") == 10**9 and memory_bytes("1024") == 1024
    assert memory_bytes("garbage") == 0


# ----------------------------------------------------------------------------- views


class TestViews:
    def test_pod_status_reasons(self) -> None:
        kube = FakeKube()
        crashing = views.pod_view(kube.pods[2])
        assert crashing["status"] == "CrashLoopBackOff" and crashing["problem"] and crashing["restarts"] == 7
        assert crashing["ready"] == "0/1" and crashing["owner_kind"] == "ReplicaSet"
        terminating = {**kube.pods[0], "metadata": {**kube.pods[0]["metadata"], "deletionTimestamp": "x"}}
        assert views.pod_status(terminating) == "Terminating"
        init = {
            **kube.pods[0],
            "status": {
                "phase": "Pending",
                "initContainerStatuses": [{"state": {"waiting": {"reason": "ImagePullBackOff"}}}],
            },
        }
        assert views.pod_status(init) == "Init:ImagePullBackOff"

    def test_pod_requests_use_max_of_init_and_sum(self) -> None:
        pod = {
            "spec": {
                "containers": [
                    {"resources": {"requests": {"cpu": "100m", "memory": "64Mi"}}},
                    {"resources": {"requests": {"cpu": "200m"}}},
                ],
                "initContainers": [{"resources": {"requests": {"cpu": "500m", "memory": "32Mi"}}}],
            }
        }
        assert views.pod_requests(pod) == (500, 64 * 2**20)

    def test_summary_alerts_and_state(self) -> None:
        kube = FakeKube()
        kube.nodes[1]["status"]["conditions"] = [{"type": "Ready", "status": "False"}]
        summary = views.summarize(kube.version, (True, []), kube.nodes, kube.pods, None)
        assert summary["nodes"] == {"total": 2, "ready": 1, "not_ready": ["worker-1"]}
        assert summary["pods"]["problems"] == 1
        assert summary["cpu"]["requested_m"] == 450 and summary["cpu"]["used_m"] is None
        assert any("worker-1 is NotReady" in a["message"] for a in summary["alerts"])
        assert views.health_state(summary) == "degraded"

    @pytest.mark.parametrize(
        ("git", "labels", "provider", "kubeadm", "expected"),
        [
            ("v1.30.1-eks-abc", {}, "", False, DistributionType.EKS),
            ("v1.30.1-gke.100", {}, "", False, DistributionType.GKE),
            ("v1.30.1", {"kubernetes.azure.com/cluster": "x"}, "", False, DistributionType.AKS),
            ("v1.30.1+k3s1", {}, "", False, DistributionType.K3S),
            ("v1.30.1+rke2r1", {}, "", False, DistributionType.RKE2),
            ("v1.30.1", {}, "kind://docker/kind/kind-control-plane", True, DistributionType.KIND),
            ("v1.30.1", {}, "", True, DistributionType.KUBEADM),
            ("v1.30.1", {}, "", False, DistributionType.OTHER),
        ],
    )
    def test_detect_distribution(
        self, git: str, labels: dict[str, str], provider: str, kubeadm: bool, expected: DistributionType
    ) -> None:
        node = {"metadata": {"name": "n", "labels": labels}, "spec": {"providerID": provider}}
        assert views.detect_distribution({"gitVersion": git}, [node], kubeadm) is expected


# ----------------------------------------------------------------------------- real HTTPS client


def _cert(
    subject: str,
    issuer_key: Any,
    issuer_name: x509.Name | None,
    *,
    ca: bool,
    sans: list[x509.GeneralName] | None = None,
    client: bool = False,
) -> tuple[Any, x509.Certificate]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject)])
    now = dt.datetime.now(dt.UTC)
    builder = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(issuer_name or name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
    )
    if sans:
        builder = builder.add_extension(x509.SubjectAlternativeName(sans), critical=False)
    if client:
        builder = builder.add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False
        )
    return key, builder.sign(issuer_key or key, hashes.SHA256())


def _pem(cert: x509.Certificate) -> str:
    return cert.public_bytes(serialization.Encoding.PEM).decode()


def _key_pem(key: Any) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


@pytest.fixture(scope="module")
def tls_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Any]]:
    """HTTPS server whose certificate is valid for 'kube.test'/10.9.9.9, requiring client certs from our CA."""
    ca_key, ca = _cert("test-ca", None, None, ca=True)
    srv_key, srv = _cert(
        "apiserver",
        ca_key,
        ca.subject,
        ca=False,
        sans=[x509.DNSName("kube.test"), x509.IPAddress(ipaddress.ip_address("10.9.9.9"))],
    )
    cli_key, cli = _cert("admin", ca_key, ca.subject, ca=False, client=True)
    directory = tmp_path_factory.mktemp("tls")
    (directory / "srv.pem").write_text(_pem(srv) + _key_pem(srv_key))
    (directory / "ca.pem").write_text(_pem(ca))
    seen: dict[str, Any] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            seen["authorization"] = self.headers.get("Authorization")
            peer = self.connection.getpeercert()  # type: ignore[attr-defined]
            seen["client_cert"] = bool(peer)
            if self.headers.get("Authorization") == "Bearer bad":
                self.send_response(401)
                self.end_headers()
                return
            body = json.dumps({"gitVersion": "v1.37.2"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            return None

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(directory / "srv.pem"))
    context.load_verify_locations(str(directory / "ca.pem"))
    context.verify_mode = ssl.CERT_OPTIONAL
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield {
        "port": server.server_address[1],
        "ca": _pem(ca),
        "cert": _pem(cli),
        "key": _key_pem(cli_key),
        "seen": seen,
    }
    server.shutdown()


def _target(
    tls: dict[str, Any],
    *,
    server_name: str | None,
    token: str | None = None,
    cert: bool = False,
    ca: str | None = None,
) -> kclient.ConnectionTarget:
    resolved = kubeconfig.ResolvedKubeconfig(
        context="t",
        server="https://kube.test:6443",
        ca_pem=ca or tls["ca"],
        tls_server_name=None,
        token=token,
        client_cert_pem=tls["cert"] if cert else None,
        client_key_pem=tls["key"] if cert else None,
    )
    return kclient.ConnectionTarget(
        resolved, server_override=f"https://127.0.0.1:{tls['port']}", tls_server_name=server_name
    )


class TestHttpsClient:
    def test_tls_server_name_allows_reaching_through_another_address(self, tls_server: dict[str, Any]) -> None:
        api = kclient.KubeClient(_target(tls_server, server_name="kube.test", token="t0k"))
        assert kclient.get_json(api, "/version") == {"gitVersion": "v1.37.2"}
        assert tls_server["seen"]["authorization"] == "Bearer t0k"
        ip_name = kclient.KubeClient(_target(tls_server, server_name="10.9.9.9", token="t0k"))
        assert kclient.get_json(ip_name, "/version")["gitVersion"] == "v1.37.2"

    def test_hostname_mismatch_is_rejected(self, tls_server: dict[str, Any]) -> None:
        api = kclient.KubeClient(_target(tls_server, server_name=None, token="t0k"))
        with pytest.raises(kclient.ClusterUnreachableError, match="TLS verification"):
            kclient.get_json(api, "/version")

    def test_untrusted_ca_is_rejected(self, tls_server: dict[str, Any]) -> None:
        _other_key, other_ca = _cert("other-ca", None, None, ca=True)
        api = kclient.KubeClient(_target(tls_server, server_name="kube.test", token="t", ca=_pem(other_ca)))
        with pytest.raises(kclient.ClusterUnreachableError, match="TLS verification"):
            kclient.get_json(api, "/version")

    def test_client_certificate_from_memory(self, tls_server: dict[str, Any]) -> None:
        api = kclient.KubeClient(_target(tls_server, server_name="kube.test", cert=True))
        assert kclient.get_json(api, "/version")["gitVersion"] == "v1.37.2"
        assert tls_server["seen"]["client_cert"] is True

    def test_rejected_credentials(self, tls_server: dict[str, Any]) -> None:
        api = kclient.KubeClient(_target(tls_server, server_name="kube.test", token="bad"))
        with pytest.raises(kclient.ClusterAuthError):
            kclient.get_json(api, "/version")

    def test_unreachable(self) -> None:
        resolved = kubeconfig.ResolvedKubeconfig("t", "https://127.0.0.1:1", "", None, "t", None, None)
        with pytest.raises((kclient.ClusterUnreachableError, kclient.ClusterAuthError)):
            kclient.get_json(kclient.KubeClient(kclient.ConnectionTarget(resolved)), "/version")


def test_list_items_paginates_and_truncates() -> None:
    class Paged:
        def __init__(self) -> None:
            self.calls = 0

        def request(self, method: str, path: str, **kw: Any) -> tuple[int, bytes]:
            self.calls += 1
            return 200, json.dumps({"items": [{"i": self.calls}] * 3, "metadata": {"continue": "next"}}).encode()

        def close(self) -> None:
            return None

    items, truncated = kclient.list_items(Paged(), "/api/v1/pods", page_size=3, max_items=7)
    assert len(items) == 7 and truncated


# ----------------------------------------------------------------------------- API (fake cluster)


@pytest.fixture
def kube() -> Iterator[FakeKube]:
    fake = FakeKube()
    kclient.set_client_factory(fake.factory)
    yield fake
    kclient.set_client_factory(None)


def import_cluster(client: TestClient, **overrides: Any) -> dict[str, Any]:
    body = {
        "name": "prod",
        "kubeconfig": kubeconfig_text(),
        "server_override": "https://127.0.0.1:16443",
        "tls_server_name": "10.0.0.11",
        **overrides,
    }
    response = client.post(f"{API}/clusters/import", json=body)
    assert response.status_code == 202, response.text
    return response.json()


class TestImportAndLiveData:
    def test_inspect_lists_contexts_without_storing(self, client: TestClient) -> None:
        result = client.post(f"{API}/clusters/import/inspect", json={"kubeconfig": kubeconfig_text()}).json()
        assert result["current_context"] == "admin@prod"
        assert {c["name"] for c in result["contexts"]} == {"admin@prod", "other"}
        assert client.get(f"{API}/credentials").json()["total"] == 0

    def test_import_connects_and_records_health(self, client: TestClient, kube: FakeKube) -> None:
        imported = import_cluster(client)
        assert imported["operation"]["status"] == "succeeded", imported["operation"]["error"]
        cluster = client.get(f"{API}/clusters/{imported['cluster_id']}").json()
        assert cluster["origin"] == "imported" and cluster["distribution"] == "kubeadm"
        assert cluster["lifecycle_state"] == "active" and cluster["kubernetes_version"] == "v1.37.2"
        assert cluster["kubeconfig_context"] == "admin@prod" and cluster["connected"]
        assert cluster["health"]["nodes"] == {"total": 2, "ready": 2, "not_ready": []}
        assert cluster["health"]["pods"]["problems"] == 1
        # connection settings reached the client
        assert kube.targets[-1].server == "https://127.0.0.1:16443" and kube.targets[-1].verify_name == "10.0.0.11"
        # only the selected context is stored, encrypted, and never returned
        credential = next(c for c in client.get(f"{API}/credentials").json()["items"] if c["kind"] == "kubeconfig")
        assert credential["public_metadata"] == {"cluster": "prod", "context": "admin@prod"}
        assert "other-token" not in client.get(f"{API}/clusters/{imported['cluster_id']}").text

    def test_live_reads(self, client: TestClient, kube: FakeKube) -> None:
        cid = import_cluster(client)["cluster_id"]
        nodes = client.get(f"{API}/clusters/{cid}/nodes").json()
        assert [(n["name"], n["roles"], n["ready"]) for n in nodes] == [
            ("cp-1", ["control-plane"], True),
            ("worker-1", ["worker"], True),
        ]
        assert nodes[1]["cpu"]["requested_m"] == 350 and nodes[1]["pods"]["count"] == 2
        node = client.get(f"{API}/clusters/{cid}/nodes/worker-1").json()
        assert {p["name"] for p in node["pod_list"]} == {"web-1", "web-2"}
        assert client.get(f"{API}/clusters/{cid}/nodes/missing").status_code == 404
        namespaces = {n["name"]: n for n in client.get(f"{API}/clusters/{cid}/namespaces").json()}
        assert namespaces["apps"]["pods"]["total"] == 2 and namespaces["apps"]["problem_pods"] == 1
        pods = client.get(f"{API}/clusters/{cid}/pods", params={"namespace": "apps", "problems": True}).json()
        assert [p["name"] for p in pods["items"]] == ["web-2"]
        events = client.get(f"{API}/clusters/{cid}/events").json()
        assert events[0]["reason"] == "BackOff" and events[0]["object"] == "Pod/web-2"
        topology = client.get(f"{API}/clusters/{cid}/topology").json()
        assert {n["name"]: len(n["pod_list"]) for n in topology["nodes"]} == {"cp-1": 1, "worker-1": 2}
        assert client.get(f"{API}/clusters/{cid}/pods", params={"namespace": "Bad_NS"}).status_code == 422

    def test_unreachable_cluster_returns_503_and_is_marked(self, client: TestClient, kube: FakeKube) -> None:
        cid = import_cluster(client)["cluster_id"]
        kube.unreachable = True
        response = client.get(f"{API}/clusters/{cid}/health")
        assert response.status_code == 503 and response.json()["error"]["code"] == "cluster_unreachable"
        cluster = client.get(f"{API}/clusters/{cid}").json()
        assert cluster["lifecycle_state"] == "unreachable" and cluster["health_error"]["code"] == "cluster_unreachable"
        assert client.get(f"{API}/health/ready").status_code == 200  # the platform itself is unaffected
        kube.unreachable = False
        assert client.get(f"{API}/clusters/{cid}/health").status_code == 200
        assert client.get(f"{API}/clusters/{cid}").json()["lifecycle_state"] == "active"

    def test_import_while_unreachable_fails_cleanly(self, client: TestClient, kube: FakeKube) -> None:
        kube.unreachable = True
        imported = import_cluster(client)
        assert imported["operation"]["status"] == "failed"
        assert client.get(f"{API}/clusters/{imported['cluster_id']}").json()["lifecycle_state"] == "unreachable"

    def test_probe_updates_health(self, client: TestClient, kube: FakeKube) -> None:
        from app.core.secrets import get_secret_store
        from app.db.session import get_session_factory
        from app.modules.kubernetes.service import probe_clusters

        cid = import_cluster(client)["cluster_id"]
        kube.nodes[1]["status"]["conditions"] = [{"type": "Ready", "status": "False"}]
        kclient.set_client_factory(kube.factory)  # reset the client cache
        assert probe_clusters(get_session_factory(), get_secret_store()) == {"ok": 1}
        cluster = client.get(f"{API}/clusters/{cid}").json()
        assert cluster["lifecycle_state"] == "degraded" and cluster["health"]["nodes"]["not_ready"] == ["worker-1"]

    def test_connection_settings_update_and_validation(self, client: TestClient, kube: FakeKube) -> None:
        cid = import_cluster(client)["cluster_id"]
        bad = client.put(f"{API}/clusters/{cid}/connection", json={"server_override": "http://plain:80"})
        assert bad.status_code == 422
        op = client.put(
            f"{API}/clusters/{cid}/connection",
            json={"server_override": "https://lb.example:6443", "tls_server_name": None},
        ).json()
        assert op["status"] == "succeeded"
        assert kube.targets[-1].server == "https://lb.example:6443" and kube.targets[-1].verify_name is None

    def test_import_validation(self, client: TestClient, kube: FakeKube) -> None:
        exec_cfg = client.post(f"{API}/clusters/import", json={"name": "x", "kubeconfig": kubeconfig_text(auth="exec")})
        assert exec_cfg.status_code == 422 and "authentication plugin" in exec_cfg.json()["error"]["message"]
        assert (
            client.post(f"{API}/clusters/import", json={"name": "Bad", "kubeconfig": kubeconfig_text()}).status_code
            == 422
        )
        import_cluster(client)
        assert (
            client.post(f"{API}/clusters/import", json={"name": "prod", "kubeconfig": kubeconfig_text()}).status_code
            == 409
        )

    def test_unregister(self, client: TestClient, kube: FakeKube) -> None:
        cid = import_cluster(client)["cluster_id"]
        assert client.post(f"{API}/clusters/{cid}/unregister").status_code == 428
        assert client.post(f"{API}/clusters/{cid}/unregister", params={"confirm": "prod"}).status_code == 204
        assert client.get(f"{API}/clusters/{cid}").status_code == 404
        assert client.get(f"{API}/credentials").json()["total"] == 0
        actions = [
            e["action"] for e in client.get(f"{API}/audit-events", params={"resource_type": "cluster"}).json()["items"]
        ]
        assert actions[:2] == ["cluster.unregister", "cluster.import"]

    def test_metrics_server_addon(self, client: TestClient, kube: FakeKube, monkeypatch: pytest.MonkeyPatch) -> None:
        import io

        from app.modules.kubernetes import workflows

        manifest = (
            "apiVersion: v1\nkind: ServiceAccount\nmetadata: {name: metrics-server, namespace: kube-system}\n---\n"
            "apiVersion: rbac.authorization.k8s.io/v1\nkind: ClusterRole\n"
            "metadata: {name: system:metrics-server}\n---\n"
            "apiVersion: apps/v1\nkind: Deployment\nmetadata: {name: metrics-server, namespace: kube-system}\n"
            "spec: {template: {spec: {containers: [{name: metrics-server, args: [--secure-port=10250]}]}}}\n---\n"
            "apiVersion: apiregistration.k8s.io/v1\nkind: APIService\nmetadata: {name: v1beta1.metrics.k8s.io}\n"
        )
        monkeypatch.setattr(workflows.urllib.request, "urlopen", lambda url, timeout: io.BytesIO(manifest.encode()))
        cid = import_cluster(client)["cluster_id"]
        op = client.post(f"{API}/clusters/{cid}/addons/metrics-server", json={"kubelet_insecure_tls": True}).json()
        assert op["status"] == "succeeded", op["error"]
        paths = [p for p, _ in kube.applied]
        assert "/api/v1/namespaces/kube-system/serviceaccounts/metrics-server" in paths
        assert "/apis/rbac.authorization.k8s.io/v1/clusterroles/system:metrics-server" in paths
        deployment = next(o for p, o in kube.applied if p.endswith("/deployments/metrics-server"))
        assert "--kubelet-insecure-tls" in deployment["spec"]["template"]["spec"]["containers"][0]["args"]
        health = client.get(f"{API}/clusters/{cid}/health").json()
        assert health["metrics_available"] and health["cpu"]["used_m"] == 1000
