"""Isolated HTTPS Git fetches: TLS/DNS pinning, no checkout, hooks or submodules."""

from __future__ import annotations

import os
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, Field, model_validator

from app.core.errors import InvalidStateError
from app.modules.gitops.tunnel import PinnedTunnel
from app.modules.helm.catalog import Repository, values
from app.modules.helm.credentials import Authentication
from app.modules.helm.transport import check_url, permitted_address


class Configuration(BaseModel):
    url: str = Field(max_length=2048)
    branch: str = Field(default="main", pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
    path: str = Field(default=".", max_length=256)
    namespace: str = Field(pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", max_length=63)
    allow_private_network: bool = False
    ca_pem: str | None = Field(default=None, max_length=65536)

    @model_validator(mode="after")
    def valid(self):
        self.url = check_url(self.url, repository=True, private=self.allow_private_network)
        if urlsplit(self.url).scheme != "https":
            raise ValueError("Git repositories require HTTPS")
        Repository(name="git", url=self.url, allow_private_network=self.allow_private_network, ca_pem=self.ca_pem)
        if (
            ".." in self.branch
            or "@{" in self.branch
            or "//" in self.branch
            or self.branch.endswith(("/", "."))
            or any(p.startswith(".") or p.endswith(".lock") for p in self.branch.split("/"))
        ):
            raise ValueError("Use a valid branch name")
        if self.path != "." and (
            not re.fullmatch(r"[A-Za-z0-9_./-]+", self.path)
            or any(p in {"", ".", ".."} for p in self.path.split("/"))
            or self.path.startswith("-")
        ):
            raise ValueError("Use a relative directory without traversal or wildcards")
        return self


class ApplicationInput(Configuration):
    name: str = Field(pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", max_length=63)
    authentication: Authentication | None = None
    clear_credentials: bool = False


class GitSource:
    def __init__(self, config: Configuration, authorization: str | None = None, ctx=None):
        self.config, self.authorization, self.ctx = config, authorization, ctx
        self.temp = tempfile.TemporaryDirectory(prefix="kplatform-gitops-")
        self.root = Path(self.temp.name)
        self.binary = shutil.which("git")
        self.tunnel: PinnedTunnel | None = None
        self.deadline = time.monotonic() + 120
        self.env = {k: os.environ[k] for k in ("PATH", "SYSTEMROOT", "WINDIR", "LANG", "LC_ALL") if k in os.environ}
        self.env.update(
            HOME=str(self.root),
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_TERMINAL_PROMPT="0",
            GIT_ASKPASS="",
            GIT_PROTOCOL_FROM_USER="0",
            GIT_ATTR_NOSYSTEM="1",
        )

    def __enter__(self):
        try:
            if not self.binary:
                raise InvalidStateError("Install Git 2.46 or newer on the API and worker hosts")
            match = re.search(rb"(\d+)\.(\d+)", self.run(["--version"]))
            if not match or tuple(map(int, match.groups())) < (2, 46):
                raise InvalidStateError("Install Git 2.46 or newer for verified DNS-pinned HTTPS fetches")
            parsed = urlsplit(self.config.url)
            host = parsed.hostname or ""
            port = parsed.port or 443
            addresses = sorted({str(r[4][0]) for r in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})
            if not addresses or any(not permitted_address(a, self.config.allow_private_network) for a in addresses):
                raise InvalidStateError("Repository endpoint is blocked by network policy")
            address = addresses[0]
            if ":" in address:
                address = "[" + address + "]"
            self.tunnel = PinnedTunnel(host, port, addresses[0], self.deadline)
            settings = {
                "protocol.allow": "never",
                "protocol.https.allow": "always",
                "http.followRedirects": "false",
                "http.proxy": self.tunnel.url,
                "http.sslVerify": "true",
                "http.curloptResolve": f"{host}:{port}:{address}",
                "http.lowSpeedLimit": "1024",
                "http.lowSpeedTime": "20",
                "credential.helper": "",
                "core.hooksPath": str(self.root / "no-hooks"),
                "fetch.recurseSubmodules": "false",
                "transfer.fsckObjects": "true",
                "gc.auto": "0",
                "maintenance.auto": "false",
                "http.maxRequests": "1",
            }
            if self.authorization:
                settings["http.extraHeader"] = "Authorization: " + self.authorization
            if self.config.ca_pem:
                ca = self.root / "ca.pem"
                ca.write_text(self.config.ca_pem)
                ca.chmod(0o600)
                settings["http.sslCAInfo"] = str(ca)
            self.env["GIT_CONFIG_COUNT"] = str(len(settings))
            for i, (key, value) in enumerate(settings.items()):
                self.env[f"GIT_CONFIG_KEY_{i}"] = key
                self.env[f"GIT_CONFIG_VALUE_{i}"] = value
            self.run(["init", "--bare", "."])
            return self
        except BaseException:
            if self.tunnel:
                self.tunnel.close()
            self.temp.cleanup()
            raise

    def __exit__(self, *args):
        if self.tunnel:
            self.tunnel.close()
        self.temp.cleanup()

    def run(self, args, limit=4 * 1024 * 1024):
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
            process = subprocess.Popen(
                [self.binary or "git", *args],
                cwd=self.root,
                env=self.env,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=errors,
                start_new_session=os.name == "posix",
            )
            try:
                while process.poll() is None:
                    if self.ctx:
                        self.ctx.check_cancelled()
                    size = 0
                    for file in self.root.rglob("*"):
                        try:
                            if file.is_file():
                                size += file.stat().st_size
                        except FileNotFoundError:
                            pass  # Git atomically renames temporary packs/indexes during fetch.
                    if (
                        time.monotonic() > self.deadline
                        or size > 128 * 1024 * 1024
                        or output.tell() + errors.tell() > limit
                    ):
                        raise InvalidStateError("Repository exceeded fetch time, size or output limits")
                    time.sleep(0.1)
                if process.returncode:
                    raise InvalidStateError(
                        "Git request failed; check URL, branch, authentication and TLS trust. Raw output withheld."
                    )
                output.seek(0)
                data = output.read(limit + 1)
                if len(data) > limit:
                    raise InvalidStateError("Repository output exceeded supported limits")
                return data
            finally:
                if process.poll() is None:
                    if os.name == "posix":
                        os.killpg(process.pid, signal.SIGKILL)
                    else:
                        process.kill()
                    process.wait()

    def fetch(self, *, allow_empty: bool = False):
        self.run(
            [
                "fetch",
                "--depth=1",
                "--no-tags",
                "--no-recurse-submodules",
                "--",
                self.config.url,
                "refs/heads/" + self.config.branch,
            ]
        )
        commit = self.run(["rev-parse", "--verify", "FETCH_HEAD^{commit}"]).decode().strip()
        if not re.fullmatch(r"[a-f0-9]{40,64}", commit):
            raise InvalidStateError("Unsupported Git commit identifier")
        listing = self.run(["ls-tree", "-rz", "--full-tree", commit, "--", self.config.path], 2 * 1024 * 1024)
        objects = []
        size = 0
        for entry in listing.split(b"\0"):
            if not entry:
                continue
            info, name = entry.split(b"\t", 1)
            mode, kind, sha = info.decode().split()
            file = name.decode("utf-8")
            if mode not in {"100644", "100755"} or kind != "blob":
                raise InvalidStateError("GitOps directories cannot contain symlinks or submodules")
            if not file.lower().endswith((".yaml", ".yml", ".json")):
                continue
            if PurePosixPath(file).name.lower() in {"kustomization.yaml", "kustomization.yml", "chart.yaml"}:
                raise InvalidStateError(
                    "This GitOps workflow requires rendered plain YAML, without Helm or Kustomize builds"
                )
            blob = self.run(["cat-file", "blob", sha], 1024 * 1024)
            size += len(blob)
            if size > 4 * 1024 * 1024:
                raise InvalidStateError("GitOps manifests exceed 4 MiB")
            try:
                # Shared bounded mapping validation prevents recursive YAML aliases/parser excerpts.
                text = blob.decode("utf-8")
                docs = list(yaml.safe_load_all(text))
                for obj in docs:
                    if obj is None:
                        continue
                    values(yaml.safe_dump(obj))
                    if not isinstance(obj, dict) or not all(obj.get(k) for k in ("apiVersion", "kind", "metadata")):
                        raise ValueError()
                    objects.append(obj)
                    if len(objects) > 100:
                        raise ValueError()
            except (ValueError, TypeError, yaml.YAMLError, RecursionError, UnicodeError):
                raise InvalidStateError(
                    "Repository contains unsupported YAML; use at most 100 bounded Kubernetes objects"
                ) from None
        if not objects:
            if not allow_empty:
                raise InvalidStateError("No Kubernetes YAML/JSON manifests found in the selected directory")
            tree = commit + "^{tree}" if self.config.path == "." else commit + ":" + self.config.path
            if self.run(["cat-file", "-t", tree]).strip() != b"tree":
                raise InvalidStateError("Empty applications require an existing manifest directory")
        return commit, objects
