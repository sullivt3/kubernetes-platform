"""Opt-in public HTTPS destination IPs, pinned for each disposable runtime."""

import ipaddress
import re
import socket

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select

from app.core.errors import InvalidStateError
from app.modules.auditing import service as audit
from app.modules.command_center.session_models import CommandSession
from app.modules.organizations.policy import require


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hosts: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("hosts")
    @classmethod
    def validate_hosts(cls, hosts):
        result = []
        for host in hosts:
            host = host.lower().strip()
            if (
                len(host) > 253
                or not re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", host)
                or host.endswith((".local", ".internal", ".svc", ".cluster.local", ".localhost"))
            ):
                raise ValueError("Use exact public DNS hostnames without URLs, wildcards or ports")
            result.append(host)
        return sorted(set(result))


def get_policy(cluster):
    return Policy.model_validate((cluster.spec or {}).get("command_repository_access") or {}).model_dump()


def update(service, principal, cluster_id, body):
    from app.modules.command_center import sessions

    require(principal, "platform.manage")
    cluster = service.cluster(principal, cluster_id)
    service.session.refresh(cluster, with_for_update=True)
    cluster.spec = {**cluster.spec, "command_repository_access": body.model_dump()}
    affected = list(
        service.session.scalars(
            select(CommandSession).where(
                CommandSession.cluster_id == cluster_id,
                CommandSession.status != "closed",
                CommandSession.specification["repository_access"].as_boolean().is_(True),
            )
        )
    )
    for row in affected:
        row.status = "closing"
        row.specification = {**row.specification, "outcome": "repository_policy_changed"}
    audit.record(
        service.session,
        principal=principal,
        action="command_session.repository_policy_updated",
        resource_type="cluster",
        resource_id=cluster.id,
        details={"hosts": body.hosts},
    )
    service.session.commit()
    for row in affected:
        sessions.enqueue_cleanup(row.id)
    return body.model_dump()


def resolve(host):
    return socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)


def pinned(hosts):
    aliases, rules = [], []
    for host in hosts:
        try:
            addresses = sorted({str(record[4][0]) for record in resolve(host)})
            if not addresses or len(addresses) > 16:
                raise ValueError()
            for address in addresses:
                ip = ipaddress.ip_address(address)
                if not ip.is_global or ip.is_multicast or (getattr(ip, "ipv4_mapped", None) is not None):
                    raise ValueError()
                aliases.append({"ip": address, "hostnames": [host]})
                rules.append(
                    {
                        "to": [{"ipBlock": {"cidr": address + ("/32" if ip.version == 4 else "/128")}}],
                        "ports": [{"protocol": "TCP", "port": 443}],
                    }
                )
        except (OSError, ValueError):
            raise InvalidStateError("Repository DNS must resolve exclusively to bounded public addresses") from None
    return aliases, rules


def authorize(row, cluster):
    if (
        row.specification.get("repository_access")
        and row.specification["repository_hosts"] != get_policy(cluster)["hosts"]
    ):
        raise InvalidStateError("Repository access policy changed")
