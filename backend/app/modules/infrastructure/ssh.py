"""SSH implementation of :class:`HostExecutor` (paramiko).

Host key policy
---------------
* If the machine has a recorded host key, the server MUST present exactly that key;
  anything else raises :class:`HostKeyMismatchError` (possible MITM or rebuilt host).
* If no key is recorded yet (first contact), the presented key is accepted and
  reported back through ``observed_host_key`` so the caller can persist it
  (trust-on-first-use). Accepting a *changed* key requires an explicit user action.

Privilege escalation
--------------------
Scripts run as root. For non-root users the executor uses ``sudo -n`` (key auth,
passwordless sudo) or ``sudo -S -k`` with the password supplied on stdin (password
auth); ``-k`` forces sudo to always consume the password line so it can never be
interpreted as script input.
"""

from __future__ import annotations

import base64
import hashlib
import io
import shlex
import time
from collections.abc import Callable
from dataclasses import dataclass

import paramiko

from app.modules.infrastructure.host_access import (
    CommandOptions,
    CommandResult,
    HostAccessError,
    HostEndpoint,
    HostExecutor,
    HostKey,
    HostKeyMismatchError,
    HostUnreachableError,
)

OutputCallback = Callable[[str], None]


@dataclass(frozen=True, slots=True)
class SshAuth:
    username: str
    private_key: str | None = None
    password: str | None = None

    def __post_init__(self) -> None:
        if not (self.private_key or self.password):
            raise ValueError("SSH authentication requires a private key or a password")


def load_private_key(pem: str) -> paramiko.PKey:
    """Load an unencrypted Ed25519, ECDSA or RSA private key from text."""
    last_error: Exception | None = None
    for key_cls in (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey):
        try:
            return key_cls.from_private_key(io.StringIO(pem))
        except paramiko.PasswordRequiredException as exc:
            raise HostAccessError("Passphrase-protected private keys are not supported yet") from exc
        except (paramiko.SSHException, ValueError) as exc:
            last_error = exc
    raise HostAccessError(f"Unsupported or invalid private key ({last_error})")


def host_key_from(key: paramiko.PKey) -> HostKey:
    digest = base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")
    return HostKey(key_type=key.get_name(), data=key.get_base64(), fingerprint=f"SHA256:{digest}")


class _RecordingPolicy(paramiko.MissingHostKeyPolicy):
    """Trust-on-first-use: accept the unknown key and remember it."""

    def __init__(self) -> None:
        self.key: paramiko.PKey | None = None

    def missing_host_key(self, client: paramiko.SSHClient, hostname: str, key: paramiko.PKey) -> None:
        self.key = key


def _known_hosts_name(endpoint: HostEndpoint) -> str:
    return endpoint.host if endpoint.port == 22 else f"[{endpoint.host}]:{endpoint.port}"


