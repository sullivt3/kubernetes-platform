"""Historical log search uses bounded, scoped Loki queries through a Service."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient
from pytest import MonkeyPatch

from app.modules.kubernetes import client as kclient
from tests.fake_kube import kubeconfig_text
from tests.fake_store import FakeStore


def test_loki_connection_and_scoped_literal_search(client: TestClient, monkeypatch: MonkeyPatch) -> None:
    store = FakeStore()
    kclient.set_client_factory(store.factory)
    try:
        created = client.post("/api/v1/clusters/import", json={"name": "logging", "kubeconfig": kubeconfig_text()})
        assert created.status_code == 202, created.text
        base = f"/api/v1/clusters/{created.json()['cluster_id']}/logs"
        assert client.get(base + "/connection").json() == {"connection": None}
        assert client.get(base, params={"namespace": "apps"}).status_code == 409
        settings = {"connection": {"namespace": "monitoring", "service": "loki", "port": "http"}}
        assert client.put(base + "/connection", json=settings).json() == settings
        assert client.get(base + "/connection").json() == settings
        invalid_service = {"connection": {**settings["connection"], "port": "a/b"}}
        assert client.put(base + "/connection", json=invalid_service).status_code == 422
        assert client.get(base).status_code == 422
        for filters in ({"namespace": 'apps" |~ ".+'}, {"namespace": "apps", "window": "7d"}):
            assert client.get(base, params=filters).status_code == 422

        def namespace_labels(method, path, *, params, timeout):
            assert method == "GET" and timeout == 12
            assert path == "/api/v1/namespaces/monitoring/services/loki:http/proxy/loki/api/v1/label/namespace/values"
            assert params["end"] - params["start"] == 21600 * 1_000_000_000
            return 200, json.dumps(
                {"status": "success", "data": ["monitoring", "default", "default", "bad/value", 123]}
            ).encode()

        monkeypatch.setattr(store, "request", namespace_labels)
        namespaces = client.get(base + "/namespaces", params={"window": "6h"})
        assert namespaces.status_code == 200, namespaces.text
        assert namespaces.json() == ["default", "monitoring"]
        assert namespaces.headers["cache-control"] == "no-store"
        monkeypatch.setattr(store, "request", lambda *args, **kwargs: (200, b'{"status":"success","data":null}'))
        assert client.get(base + "/namespaces").json() == []

        text = '"} |= "needle"\n'
        expected = "/api/v1/namespaces/monitoring/services/loki:http/proxy/loki/api/v1/query_range"

        def query(method, path, *, params, timeout):
            assert method == "GET" and path == expected and timeout == 12
            assert params["query"] == '{namespace="apps",pod="web.v1",container="main"} |= ' + json.dumps(text)
            assert params["direction"] == "backward" and params["limit"] == 200
            assert params["end"] - params["start"] == 3600 * 1_000_000_000
            right = {"namespace": "apps", "pod": "web.v1", "container": "main"}
            start = params["start"]
            return 200, json.dumps(
                {
                    "status": "success",
                    "data": {
                        "resultType": "streams",
                        "result": [
                            {"stream": right, "values": [[str(start + i), "earlier"] for i in range(199)]},
                            {"stream": right, "values": [[str(start + 500), "x" * 5000]]},
                            {
                                "stream": {**right, "namespace": "external"},
                                "values": [[str(start + 800), "other namespace"]],
                            },
                        ],
                    },
                }
            ).encode()

        monkeypatch.setattr(store, "request", query)
        response = client.get(
            base,
            params={
                "namespace": "apps",
                "pod": "web.v1",
                "container": "main",
                "contains": text,
                "window": "1h",
            },
        )
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        result = response.json()
        assert result["limit"] == len(result["entries"]) == 200 and result["limited"]
        assert result["entries"][0]["line"] == "x" * 4096 and result["entries"][0]["truncated"]
        assert all(row["namespace"] == "apps" and row["pod"] == "web.v1" for row in result["entries"])
        assert "other namespace" not in response.text
        stamps = [int(entry["timestamp_ns"]) for entry in result["entries"]]
        assert stamps == sorted(stamps, reverse=True)
        monkeypatch.setattr(store, "request", lambda *args, **kwargs: (503, b'{"message":"upstream internals"}'))
        failed = client.get(base, params={"namespace": "apps"})
        assert failed.status_code == 502 and failed.json()["error"]["code"] == "loki_query_failed"
        assert "upstream internals" not in failed.text
        assert client.put(base + "/connection", json={"connection": None}).json() == {"connection": None}
        assert client.get(base, params={"namespace": "apps"}).status_code == 409
    finally:
        kclient.set_client_factory(None)
