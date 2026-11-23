"""Write-only repository authentication, encrypted and bound to a reviewed source."""

from __future__ import annotations

import base64
import json
import uuid
from typing import Literal

from pydantic import BaseModel, Field, SecretStr, model_validator
from sqlalchemy import select

from app.core.errors import InvalidStateError
from app.core.secrets import SealedSecret
from app.modules.cluster_lifecycle.models import ClusterLifecycleState
from app.modules.helm.catalog import Repository
from app.modules.helm.models import HelmRepositoryCredential
from app.modules.organizations.policy import require


class Authentication(BaseModel):
    mode: Literal["basic", "bearer"] = "basic"
    username: str = Field(default="", max_length=256)
    password: SecretStr

    @model_validator(mode="after")
    def valid_header(self) -> Authentication:
        value = self.password.get_secret_value()
        if not value or len(value) > 16384 or any(c in value + self.username for c in "\r\n"):
            raise ValueError("Provide a nonempty password/token without line breaks")
        if self.mode == "basic" and (not self.username or ":" in self.username):
            raise ValueError("Basic authentication requires a username without colons")
        return self


class RepositoryInput(Repository):
    authentication: Authentication | None = None
    clear_credentials: bool = False


def authorization(session, store, cluster, repository: Repository, *, current: bool = False) -> str | None:
    if cluster.lifecycle_state in {ClusterLifecycleState.DELETING, ClusterLifecycleState.DELETED}:
        raise InvalidStateError("The cluster is being deleted")
    if current:
        configured = next((r for r in cluster.helm_repositories or [] if r["name"] == repository.name), None)
        if not configured or Repository.model_validate(configured).model_dump() != repository.model_dump():
            raise InvalidStateError("Repository configuration changed after review; preview again")
    if repository.auth_id is None:
        return None
    row = session.get(HelmRepositoryCredential, repository.auth_id)
    if (
        row is None
        or row.cluster_id != cluster.id
        or row.organization_id != cluster.organization_id
        or row.project_id != cluster.project_id
        or row.repository_url != repository.url
        or row.revoked
    ):
        raise InvalidStateError("Repository credentials are unavailable or revoked; configure them and review again")
    try:
        auth = Authentication.model_validate(json.loads(store.unseal(SealedSecret(row.key_id, row.ciphertext))))
    except (ValueError, TypeError):
        raise InvalidStateError("Repository authentication could not be loaded") from None
    if auth.mode == "bearer":
        return "Bearer " + auth.password.get_secret_value()
    raw = (auth.username + ":" + auth.password.get_secret_value()).encode()
    return "Basic " + base64.b64encode(raw).decode()


def save(service, principal, cluster, entries: list[RepositoryInput]) -> list[dict]:
    if len({r.name for r in entries}) != len(entries):
        raise InvalidStateError("Repository names must be unique")
    previous = {r["name"]: Repository.model_validate(r) for r in cluster.helm_repositories or []}
    result = []
    for item in entries:
        old = previous.get(item.name)
        repo = Repository.model_validate(item.model_dump(exclude={"authentication", "clear_credentials"}))
        restricted = repo.allow_private_network or bool(repo.ca_pem) or bool(repo.token_service_url)
        if restricted and (
            old is None
            or (old.url, old.allow_private_network, old.ca_pem, old.token_service_url)
            != (repo.url, repo.allow_private_network, repo.ca_pem, repo.token_service_url)
        ):
            require(principal, "organization.manage")
        if item.authentication and item.clear_credentials:
            raise InvalidStateError("Choose either replacement credentials or credential removal")
        if item.authentication:
            sealed = service.secret_store.seal(
                json.dumps(
                    {
                        "mode": item.authentication.mode,
                        "username": item.authentication.username,
                        "password": item.authentication.password.get_secret_value(),
                    }
                ).encode()
            )
            row = HelmRepositoryCredential(
                organization_id=cluster.organization_id,
                project_id=cluster.project_id,
                cluster_id=cluster.id,
                repository_url=repo.url,
                key_id=sealed.key_id,
                ciphertext=sealed.ciphertext,
            )
            service.session.add(row)
            service.session.flush()
            repo.auth_id = row.id
        elif item.clear_credentials:
            repo.auth_id = None
        elif repo.auth_id is not None:
            authorization(service.session, service.secret_store, cluster, repo)
        elif old and old.auth_id:
            # Omitting a credential reference must not silently downgrade an authenticated source.
            if old.url != repo.url:
                raise InvalidStateError("Re-enter credentials or explicitly remove them when changing a repository URL")
            repo.auth_id = old.auth_id
            authorization(service.session, service.secret_store, cluster, repo)
        result.append(repo.model_dump(mode="json"))
    active = {uuid.UUID(r["auth_id"]) for r in result if r.get("auth_id")}
    for row in service.session.scalars(
        select(HelmRepositoryCredential).where(
            HelmRepositoryCredential.cluster_id == cluster.id,
            HelmRepositoryCredential.revoked.is_(False),
        )
    ):
        if row.id not in active:
            row.revoked = True
    return result


def source_authorization(session, store, cluster, source: dict) -> str | None:
    if not source.get("repository"):
        return None  # Backward-compatible public plans from migration 0010.
    return authorization(session, store, cluster, Repository.model_validate(source["repository"]), current=True)