class SshHostExecutor(HostExecutor):
    def __init__(self, endpoint: HostEndpoint, auth: SshAuth, *, connect_timeout: float = 15.0) -> None:
        self.endpoint = endpoint
        self._auth = auth
        self._client = paramiko.SSHClient()
        self.observed_host_key: HostKey | None = None

        recorder = _RecordingPolicy()
        if endpoint.host_key is not None:
            pinned = paramiko.PKey.from_type_string(
                endpoint.host_key.key_type, base64.b64decode(endpoint.host_key.data)
            )
            self._client.get_host_keys().add(_known_hosts_name(endpoint), pinned.get_name(), pinned)
            self._client.set_missing_host_key_policy(paramiko.RejectPolicy())
        else:
            self._client.set_missing_host_key_policy(recorder)

        try:
            self._client.connect(
                endpoint.host,
                port=endpoint.port,
                username=auth.username,
                pkey=load_private_key(auth.private_key) if auth.private_key else None,
                password=auth.password if not auth.private_key else None,
                timeout=connect_timeout,
                banner_timeout=connect_timeout,
                auth_timeout=connect_timeout,
                allow_agent=False,
                look_for_keys=False,
            )
        except paramiko.BadHostKeyException as exc:
            self._client.close()
            raise HostKeyMismatchError(
                f"Host key for {endpoint.host}:{endpoint.port} changed: expected "
                f"{endpoint.host_key.fingerprint if endpoint.host_key else '?'}, got "
                f"{host_key_from(exc.key).fingerprint}. The server was rebuilt or the connection is being "
                "intercepted. Re-inspect the machine and explicitly accept the new key if this is expected.",
                observed=host_key_from(exc.key),
            ) from exc
        except paramiko.AuthenticationException as exc:
            self._client.close()
            raise HostAccessError(f"Authentication failed for {auth.username}@{endpoint.host}: {exc}") from exc
        except (TimeoutError, OSError, paramiko.SSHException) as exc:
            self._client.close()
            raise HostUnreachableError(f"Cannot connect to {endpoint.host}:{endpoint.port}: {exc}") from exc

        transport = self._client.get_transport()
        assert transport is not None
        self.observed_host_key = host_key_from(transport.get_remote_server_key())
        transport.set_keepalive(30)

    @property
    def is_root(self) -> bool:
        return self._auth.username == "root"

    def _wrap(self, command: str, sudo: bool) -> tuple[str, str]:
        """Return (command, stdin prefix) applying sudo when required."""
        if not sudo or self.is_root:
            return command, ""
        if self._auth.private_key:
            return f"sudo -n -- {command}", ""
        # Password auth: -S reads the password from stdin, -k forces a prompt every time.
        return f"sudo -S -k -p '' -- {command}", f"{self._auth.password}\n"

    def run(
        self,
        command: str,
        options: CommandOptions | None = None,
        on_output: OutputCallback | None = None,
    ) -> CommandResult:
        options = options or CommandOptions()
        if options.env:  # applied inside sudo, which would otherwise reset the environment
            exports = " ".join(f"{k}={shlex.quote(v)}" for k, v in options.env.items())
            command = f"env {exports} {command}"
        full_command, stdin_prefix = self._wrap(command, options.sudo)

        started = time.monotonic()
        transport = self._client.get_transport()
        if transport is None or not transport.is_active():
            raise HostUnreachableError(f"SSH connection to {self.endpoint.host} was lost")
        channel = transport.open_session(timeout=options.timeout_seconds)
        channel.settimeout(1.0)
        channel.exec_command(full_command)
        stdin_data = stdin_prefix + (options.stdin or "")
        if stdin_data:
            channel.sendall(stdin_data.encode())
        channel.shutdown_write()

        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        buffers = {"out": "", "err": ""}

        def emit(kind: str, chunk: bytes, final: bool = False) -> None:
            text = buffers[kind] + chunk.decode(errors="replace")
            lines = text.split("\n")
            buffers[kind] = "" if final else lines.pop()
            for line in lines:
                line = line.rstrip("\r")
                (stdout_parts if kind == "out" else stderr_parts).append(line)
                if on_output and line.strip():
                    on_output(line)

        while True:
            if channel.recv_ready():
                emit("out", channel.recv(65536))
            elif channel.recv_stderr_ready():
                emit("err", channel.recv_stderr(65536))
            elif channel.exit_status_ready():
                # drain anything left
                while channel.recv_ready():
                    emit("out", channel.recv(65536))
                while channel.recv_stderr_ready():
                    emit("err", channel.recv_stderr(65536))
                break
            else:
                if time.monotonic() - started > options.timeout_seconds:
                    channel.close()
                    raise HostAccessError(
                        f"Command timed out after {options.timeout_seconds:.0f}s on {self.endpoint.host}"
                    )
                time.sleep(0.05)
        emit("out", b"", final=True)
        emit("err", b"", final=True)
        exit_code = channel.recv_exit_status()
        channel.close()
        return CommandResult(
            command=command,
            exit_code=exit_code,
            stdout="\n".join(stdout_parts),
            stderr="\n".join(stderr_parts),
            duration_seconds=time.monotonic() - started,
        )

    def put_file(self, content: bytes, remote_path: str, *, mode: int = 0o600, sudo: bool = False) -> None:
        encoded = base64.b64encode(content).decode()
        script = (
            f"umask 077; mkdir -p {shlex.quote(remote_path.rsplit('/', 1)[0] or '/')} && "
            f"base64 -d > {shlex.quote(remote_path)} && chmod {mode:o} {shlex.quote(remote_path)}"
        )
        result = self.run(f"bash -c {shlex.quote(script)}", CommandOptions(sudo=sudo, stdin=encoded))
        if not result.ok:
            raise HostAccessError(f"Could not write {remote_path}: {result.stderr.strip()}")

    def get_file(self, remote_path: str, *, sudo: bool = False) -> bytes:
        result = self.run(f"cat {shlex.quote(remote_path)}", CommandOptions(sudo=sudo))
        if not result.ok:
            raise HostAccessError(f"Could not read {remote_path}: {result.stderr.strip()}")
        return result.stdout.encode()

    def close(self) -> None:
        self._client.close()
