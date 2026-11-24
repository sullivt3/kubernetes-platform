"""Pinned SSH upload-pack transport; private keys stay in the parent process."""

from __future__ import annotations

import base64
import hmac
import io
import secrets
import select
import shlex
import socket
import socketserver
import threading
import time
from typing import Literal

import paramiko
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator


def load_key(value, passphrase=None):
    for cls in (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey):
        try:
            key = cls.from_private_key(io.StringIO(value), password=passphrase)
            if key.get_name() == "ssh-rsa" and key.get_bits() < 2048:
                break
            return key
        except Exception:
            continue
    raise ValueError("Provide a supported Ed25519/ECDSA or RSA-2048+ private key and correct passphrase")


def host_key(value):
    try:
        kind, encoded = value.strip().split()
        if kind not in {"ssh-ed25519", "ssh-rsa", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521"}:
            raise ValueError()
        key = paramiko.PKey.from_type_string(kind, base64.b64decode(encoded, validate=True))
        if key.get_name() != kind or (kind == "ssh-rsa" and key.get_bits() < 2048):
            raise ValueError()
        return key
    except Exception:
        raise ValueError(
            "Provide one trusted SSH host public key: algorithm base64, without hostname or comments"
        ) from None


class SSHAuthentication(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["ssh"]
    private_key: SecretStr = Field(max_length=65536)
    passphrase: SecretStr | None = Field(default=None, max_length=1024)

    @model_validator(mode="after")
    def valid(self):
        load_key(self.private_key.get_secret_value(), self.passphrase.get_secret_value() if self.passphrase else None)
        return self


# Git invokes this instead of system ssh. It cannot select another destination,
# user, command, key, agent, proxy or SSH configuration. The capability is in its
# isolated child environment; no private key or passphrase crosses that boundary.
RELAY = """import os,socket,sys,threading
def main():
    if os.name == "nt":
        import msvcrt
        msvcrt.setmode(sys.stdin.fileno(),os.O_BINARY)
        msvcrt.setmode(sys.stdout.fileno(),os.O_BINARY)
    with socket.create_connection(("127.0.0.1", int(sys.argv[1])),timeout=5) as peer:
        peer.settimeout(None)
        peer.sendall(bytes.fromhex(os.environ.pop("KPLATFORM_GIT_SSH_TOKEN")))
        def upload():
            try:
                while True:
                    data=os.read(sys.stdin.fileno(),65536)
                    if not data:break
                    peer.sendall(data)
                peer.shutdown(socket.SHUT_WR)
            except OSError:pass
        threading.Thread(target=upload,daemon=True).start()
        while True:
            data=peer.recv(65536)
            if not data:break
            sys.stdout.buffer.write(data);sys.stdout.buffer.flush()
try:main()
except (OSError,ValueError):sys.exit(1)
"""


class PinnedSSH:
    def __init__(self, host, port, address, username, path, pin, authentication, deadline):
        self.deadline = deadline
        self.transferred = 0
        self.lock = threading.Lock()
        self.active: set[paramiko.Transport] = set()
        self.capability = secrets.token_bytes(32)
        expected = host_key(pin)
        key = load_key(
            authentication.private_key.get_secret_value(),
            authentication.passphrase.get_secret_value() if authentication.passphrase else None,
        )
        tunnel = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                transport = None
                try:
                    self.request.settimeout(3)
                    token = b""
                    while len(token) < 32:
                        data = self.request.recv(32 - len(token))
                        if not data:
                            return
                        token += data
                    if not hmac.compare_digest(token, tunnel.capability) or time.monotonic() >= tunnel.deadline:
                        return
                    with socket.create_connection((address, port), timeout=5) as sock:
                        transport = paramiko.Transport(sock)
                        with tunnel.lock:
                            if time.monotonic() >= tunnel.deadline:
                                return
                            tunnel.active.add(transport)
                        transport.get_security_options().key_types = (
                            ("rsa-sha2-512", "rsa-sha2-256")
                            if expected.get_name() == "ssh-rsa"
                            else (expected.get_name(),)
                        )
                        transport.start_client(timeout=10)
                        if not hmac.compare_digest(transport.get_remote_server_key().asbytes(), expected.asbytes()):
                            return
                        transport.auth_timeout = 10
                        transport.auth_publickey(username, key)
                        channel = transport.open_session(timeout=10)
                        channel.settimeout(2)
                        channel.exec_command("git-upload-pack " + shlex.quote(path))
                        self.request.settimeout(2)
                        input_open = True
                        while time.monotonic() < tunnel.deadline:
                            peers = [channel, self.request] if input_open else [channel]
                            ready, _, _ = select.select(peers, [], [], 0.2)
                            for source in ready:
                                data = source.recv(65536)
                                if not data:
                                    if source is self.request:
                                        input_open = False
                                        channel.shutdown_write()
                                        continue
                                    return
                                with tunnel.lock:
                                    tunnel.transferred += len(data)
                                    if tunnel.transferred > 128 * 1024 * 1024:
                                        return
                                (channel if source is self.request else self.request).sendall(data)
                            if channel.recv_stderr_ready():
                                discarded = channel.recv_stderr(65536)
                                with tunnel.lock:
                                    tunnel.transferred += len(discarded)
                                    if tunnel.transferred > 128 * 1024 * 1024:
                                        return
                except Exception:
                    # Neither Paramiko diagnostics nor remote stderr may expose
                    # credentials or repository content through public operations.
                    return
                finally:
                    if transport:
                        transport.close()
                        with tunnel.lock:
                            tunnel.active.discard(transport)

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

            def handle_error(self, request, client_address):
                pass

        self.server = Server(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def close(self):
        self.deadline = 0
        with self.lock:
            active = list(self.active)
        for transport in active:
            transport.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
