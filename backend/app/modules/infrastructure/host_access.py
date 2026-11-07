"""In-guest access to machines (command execution and file transfer).

Distinct from :mod:`app.modules.providers`: a provider creates/destroys machines,
a ``HostExecutor`` runs commands *inside* one. A Proxmox VM, an EC2 instance and a
bare-metal server are all prepared through the same executor interface, so OS
preparation and Kubernetes installation logic is written once.

Connections are opened through :func:`open_host`, whose connector can be replaced
(tests use an in-memory fake; future transports could add WinRM or SSM).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from types import TracebackType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.modules.infrastructure.ssh import SshAuth


class HostAccessError(Exception):
    """A command could not be executed (auth failure, timeout, protocol error)."""


class HostUnreachableError(HostAccessError):
    """The host could not be reached at all (likely transient)."""


class HostKeyMismatchError(HostAccessError):
    def __init__(self, message: str, *, observed: HostKey) -> None:
        super().__init__(message)
        self.observed = observed


@dataclass(frozen=True, slots=True)
class HostKey:
    key_type: str
    data: str  # base64 public key
    fingerprint: str  # "SHA256:..."


@dataclass(frozen=True, slots=True)
class HostEndpoint:
    host: str
    port: int = 22
    username: str = "root"
    # Pinned host key. None = first contact (trust on first use).
    host_key: HostKey | None = None


@dataclass(frozen=True, slots=True)
class CommandResult:
    command: str
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


@dataclass(frozen=True, slots=True)
class CommandOptions:
    sudo: bool = False
    timeout_seconds: float = 300.0
    env: dict[str, str] = field(default_factory=dict)
    stdin: str | None = None


class HostExecutor(ABC):
    """A live session to one machine. Use as a context manager."""

    endpoint: HostEndpoint
    observed_host_key: HostKey | None

    @abstractmethod
    def run(
        self,
        command: str,
        options: CommandOptions | None = None,
        on_output: Callable[[str], None] | None = None,
    ) -> CommandResult: ...

    @abstractmethod
    def put_file(self, content: bytes, remote_path: str, *, mode: int = 0o600, sudo: bool = False) -> None: ...

    @abstractmethod
    def get_file(self, remote_path: str, *, sudo: bool = False) -> bytes: ...

    @abstractmethod
    def close(self) -> None: ...

    def __enter__(self) -> HostExecutor:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


Connector = Callable[[HostEndpoint, "SshAuth"], HostExecutor]


def _ssh_connector(endpoint: HostEndpoint, auth: SshAuth) -> HostExecutor:
    from app.modules.infrastructure.ssh import SshHostExecutor

    return SshHostExecutor(endpoint, auth)


_connector: Connector = _ssh_connector


def open_host(endpoint: HostEndpoint, auth: SshAuth) -> HostExecutor:
    return _connector(endpoint, auth)


def set_connector(connector: Connector | None) -> None:
    """Replace the transport (tests). ``None`` restores SSH."""
    global _connector
    _connector = connector or _ssh_connector
