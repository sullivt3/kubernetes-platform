"""Validated Kubernetes Service addresses for the monitoring and log proxies."""

from __future__ import annotations

import re
from typing import Annotated

from pydantic import BaseModel, StringConstraints, field_validator

DNSLabel = Annotated[str, StringConstraints(pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", max_length=63)]


class ServiceEndpoint(BaseModel):
    namespace: DNSLabel
    service: DNSLabel
    port: str

    @field_validator("port")
    @classmethod
    def validate_port(cls, value: str) -> str:
        if re.fullmatch(r"[0-9]{1,5}", value) and 1 <= int(value) <= 65535:
            return str(int(value))
        if re.fullmatch(r"[a-z0-9]([-a-z0-9]{0,13}[a-z0-9])?", value) and re.search(r"[a-z]", value):
            return value
        raise ValueError("Enter a Service port number or a Kubernetes port name")

    def proxy_path(self, path: str) -> str:
        return f"/api/v1/namespaces/{self.namespace}/services/{self.service}:{self.port}/proxy{path}"
