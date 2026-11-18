"""Bounded historical Loki queries through a Kubernetes Service proxy."""

from __future__ import annotations

import json
import re
import time
from typing import Annotated, Any, Literal

from pydantic import BaseModel, StringConstraints, TypeAdapter, ValidationError

from app.core.errors import PlatformError
from app.modules.kubernetes.client import ClusterAPI, KubernetesAPIError, get_json
from app.modules.kubernetes.service_proxy import DNSLabel, ServiceEndpoint

Window = Literal["15m", "1h", "6h"]
WINDOW_SECONDS: dict[Window, int] = {"15m": 900, "1h": 3600, "6h": 21600}
PodName = Annotated[str, StringConstraints(pattern=r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$", max_length=253)]
SearchText = Annotated[str, StringConstraints(max_length=160)]
RESULT_LIMIT = 200
LINE_LIMIT = 4096


class LokiQueryError(PlatformError):
    status_code = 502
    code = "loki_query_failed"


class SearchFilters(BaseModel):
    namespace: DNSLabel
    pod: PodName | None = None
    container: DNSLabel | None = None
    contains: SearchText | None = None
    window: Window = "15m"

    def stream_labels(self) -> dict[str, str]:
        labels = {"namespace": self.namespace, "pod": self.pod, "container": self.container}
        return {name: value for name, value in labels.items() if value is not None}

    def logql(self) -> str:
        selector = "{" + ",".join(f"{name}={json.dumps(value)}" for name, value in self.stream_labels().items()) + "}"
        if self.contains:
            selector += " |= " + json.dumps(self.contains, ensure_ascii=False)
        return selector


class LogEntry(BaseModel):
    timestamp_ns: str
    namespace: str
    pod: str | None
    container: str | None
    line: str
    truncated: bool


class LogResults(BaseModel):
    entries: list[LogEntry]
    limit: int = RESULT_LIMIT
    limited: bool


def _bounds(window: Window) -> dict[str, int]:
    end = time.time_ns()
    return {"start": end - WINDOW_SECONDS[window] * 1_000_000_000, "end": end}


def _loki_data(api: ClusterAPI, path: str, params: dict[str, Any]) -> Any:
    try:
        payload = get_json(api, path, params, timeout=12)
        if payload.get("status") == "success":
            return payload["data"]
        raise ValueError("Expected a successful Loki response")
    except KubernetesAPIError as exc:
        if exc.http_status in {404, 502, 503, 504}:
            raise LokiQueryError("Loki is unavailable through the configured Kubernetes Service") from None
        raise LokiQueryError("Loki rejected the log query") from None
    except (AttributeError, KeyError, TypeError, ValueError):
        raise LokiQueryError("The configured Service returned an invalid Loki response") from None


def list_labels(
    api: ClusterAPI,
    connection: ServiceEndpoint,
    label: Literal["namespace", "pod", "container"],
    window: Window,
    filters: SearchFilters | None = None,
) -> list[str]:
    path = connection.proxy_path(f"/loki/api/v1/label/{label}/values")
    params: dict[str, Any] = _bounds(window)
    if filters:
        params["query"] = filters.logql()
    values = _loki_data(api, path, params)
    if values is None:
        return []
    if not isinstance(values, list):
        raise LokiQueryError("The configured Service returned an invalid namespace list")
    namespaces = set()
    for value in values:
        try:
            namespaces.add(TypeAdapter(PodName if label == "pod" else DNSLabel).validate_python(value))
        except ValidationError:
            continue
    return sorted(namespaces)


def search(api: ClusterAPI, connection: ServiceEndpoint, filters: SearchFilters) -> LogResults:
    params = {
        "query": filters.logql(),
        **_bounds(filters.window),
        "limit": RESULT_LIMIT,
        "direction": "backward",
    }
    path = connection.proxy_path("/loki/api/v1/query_range")
    try:
        data = _loki_data(api, path, params)
        if data.get("resultType") != "streams":
            raise ValueError("Expected Loki streams")
        entries = []
        labels = filters.stream_labels()
        for result in data["result"]:
            stream = result["stream"]
            if any(stream.get(key) != value for key, value in labels.items()):
                continue
            for timestamp, line in result["values"]:
                if not re.fullmatch(r"[0-9]{1,19}", timestamp) or not isinstance(line, str):
                    raise ValueError("Invalid Loki log entry")
                entries.append(
                    LogEntry(
                        timestamp_ns=timestamp,
                        namespace=filters.namespace,
                        pod=stream.get("pod"),
                        container=stream.get("container"),
                        line=line[:LINE_LIMIT],
                        truncated=len(line) > LINE_LIMIT,
                    )
                )
        entries.sort(key=lambda entry: int(entry.timestamp_ns), reverse=True)
        return LogResults(entries=entries[:RESULT_LIMIT], limited=len(entries) >= RESULT_LIMIT)
    except (AttributeError, KeyError, TypeError, ValueError):
        raise LokiQueryError("The configured Service returned an invalid Loki response") from None
