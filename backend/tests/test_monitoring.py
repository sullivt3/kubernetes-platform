"""Cluster-scoped monitoring configuration and live Prometheus range request contract."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient
from pytest import MonkeyPatch

from app.modules.kubernetes import client as kclient
from tests.fake_kube import kubeconfig_text
from tests.fake_store import FakeStore


def test_prometheus_service_connection_and_history(client: TestClient, monkeypatch: MonkeyPatch) -> None:
    store = FakeStore()
    kclient.set_client_factory(store.factory)
    try:
        created = client.post("/api/v1/clusters/import", json={"name": "monitored", "kubeconfig": kubeconfig_text()})
        assert created.status_code == 202, created.text
        base = f"/api/v1/clusters/{created.json()['cluster_id']}/monitoring"
        assert client.get(base + "/connection").json() == {"connection": None}
        assert client.get(base).status_code == 409
        settings = {"connection": {"namespace": "monitoring", "service": "prometheus", "port": "web"}}
        assert client.put(base + "/connection", json=settings).json() == settings
        assert client.get(base + "/connection").json() == settings
        for bad in ({"service": "https://host"}, {"namespace": "../test"}, {"port": "0"}, {"port": "a/b"}):
            invalid = {"connection": {**settings["connection"], **bad}}
            assert client.put(base + "/connection", json=invalid).status_code == 422
        assert client.get(base + "/connection").json() == settings
        assert client.get(base, params={"window": "7d"}).status_code == 422
        assert client.get(base, params={"namespace": 'a"}, test'}).status_code == 422

        requests = []
        expected = "/api/v1/namespaces/monitoring/services/prometheus:web/proxy/api/v1/query_range"

        def query(method, path, *, params, timeout):
            assert method == "GET" and path == expected and timeout == 12
            requests.append(params)
            start = params["start"]
            raw = {
                "status": "success",
                "data": {
                    "resultType": "matrix",
                    "result": [
                        {
                            "metric": {"instance": "node-a:9100", "node": "node-a", "private": "omit"},
                            "values": [[start, "20.5"], [start + 120, "NaN"], [start + 240, "22"]],
                        }
                    ],
                },
            }
            if "container_memory" in params["query"]:
                raw["data"]["result"] = []
            if "node_memory" in params["query"]:
                return 422, b'{"error":"bad query"}'
            return 200, json.dumps(raw).encode()

        monkeypatch.setattr(store, "request", query)
        report = client.get(base, params={"window": "6h", "namespace": "apps"})
        assert report.status_code == 200, report.text
        assert report.headers["cache-control"] == "no-store"
        data = report.json()
        assert data["end"] - data["start"] == 21600
        assert data["step"] == 120 and len(requests) == 4
        assert all(req["timeout"] == "8s" and req["limit"] == 96 for req in requests)
        assert all('namespace="apps"' in req["query"] for req in requests if "container_" in req["query"])
        assert data["panels"]["node_cpu"]["series"] == [
            {
                "labels": {"instance": "node-a:9100", "node": "node-a"},
                "samples": [[data["start"], 20.5], [data["start"] + 120, None], [data["start"] + 240, 22.0]],
            }
        ]
        assert data["panels"]["node_memory"]["status"] == "error"
        assert data["panels"]["pod_memory"] == {"series": [], "status": "empty"}
        monkeypatch.setattr(store, "request", lambda *args, **kwargs: (404, b"{}"))
        missing = client.get(base)
        assert missing.status_code == 502
        assert missing.json()["error"]["code"] == "prometheus_unavailable"
        assert client.put(base + "/connection", json={"connection": None}).json() == {"connection": None}
        assert client.get(base).status_code == 409
    finally:
        kclient.set_client_factory(None)
