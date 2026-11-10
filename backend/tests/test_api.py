"""HTTP API behaviour, end to end through FastAPI (inline orchestration)."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

API = "/api/v1"
KUBECONFIG = (
    "apiVersion: v1\nclusters:\n- name: c\ncontexts:\n- name: x\nusers:\n- name: u\n  token: s3cr3t-token-value\n"
)


def parse_sse(body: str) -> list[tuple[str, str]]:
    events = []
    for block in body.strip().split("\n\n"):
        name, data = "message", []
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data.append(line[6:])
        if data:
            events.append((name, "\n".join(data)))
    return events


class TestHealthAndSystem:
    def test_liveness(self, client: TestClient) -> None:
        assert client.get(f"{API}/health/live").json() == {"status": "ok"}

    def test_readiness_reports_components(self, client: TestClient) -> None:
        response = client.get(f"{API}/health/ready")
        assert response.status_code == 200
        body = response.json()
        components = {c["name"]: c for c in body["components"]}
        assert set(components) == {"database", "redis", "workers", "secret_store"}
        assert components["database"]["status"] == "ok"
        assert components["database"]["details"]["up_to_date"] is True
        assert components["secret_store"]["details"]["primary_key_id"] == "test"
        assert body["status"] in {"ok", "degraded"}

    def test_system_info(self, client: TestClient) -> None:
        info = client.get(f"{API}/system/info").json()
        assert info["environment"] == "test"
        assert info["auth_mode"] == "dev"
        assert info["current_user"]["roles"] == ["platform-admin"]
        assert "SECRET" not in json.dumps(info).upper().replace("SECRET_BACKEND", "").replace(
            "SECRET_PRIMARY_KEY_ID", ""
        )

    def test_request_id_and_security_headers(self, client: TestClient) -> None:
        response = client.get(f"{API}/system/info", headers={"X-Request-ID": "trace-123"})
        assert response.headers["x-request-id"] == "trace-123"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["cache-control"] == "no-store"

    def test_invalid_request_id_is_replaced(self, client: TestClient) -> None:
        response = client.get(f"{API}/health/live", headers={"X-Request-ID": "bad id with spaces"})
        assert response.headers["x-request-id"] != "bad id with spaces"


class TestCatalog:
    def test_providers(self, client: TestClient) -> None:
        providers = client.get(f"{API}/catalog/providers").json()
        assert {p["type"] for p in providers} == {"local", "ssh", "proxmox", "vsphere", "aws", "azure", "gcp"}
        status = {p["type"]: p["implementation_status"] for p in providers}
        assert status == {
            "ssh": "available",
            "local": "available",
            "proxmox": "preview",
            "aws": "preview",
            "azure": "preview",
            "gcp": "preview",
            "vsphere": "preview",
        }
        proxmox = next(p for p in providers if p["type"] == "proxmox")
        assert "api_url" in proxmox["config_schema"]["properties"]

    def test_distributions(self, client: TestClient) -> None:
        distributions = client.get(f"{API}/catalog/distributions").json()
        kubeadm = next(d for d in distributions if d["type"] == "kubeadm")
        assert "ssh" in kubeadm["supported_providers"]
        assert kubeadm["supported_runtimes"] == ["containerd"]

    def test_operation_types(self, client: TestClient) -> None:
        types = {t["type"]: t for t in client.get(f"{API}/catalog/operation-types").json()}
        assert types["system.self_check"]["user_startable"] is True


class TestOperations:
    def test_self_check_end_to_end(self, client: TestClient) -> None:
        response = client.post(f"{API}/system/self-check")
        assert response.status_code == 202
        op = response.json()
        assert op["status"] == "succeeded", op
        assert [s["key"] for s in op["steps"]] == ["worker", "database", "redis", "secret_store", "catalogs"]
        assert all(s["status"] == "succeeded" for s in op["steps"])
        assert op["result"]["database"]["schema_revision"]
        assert op["result"]["providers"] == {"available": 2, "preview": 5}
        assert op["result"]["distributions"] == {"available": 4, "preview": 4}

        listed = client.get(f"{API}/operations").json()
        assert listed["total"] == 1 and listed["items"][0]["id"] == op["id"]
        assert client.get(f"{API}/operations", params={"active": True}).json()["total"] == 0

        logs = client.get(f"{API}/operations/{op['id']}/logs").json()
        assert logs[-1]["message"] == "Operation completed successfully"
        later = client.get(f"{API}/operations/{op['id']}/logs", params={"after": logs[-2]["id"]}).json()
        assert [entry["id"] for entry in later] == [logs[-1]["id"]]

    def test_event_stream_replays_and_ends_for_finished_operation(self, client: TestClient) -> None:
        op = client.post(f"{API}/system/self-check").json()
        with client.stream("GET", f"{API}/operations/{op['id']}/events") as response:
            assert response.headers["content-type"].startswith("text/event-stream")
            # no-transform prevents proxies (incl. Next.js gzip) from buffering the stream
            assert "no-transform" in response.headers["cache-control"]
            body = "".join(response.iter_text())
        events = parse_sse(body)
        names = [name for name, _ in events]
        assert names.count("log") >= 5
        assert "operation" in names
        assert names[-1] == "end"
        snapshot = json.loads(next(data for name, data in events if name == "operation"))
        assert snapshot["status"] == "succeeded"

    def test_event_stream_resumes_after_cursor(self, client: TestClient) -> None:
        op = client.post(f"{API}/system/self-check").json()
        logs = client.get(f"{API}/operations/{op['id']}/logs").json()
        with client.stream(
            "GET", f"{API}/operations/{op['id']}/events", headers={"Last-Event-ID": str(logs[-2]["id"])}
        ) as response:
            events = parse_sse("".join(response.iter_text()))
        assert [json.loads(d)["id"] for n, d in events if n == "log"] == [logs[-1]["id"]]

    def test_cancel_and_retry_rules(self, client: TestClient) -> None:
        op = client.post(f"{API}/system/self-check").json()
        cancel = client.post(f"{API}/operations/{op['id']}/cancel")
        assert cancel.status_code == 409
        assert cancel.json()["error"]["code"] == "invalid_state"
        assert client.post(f"{API}/operations/{op['id']}/retry").status_code == 409

    def test_unknown_operation_returns_structured_404(self, client: TestClient) -> None:
        response = client.get(f"{API}/operations/00000000-0000-0000-0000-000000000000")
        assert response.status_code == 404
        error = response.json()["error"]
        assert error["code"] == "not_found" and error["request_id"]

    def test_validation_errors_are_structured(self, client: TestClient) -> None:
        response = client.get(f"{API}/operations", params={"limit": 0})
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "validation_failed"

    def test_idempotency_header(self, client: TestClient) -> None:
        first = client.post(f"{API}/system/self-check", headers={"Idempotency-Key": "k-1"}).json()
        second = client.post(f"{API}/system/self-check", headers={"Idempotency-Key": "k-1"}).json()
        assert first["id"] == second["id"]


class TestCredentials:
    def create(self, client: TestClient, **overrides: object) -> dict:  # type: ignore[type-arg]
        body = {
            "name": "prod-kubeconfig",
            "kind": "kubeconfig",
            "secret_value": KUBECONFIG,
            "public_metadata": {"context": "prod"},
        }
        body.update(overrides)
        return client.post(f"{API}/credentials", json=body)  # type: ignore[return-value]

    def test_secret_never_returned(self, client: TestClient) -> None:
        response = self.create(client)
        assert response.status_code == 201
        created = response.json()
        assert "secret_value" not in created and "ciphertext" not in created
        assert "s3cr3t-token-value" not in response.text
        detail = client.get(f"{API}/credentials/{created['id']}")
        assert detail.json()["used_by"] == []
        listing = client.get(f"{API}/credentials")
        for r in (detail, listing):
            assert "s3cr3t-token-value" not in r.text

    def test_secret_is_encrypted_at_rest_and_decryptable_by_workers(self, client: TestClient, session) -> None:  # type: ignore[no-untyped-def]
        from sqlalchemy import select

        from app.core.secrets import get_secret_store
        from app.modules.credentials.models import Credential
        from app.modules.credentials.service import CredentialService

        created = self.create(client).json()
        row = session.scalar(select(Credential).where(Credential.name == "prod-kubeconfig"))
        assert b"s3cr3t-token-value" not in row.ciphertext
        plaintext = CredentialService(session, get_secret_store()).reveal(row.organization_id, row.id)
        assert plaintext.decode() == KUBECONFIG
        assert client.get(f"{API}/credentials/{created['id']}").json()["last_used_at"] is not None

    def test_duplicate_name_conflicts(self, client: TestClient) -> None:
        self.create(client)
        assert self.create(client).status_code == 409

    def test_payload_shape_validated_without_echoing_secret(self, client: TestClient) -> None:
        response = self.create(client, kind="ssh_private_key", secret_value="definitely-not-a-key")
        assert response.status_code == 422
        assert "definitely-not-a-key" not in response.text

    def test_sensitive_metadata_rejected(self, client: TestClient) -> None:
        response = self.create(client, public_metadata={"password": "oops"})
        assert response.status_code == 422

    def test_invalid_name_rejected(self, client: TestClient) -> None:
        assert self.create(client, name="Bad Name").status_code == 422

    def test_rotation(self, client: TestClient) -> None:
        created = self.create(client).json()
        rotated = client.put(f"{API}/credentials/{created['id']}/secret", json={"secret_value": KUBECONFIG + "#"})
        assert rotated.status_code == 200 and rotated.json()["rotated_at"]

    def test_delete_requires_confirmation(self, client: TestClient) -> None:
        created = self.create(client).json()
        url = f"{API}/credentials/{created['id']}"
        missing = client.delete(url)
        assert missing.status_code == 428
        assert missing.json()["error"]["details"]["expected_confirmation"] == "prod-kubeconfig"
        assert client.delete(url, params={"confirm": "wrong"}).status_code == 428
        assert client.delete(url, params={"confirm": "prod-kubeconfig"}).status_code == 204
        assert client.get(url).status_code == 404

    def test_actions_are_audited_without_secrets(self, client: TestClient) -> None:
        created = self.create(client).json()
        client.delete(
            f"{API}/credentials/{created['id']}",
            params={"confirm": "prod-kubeconfig"},
            headers={"X-Request-ID": "audit-req-1"},
        )
        events = client.get(f"{API}/audit-events", params={"resource_type": "credential"}).json()
        actions = [e["action"] for e in events["items"]]
        assert actions == ["credential.delete", "credential.create"]
        assert events["items"][0]["request_id"] == "audit-req-1"
        assert events["items"][0]["actor_subject"] == "dev|local-admin"
        assert "s3cr3t-token-value" not in json.dumps(events)


def test_clusters_list_is_empty_until_registration_exists(client: TestClient) -> None:
    assert client.get(f"{API}/clusters").json() == []


def test_openapi_documents_every_route(client: TestClient) -> None:
    schema = client.get(f"{API}/openapi.json").json()
    assert f"{API}/operations/{{operation_id}}/events" in schema["paths"]
    assert len(schema["paths"]) >= 18
