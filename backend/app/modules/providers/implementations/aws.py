"""Amazon Web Services provider: EC2, EKS and Network Load Balancer operations.

Requests are signed with Signature Version 4 (``aws_sigv4``) and sent through the
shared provider HTTP client; no AWS SDK is required.

Provisioning requires EC2 run/describe/terminate permissions in addition to read
access, and explicit AMI, subnet and security groups on each request.

Status: PREVIEW — tested against AWS's published SigV4 test vectors and recorded
API responses, not yet verified against a live account.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import quote, urlencode, urlsplit
from xml.etree import ElementTree

from pydantic import BaseModel, Field, TypeAdapter, ValidationError, model_validator

from app.modules.credentials.models import CredentialKind
from app.modules.providers import http
from app.modules.providers.base import InfrastructureProvider
from app.modules.providers.implementations.cloud_actions import CloudMachineActions
from app.modules.providers.implementations.managed_actions import ManagedClusterActions
from app.modules.providers.types import (
    ConnectionCheckResult,
    DiscoveredMachine,
    HardwareSpec,
    ImplementationStatus,
    LoadBalancerRequest,
    LoadBalancerResult,
    MachineAddress,
    MachineRef,
    MachineSpecRequest,
    MachineStatus,
    ManagedClusterRequest,
    ManagedClusterResult,
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


EKS_TAINT_EFFECTS = {
    "NoSchedule": "NO_SCHEDULE",
    "PreferNoSchedule": "PREFER_NO_SCHEDULE",
    "NoExecute": "NO_EXECUTE",
}


class _EksTaint(BaseModel):
    key: str = Field(min_length=1)
    value: str | None = Field(default=None, max_length=63)
    effect: Literal["NoSchedule", "PreferNoSchedule", "NoExecute"]


class _EksNodePool(BaseModel):
    name: str = Field(min_length=1, max_length=63)
    count: int = Field(ge=0)
    minimum: int = Field(default=1, ge=0)
    maximum: int | None = Field(default=None, ge=1)
    labels: dict[str, str] = Field(default_factory=dict)
    taints: list[_EksTaint] = Field(default_factory=list, max_length=50)
    disk_gb: int | None = Field(default=None, ge=20, le=2000)
    machine_type: str | None = Field(default=None, min_length=1)

    @property
    def maximum_size(self) -> int:
        return self.maximum if self.maximum is not None else max(3, self.count)

    @model_validator(mode="after")
    def _bounds(self) -> _EksNodePool:
        if self.count > 0 and not self.minimum <= self.count <= self.maximum_size:
            raise ValueError("Node count must be between its minimum and maximum")
        return self


class _EksComputeOptions(BaseModel):
    managed_node_instance_types: list[str] = Field(default_factory=lambda: ["t3.medium"], min_length=1)
    managed_node_disk_size_gb: int = Field(default=20, ge=20, le=2000)


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


class AwsProvider(CloudMachineActions, ManagedClusterActions, InfrastructureProvider):
    provider_type = ProviderType.AWS
    display_name = "Amazon Web Services"
    description = "EC2 and EKS provisioning, inventory, lifecycle management, and Network Load Balancers."
    category = ProviderCategory.CLOUD
    capabilities = frozenset(
        {
            ProviderCapability.VALIDATE_CONNECTION,
            ProviderCapability.DISCOVER,
            ProviderCapability.PROVISION,
            ProviderCapability.DELETE,
            ProviderCapability.STATUS,
            ProviderCapability.POWER,
            ProviderCapability.NETWORK,
            ProviderCapability.LOAD_BALANCER,
            ProviderCapability.MANAGED_KUBERNETES,
        }
    )
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

    @staticmethod
    def _shell_quote(value: str) -> str:
        return "'" + value.replace("'", "'\\''") + "'"

    def _instance(self, region: str, instance_id: str) -> ElementTree.Element | None:
        root = self._query(
            "ec2",
            region,
            {
                "Action": "DescribeInstances",
                "Version": EC2_VERSION,
                "InstanceId.1": instance_id,
            },
            what="DescribeInstances",
        )
        return next(root.iterfind(".//reservationSet/item/instancesSet/item"), None)

    def _describe_machine(self, region: str, instance_id: str) -> DiscoveredMachine | None:
        item = self._instance(region, instance_id)
        if item is None:
            return None
        state = _text(item, "instanceState/name", "unknown")
        addresses = [MachineAddress(type="internal", address=_text(item, "privateIpAddress"))]
        public_ip = _text(item, "ipAddress")
        if public_ip:
            addresses.append(MachineAddress(type="external", address=public_ip))
        tags = {_text(t, "key"): _text(t, "value") for t in item.iterfind("tagSet/item")}
        return DiscoveredMachine(
            external_id=f"{region}/{instance_id}",
            name=tags.get("Name", instance_id),
            addresses=[a for a in addresses if a.address],
            power_state={"running": "running", "stopped": "stopped"}.get(state, state),
            provider_metadata={
                "region": region,
                "instance_type": _text(item, "instanceType"),
                "image": _text(item, "imageId"),
            },
            importable=False,
        )

    def provision(self, request: MachineSpecRequest) -> DiscoveredMachine:
        options = request.provider_options
        image = request.image or options.get("image_id")
        instance_type = options.get("instance_type")
        subnet_id = options.get("subnet_id") or request.network
        security_groups = options.get("security_group_ids")
        if not image or not str(image).startswith("ami-"):
            raise http.ProviderApiError("AWS provisioning requires an AMI ID in image")
        if not instance_type or not subnet_id or not isinstance(security_groups, list) or not security_groups:
            raise http.ProviderApiError("AWS provisioning requires instance_type, subnet_id and security_group_ids")
        if not request.ssh_public_key:
            raise http.ProviderApiError("AWS provisioning requires an SSH public key")
        region = str(options.get("region") or self.cfg.region)
        user_data = "#!/bin/bash\nset -eu\n"
        username = self._shell_quote(request.ssh_username)
        home = f"/home/{request.ssh_username}"
        user_data += f"id -u {username} >/dev/null 2>&1 || useradd --create-home --shell /bin/bash {username}\n"
        user_data += f"install -d -m 700 -o {username} -g {username} {home}/.ssh\n"
        user_data += f"printf '%s\\n' {self._shell_quote(request.ssh_public_key)} > {home}/.ssh/authorized_keys\n"
        user_data += f"chmod 600 {home}/.ssh/authorized_keys\n"
        user_data += f"chown {username}:{username} {home}/.ssh/authorized_keys\n"
        sudo_rule = self._shell_quote(request.ssh_username + " ALL=(ALL) NOPASSWD:ALL")
        user_data += f"printf '%s\\n' {sudo_rule} >/etc/sudoers.d/90-platform\nchmod 440 /etc/sudoers.d/90-platform\n"
        form = {
            "Action": "RunInstances",
            "Version": EC2_VERSION,
            "ImageId": str(image),
            "InstanceType": str(instance_type),
            "MinCount": "1",
            "MaxCount": "1",
            "UserData": base64.b64encode(user_data.encode()).decode(),
            "ClientToken": hashlib.sha256(request.name.encode()).hexdigest()[:64],
            "BlockDeviceMapping.1.DeviceName": str(options.get("device_name", "/dev/sda1")),
            "BlockDeviceMapping.1.Ebs.VolumeSize": str(request.disk_gb),
            "BlockDeviceMapping.1.Ebs.VolumeType": str(options.get("volume_type", "gp3")),
            "BlockDeviceMapping.1.Ebs.DeleteOnTermination": "true",
            "TagSpecification.1.ResourceType": "instance",
            "TagSpecification.1.Tag.1.Key": "Name",
            "TagSpecification.1.Tag.1.Value": request.name,
            "TagSpecification.1.Tag.2.Key": "kubernetes-platform:managed",
            "TagSpecification.1.Tag.2.Value": "true",
        }
        form["NetworkInterface.1.SubnetId"] = str(subnet_id)
        for index, group in enumerate(security_groups, 1):
            form[f"NetworkInterface.1.SecurityGroupId.{index}"] = str(group)
        if options.get("key_name"):
            form["KeyName"] = str(options["key_name"])
        form["NetworkInterface.1.DeviceIndex"] = "0"
        if options.get("associate_public_ip", True):
            form["NetworkInterface.1.AssociatePublicIpAddress"] = "true"
        root = self._query("ec2", region, form, what="RunInstances")
        instance_id = _text(root, ".//instancesSet/item/instanceId")
        if not instance_id:
            raise http.ProviderApiError("AWS RunInstances returned no instance ID")
        found = self._describe_machine(region, instance_id)
        if found is None:
            return DiscoveredMachine(
                external_id=f"{region}/{instance_id}",
                name=request.name,
                power_state="pending",
                provider_metadata={"region": region},
            )
        return found

    def get_status(self, machine: MachineRef) -> MachineStatus:
        region, _, instance_id = machine.external_id.partition("/")
        if not region or not instance_id:
            raise http.ProviderApiError("AWS machine ID must contain region/instance-id")
        found = self._describe_machine(region, instance_id)
        return MachineStatus(
            external_id=machine.external_id,
            exists=found is not None,
            power_state=found.power_state if found else "deleted",
            addresses=found.addresses if found else [],
        )

    def delete(self, machine: MachineRef) -> None:
        region, _, instance_id = machine.external_id.partition("/")
        if not region or not instance_id:
            raise http.ProviderApiError("AWS machine ID must contain region/instance-id")
        current = self._instance(region, instance_id)
        if current is None or _text(current, "instanceState/name") in {"shutting-down", "terminated"}:
            return
        tags = {_text(tag, "key"): _text(tag, "value") for tag in current.iterfind("tagSet/item")}
        if tags.get("kubernetes-platform:managed") != "true":
            raise http.ProviderApiError("Refusing to terminate an EC2 instance not tagged as platform-managed")
        self._query(
            "ec2",
            region,
            {"Action": "TerminateInstances", "Version": EC2_VERSION, "InstanceId.1": instance_id},
            what="TerminateInstances",
        )

    def _elb(self, form: dict[str, str], what: str) -> ElementTree.Element:
        form = {"Version": "2015-12-01", **form}
        return self._query("elasticloadbalancing", self.cfg.region, form, what=what)

    @staticmethod
    def _eks_error(response: http.HttpResponse) -> str:
        try:
            body = response.json() or {}
        except ValueError:
            return response.text[:300]
        error = body.get("message") or body.get("Message") or response.text[:300]
        kind = response.headers.get("x-amzn-errortype", "")
        return f"{kind}: {error}".strip(": ")

    def _eks(self, action: str, payload: dict[str, object], *, what: str) -> dict[str, Any]:
        params = dict(payload)
        name = quote(str(params.pop("clusterName", params.pop("name", ""))), safe="")
        group = quote(str(params.pop("nodegroupName", "")), safe="")
        update = quote(str(params.pop("updateId", "")), safe="")
        paths = {
            "CreateCluster": ("POST", "/clusters"),
            "DescribeCluster": ("GET", f"/clusters/{name}"),
            "DeleteCluster": ("DELETE", f"/clusters/{name}"),
            "CreateNodegroup": ("POST", f"/clusters/{name}/node-groups"),
            "DescribeNodegroup": ("GET", f"/clusters/{name}/node-groups/{group}"),
            "DeleteNodegroup": ("DELETE", f"/clusters/{name}/node-groups/{group}"),
            "ListNodegroups": ("GET", f"/clusters/{name}/node-groups"),
            "UpdateClusterVersion": ("POST", f"/clusters/{name}/updates"),
            "UpdateNodegroupConfig": ("POST", f"/clusters/{name}/node-groups/{group}/update-config"),
            "UpdateNodegroupVersion": ("POST", f"/clusters/{name}/node-groups/{group}/update-version"),
            "DescribeUpdate": (
                "GET",
                f"/clusters/{name}/updates/{update}" + (f"?nodegroupName={group}" if group else ""),
            ),
        }
        if action not in paths:
            raise http.ProviderApiError(f"Unsupported EKS action: {action}")
        method, path = paths[action]
        if action == "CreateCluster":
            params["name"] = str(payload["name"])
        if action == "CreateNodegroup":
            params["nodegroupName"] = str(payload["nodegroupName"])
        url = f"https://eks.{self.cfg.region}.amazonaws.com{path}"
        if method == "GET" and params:
            url += ("&" if "?" in url else "?") + urlencode(params)
        body = json.dumps(params, separators=(",", ":")).encode() if method == "POST" else b""
        access, secret, token = self._keys()
        headers = aws_sigv4(
            method=method,
            url=url,
            headers={"Content-Type": "application/json"},
            body=body,
            region=self.cfg.region,
            service="eks",
            access_key=access,
            secret_key=secret,
            session_token=token,
        )
        response = http.send(method, url, headers=headers, body=body)
        http.expect_ok(response, f"AWS {what}", message_of=self._eks_error)
        return response.json() if response.body else {}

    def _eks_nodegroups(self, name: str) -> list[str]:
        result: list[str] = []
        token: str | None = None
        for _ in range(50):
            params: dict[str, object] = {"clusterName": name, "maxResults": 100}
            if token:
                params["nextToken"] = token
            page = self._eks("ListNodegroups", params, what="ListNodegroups")
            result.extend(page.get("nodegroups") or [])
            token = page.get("nextToken")
            if not token:
                return result
        raise http.ProviderApiError("EKS node group pagination exceeded the safety limit")

    def _eks_cluster(self, name: str) -> dict[str, Any] | None:
        try:
            return self._eks("DescribeCluster", {"name": name}, what="DescribeCluster").get("cluster")
        except http.ProviderApiError as exc:
            if "ResourceNotFoundException" in exc.message:
                return None
            raise

    def managed_cluster_id(self, name: str, options: dict[str, object]) -> str:
        return f"{self.cfg.region}|{name}"

    def _wait_eks_cluster(self, name: str, statuses: set[str], *, gone: bool = False) -> dict[str, Any] | None:
        deadline = time.monotonic() + 3600
        while time.monotonic() < deadline:
            cluster = self._eks_cluster(name)
            if gone and cluster is None:
                return None
            if cluster and cluster.get("status") in statuses:
                return cluster
            if cluster and cluster.get("status") in {"FAILED", "DELETING"} and not gone:
                raise http.ProviderApiError(f"EKS cluster entered {cluster.get('status')} state")
            time.sleep(15)
        raise http.ProviderApiError(f"Timed out waiting for EKS cluster {name}")

    def create_managed_cluster(self, request: ManagedClusterRequest) -> ManagedClusterResult:
        options = request.options
        role_arn = options.get("managed_cluster_role_arn")
        node_role_arn = options.get("managed_node_role_arn")
        subnet_ids = options.get("managed_subnet_ids")
        if not isinstance(role_arn, str) or not role_arn.startswith("arn:aws:iam::"):
            raise http.ProviderApiError("EKS requires options.managed_cluster_role_arn")
        if not isinstance(node_role_arn, str) or not node_role_arn.startswith("arn:aws:iam::"):
            raise http.ProviderApiError("EKS node groups require options.managed_node_role_arn")
        if (
            not isinstance(subnet_ids, list)
            or len(subnet_ids) < 2
            or any(not isinstance(item, str) for item in subnet_ids)
        ):
            raise http.ProviderApiError("EKS needs at least two subnet IDs in different Availability Zones")
        try:
            pools = TypeAdapter(list[_EksNodePool]).validate_python(request.node_pools)
            compute = _EksComputeOptions.model_validate(options)
        except ValidationError as exc:
            raise http.ProviderApiError(
                "Invalid EKS node group or compute settings",
                details={"errors": exc.errors(include_url=False, include_context=False, include_input=False)},
            ) from exc
        cluster = self._eks_cluster(request.name)
        if cluster is None:
            payload: dict[str, object] = {
                "name": request.name,
                "version": ".".join(request.version.removeprefix("v").split(".")[:2]),
                "roleArn": role_arn,
                "kubernetesNetworkConfig": {
                    "ipFamily": "ipv4",
                    "serviceIpv4Cidr": str(options.get("managed_service_cidr") or "10.96.0.0/12"),
                },
                "resourcesVpcConfig": {
                    "subnetIds": subnet_ids,
                    "endpointPublicAccess": bool(options.get("managed_endpoint_public_access", True)),
                    "endpointPrivateAccess": bool(options.get("managed_endpoint_private_access", False)),
                },
                "accessConfig": {
                    "authenticationMode": "API",
                    "bootstrapClusterCreatorAdminPermissions": True,
                },
                "tags": {"kubernetes-platform:managed": "true"},
            }
            security_groups = options.get("managed_security_group_ids")
            if security_groups:
                payload["resourcesVpcConfig"]["securityGroupIds"] = security_groups  # type: ignore[index]
            try:
                self._eks("CreateCluster", payload, what="CreateCluster")
            except http.ProviderApiError as exc:
                if "ResourceInUseException" not in exc.message:
                    raise
            cluster = self._wait_eks_cluster(request.name, {"ACTIVE"})
        if not cluster or (cluster.get("tags") or {}).get("kubernetes-platform:managed") != "true":
            raise http.ProviderApiError("Refusing to use an EKS cluster without the platform ownership tag")
        if cluster.get("status") != "ACTIVE":
            cluster = self._wait_eks_cluster(request.name, {"ACTIVE"})

        if cluster is None:
            raise http.ProviderApiError("EKS cluster disappeared while waiting for it to become active")

        for pool in pools:
            pool_name, count = pool.name, pool.count
            if count < 1:
                continue
            existing_group = None
            try:
                existing_group = self._eks(
                    "DescribeNodegroup",
                    {"clusterName": request.name, "nodegroupName": pool_name},
                    what="DescribeNodegroup",
                ).get("nodegroup")
            except http.ProviderApiError as exc:
                if "ResourceNotFoundException" not in exc.message:
                    raise
                self._eks(
                    "CreateNodegroup",
                    {
                        "clusterName": request.name,
                        "nodegroupName": pool_name,
                        "nodeRole": node_role_arn,
                        "subnets": subnet_ids,
                        "scalingConfig": {
                            "minSize": pool.minimum,
                            "maxSize": pool.maximum_size,
                            "desiredSize": count,
                        },
                        "instanceTypes": [pool.machine_type]
                        if pool.machine_type
                        else compute.managed_node_instance_types,
                        "diskSize": pool.disk_gb or compute.managed_node_disk_size_gb,
                        "labels": pool.labels,
                        "taints": [
                            {
                                "key": item.key,
                                "effect": EKS_TAINT_EFFECTS[item.effect],
                                **({"value": item.value} if item.value is not None else {}),
                            }
                            for item in pool.taints
                        ],
                        "tags": {"kubernetes-platform:managed": "true"},
                    },
                    what="CreateNodegroup",
                )
            if existing_group and (existing_group.get("tags") or {}).get("kubernetes-platform:managed") != "true":
                raise http.ProviderApiError(
                    f"Refusing to use EKS node group {pool_name} without the platform ownership tag"
                )
            deadline = time.monotonic() + 3600
            while time.monotonic() < deadline:
                nodegroup = (
                    self._eks(
                        "DescribeNodegroup",
                        {"clusterName": request.name, "nodegroupName": pool_name},
                        what="DescribeNodegroup",
                    ).get("nodegroup")
                    or {}
                )
                if nodegroup.get("status") == "ACTIVE":
                    break
                if nodegroup.get("status") == "CREATE_FAILED":
                    raise http.ProviderApiError(f"EKS node group {pool_name} failed to create")
                time.sleep(15)
            else:
                raise http.ProviderApiError(f"Timed out waiting for EKS node group {pool_name}")

        cluster = self._eks_cluster(request.name) or cluster
        endpoint = (cluster.get("endpoint") or "").strip()
        ca_data = ((cluster.get("certificateAuthority") or {}).get("data") or "").strip()
        if not endpoint or not ca_data:
            raise http.ProviderApiError("EKS returned an incomplete API endpoint or certificate authority")
        return ManagedClusterResult(
            external_id=f"{self.cfg.region}|{request.name}", endpoint=endpoint, certificate_authority_data=ca_data
        )

    def delete_managed_cluster(self, external_id: str) -> None:
        region, separator, name = external_id.partition("|")
        if not separator or region != self.cfg.region or not name:
            raise http.ProviderApiError("EKS cluster ID must contain region|name matching the AWS connection")
        cluster = self._eks_cluster(name)
        if cluster is None:
            return
        if (cluster.get("tags") or {}).get("kubernetes-platform:managed") != "true":
            raise http.ProviderApiError("Refusing to delete an EKS cluster without the platform ownership tag")
        groups = self._eks_nodegroups(name)
        for group in groups:
            described = (
                self._eks(
                    "DescribeNodegroup", {"clusterName": name, "nodegroupName": group}, what="DescribeNodegroup"
                ).get("nodegroup")
                or {}
            )
            if (described.get("tags") or {}).get("kubernetes-platform:managed") != "true":
                raise http.ProviderApiError(f"Refusing to delete unmanaged EKS node group {group}")
            self._eks("DeleteNodegroup", {"clusterName": name, "nodegroupName": group}, what="DeleteNodegroup")
            deadline = time.monotonic() + 3600
            while time.monotonic() < deadline:
                try:
                    self._eks(
                        "DescribeNodegroup", {"clusterName": name, "nodegroupName": group}, what="DescribeNodegroup"
                    )
                except http.ProviderApiError as exc:
                    if "ResourceNotFoundException" in exc.message:
                        break
                    raise
                time.sleep(15)
            else:
                raise http.ProviderApiError(f"Timed out deleting EKS node group {group}")
        self._eks("DeleteCluster", {"name": name}, what="DeleteCluster")
        self._wait_eks_cluster(name, set(), gone=True)

    def eks_bearer_token(self, cluster_name: str) -> str:
        """Build the short-lived EKS IAM authenticator token without invoking an exec plugin."""
        access, secret, session = self._keys()
        moment = datetime.now(UTC)
        amz_date = moment.strftime("%Y%m%dT%H%M%SZ")
        date = moment.strftime("%Y%m%d")
        host = f"sts.{self.cfg.region}.amazonaws.com"
        scope = f"{date}/{self.cfg.region}/sts/aws4_request"
        params = {
            "Action": "GetCallerIdentity",
            "Version": STS_VERSION,
            "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
            "X-Amz-Credential": f"{access}/{scope}",
            "X-Amz-Date": amz_date,
            "X-Amz-Expires": "60",
            "X-Amz-SignedHeaders": "host;x-k8s-aws-id",
        }
        if session:
            params["X-Amz-Security-Token"] = session
        canonical_query = "&".join(
            f"{quote(key, safe='-_.~')}={quote(value, safe='-_.~')}" for key, value in sorted(params.items())
        )
        canonical_request = "\n".join(
            [
                "GET",
                "/",
                canonical_query,
                f"host:{host}\nx-k8s-aws-id:{cluster_name}\n",
                "host;x-k8s-aws-id",
                hashlib.sha256(b"").hexdigest(),
            ]
        )
        string_to_sign = "\n".join(
            ["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical_request.encode()).hexdigest()]
        )
        signing_key = _hmac(_hmac(_hmac(_hmac(f"AWS4{secret}".encode(), date), self.cfg.region), "sts"), "aws4_request")
        signature = hmac.new(signing_key, string_to_sign.encode(), hashlib.sha256).hexdigest()
        url = f"https://{host}/?{canonical_query}&X-Amz-Signature={signature}"
        return "k8s-aws-v1." + base64.urlsafe_b64encode(url.encode()).rstrip(b"=").decode()

    def _describe_lb(self, name: str | None = None, arn: str | None = None) -> ElementTree.Element | None:
        form = {"Action": "DescribeLoadBalancers"}
        if name:
            form["Names.member.1"] = name
        if arn:
            form["LoadBalancerArns.member.1"] = arn
        try:
            root = self._elb(form, "DescribeLoadBalancers")
        except http.ProviderApiError as exc:
            if "LoadBalancerNotFound" in exc.message:
                return None
            raise
        return next(root.iterfind(".//LoadBalancers/member"), None)

    @staticmethod
    def _field(element: ElementTree.Element | None, path: str) -> str:
        return _text(element, path)

    def create_load_balancer(self, request: LoadBalancerRequest) -> LoadBalancerResult:
        if request.scheme not in {"internal", "internet-facing"}:
            raise http.ProviderApiError("AWS load balancer scheme must be internal or internet-facing")
        existing = self._describe_lb(name=request.name)
        if existing is None:
            form = {
                "Action": "CreateLoadBalancer",
                "Name": request.name,
                "Type": "network",
                "Scheme": request.scheme,
                "IpAddressType": "ipv4",
            }
            for index, subnet in enumerate(request.subnet_ids, 1):
                form[f"Subnets.member.{index}"] = subnet
            for index, (key, value) in enumerate(
                (("kubernetes-platform:managed", "true"), ("kubernetes-platform:cluster", request.name)), 1
            ):
                form[f"Tags.member.{index}.Key"] = key
                form[f"Tags.member.{index}.Value"] = value
            try:
                created = self._elb(form, "CreateLoadBalancer")
                existing = next(created.iterfind(".//LoadBalancers/member"), None)
            except http.ProviderApiError as exc:
                if "DuplicateLoadBalancerName" not in exc.message:
                    raise
                existing = self._describe_lb(name=request.name)
        if existing is None:
            existing = self._describe_lb(name=request.name)
        if existing is None:
            raise http.ProviderApiError("AWS did not return the created Network Load Balancer")
        lb_arn = _text(existing, "LoadBalancerArn")
        dns_name = _text(existing, "DNSName")
        vpc_id = _text(existing, "VpcId")
        if not lb_arn or not dns_name:
            raise http.ProviderApiError("AWS returned an incomplete Network Load Balancer description")
        if not self._has_managed_tag(lb_arn):
            raise http.ProviderApiError("Refusing to use an AWS load balancer without the platform ownership tag")
        if vpc_id != request.vpc_id:
            raise http.ProviderApiError("The existing Network Load Balancer is in a different VPC")

        target_name = f"{request.name[:24]}-tg-{hashlib.sha256(request.name.encode()).hexdigest()[:4]}"
        target_group = None
        created_target_group = False
        try:
            root = self._elb({"Action": "DescribeTargetGroups", "Names.member.1": target_name}, "DescribeTargetGroups")
            target_group = next(root.iterfind(".//TargetGroups/member"), None)
        except http.ProviderApiError as exc:
            if "TargetGroupNotFound" not in exc.message:
                raise
        if target_group is None:
            root = self._elb(
                {
                    "Action": "CreateTargetGroup",
                    "Name": target_name,
                    "Protocol": "TCP",
                    "Port": "6443",
                    "VpcId": request.vpc_id,
                    "TargetType": "instance",
                    "HealthCheckProtocol": "TCP",
                    "HealthCheckPort": "traffic-port",
                },
                "CreateTargetGroup",
            )
            target_group = next(root.iterfind(".//TargetGroups/member"), None)
            created_target_group = target_group is not None
        tg_arn = _text(target_group, "TargetGroupArn")
        if not tg_arn:
            raise http.ProviderApiError("AWS did not return the target group ARN")
        if created_target_group:
            self._elb(
                {
                    "Action": "AddTags",
                    "ResourceArns.member.1": tg_arn,
                    "Tags.member.1.Key": "kubernetes-platform:managed",
                    "Tags.member.1.Value": "true",
                },
                "Tag target group",
            )
        elif not self._has_managed_tag(tg_arn):
            raise http.ProviderApiError("Refusing to use an AWS target group without the platform ownership tag")

        listeners = self._elb({"Action": "DescribeListeners", "LoadBalancerArn": lb_arn}, "DescribeListeners")
        listener = next(
            (item for item in listeners.iterfind(".//Listeners/member") if _text(item, "Port") == "6443"), None
        )
        if listener is None:
            self._elb(
                {
                    "Action": "CreateListener",
                    "LoadBalancerArn": lb_arn,
                    "Protocol": "TCP",
                    "Port": "6443",
                    "DefaultActions.member.1.Type": "forward",
                    "DefaultActions.member.1.TargetGroupArn": tg_arn,
                },
                "CreateListener",
            )
        current = self._elb({"Action": "DescribeTargetHealth", "TargetGroupArn": tg_arn}, "DescribeTargetHealth")
        current_ids = {_text(item, "Target/Id") for item in current.iterfind(".//TargetHealthDescriptions/member")}
        wanted = set(request.targets)
        stale = sorted(current_ids - wanted)
        missing = sorted(wanted - current_ids)
        for action, target_ids in (("DeregisterTargets", stale), ("RegisterTargets", missing)):
            if not target_ids:
                continue
            form = {"Action": action, "TargetGroupArn": tg_arn}
            for index, target in enumerate(target_ids, 1):
                form[f"Targets.member.{index}.Id"] = target
                form[f"Targets.member.{index}.Port"] = "6443"
            self._elb(form, action)
        return LoadBalancerResult(external_id=f"{self.cfg.region}|{lb_arn}|{tg_arn}", endpoint=dns_name)

    def delete_load_balancer(self, external_id: str) -> None:
        region, separator, identifiers = external_id.partition("|")
        lb_arn, separator2, tg_arn = identifiers.partition("|")
        if not separator or not separator2 or not lb_arn or not tg_arn:
            raise http.ProviderApiError("AWS load balancer ID must contain region|load-balancer-arn|target-group-arn")
        if region != self.cfg.region:
            raise http.ProviderApiError("The load balancer region does not match this AWS connection")
        lb = self._describe_lb(arn=lb_arn)
        if lb is not None:
            if not self._has_managed_tag(lb_arn):
                raise http.ProviderApiError(
                    "Refusing to delete an AWS load balancer without the platform ownership tag"
                )
            self._elb({"Action": "DeleteLoadBalancer", "LoadBalancerArn": lb_arn}, "DeleteLoadBalancer")
            deadline = time.monotonic() + 120
            while self._describe_lb(arn=lb_arn) is not None:
                if time.monotonic() >= deadline:
                    raise http.ProviderApiError("AWS load balancer is still deleting; retry cluster deletion")
                time.sleep(2)
        if not self._has_managed_tag(tg_arn):
            raise http.ProviderApiError("Refusing to delete an AWS target group without the platform ownership tag")
        try:
            self._elb({"Action": "DeleteTargetGroup", "TargetGroupArn": tg_arn}, "DeleteTargetGroup")
        except http.ProviderApiError as exc:
            if "TargetGroupNotFound" in exc.message:
                return
            if "ResourceInUse" in exc.message:
                raise http.ProviderApiError("AWS target group is still in use; retry cluster deletion") from exc
            raise

    def _has_managed_tag(self, arn: str) -> bool:
        tags = self._elb({"Action": "DescribeTags", "ResourceArns.member.1": arn}, "DescribeTags")
        return any(
            _text(tag, "Key") == "kubernetes-platform:managed" and _text(tag, "Value") == "true"
            for tag in tags.iterfind(".//TagDescriptions/member/Tags/member")
        )
