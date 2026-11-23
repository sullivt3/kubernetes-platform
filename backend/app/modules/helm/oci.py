"""Read-only OCI Distribution chart discovery and immutable Helm artifact retrieval."""

from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import parse_http_list, parse_keqv_list

from app.modules.helm import transport
from app.modules.helm.transport import CatalogError

MANIFEST = "application/vnd.oci.image.manifest.v1+json"
CONFIG = "application/vnd.cncf.helm.config.v1+json"
CONTENT = "application/vnd.cncf.helm.chart.content.v1.tar+gzip"
SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-([0-9A-Za-z.-]+))?(?:\+([0-9A-Za-z.-]+))?$")
DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")


class OCIClient:
    def __init__(self, repository, authorization: str | None = None):
        self.repository = repository
        parsed = urlsplit(repository.url)
        self.base = "https://" + parsed.netloc
        self.path = parsed.path.lstrip("/")
        self.name = self.path.rsplit("/", 1)[-1]
        self.authorization = authorization
        self.token: str | None = None

    def get(self, path: str, limit: int, accept: str | None = None):
        url = self.base + path
        response = transport.request(
            url,
            limit,
            policy=self.repository,
            authorization=self.token or self.authorization,
            headers={"Accept": accept} if accept else None,
        )
        if response.status == 401:
            challenge = response.headers.get("www-authenticate", "")
            if not challenge.lower().startswith("bearer "):
                raise CatalogError("OCI registry rejected authentication")
            try:
                fields = parse_keqv_list(parse_http_list(challenge[7:]))
                realm = fields["realm"]
                transport.check_url(realm, private=self.repository.allow_private_network)
                if urlsplit(realm).query:
                    raise ValueError()
                same = transport.origin(realm) == transport.origin(self.base)
                trusted = self.repository.token_service_url == realm
                if self.authorization and not (same or trusted):
                    raise CatalogError(
                        "Configure the exact trusted token service URL before sending credentials to another origin"
                    )
                service = fields.get("service", "")
                if len(service) > 256 or not re.fullmatch(r"[a-zA-Z0-9_.:-]*", service):
                    raise ValueError()
                token_url = realm + "?" + urlencode({"service": service, "scope": f"repository:{self.path}:pull"})
                policy = self.repository.model_copy(update={"url": realm}) if same or trusted else None
                reply = transport.request(
                    token_url,
                    1024 * 1024,
                    policy=policy,
                    authorization=self.authorization if same or trusted else None,
                    credential_origin=transport.origin(realm),
                    redirects=False,
                )
                if reply.status != 200:
                    raise ValueError()
                body = json.loads(reply.data)
                token = body.get("token") or body.get("access_token")
                if not isinstance(token, str) or not token or len(token) > 16384 or any(c in token for c in "\r\n"):
                    raise ValueError()
                self.token = "Bearer " + token
            except (ValueError, KeyError, TypeError, AttributeError):
                raise CatalogError(
                    "OCI token authentication failed; check credentials and registry permissions"
                ) from None
            response = transport.request(
                url,
                limit,
                policy=self.repository,
                authorization=self.token,
                headers={"Accept": accept} if accept else None,
            )
        if response.status != 200:
            raise CatalogError("OCI registry rejected the requested chart, version or credentials")
        return response

    def index(self) -> dict:
        url = f"/v2/{self.path}/tags/list?n=100"
        tags: list[str] = []
        seen = set()
        for _ in range(10):
            response = self.get(url, 1024 * 1024)
            try:
                obj = json.loads(response.data)
                values = obj.get("tags") or []
                if not isinstance(values, list) or any(not isinstance(t, str) for t in values):
                    raise ValueError()
                tags.extend(values[:1000])
                link = response.headers.get("link", "")
                if not link:
                    break
                match = re.fullmatch(r'<([^>]+)>;\s*rel="?next"?', link)
                if not match:
                    raise ValueError()
                next_url = urljoin(self.base + url, match[1])
                parsed = urlsplit(next_url)
                if (
                    transport.origin(next_url) != transport.origin(self.base)
                    or parsed.path != f"/v2/{self.path}/tags/list"
                    or next_url in seen
                ):
                    raise ValueError()
                seen.add(next_url)
                url = parsed.path + ("?" + parsed.query if parsed.query else "")
            except (ValueError, TypeError, AttributeError):
                raise CatalogError("OCI registry returned unsupported tag pagination") from None
        versions = []
        for tag in set(tags):
            version = tag.replace("_", "+")
            if SEMVER.fullmatch(version):
                versions.append(version)

        def order(version):
            m = SEMVER.fullmatch(version)
            assert m is not None
            pre = tuple((0, int(p)) if p.isdigit() else (1, p) for p in (m[4] or "").split("."))
            return int(m[1]), int(m[2]), int(m[3]), not bool(m[4]), pre

        versions.sort(key=order, reverse=True)
        return {self.name: [{"version": v, "description": f"OCI chart {self.name}"} for v in versions[:100]]}

    def manifest(self, reference: str) -> tuple[dict, str]:
        response = self.get(f"/v2/{self.path}/manifests/{reference}", 1024 * 1024, MANIFEST)
        actual = "sha256:" + hashlib.sha256(response.data).hexdigest()
        if (reference.startswith("sha256:") and actual != reference) or response.headers.get(
            "docker-content-digest", actual
        ) != actual:
            raise CatalogError("OCI manifest digest did not match the reviewed artifact")
        try:
            obj = json.loads(response.data)
            if (
                obj.get("schemaVersion") != 2
                or obj.get("mediaType", MANIFEST) != MANIFEST
                or obj["config"]["mediaType"] != CONFIG
            ):
                raise ValueError()
            content = [layer for layer in obj["layers"] if layer["mediaType"] == CONTENT]
            if len(content) != 1 or not DIGEST.fullmatch(content[0]["digest"]) or content[0]["size"] > 16 * 1024 * 1024:
                raise ValueError()
            return {"layer": content[0], "config": obj["config"]}, actual
        except (ValueError, KeyError, TypeError, AttributeError):
            raise CatalogError("OCI artifact is not a supported Helm chart manifest") from None

    def blob(self, descriptor: dict, limit: int) -> bytes:
        digest = descriptor.get("digest", "")
        if (
            not DIGEST.fullmatch(digest)
            or not isinstance(descriptor.get("size"), int)
            or not 0 <= descriptor["size"] <= limit
        ):
            raise CatalogError("OCI blob descriptor exceeds supported limits")
        data = self.get(f"/v2/{self.path}/blobs/{digest}", limit).data
        if len(data) != descriptor["size"] or "sha256:" + hashlib.sha256(data).hexdigest() != digest:
            raise CatalogError("OCI blob digest or size verification failed")
        return data

    def chart(self, name: str, version: str) -> dict:
        if name != self.name or not SEMVER.fullmatch(version):
            raise CatalogError("Select the chart named in the OCI URL and an explicit semantic version")
        manifest, digest = self.manifest(version.replace("+", "_"))
        try:
            config = json.loads(self.blob(manifest["config"], 1024 * 1024))
            if config["name"] != name or config["version"] != version:
                raise ValueError()
        except (ValueError, TypeError, KeyError):
            raise CatalogError("OCI chart metadata does not match its name and version") from None
        return {
            "kind": "oci",
            "chart": name,
            "version": version,
            "url": self.repository.url,
            "digest": manifest["layer"]["digest"][7:],
            "manifest_digest": digest,
            "repository": self.repository.model_dump(mode="json"),
        }

    def archive(self, spec: dict) -> bytes:
        if spec["url"] != self.repository.url or not DIGEST.fullmatch(spec.get("manifest_digest", "")):
            raise CatalogError("Invalid reviewed OCI source")
        manifest, _ = self.manifest(spec["manifest_digest"])
        if manifest["layer"]["digest"] != "sha256:" + spec["digest"]:
            raise CatalogError("OCI chart content changed after review")
        return self.blob(manifest["layer"], 16 * 1024 * 1024)
