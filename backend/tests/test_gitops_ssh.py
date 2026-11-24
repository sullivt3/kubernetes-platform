"""Real Git/SSH interoperability plus encrypted API and trust boundaries."""

import io
import os
import shlex
import shutil
import socket
import socketserver
import subprocess
import threading
import time
import uuid
from pathlib import Path

import paramiko
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from app.core.errors import InvalidStateError
from app.core.secrets import get_secret_store
from app.db.session import get_session_factory
from app.modules.gitops import service as gs
from app.modules.gitops import source as git_source
from app.modules.gitops.models import GitOpsApplication
from app.modules.gitops.source import Configuration, GitSource
from app.modules.gitops.ssh_source import SSHAuthentication
from app.modules.kubernetes import client as kc
from app.orchestration.workflow import OperationCancelledError
from tests.fake_kube import kubeconfig_text
from tests.fake_store import FakeStore
from tests.test_gitops import env  # noqa: F401
from tests.test_identity import identity  # noqa: F401


@pytest.fixture(scope="module")
def keys():
    host = paramiko.RSAKey.generate(2048)
    client = paramiko.RSAKey.generate(2048)
    other = paramiko.RSAKey.generate(2048)
    plain, encrypted = io.StringIO(), io.StringIO()
    client.write_private_key(plain)
    client.write_private_key(encrypted, password="test-only-fixture-passphrase")
    return host, client, other, plain.getvalue(), encrypted.getvalue()


@pytest.fixture
def ssh_repository(tmp_path, keys):
    yield from private_ssh_repository(tmp_path, keys)


