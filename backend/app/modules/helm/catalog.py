"""Bounded public HTTPS chart catalog downloads, with pinned DNS and archive digests."""

from __future__ import annotations

import hashlib
import io
import re
import ssl
import tarfile
import uuid
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urljoin, urlsplit

import yaml
from pydantic import BaseModel, Field, model_validator

from app.core.errors import ValidationFailedError
from app.modules.helm import transport
from app.modules.helm.transport import CatalogError as CatalogError
from app.modules.helm.transport import check_url as check_url

NAME = r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$"
VERSION = r"^[a-zA-Z0-9][a-zA-Z0-9.+_-]{0,127}$"


class Repository(BaseModel):
    name: str = Field(pattern=NAME, max_length=63)
    url: str = Field(max_length=2048)
    auth_id: uuid.UUID | None = None
    allow_private_network: bool = False
    ca_pem: str | None = Field(default=None, max_length=65536)
    token_service_url: str | None = Field(default=None, max_length=2048)

    @model_validator(mode="after")
    def validate_connection(self) -> Repository:
        self.url = check_url(self.url, repository=True, private=self.allow_private_network)
        if self.url.startswith("oci://") and not re.fullmatch(NAME, urlsplit(self.url).path.rsplit("/", 1)[-1]):
            raise ValueError("The OCI URL must include a valid chart name, without a version or digest")
        if self.ca_pem:
            try:
                if "PRIVATE KEY" in self.ca_pem:
                    raise ValueError()
                ssl.create_default_context().load_verify_locations(cadata=self.ca_pem)
            except (ValueError, ssl.SSLError):
                raise ValueError("Provide a PEM CA certificate bundle without private keys") from None
        if self.token_service_url:
            check_url(self.token_service_url, private=self.allow_private_network)
            if urlsplit(self.token_service_url).query:
                raise ValueError("The trusted token service URL cannot include query parameters")
        return self


def download(url: str, limit: int, repository: Repository | None = None, authorization: str | None = None) -> bytes:
    response = transport.request(url, limit, policy=repository, authorization=authorization)
    if response.status != 200:
        raise CatalogError("Repository rejected the request; check the saved credentials and source permissions")
    return response.data


def index(repository: Repository, authorization: str | None = None) -> dict[str, list[dict]]:
    if repository.url.startswith("oci://"):
        from app.modules.helm.oci import OCIClient

        return OCIClient(repository, authorization).index()
    try:
        data = download(repository.url + "/index.yaml", 8 * 1024 * 1024, repository, authorization)
        obj = yaml.safe_load(data)
        entries = obj["entries"]
        if not isinstance(entries, dict) or len(entries) > 10000:
            raise ValueError()
        return {
            name: [
                v for v in versions[:100] if isinstance(v, dict) and re.fullmatch(VERSION, str(v.get("version", "")))
            ]
            for name, versions in entries.items()
            if isinstance(name, str) and re.fullmatch(NAME, name) and isinstance(versions, list)
        }
    except (yaml.YAMLError, ValueError, TypeError, KeyError, RecursionError):
        raise CatalogError("The repository did not return a supported Helm index") from None


def chart(repository: Repository, name: str, version: str, authorization: str | None = None) -> dict:
    if repository.url.startswith("oci://"):
        from app.modules.helm.oci import OCIClient

        return OCIClient(repository, authorization).chart(name, version)
    entries = index(repository, authorization) if authorization else index(repository)
    entry = next((v for v in entries.get(name, []) if v["version"] == version), None)
    if not entry or not re.fullmatch(r"[a-fA-F0-9]{64}", str(entry.get("digest", ""))):
        raise ValidationFailedError("Select an indexed chart version with a SHA256 digest")
    urls = entry.get("urls")
    if not isinstance(urls, list) or not urls or not isinstance(urls[0], str):
        raise CatalogError("Chart archive URL is missing")
    try:
        url = check_url(urljoin(repository.url + "/", urls[0]), private=repository.allow_private_network)
    except ValueError:
        raise CatalogError("Chart archive requires a public HTTPS URL") from None
    return {
        "chart": name,
        "version": version,
        "url": url,
        "digest": entry["digest"].lower(),
        "repository": repository.model_dump(mode="json"),
    }


def archive(spec: dict, authorization: str | None = None) -> bytes:
    repository = Repository.model_validate(spec["repository"]) if spec.get("repository") else None
    if spec.get("kind") == "oci":
        from app.modules.helm.oci import OCIClient

        assert repository is not None
        data = OCIClient(repository, authorization).archive(spec)
    elif repository:
        data = download(spec["url"], 16 * 1024 * 1024, repository, authorization)
    else:
        data = download(spec["url"], 16 * 1024 * 1024)
    if hashlib.sha256(data).hexdigest() != spec["digest"]:
        raise CatalogError("Chart digest changed; review the selected version again")
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as bundle:
            size = 0
            for count, member in enumerate(bundle):
                path = PurePosixPath(member.name)
                size += member.size
                if (
                    count > 10000
                    or size > 64 * 1024 * 1024
                    or path.is_absolute()
                    or ".." in path.parts
                    or "\\" in member.name
                    or not (member.isfile() or member.isdir())
                ):
                    raise ValueError()
    except (tarfile.TarError, ValueError, OSError):
        raise CatalogError("Chart archive is unsafe or exceeds the supported limits") from None
    return data


def values(text: str) -> None:
    """Validate without including parser excerpts (which may contain secret values)."""
    if len(text.encode()) > 128 * 1024:
        raise ValidationFailedError("Values YAML exceeds 128 KiB")
    try:
        obj = yaml.safe_load(text) if text.strip() else {}
        if not isinstance(obj, dict):
            raise ValueError()
        visited = 0

        def visit(value: Any, depth: int, parents: set[int]) -> None:
            nonlocal visited
            visited += 1
            if depth > 32 or visited > 10000 or id(value) in parents:
                raise ValueError()
            if isinstance(value, (dict, list)):
                if isinstance(value, dict) and any(not isinstance(k, str) for k in value):
                    raise ValueError()
                for child in value.values() if isinstance(value, dict) else value:
                    visit(child, depth + 1, parents | {id(value)})
            elif value is not None and not isinstance(value, (str, int, float, bool)):
                raise ValueError()

        visit(obj, 0, set())
    except (yaml.YAMLError, ValueError, RecursionError):
        raise ValidationFailedError(
            "Values must be a bounded YAML mapping with string keys and no cyclic aliases"
        ) from None
