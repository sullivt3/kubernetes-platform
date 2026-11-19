"""Operational findings preserve coverage and point to actual failing resources."""

from app.modules.kubernetes import alerts
from app.modules.kubernetes.log_collection import alloy_config
from tests.fake_store import FakeStore


def test_node_and_pod_findings_and_missing_metric_coverage() -> None:
    store = FakeStore()
    store.put(
        {
            "apiVersion": "v1",
            "kind": "Node",
            "metadata": {"name": "offline"},
            "status": {"conditions": [{"type": "Ready", "status": "False"}]},
        }
    )
    store.put(
        {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": "broken", "namespace": "default"},
            "status": {
                "containerStatuses": [
                    {"name": "app", "state": {"waiting": {"reason": "CrashLoopBackOff"}}},
                ]
            },
        }
    )
    result = alerts.collect(store, None)
    assert {(r["name"], r["title"]) for r in result["findings"]} == {
        ("offline", "Node unavailable"),
        ("broken", "CrashLoopBackOff"),
        ("API server", "API readiness failed"),
    }
    assert result["coverage"]["cpu"] == "not_configured"
    assert all(row["guidance"] for row in result["findings"])
    assert 'names = ["default", "kplatform-monitoring"]' in alloy_config(["default", "kplatform-monitoring"])


def test_failed_container_is_reported_between_restart_attempts() -> None:
    store = FakeStore()
    store.put(
        {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": "restarting", "namespace": "default"},
            "status": {
                "phase": "Running",
                "containerStatuses": [
                    {
                        "name": "app",
                        "restartCount": 4,
                        "state": {"terminated": {"exitCode": 1, "reason": "Error"}},
                    }
                ],
            },
        }
    )
    report = alerts.collect(store, None)
    finding = next(f for f in report["findings"] if f["name"] == "restarting")
    assert finding["title"] == "Container failed"
    assert "code 1" in finding["evidence"] and "restarts: 4" in finding["evidence"]