def private_ssh_repository(tmp_path, keys, bind="127.0.0.1"):
    binary = shutil.which("git")
    repo = tmp_path / "private-repo"
    repo.mkdir()
    subprocess.run(
        [binary, "-c", "core.hooksPath=/dev/null", "init", "-b", "main", str(repo)], check=True, capture_output=True
    )
    manifests = repo / "manifests"
    manifests.mkdir()
    (manifests / "app.yaml").write_text(
        "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: ssh-proof\ndata:\n  message: private-read\n"
    )
    for args in [
        ["add", "."],
        [
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.test",
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "-m",
            "fixture",
        ],
    ]:
        subprocess.run([binary, "-C", str(repo), *args], check=True, capture_output=True)
    host, client, *_ = keys
    state = {"auth_attempts": 0, "commands": [], "transports": []}

    class Auth(paramiko.ServerInterface):
        def check_auth_publickey(self, username, key):
            state["auth_attempts"] += 1
            return (
                paramiko.AUTH_SUCCESSFUL
                if username == "git" and key.asbytes() == client.asbytes()
                else paramiko.AUTH_FAILED
            )

        def get_allowed_auths(self, username):
            return "publickey"

        def check_channel_request(self, kind, chanid):
            return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

        def check_channel_exec_request(self, channel, command):
            args = shlex.split(command.decode())
            if len(args) != 2 or args[0] != "git-upload-pack":
                return False
            requested = Path(args[1])
            if (requested if requested.is_absolute() else repo.parent / requested) != repo:
                return False
            state["commands"].append(args)

            def serve():
                process = subprocess.Popen(
                    [binary, "upload-pack", str(repo)],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                )

                def receive():
                    try:
                        while data := channel.recv(65536):
                            process.stdin.write(data)
                            process.stdin.flush()
                    except (OSError, ValueError):
                        pass
                    finally:
                        process.stdin.close()

                threading.Thread(target=receive, daemon=True).start()
                try:
                    while data := os.read(process.stdout.fileno(), 65536):
                        channel.sendall(data)
                    process.wait(timeout=15)
                    channel.send_exit_status(process.returncode)
                finally:
                    channel.close()
                    if process.poll() is None:
                        process.kill()
                    process.wait()
                    process.stdout.close()

            threading.Thread(target=serve, daemon=True).start()
            return True

    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            transport = paramiko.Transport(self.request)
            state["transports"].append(transport)
            transport.add_server_key(host)
            try:
                transport.start_server(server=Auth())
                channel = transport.accept(10)
                end = time.monotonic() + 20
                while channel and not channel.closed and time.monotonic() < end:
                    time.sleep(0.02)
            except (OSError, paramiko.SSHException):
                pass
            finally:
                transport.close()

    server = socketserver.ThreadingTCPServer((bind, 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield repo, server.server_address[1], state
    finally:
        for transport in state["transports"]:
            transport.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def config(keys, **changes):
    host = keys[0]
    return {
        "url": "ssh://git@git.example.test/team/repo.git",
        "namespace": "default",
        "ssh_host_key": host.get_name() + " " + host.get_base64(),
        **changes,
    }


@pytest.mark.parametrize(
    "url",
    [
        "ssh://git@git.example.test/a/../b",
        "ssh://git:password@git.example.test/repo",
        "ssh://git@git.example.test/repo?token=value",
        "ssh://git@git.example.test/repo;whoami",
        "ssh://-flag@git.example.test/repo",
        "ssh://git@git.example.test:0/repo",
        "file:///repo",
        "git@git.example.test:-flag",
        "git@git.example.test://team/repo.git",
    ],
)
def test_unsafe_ssh_urls_rejected(keys, url):
    with pytest.raises(ValidationError):
        Configuration(**config(keys, url=url))


def test_explicit_pin_required_and_scp_path_semantics_preserved(keys):
    value = "git@git.example.test:team/repo.git"
    assert Configuration(**config(keys, url=value)).url == value
    assert git_source.split_url(value)[1] == "team/repo.git"
    assert git_source.split_url("ssh://git@git.example.test/team/repo.git")[1] == "/team/repo.git"
    with pytest.raises(ValidationError):
        Configuration(**config(keys, ssh_host_key=None))
    with pytest.raises(ValidationError):
        Configuration(**config(keys, ssh_host_key="ssh-rsa invalid"))


@pytest.mark.parametrize(
    "bad_pin,bad_key,encrypted",
    [(False, False, False), (False, False, True), (True, False, False), (False, True, False)],
)
def test_real_private_git_fetch_pin_and_key_failures(keys, ssh_repository, monkeypatch, bad_pin, bad_key, encrypted):
    repo, port, state = ssh_repository
    # Production rejects loopback. Only this local SSH fixture permits it.
    original = git_source.permitted_address
    monkeypatch.setattr(
        git_source,
        "permitted_address",
        lambda address, private: (address == "127.0.0.1" and private) or original(address, private),
    )
    host = keys[2] if bad_pin else keys[0]
    cfg = Configuration(
        **config(
            keys,
            url=f"ssh://git@127.0.0.1:{port}{repo}",
            allow_private_network=True,
            path="manifests",
            ssh_host_key=host.get_name() + " " + host.get_base64(),
        )
    )
    private = keys[4] if encrypted else keys[3]
    if bad_key:
        output = io.StringIO()
        keys[2].write_private_key(output)
        private = output.getvalue()
    auth = SSHAuthentication(
        mode="ssh", private_key=private, passphrase="test-only-fixture-passphrase" if encrypted else None
    )
    source = GitSource(cfg, auth)
    with source:
        assert all(private not in value for value in source.env.values())
        assert not any(private.encode() in p.read_bytes() for p in source.root.rglob("*") if p.is_file())
        if bad_pin or bad_key:
            with pytest.raises(InvalidStateError, match="Raw output withheld"):
                source.fetch()
        else:
            commit, objects = source.fetch()
            assert len(commit) == 40 and objects[0]["metadata"]["name"] == "ssh-proof"
    assert not source.root.exists()
    assert state["auth_attempts"] == 0 if bad_pin else state["auth_attempts"] > 0


def test_ssh_credential_encryption_rotation_preservation_and_url_binding(env, keys):
    client, base, app, body, *_ = env
    payload = {**body, **config(keys), "authentication": {"mode": "ssh", "private_key": keys[3]}}
    response = client.put(base + "/" + app["id"], json=payload)
    assert response.status_code == 200, response.text
    assert "PRIVATE KEY" not in response.text
    with get_session_factory()() as db:
        row = db.get(GitOpsApplication, uuid.UUID(app["id"]))
        cipher = row.ciphertext
        assert keys[3].encode() not in cipher
        auth = gs.auth_header(row, get_secret_store())
        assert auth.private_key.get_secret_value() == keys[3]
    payload.pop("authentication")
    assert client.put(base + "/" + app["id"], json=payload).status_code == 200

    with get_session_factory()() as db:
        assert db.get(GitOpsApplication, uuid.UUID(app["id"])).ciphertext == cipher
    payload["url"] = "ssh://git@other.example.test/team/repo.git"
    assert client.put(base + "/" + app["id"], json=payload).status_code == 409
    payload["authentication"] = {"mode": "ssh", "private_key": keys[4], "passphrase": "test-only-fixture-passphrase"}
    assert client.put(base + "/" + app["id"], json=payload).status_code == 200


def test_ssh_host_trust_is_admin_controlled_and_viewers_cannot_fetch(identity, keys):
    store = FakeStore()
    kc.set_client_factory(store.factory)
    try:
        actor = identity
        created = actor.client.post(
            "/api/v1/clusters/import",
            headers=actor.bearer(),
            json={"name": "ssh-roles", "kubeconfig": kubeconfig_text()},
        )
        base = f"/api/v1/clusters/{created.json()['cluster_id']}/gitops/applications"
        body = {**config(keys), "name": "ssh-role", "authentication": {"mode": "ssh", "private_key": keys[3]}}
        assert actor.client.post(base, headers=actor.bearer(), json=body).status_code == 403
        actor.role("org-admin")
        created = actor.client.post(base, headers=actor.bearer(), json=body)
        assert created.status_code == 201
        app_id = created.json()["id"]
        actor.role("operator")
        body.pop("authentication")
        assert actor.client.put(base + "/" + app_id, headers=actor.bearer(), json=body).status_code == 200
        body["ssh_host_key"] = keys[2].get_name() + " " + keys[2].get_base64()
        assert actor.client.put(base + "/" + app_id, headers=actor.bearer(), json=body).status_code == 403
        actor.role("viewer")
        assert actor.client.get(base, headers=actor.bearer()).status_code == 200
        assert actor.client.post(base + "/" + app_id + "/review", headers=actor.bearer()).status_code == 403
    finally:
        kc.set_client_factory(None)


@pytest.mark.parametrize("encrypted", [False, True])
def test_ed25519_openssh_key_and_host_pin_interoperate(tmp_path, keys, monkeypatch, encrypted):
    private = Ed25519PrivateKey.generate()
    plain = private.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, serialization.NoEncryption()
    ).decode()
    sealed = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.OpenSSH,
        serialization.BestAvailableEncryption(b"test-only-fixture-passphrase"),
    ).decode()
    key = paramiko.Ed25519Key.from_private_key(io.StringIO(plain))
    ed_keys = (key, key, keys[2], plain, sealed)
    fixture = private_ssh_repository(tmp_path, ed_keys)
    try:
        repo = next(fixture)
        test_real_private_git_fetch_pin_and_key_failures(ed_keys, repo, monkeypatch, False, False, encrypted)
    finally:
        fixture.close()


