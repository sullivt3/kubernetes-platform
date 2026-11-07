"""Amazon Web Services provider — foundations: credential validation and read-only
EC2 inventory. Creating and managing instances is planned for Phase 5.

Requests are signed with Signature Version 4 (``aws_sigv4``) and sent through the
shared provider HTTP client; no AWS SDK is required.

Required IAM permissions: ``sts:GetCallerIdentity`` (always allowed),
``ec2:DescribeInstances``, ``ec2:DescribeInstanceTypes`` and, when a role is
configured, ``sts:AssumeRole`` on it.

Status: PREVIEW — tested against AWS's published SigV4 test vectors and recorded
API responses, not yet verified against a live account.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime
from urllib.parse import quote, urlsplit
from xml.etree import ElementTree

from pydantic import BaseModel, Field

from app.modules.credentials.models import CredentialKind
from app.modules.providers import http
from app.modules.providers.base import InfrastructureProvider
from app.modules.providers.types import (
    ConnectionCheckResult,
    DiscoveredMachine,
    HardwareSpec,
    ImplementationStatus,
    MachineAddress,
    OSInfo,
    ProviderCapability,
    ProviderCategory,
    ProviderType,
)

EC2_VERSION = "2016-11-15"
STS_VERSION = "2011-06-15"
REGION_PATTERN = r"^[a-z]{2}(-gov|-iso[a-z]?)?-[a-z]+-\d$"


# ---- Signature Version 4 ------------------------------------------------------------


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def aws_sigv4(
    *,
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes,
    region: str,
    service: str,
    access_key: str,
    secret_key: str,
    session_token: str | None = None,
    now: datetime | None = None,
) -> dict[str, str]:
    """Return ``headers`` plus ``Authorization``/``X-Amz-Date`` (and the session token)."""
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    amz_date = moment.strftime("%Y%m%dT%H%M%SZ")
    date = moment.strftime("%Y%m%d")
    parts = urlsplit(url)
    signed = dict(headers)
    signed["Host"] = parts.netloc
    signed["X-Amz-Date"] = amz_date
    if session_token:
        signed["X-Amz-Security-Token"] = session_token

    canonical_headers = sorted((k.lower(), " ".join(v.strip().split())) for k, v in signed.items())
    signed_header_names = ";".join(k for k, _ in canonical_headers)
    query = sorted(
        (quote(k, safe="-_.~"), quote(v, safe="-_.~"))
        for k, _, v in (pair.partition("=") for pair in parts.query.split("&") if pair)
    )
    canonical_request = "\n".join(
        [
            method,
            quote(parts.path or "/", safe="/-_.~"),
            "&".join(f"{k}={v}" for k, v in query),
            "".join(f"{k}:{v}\n" for k, v in canonical_headers),
            signed_header_names,
            hashlib.sha256(body).hexdigest(),
        ]
    )
    scope = f"{date}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(
        ["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical_request.encode()).hexdigest()]
    )
    key = _hmac(_hmac(_hmac(_hmac(f"AWS4{secret_key}".encode(), date), region), service), "aws4_request")
    signature = hmac.new(key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    signed["Authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, SignedHeaders={signed_header_names}, Signature={signature}"
    )
    return signed


# ---- XML helpers (AWS query APIs answer in namespaced XML) ----------------------------


def _strip_ns(root: ElementTree.Element) -> ElementTree.Element:
    for element in root.iter():
        if "}" in element.tag:
            element.tag = element.tag.split("}", 1)[1]
    return root


def _xml(body: bytes) -> ElementTree.Element:
    return _strip_ns(ElementTree.fromstring(body))


def _text(element: ElementTree.Element | None, path: str, default: str = "") -> str:
    if element is None:
        return default
    found = element.find(path)
    return found.text or default if found is not None and found.text is not None else default


def _aws_error(response: http.HttpResponse) -> str:
    try:
        root = _xml(response.body)
    except ElementTree.ParseError:
        return response.text[:300]
    code = next((e.text for e in root.iter("Code")), "")
    message = next((e.text for e in root.iter("Message")), "")
    return f"{code}: {message}".strip(": ")


class AwsConfig(BaseModel):
    access_key_id: str = Field(
        pattern=r"^(AKIA|ASIA)[A-Z0-9]{12,124}$",
        description="Access key ID (the secret access key goes in the credential)",
    )
    region: str = Field(pattern=REGION_PATTERN, description="Home region, e.g. us-east-1")
    discovery_regions: list[str] = Field(
        default_factory=list, description="Additional regions to inventory (default: the home region only)"
    )
    assume_role_arn: str | None = Field(
        default=None,
        pattern=r"^arn:aws[a-z-]*:iam::\d{12}:role/[\w+=,.@/-]+$",
        description="Optional role to assume for all calls",
    )


class AwsProvider(InfrastructureProvider):
    provider_type = ProviderType.AWS
    display_name = "Amazon Web Services"
    description = "EC2 inventory and account validation today; instance provisioning and EKS are planned."
    category = ProviderCategory.CLOUD
    capabilities = frozenset({ProviderCapability.VALIDATE_CONNECTION, ProviderCapability.DISCOVER})
    planned_capabilities = frozenset(
        {
            ProviderCapability.PROVISION,
            ProviderCapability.DELETE,
            ProviderCapability.STATUS,
            ProviderCapability.POWER,
            ProviderCapability.NETWORK,
        }
    )
    planned_capabilities_phase = 5
    implementation_status = ImplementationStatus.PREVIEW
    config_model = AwsConfig
    credential_kinds = frozenset({CredentialKind.AWS_ACCESS_KEY})

    def __init__(self, config: BaseModel, credential: bytes | None) -> None:
        super().__init__(config, credential)
        self._assumed: tuple[str, str, str] | None = None

    @property
    def cfg(self) -> AwsConfig:
        return self.config  # type: ignore[return-value]

    def _keys(self) -> tuple[str, str, str | None]:
        if self.cfg.assume_role_arn:
            if self._assumed is None:
                root = self._query(
                    "sts",
                    self.cfg.region,
                    {
                        "Action": "AssumeRole",
                        "Version": STS_VERSION,
                        "RoleArn": self.cfg.assume_role_arn,
                        "RoleSessionName": "kubernetes-platform",
                        "DurationSeconds": "3600",
                    },
                    keys=(self.cfg.access_key_id, (self._credential or b"").decode().strip(), None),
                    what="Assume role",
                )
                creds = root.find(".//Credentials")
                self._assumed = (
                    _text(creds, "AccessKeyId"),
                    _text(creds, "SecretAccessKey"),
                    _text(creds, "SessionToken"),
                )
            return self._assumed
        return self.cfg.access_key_id, (self._credential or b"").decode().strip(), None

    def _query(
        self,
        service: str,
        region: str,
        form: dict[str, str],
        *,
        what: str,
        keys: tuple[str, str, str | None] | None = None,
    ) -> ElementTree.Element:
        access, secret, token = keys or self._keys()
        url = f"https://{service}.{region}.amazonaws.com/"
        body = "&".join(f"{quote(k, safe='-_.~')}={quote(v, safe='-_.~')}" for k, v in form.items()).encode()
        headers = aws_sigv4(
            method="POST",
            url=url,
            headers={"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"},
            body=body,
            region=region,
            service=service,
            access_key=access,
            secret_key=secret,
            session_token=token,
        )
        headers["Accept"] = "application/xml"
        response = http.send("POST", url, headers=headers, body=body)
        http.expect_ok(response, f"AWS {what}", message_of=_aws_error)
        return _xml(response.body)

    def validate_connection(self) -> ConnectionCheckResult:
        root = self._query(
            "sts", self.cfg.region, {"Action": "GetCallerIdentity", "Version": STS_VERSION}, what="Identity"
        )
        details = {
            "account": _text(root, ".//Account"),
            "arn": _text(root, ".//Arn"),
        }
        try:
            self._query(
                "ec2",
                self.cfg.region,
                {"Action": "DescribeInstances", "Version": EC2_VERSION, "MaxResults": "5"},
                what="DescribeInstances",
            )
            details["ec2_read"] = "allowed"
        except http.ProviderAuthError as exc:
            return ConnectionCheckResult(
                ok=False,
                message=f"Authenticated as {details['arn']}, but EC2 read access is denied: {exc.message}",
                details=details,
            )
        return ConnectionCheckResult(
            ok=True, message=f"Account {details['account']} ({details['arn']})", details=details
        )

    def _instance_types(self, region: str, names: set[str]) -> dict[str, tuple[int, int]]:
        sizes: dict[str, tuple[int, int]] = {}
        ordered = sorted(names)
        for start in range(0, len(ordered), 100):
            form = {"Action": "DescribeInstanceTypes", "Version": EC2_VERSION}
            for index, name in enumerate(ordered[start : start + 100], 1):
                form[f"InstanceType.{index}"] = name
            root = self._query("ec2", region, form, what="DescribeInstanceTypes")
            for item in root.iterfind(".//instanceTypeSet/item"):
                sizes[_text(item, "instanceType")] = (
                    int(_text(item, "vCpuInfo/defaultVCpus", "0")),
                    int(_text(item, "memoryInfo/sizeInMiB", "0")),
                )
        return sizes

    def discover(self) -> list[DiscoveredMachine]:
        machines: list[DiscoveredMachine] = []
        for region in dict.fromkeys([self.cfg.region, *self.cfg.discovery_regions]):
            instances: list[ElementTree.Element] = []
            token: str | None = None
            while True:
                form = {"Action": "DescribeInstances", "Version": EC2_VERSION, "MaxResults": "1000"}
                if token:
                    form["NextToken"] = token
                root = self._query("ec2", region, form, what=f"DescribeInstances ({region})")
                instances.extend(root.iterfind(".//reservationSet/item/instancesSet/item"))
                token = _text(root, "nextToken") or None
                if not token:
                    break
            sizes = self._instance_types(region, {_text(i, "instanceType") for i in instances}) if instances else {}
            for item in instances:
                tags = {_text(t, "key"): _text(t, "value") for t in item.iterfind("tagSet/item")}
                instance_type = _text(item, "instanceType")
                cores, memory = sizes.get(instance_type, (0, 0))
                addresses = [MachineAddress(type="internal", address=_text(item, "privateIpAddress"))]
                if _text(item, "ipAddress"):
                    addresses.append(MachineAddress(type="external", address=_text(item, "ipAddress")))
                state = _text(item, "instanceState/name")
                machines.append(
                    DiscoveredMachine(
                        external_id=f"{region}/{_text(item, 'instanceId')}",
                        name=tags.get("Name") or _text(item, "instanceId"),
                        addresses=[a for a in addresses if a.address],
                        power_state={"running": "running", "stopped": "stopped"}.get(state, state or "unknown"),
                        hardware=HardwareSpec(
                            cpu_cores=cores or None,
                            memory_mb=memory or None,
                            architecture=_text(item, "architecture") or None,
                        ),
                        os=OSInfo(distribution=_text(item, "platformDetails") or None),
                        provider_metadata={
                            "region": region,
                            "zone": _text(item, "placement/availabilityZone"),
                            "instance_type": instance_type,
                            "vpc": _text(item, "vpcId"),
                            "image": _text(item, "imageId"),
                        },
                        importable=False,
                    )
                )
        return machines
