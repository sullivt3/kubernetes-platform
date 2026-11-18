"""Bounded read-only metrics queries through the cluster's Kubernetes Service proxy."""

from __future__ import annotations

import json
import math
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Literal

from pydantic import BaseModel, HttpUrl, field_validator

from app.core.errors import PlatformError
from app.modules.kubernetes.client import ClusterAPI, KubernetesAPIError, get_json
from app.modules.kubernetes.service_proxy import ServiceEndpoint

Window = Literal["1h", "6h", "24h"]
Metric = Literal["node_cpu", "node_memory", "pod_cpu", "pod_memory"]
WINDOWS: dict[Window, tuple[int, int]] = {"1h": (3600, 30), "6h": (21600, 120), "24h": (86400, 300)}
SERIES_LIMIT = 8


class PrometheusService(ServiceEndpoint):
    grafana_url: HttpUrl | None = None

    @field_validator("grafana_url")
    @classmethod
    def validate_grafana_url(cls, value: HttpUrl | None) -> HttpUrl | None:
        if value is None:
            return value
        if value.scheme == "http" and value.host not in {"127.0.0.1", "localhost", "[::1]"}:
            raise ValueError("Use HTTPS for Grafana or HTTP on loopback for local testing")
        if any(part is not None for part in (value.username, value.password, value.query, value.fragment)):
            raise ValueError("Enter the Grafana base URL without credentials, query parameters, or a fragment")
        return value


class PrometheusUnavailableError(PlatformError):
    status_code = 502
    code = "prometheus_unavailable"


class MetricSeries(BaseModel):
    labels: dict[str, str]
    samples: list[tuple[float, float | None]]


class MetricPanel(BaseModel):
    series: list[MetricSeries]
    status: Literal["ok", "empty", "unavailable", "error"]


class MonitoringReport(BaseModel):
    start: int
    end: int
    step: int
    panels: dict[Metric, MetricPanel]


def _queries(namespace: str | None) -> dict[Metric, str]:
    pod = 'pod!="",container!="",container!="POD"'
    if namespace:
        pod += ",namespace=" + json.dumps(namespace)
    return {
        "node_cpu": '100 * (1 - avg by (instance,node) (rate(node_cpu_seconds_total{mode="idle"}[5m])))',
        "node_memory": (
            "max by (instance,node) (100 * (1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes))"
        ),
        "pod_cpu": (
            "sum by (namespace,pod) (max by (namespace,pod,container) "
            f"(rate(container_cpu_usage_seconds_total{{{pod}}}[5m])))"
        ),
        "pod_memory": (
            f"sum by (namespace,pod) (max by (namespace,pod,container) (container_memory_working_set_bytes{{{pod}}}))"
        ),
    }


def _parse_series(raw: dict[str, Any], *, start: int, end: int, step: int) -> MetricPanel:
    data = raw.get("data") or {}
    if raw.get("status") != "success" or data.get("resultType") != "matrix":
        return MetricPanel(series=[], status="error")
    series = []
    for item in data["result"][:96]:
        labels = {k: str(v) for k, v in item["metric"].items() if k in {"node", "instance", "pod", "namespace"}}
        samples = []
        for timestamp, value in item["values"][: (end - start) // step + 1]:
            ts, val = float(timestamp), float(value)
            if start <= ts <= end:
                samples.append((ts, val if math.isfinite(val) else None))
        if any(value is not None for _, value in samples):
            series.append(MetricSeries(labels=labels, samples=samples))
    series.sort(key=lambda row: (-max(v for _, v in row.samples if v is not None), sorted(row.labels.items())))
    return MetricPanel(series=series[:SERIES_LIMIT], status="ok" if series else "empty")


def _query(
    api: ClusterAPI, connection: PrometheusService, expression: str, *, start: int, end: int, step: int
) -> MetricPanel:
    # Cap evaluation cost and response size. PromQL is server-defined.
    params = {
        "query": f"topk({SERIES_LIMIT}, {expression})",
        "start": start,
        "end": end,
        "step": step,
        "limit": 96,
        "timeout": "8s",
    }
    try:
        payload = get_json(api, connection.proxy_path("/api/v1/query_range"), params, timeout=12)
        return _parse_series(payload, start=start, end=end, step=step)
    except KubernetesAPIError as exc:
        status = "unavailable" if exc.http_status in {404, 502, 503, 504} else "error"
        return MetricPanel(series=[], status=status)
    except (AttributeError, KeyError, TypeError, ValueError):
        return MetricPanel(series=[], status="error")


def collect(api: ClusterAPI, connection: PrometheusService, window: Window, namespace: str | None) -> MonitoringReport:
    duration, step = WINDOWS[window]
    end = int(time.time()) // step * step
    start = end - duration
    queries = _queries(namespace)
    with ThreadPoolExecutor(max_workers=len(queries)) as pool:
        results = {
            key: pool.submit(_query, api, connection, query, start=start, end=end, step=step)
            for key, query in queries.items()
        }
        panels = {key: result.result() for key, result in results.items()}
    if all(panel.status == "unavailable" for panel in panels.values()):
        raise PrometheusUnavailableError("Prometheus is unavailable through the configured Kubernetes Service")
    return MonitoringReport(start=start, end=end, step=step, panels=panels)