def test_loopback_relay_requires_capability_before_using_private_key(keys, ssh_repository, monkeypatch):
    repo, port, state = ssh_repository
    monkeypatch.setattr(git_source, "permitted_address", lambda address, private: address == "127.0.0.1" and private)
    cfg = Configuration(**config(keys, url=f"ssh://git@127.0.0.1:{port}{repo}", allow_private_network=True))
    auth = SSHAuthentication(mode="ssh", private_key=keys[3])
    with (
        GitSource(cfg, auth) as source,
        socket.create_connection(("127.0.0.1", source.tunnel.port), timeout=5) as connection,
    ):
        connection.sendall(b"\0" * 32)
        assert connection.recv(1) == b""
    assert state["auth_attempts"] == 0


def test_invalid_key_validation_never_echoes_input(env, keys):
    client, base, app, body, *_ = env
    marker = "private-key-invalid-test-only-marker"
    response = client.put(
        base + "/" + app["id"], json={**body, **config(keys), "authentication": {"mode": "ssh", "private_key": marker}}
    )
    assert response.status_code == 422 and marker not in response.text


def test_production_network_policy_blocks_loopback_before_authentication(keys, ssh_repository):
    repo, port, state = ssh_repository
    cfg = Configuration(**config(keys, url=f"ssh://git@127.0.0.1:{port}{repo}", allow_private_network=True))
    source = GitSource(cfg, SSHAuthentication(mode="ssh", private_key=keys[3]))
    with pytest.raises(InvalidStateError, match="blocked by network policy"), source:
        source.fetch()
    assert state["auth_attempts"] == 0 and not source.root.exists()


def test_ssh_cancellation_removes_temporary_source_and_closes_transport(keys, ssh_repository, monkeypatch):
    repo, port, _ = ssh_repository
    monkeypatch.setattr(git_source, "permitted_address", lambda address, private: address == "127.0.0.1" and private)
    cfg = Configuration(**config(keys, url=f"ssh://git@127.0.0.1:{port}{repo}", allow_private_network=True))
    source = GitSource(cfg, SSHAuthentication(mode="ssh", private_key=keys[3]))

    class Cancelled:
        def check_cancelled(self):
            raise OperationCancelledError("fixture cancelled")

    with pytest.raises(OperationCancelledError), source:
        source.ctx = Cancelled()
        source.fetch()
    assert not source.root.exists()
    assert all(not transport.is_active() for transport in source.tunnel.active)


def test_scp_repository_path_is_forwarded_without_becoming_absolute(keys, ssh_repository, monkeypatch):
    _, port, state = ssh_repository
    original = git_source.PinnedSSH

    class FixturePort(original):
        def __init__(self, host, requested_port, *args):
            assert requested_port == 22
            super().__init__(host, port, *args)

    monkeypatch.setattr(git_source, "PinnedSSH", FixturePort)
    monkeypatch.setattr(git_source, "permitted_address", lambda address, private: address == "127.0.0.1" and private)
    cfg = Configuration(**config(keys, url="git@127.0.0.1:private-repo", allow_private_network=True, path="manifests"))
    with GitSource(cfg, SSHAuthentication(mode="ssh", private_key=keys[3])) as source:
        _, objects = source.fetch()
    assert objects[0]["metadata"]["name"] == "ssh-proof"
    assert state["commands"] == [["git-upload-pack", "private-repo"]]
