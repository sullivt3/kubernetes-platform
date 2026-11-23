"""Private kubeconfigs, private Helm state, bounded processes and sanitized output."""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

import yaml

from app.core.errors import InvalidStateError, PlatformError
from app.modules.kubernetes.client import ConnectionTarget
from app.orchestration.context import StepContext


class HelmError(PlatformError):
    status_code = 502
    code = "helm_failed"


class Runner:
    def __init__(self, target: ConnectionTarget | None = None, ctx: StepContext | None = None):
        self.target, self.ctx = target, ctx
        self.temporary = tempfile.TemporaryDirectory(prefix="kplatform-helm-")
        self.root = Path(self.temporary.name)
        self.binary = shutil.which("helm")
        self.env = {
            key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "WINDIR", "LANG", "LC_ALL") if key in os.environ
        }
        self.env.update(
            {
                "HOME": str(self.root),
                "HELM_CONFIG_HOME": str(self.root / "config"),
                "HELM_CACHE_HOME": str(self.root / "cache"),
                "HELM_DATA_HOME": str(self.root / "data"),
                "HELM_PLUGINS": str(self.root / "plugins"),
                "HELM_DRIVER": "secret",
            }
        )

    def __enter__(self) -> Runner:
        try:
            if self.target:
                target = self.target
                cluster = {
                    "server": target.server,
                    "certificate-authority-data": base64.b64encode(target.kubeconfig.ca_pem.encode()).decode(),
                }
                if target.verify_name:
                    cluster["tls-server-name"] = target.verify_name
                user = {}
                token = target.token_provider() if target.token_provider else target.kubeconfig.token
                if token:
                    user["token"] = token
                for field, value in [
                    ("client-certificate-data", target.kubeconfig.client_cert_pem),
                    ("client-key-data", target.kubeconfig.client_key_pem),
                ]:
                    if value:
                        user[field] = base64.b64encode(value.encode()).decode()
                document = {
                    "apiVersion": "v1",
                    "kind": "Config",
                    "current-context": "platform",
                    "clusters": [{"name": "cluster", "cluster": cluster}],
                    "users": [{"name": "user", "user": user}],
                    "contexts": [{"name": "platform", "context": {"cluster": "cluster", "user": "user"}}],
                }
                config = self.write("kubeconfig", yaml.safe_dump(document).encode())
                self.env["KUBECONFIG"] = str(config)
            else:
                # Never inherit the developer's kubeconfig for catalog rendering.
                self.env["KUBECONFIG"] = str(self.write("kubeconfig", b"apiVersion: v1\nkind: Config\n"))
            if self.run(["version", "--template", "{{.Version}}"], timeout=10).strip() != b"v4.3.2":
                raise InvalidStateError("Install the pinned Helm 4.3.2 runtime on the API and worker host")
            return self
        except BaseException:
            self.temporary.cleanup()
            raise

    def __exit__(self, *args: Any) -> None:
        self.temporary.cleanup()

    def write(self, name: str, data: bytes) -> Path:
        path = self.root / name
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as file:
            file.write(data)
        return path

    def run(self, arguments: list[str], timeout: int = 30) -> bytes:
        if not self.binary:
            raise InvalidStateError("Install the pinned Helm runtime on the API and worker host")
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
            process = subprocess.Popen(
                [self.binary, *arguments],
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=errors,
                cwd=self.root,
                env=self.env,
                start_new_session=os.name == "posix",
            )
            deadline = time.monotonic() + timeout
            try:
                while process.poll() is None:
                    if self.ctx:
                        self.ctx.check_cancelled()
                    if time.monotonic() > deadline or output.tell() + errors.tell() > 32 * 1024 * 1024:
                        raise HelmError("Helm exceeded its time/output limit. Inspect the release before retrying.")
                    time.sleep(0.25)
                output.seek(0)
                data = output.read(32 * 1024 * 1024 + 1)
                if len(data) > 32 * 1024 * 1024:
                    raise HelmError("Helm output exceeded the supported size")
                if process.returncode:
                    # Never expose raw Helm output: templates and errors can echo values or Secrets.
                    raise HelmError(
                        "Helm failed. Check release status, chart values, resource ownership, "
                        "permissions and Kubernetes events."
                    )
                return data
            finally:
                if process.poll() is None:
                    if os.name == "posix":
                        os.killpg(process.pid, signal.SIGTERM)
                    else:
                        process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        if os.name == "posix":
                            os.killpg(process.pid, signal.SIGKILL)
                        else:
                            process.kill()
                        process.wait(timeout=5)

    def json(self, args: list[str]) -> Any:
        try:
            return json.loads(self.run(args))
        except (ValueError, TypeError):
            raise HelmError("Helm returned an unsupported response") from None

    def releases(self, namespace: str | None = None, name: str | None = None) -> list[dict]:
        args = ["list", "--output", "json", "--max", "257"]
        args += ["--namespace", namespace] if namespace else ["--all-namespaces"]
        if name:
            args += ["--filter", "^" + re.escape(name) + "$"]
        return self.json(args)

    def release(self, namespace: str, name: str) -> dict | None:
        rows = self.releases(namespace, name)
        return rows[0] if rows else None

    def history(self, namespace: str, name: str) -> list[dict]:
        return self.json(["history", name, "--namespace", namespace, "--max", "50", "--output", "json"])


def public_release(row: dict) -> dict:
    return {key: row.get(key) for key in ("name", "namespace", "revision", "updated", "status", "chart", "app_version")}


def public_history(row: dict) -> dict:
    # Deliberately omit descriptions, notes, manifests and values.
    return {key: row.get(key) for key in ("revision", "updated", "status", "chart", "app_version")}
