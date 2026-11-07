"""Open connections to registered machines from workflow steps.

This is the only place where machine credentials are decrypted. Secrets are
registered for log redaction before any command runs.
"""

from __future__ import annotations

import shlex
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.secrets import SecretStore
from app.modules.credentials.models import Credential, CredentialKind
from app.modules.credentials.service import CredentialService
from app.modules.infrastructure.host_access import (
    CommandOptions,
    CommandResult,
    HostAccessError,
    HostEndpoint,
    HostExecutor,
    HostKey,
    HostKeyMismatchError,
    HostUnreachableError,
    open_host,
)
from app.modules.infrastructure.models import Machine
from app.modules.infrastructure.ssh import SshAuth
from app.orchestration.context import StepContext
from app.orchestration.workflow import RetryableStepError, StepFailedError


@dataclass(frozen=True)
class MachineTarget:
    machine_id: uuid.UUID
    name: str
    endpoint: HostEndpoint
    auth: SshAuth


def load_target(
    session: Session, secret_store: SecretStore, organization_id: uuid.UUID, machine_id: uuid.UUID
) -> MachineTarget:
    machine = session.get(Machine, machine_id)
    if machine is None or machine.organization_id != organization_id:
        raise StepFailedError(f"Machine {machine_id} no longer exists", code="machine_not_found")
    if not machine.ssh_host or machine.access_credential_id is None:
        raise StepFailedError(f"Machine '{machine.name}' has no SSH access configured", code="no_access")
    credential = session.get(Credential, machine.access_credential_id)
    if credential is None:
        raise StepFailedError(f"The credential for machine '{machine.name}' was deleted", code="no_access")
    secret = CredentialService(session, secret_store).reveal(organization_id, credential.id).decode()
    username = machine.ssh_username or credential.public_metadata.get("username") or "root"
    if credential.kind is CredentialKind.SSH_PRIVATE_KEY:
        auth = SshAuth(username=username, private_key=secret)
    elif credential.kind is CredentialKind.SSH_PASSWORD:
        auth = SshAuth(username=username, password=secret)
    else:
        raise StepFailedError(
            f"Credential '{credential.name}' is a {credential.kind.value}, not an SSH credential",
            code="wrong_credential_kind",
        )
    host_key = (
        HostKey(machine.host_key_type, machine.host_key_data, machine.host_key_fingerprint or "")
        if machine.host_key_type and machine.host_key_data
        else None
    )
    endpoint = HostEndpoint(machine.ssh_host, machine.ssh_port, username, host_key)
    return MachineTarget(machine.id, machine.name, endpoint, auth)


def connect(ctx: StepContext, machine_id: uuid.UUID, *, accept_new_host_key: bool = False) -> HostExecutor:
    """Connect to a machine, enforcing (or, on first contact, recording) its host key."""
    with ctx.db() as session:
        target = load_target(session, ctx.secret_store, ctx.organization_id, machine_id)
    if target.auth.password:
        ctx.redact(target.auth.password)

    endpoint = target.endpoint
    if accept_new_host_key:
        endpoint = HostEndpoint(endpoint.host, endpoint.port, endpoint.username, None)
    try:
        executor = open_host(endpoint, target.auth)
    except HostKeyMismatchError as exc:
        raise StepFailedError(str(exc), code="host_key_mismatch") from exc
    except HostUnreachableError as exc:
        raise RetryableStepError(f"{target.name}: {exc}") from exc
    except HostAccessError as exc:
        raise StepFailedError(f"{target.name}: {exc}", code="ssh_error") from exc

    observed = executor.observed_host_key
    if observed is not None and (endpoint.host_key is None):
        with ctx.db() as session:
            machine = session.get(Machine, machine_id)
            assert machine is not None
            changed = machine.host_key_fingerprint not in (None, observed.fingerprint)
            machine.host_key_type = observed.key_type
            machine.host_key_data = observed.data
            machine.host_key_fingerprint = observed.fingerprint
        verb = "Accepted NEW" if changed else "Recorded"
        ctx.info(f"{target.name}: {verb} SSH host key {observed.key_type} {observed.fingerprint}")
    return executor


def run_script(
    executor: HostExecutor,
    script: str,
    *,
    env: dict[str, str] | None = None,
    sudo: bool = True,
    timeout_seconds: float = 1800.0,
    on_output: Callable[[str], None] | None = None,
) -> CommandResult:
    """Run a bash script on the host. The script travels on stdin (never on a command line)."""
    header = "".join(f"export {key}={shlex.quote(value)}\n" for key, value in (env or {}).items())
    body = (header + script).replace("\r\n", "\n")
    try:
        return executor.run(
            "bash -s", CommandOptions(sudo=sudo, stdin=body, timeout_seconds=timeout_seconds), on_output
        )
    except HostUnreachableError as exc:
        raise RetryableStepError(str(exc)) from exc
    except HostAccessError as exc:
        raise StepFailedError(str(exc), code="ssh_error") from exc


def tail(result: CommandResult, lines: int = 12) -> str:
    text = "\n".join(filter(None, [result.stdout.strip(), result.stderr.strip()]))
    return "\n".join(text.splitlines()[-lines:])
