"""Credential management.

Secret material flows in one direction: API -> SecretStore -> database. The only
way to obtain plaintext is :meth:`CredentialService.reveal`, which is intended for
orchestration workers and is never wired to an HTTP endpoint.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import ConfirmationRequiredError, ConflictError, NotFoundError, ValidationFailedError
from app.core.principal import Principal
from app.core.secrets import SealedSecret, SecretStore
from app.modules.auditing import service as audit
from app.modules.cluster_lifecycle.models import Cluster
from app.modules.credentials.models import Credential, CredentialKind
from app.modules.infrastructure.models import Machine, ProviderConnection

MAX_SECRET_BYTES = 256 * 1024
_SENSITIVE_METADATA_KEY = re.compile(r"(pass|secret|token|private|key_material|credential)", re.I)


def _validate_payload(kind: CredentialKind, value: str) -> None:
    """Light structural checks so obvious mistakes fail early. Errors never echo the secret."""
    if not value.strip():
        raise ValidationFailedError("Secret value must not be empty")
    if len(value.encode()) > MAX_SECRET_BYTES:
        raise ValidationFailedError("Secret value is too large (max 256 KiB)")
    if kind is CredentialKind.SSH_PRIVATE_KEY and "PRIVATE KEY-----" not in value:
        raise ValidationFailedError("Value does not look like a PEM/OpenSSH private key")
    if kind is CredentialKind.KUBECONFIG and not ("clusters:" in value and "contexts:" in value):
        raise ValidationFailedError("Value does not look like a kubeconfig (missing clusters/contexts)")
    if kind is CredentialKind.GCP_SERVICE_ACCOUNT:
        try:
            document = json.loads(value)
        except json.JSONDecodeError:
            raise ValidationFailedError("GCP service account key must be JSON") from None
        if document.get("type") != "service_account":
            raise ValidationFailedError("JSON is not a GCP service account key")


def _validate_metadata(metadata: dict[str, str]) -> None:
    if len(metadata) > 20:
        raise ValidationFailedError("At most 20 metadata entries are allowed")
    for key, value in metadata.items():
        if _SENSITIVE_METADATA_KEY.search(key):
            raise ValidationFailedError(
                f"Metadata key '{key}' looks sensitive; metadata is stored unencrypted and shown in the UI"
            )
        if len(key) > 64 or len(value) > 256:
            raise ValidationFailedError("Metadata keys are limited to 64 and values to 256 characters")


class CredentialService:
    def __init__(self, session: Session, secret_store: SecretStore) -> None:
        self.session = session
        self.store = secret_store

    def create(
        self,
        principal: Principal,
        *,
        name: str,
        kind: CredentialKind,
        secret_value: str,
        description: str | None = None,
        public_metadata: dict[str, str] | None = None,
        project_id: uuid.UUID | None = None,
    ) -> Credential:
        _validate_payload(kind, secret_value)
        _validate_metadata(public_metadata or {})
        sealed = self.store.seal(secret_value.encode())
        credential = Credential(
            organization_id=principal.organization_id,
            project_id=project_id,
            name=name,
            kind=kind,
            description=description,
            public_metadata=public_metadata or {},
            secret_backend=self.store.backend_name,
            key_id=sealed.key_id,
            ciphertext=sealed.ciphertext,
            created_by_id=principal.user_id,
        )
        self.session.add(credential)
        try:
            self.session.flush()
        except IntegrityError as exc:
            self.session.rollback()
            raise ConflictError(f"A credential named '{name}' already exists") from exc
        audit.record(
            self.session,
            principal=principal,
            action="credential.create",
            resource_type="credential",
            resource_id=credential.id,
            resource_name=name,
            details={"kind": kind.value},
        )
        self.session.commit()
        return credential

    def replace_secret(self, principal: Principal, credential_id: uuid.UUID, secret_value: str) -> Credential:
        credential = self.get(principal, credential_id)
        _validate_payload(credential.kind, secret_value)
        sealed = self.store.seal(secret_value.encode())
        credential.key_id = sealed.key_id
        credential.ciphertext = sealed.ciphertext
        credential.secret_backend = self.store.backend_name
        credential.rotated_at = datetime.now(UTC)
        audit.record(
            self.session,
            principal=principal,
            action="credential.rotate",
            resource_type="credential",
            resource_id=credential.id,
            resource_name=credential.name,
        )
        self.session.commit()
        return credential

    def list_credentials(
        self,
        principal: Principal,
        *,
        kind: CredentialKind | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[Credential], int]:
        query = select(Credential).where(Credential.organization_id == principal.organization_id)
        if kind:
            query = query.where(Credential.kind == kind)
        total = self.session.scalar(select(func.count()).select_from(query.subquery())) or 0
        rows = self.session.scalars(query.order_by(Credential.name).limit(limit).offset(offset)).all()
        return list(rows), total

    def get(self, principal: Principal, credential_id: uuid.UUID) -> Credential:
        credential = self.session.scalar(
            select(Credential).where(
                Credential.id == credential_id, Credential.organization_id == principal.organization_id
            )
        )
        if credential is None:
            raise NotFoundError("Credential not found")
        return credential

    def usages(self, credential_id: uuid.UUID) -> list[str]:
        refs: list[str] = []
        refs += [
            f"provider connection '{n}'"
            for n in self.session.scalars(
                select(ProviderConnection.name).where(ProviderConnection.credential_id == credential_id)
            )
        ]
        refs += [
            f"machine '{n}'"
            for n in self.session.scalars(
                select(Machine.name).where(Machine.access_credential_id == credential_id)
            )
        ]
        refs += [
            f"cluster '{n}'"
            for n in self.session.scalars(
                select(Cluster.name).where(or_(Cluster.access_credential_id == credential_id))
            )
        ]
        return refs

    def delete(self, principal: Principal, credential_id: uuid.UUID, *, confirm: str | None) -> None:
        credential = self.get(principal, credential_id)
        if confirm != credential.name:
            raise ConfirmationRequiredError(
                "Deleting a credential is irreversible. Repeat its name to confirm.",
                details={"expected_confirmation": credential.name},
            )
        in_use = self.usages(credential.id)
        if in_use:
            raise ConflictError(
                "Credential is still in use and cannot be deleted", details={"used_by": in_use}
            )
        audit.record(
            self.session,
            principal=principal,
            action="credential.delete",
            resource_type="credential",
            resource_id=credential.id,
            resource_name=credential.name,
            details={"kind": credential.kind.value},
        )
        self.session.delete(credential)
        self.session.commit()

    def reveal(self, organization_id: uuid.UUID, credential_id: uuid.UUID) -> bytes:
        """Decrypt a credential for use by an orchestration step. NEVER expose via the API."""
        credential = self.session.scalar(
            select(Credential).where(
                Credential.id == credential_id, Credential.organization_id == organization_id
            )
        )
        if credential is None:
            raise NotFoundError("Credential not found")
        plaintext = self.store.unseal(SealedSecret(credential.key_id, credential.ciphertext))
        credential.last_used_at = datetime.now(UTC)
        self.session.commit()
        return plaintext
