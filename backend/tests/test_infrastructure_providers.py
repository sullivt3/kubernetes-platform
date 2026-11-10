"""Phase 3 providers: Proxmox (simulator), local Docker (fake engine), AWS/Azure/GCP
(recorded-style responses) and network discovery (real sockets)."""

from __future__ import annotations

import base64
import json
import socket
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app.core.errors import ValidationFailedError
from app.modules.infrastructure import discovery
from app.modules.providers import http
from app.modules.providers.http import HttpRequest, HttpResponse, ProviderApiError, ProviderAuthError, TlsPolicy
from app.modules.providers.implementations import local as local_mod
from app.modules.providers.implementations.aws import aws_sigv4
from app.modules.providers.registry import provider_registry
from app.modules.providers.types import MachineRef, MachineSpecRequest, ManagedClusterRequest, PowerAction, ProviderType
from tests.fake_docker import FakeDocker
from tests.fake_proxmox import TOKEN_ID, TOKEN_SECRET, FakeProxmox

PUBLIC_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl test@example"


@pytest.fixture
def transport() -> Iterator[list[Any]]:
    """Install a scripted transport; tests append handlers."""
    handlers: list[Any] = []

    def send(request: HttpRequest, tls: TlsPolicy) -> HttpResponse:
        for handler in handlers:
            response = handler(request)
            if response is not None:
                return response
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    http.set_transport(send)
    yield handlers
    http.set_transport(None)


def _json(status: int, body: Any) -> HttpResponse:
    return HttpResponse(status, {"Content-Type": "application/json"}, json.dumps(body).encode())


# ---- AWS Signature Version 4 ---------------------------------------------------------


AKID = "AKIDEXAMPLE"
SECRET = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"
MOMENT = datetime(2015, 8, 30, 12, 36, 0, tzinfo=UTC)


def test_sigv4_matches_aws_test_suite_get_vanilla() -> None:
    # aws-sig-v4-test-suite/get-vanilla
    headers = aws_sigv4(
        method="GET",
        url="https://example.amazonaws.com/",
        headers={},
        body=b"",
        region="us-east-1",
        service="service",
        access_key=AKID,
        secret_key=SECRET,
        now=MOMENT,
    )
    assert headers["Authorization"] == (
        "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20150830/us-east-1/service/aws4_request, "
        "SignedHeaders=host;x-amz-date, "
        "Signature=5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31"
    )


def test_sigv4_matches_aws_documentation_iam_example() -> None:
    # "Create a signed request" example from the IAM / SigV4 documentation
    headers = aws_sigv4(
        method="GET",
        url="https://iam.amazonaws.com/?Action=ListUsers&Version=2010-05-08",
        headers={"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"},
        body=b"",
        region="us-east-1",
        service="iam",
        access_key=AKID,
        secret_key=SECRET,
        now=MOMENT,
    )
    assert headers["Authorization"].endswith(
        "SignedHeaders=content-type;host;x-amz-date, "
        "Signature=5d672d79c15b13162d9279b0855cfba6789a8edb4c82c400e06b5924a6f2b5d7"
    )


def test_eks_bearer_token_is_a_short_lived_signed_sts_url() -> None:
    token = _aws().eks_bearer_token("prod")
    assert token.startswith("k8s-aws-v1.")
    encoded = token.removeprefix("k8s-aws-v1.")
    url = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query))
    assert parts.hostname == "sts.eu-west-1.amazonaws.com"
    assert query["Action"] == "GetCallerIdentity" and query["X-Amz-Expires"] == "60"
    assert query["X-Amz-SignedHeaders"] == "host;x-k8s-aws-id"
    assert "X-Amz-Signature" in query


@pytest.mark.parametrize(
    ("effect", "aws_effect"),
    [("NoSchedule", "NO_SCHEDULE"), ("PreferNoSchedule", "PREFER_NO_SCHEDULE"), ("NoExecute", "NO_EXECUTE")],
)
def test_eks_cluster_create_reuses_tagged_cluster_and_creates_managed_node_group(
    transport: list[Any], effect: str, aws_effect: str
) -> None:
    actions: list[str] = []

    def eks_api(request: HttpRequest) -> HttpResponse | None:
        if urlsplit(request.url).hostname != "eks.eu-west-1.amazonaws.com":
            return None
        path = urlsplit(request.url).path
        assert "X-Amz-Target" not in request.headers
        action = {
            ("GET", "/clusters/prod"): "DescribeCluster",
            ("GET", "/clusters/prod/node-groups/workers"): "DescribeNodegroup",
            ("POST", "/clusters/prod/node-groups"): "CreateNodegroup",
        }[(request.method, path)]
        actions.append(action)
        if action == "DescribeCluster":
            return _json(
                200,
                {
                    "cluster": {
                        "name": "prod",
                        "status": "ACTIVE",
                        "tags": {"kubernetes-platform:managed": "true"},
                        "endpoint": "https://ABC.gr7.eu-west-1.eks.amazonaws.com",
                        "certificateAuthority": {"data": "Y2E="},
                    }
                },
            )
        if action == "DescribeNodegroup":
            if actions.count(action) == 1:
                return _json(404, {"message": "ResourceNotFoundException: not found"})
            return _json(
                200,
                {"nodegroup": {"status": "ACTIVE", "tags": {"kubernetes-platform:managed": "true"}}},
            )
        if action == "CreateNodegroup":
            payload = json.loads(request.body or b"{}")
            assert payload["nodegroupName"] == "workers"
            assert payload["scalingConfig"] == {"minSize": 1, "maxSize": 4, "desiredSize": 2}
            assert payload["diskSize"] == 20 and payload["instanceTypes"] == ["t3.medium"]
            assert payload["taints"] == [{"key": "dedicated", "effect": aws_effect}]
            return _json(200, {})
        return None

    transport.append(eks_api)
    provider = _aws()
    result = provider.create_managed_cluster(
        ManagedClusterRequest(
            name="prod",
            version="1.34",
            options={
                "managed_cluster_role_arn": "arn:aws:iam::123456789012:role/EKSClusterRole",
                "managed_node_role_arn": "arn:aws:iam::123456789012:role/EKSNodeRole",
                "managed_subnet_ids": ["subnet-a", "subnet-b"],
            },
            node_pools=[
                {
                    "name": "workers",
                    "count": 2,
                    "minimum": 1,
                    "maximum": 4,
                    "taints": [{"key": "dedicated", "effect": effect, "value": None}],
                }
            ],
        )
    )
    assert result.external_id == "eu-west-1|prod"
    assert result.endpoint.startswith("https://") and result.certificate_authority_data == "Y2E="
    assert "CreateCluster" not in actions and "CreateNodegroup" in actions


@pytest.mark.parametrize(
    ("pool", "extra_options"),
    [
        ({"name": "workers", "count": {"invalid": 2}}, {}),
        ({"name": "workers", "count": 2, "minimum": 3, "maximum": 4}, {}),
        ({"name": "workers", "count": 2, "maximum": 1}, {}),
        ({"name": "workers", "count": 2, "taints": [{"key": "dedicated", "effect": "invalid"}]}, {}),
        ({"name": "workers", "count": 2}, {"managed_node_disk_size_gb": "invalid"}),
        ({"name": "workers", "count": 2}, {"managed_node_instance_types": {"invalid": "type"}}),
    ],
)
def test_eks_invalid_node_settings_fail_before_cloud_requests(
    transport: list[Any], pool: dict[str, object], extra_options: dict[str, object]
) -> None:
    # The transport rejects every request, so no cluster can be created before validation.
    request = ManagedClusterRequest(
        name="prod",
        version="1.34",
        options={
            "managed_cluster_role_arn": "arn:aws:iam::123456789012:role/EKSClusterRole",
            "managed_node_role_arn": "arn:aws:iam::123456789012:role/EKSNodeRole",
            "managed_subnet_ids": ["subnet-a", "subnet-b"],
            **extra_options,
        },
        node_pools=[pool],
    )
    with pytest.raises(ProviderApiError, match="Invalid EKS node group or compute settings"):
        _aws().create_managed_cluster(request)


# ---- AWS provider --------------------------------------------------------------------

EC2_NS = 'xmlns="http://ec2.amazonaws.com/doc/2016-11-15/"'


def _instances_xml(items: str, next_token: str = "") -> bytes:
    token = f"<nextToken>{next_token}</nextToken>" if next_token else ""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<DescribeInstancesResponse {EC2_NS}><requestId>r</requestId><reservationSet><item>
<reservationId>r-1</reservationId><instancesSet>{items}</instancesSet></item></reservationSet>{token}
</DescribeInstancesResponse>""".encode()


def _instance(
    instance_id: str, name: str, state: str, private_ip: str, public_ip: str = "", managed: bool = False
) -> str:
    public = f"<ipAddress>{public_ip}</ipAddress>" if public_ip else ""
    managed_tag = "<item><key>kubernetes-platform:managed</key><value>true</value></item>" if managed else ""
    return f"""<item><instanceId>{instance_id}</instanceId><imageId>ami-1</imageId>
<instanceState><code>16</code><name>{state}</name></instanceState><instanceType>t3.medium</instanceType>
<placement><availabilityZone>eu-west-1a</availabilityZone></placement><privateIpAddress>{private_ip}</privateIpAddress>
{public}<vpcId>vpc-1</vpcId><architecture>x86_64</architecture><platformDetails>Linux/UNIX</platformDetails>
<tagSet><item><key>Name</key><value>{name}</value></item>{managed_tag}</tagSet></item>"""


def aws_api(request: HttpRequest) -> HttpResponse | None:
    host = urlsplit(request.url).netloc
    form = dict(parse_qsl((request.body or b"").decode()))
    assert request.headers["Authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/")
    if host == "sts.eu-west-1.amazonaws.com" and form["Action"] == "GetCallerIdentity":
        assert "/eu-west-1/sts/aws4_request" in request.headers["Authorization"]
        return HttpResponse(
            200,
            {},
            b"""<GetCallerIdentityResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/"><GetCallerIdentityResult>
<Arn>arn:aws:iam::123456789012:user/platform</Arn><UserId>AIDA</UserId><Account>123456789012</Account>
</GetCallerIdentityResult></GetCallerIdentityResponse>""",
        )
    if host == "ec2.eu-west-1.amazonaws.com" and form["Action"] == "DescribeInstances":
        if form.get("NextToken") == "page2":
            return HttpResponse(200, {}, _instances_xml(_instance("i-2", "worker", "stopped", "10.1.0.6")))
        return HttpResponse(200, {}, _instances_xml(_instance("i-1", "web", "running", "10.1.0.5", "3.3.3.3"), "page2"))
    if host == "ec2.eu-west-1.amazonaws.com" and form["Action"] == "DescribeInstanceTypes":
        assert form["InstanceType.1"] == "t3.medium"
        return HttpResponse(
            200,
            {},
            f"""<DescribeInstanceTypesResponse {EC2_NS}><instanceTypeSet><item><instanceType>t3.medium</instanceType>
<vCpuInfo><defaultVCpus>2</defaultVCpus></vCpuInfo><memoryInfo><sizeInMiB>4096</sizeInMiB></memoryInfo></item>
</instanceTypeSet></DescribeInstanceTypesResponse>""".encode(),
        )
    return None


def _aws() -> Any:
    return provider_registry.create(
        ProviderType.AWS, {"access_key_id": "AKIAIOSFODNN7EXAMPLE", "region": "eu-west-1"}, b"secret"
    )


def test_aws_validate_and_discover(transport: list[Any]) -> None:
    transport.append(aws_api)
    provider = _aws()
    result = provider.validate_connection()
    assert result.ok and result.details["account"] == "123456789012"
    machines = provider.discover()
    assert [(m.name, m.power_state) for m in machines] == [("web", "running"), ("worker", "stopped")]
    web = machines[0]
    assert web.external_id == "eu-west-1/i-1"
    assert [a.address for a in web.addresses] == ["10.1.0.5", "3.3.3.3"]
    assert web.hardware and (web.hardware.cpu_cores, web.hardware.memory_mb) == (2, 4096)
    assert web.importable is False


def test_aws_errors_are_explained(transport: list[Any]) -> None:
    transport.append(
        lambda r: HttpResponse(
            403,
            {},
            b"""<ErrorResponse><Error><Type>Sender</Type><Code>InvalidClientTokenId</Code>
<Message>The security token included in the request is invalid.</Message></Error></ErrorResponse>""",
        )
    )
    with pytest.raises(ProviderAuthError, match="InvalidClientTokenId"):
        _aws().validate_connection()


def test_aws_provision_status_and_delete_are_idempotent(transport: list[Any]) -> None:
    state = {"exists": False, "run_count": 0, "terminate_count": 0}

    def api(request: HttpRequest) -> HttpResponse | None:
        if urlsplit(request.url).netloc != "ec2.eu-west-1.amazonaws.com":
            return None
        form = dict(parse_qsl((request.body or b"").decode()))
        assert request.headers["Authorization"].startswith("AWS4-HMAC-SHA256")
        if form["Action"] == "RunInstances":
            state["run_count"] += 1
            assert form["ImageId"] == "ami-example"
            assert form["NetworkInterface.1.SubnetId"] == "subnet-example"
            assert form["NetworkInterface.1.SecurityGroupId.1"] == "sg-example"
            user_data = base64.b64decode(form["UserData"]).decode()
            assert PUBLIC_KEY in user_data and "NOPASSWD:ALL" in user_data
            assert form["TagSpecification.1.Tag.2.Value"] == "true"
            state["exists"] = True
            return HttpResponse(
                200,
                {},
                (
                    f"<RunInstancesResponse {EC2_NS}><instancesSet><item><instanceId>i-owned</instanceId>"
                    "</item></instancesSet></RunInstancesResponse>"
                ).encode(),
            )
        if form["Action"] == "DescribeInstances":
            items = (
                _instance("i-owned", "node-1", "running", "10.0.0.8", "198.51.100.8", managed=True)
                if state["exists"]
                else ""
            )
            return HttpResponse(200, {}, _instances_xml(items))
        if form["Action"] == "TerminateInstances":
            state["terminate_count"] += 1
            state["exists"] = False
            return HttpResponse(200, {}, f"<TerminateInstancesResponse {EC2_NS}/>".encode())
        return None

    transport.append(api)
    provider = _aws()
    request = MachineSpecRequest(
        name="node-1",
        cpu_cores=2,
        memory_mb=4096,
        disk_gb=40,
        image="ami-example",
        provider_options={
            "instance_type": "t3.medium",
            "subnet_id": "subnet-example",
            "security_group_ids": ["sg-example"],
        },
        ssh_username="platform",
        ssh_public_key=PUBLIC_KEY,
    )
    machine = provider.provision(request)
    assert machine.external_id == "eu-west-1/i-owned" and machine.power_state == "running"
    assert provider.get_status(MachineRef(external_id=machine.external_id)).exists
    provider.delete(MachineRef(external_id=machine.external_id))
    provider.delete(MachineRef(external_id=machine.external_id))
    assert state == {"exists": False, "run_count": 1, "terminate_count": 1}


# ---- Azure ---------------------------------------------------------------------------

TENANT = "11111111-1111-1111-1111-111111111111"
CLIENT = "22222222-2222-2222-2222-222222222222"
SUB = "33333333-3333-3333-3333-333333333333"


def azure_api(request: HttpRequest) -> HttpResponse | None:
    url = request.url
    if url == f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token":
        form = dict(parse_qsl((request.body or b"").decode()))
        if form["client_secret"] != "s3cret":
            return _json(401, {"error": "invalid_client", "error_description": "AADSTS7000215: Invalid secret.\nTrace"})
        assert form["scope"] == "https://management.azure.com/.default"
        return _json(200, {"access_token": "tok", "expires_in": 3599})
    assert request.headers["Authorization"] == "Bearer tok"
    if url.startswith(f"https://management.azure.com/subscriptions/{SUB}?"):
        return _json(200, {"displayName": "Lab", "state": "Enabled"})
    if "/providers/Microsoft.Compute/virtualMachines" in url and "page=2" not in url:
        vm = {
            "id": f"/subscriptions/{SUB}/resourceGroups/rg1/providers/Microsoft.Compute/virtualMachines/vm1",
            "name": "vm1",
            "location": "westeurope",
            "properties": {
                "hardwareProfile": {"vmSize": "Standard_B2s"},
                "storageProfile": {"osDisk": {"osType": "Linux"}},
                "networkProfile": {"networkInterfaces": [{"id": "/x/NIC1"}]},
                "instanceView": {"statuses": [{"code": "ProvisioningState/succeeded"}, {"code": "PowerState/running"}]},
            },
        }
        return _json(200, {"value": [vm], "nextLink": url + "&page=2"})
    if "/providers/Microsoft.Compute/virtualMachines" in url:
        return _json(200, {"value": []})
    if "/providers/Microsoft.Network/networkInterfaces" in url:
        return _json(
            200,
            {
                "value": [
                    {
                        "id": "/x/nic1",
                        "properties": {"ipConfigurations": [{"properties": {"privateIPAddress": "10.2.0.4"}}]},
                    }
                ]
            },
        )
    if "/locations/westeurope/vmSizes" in url:
        return _json(200, {"value": [{"name": "Standard_B2s", "numberOfCores": 2, "memoryInMB": 4096}]})
    return None


def _azure(secret: bytes = b"s3cret") -> Any:
    return provider_registry.create(
        ProviderType.AZURE, {"tenant_id": TENANT, "client_id": CLIENT, "subscription_id": SUB}, secret
    )


def test_azure_validate_and_discover(transport: list[Any]) -> None:
    transport.append(azure_api)
    provider = _azure()
    assert provider.validate_connection().ok
    [vm] = provider.discover()
    assert (vm.name, vm.power_state, vm.addresses[0].address) == ("vm1", "running", "10.2.0.4")
    assert vm.hardware and vm.hardware.memory_mb == 4096
    assert vm.provider_metadata["resource_group"] == "rg1"


def test_azure_bad_secret(transport: list[Any]) -> None:
    transport.append(azure_api)
    with pytest.raises(ProviderAuthError, match="AADSTS7000215") as exc_info:
        _azure(b"wrong").validate_connection()
    assert "Trace" not in exc_info.value.message


def test_azure_provision_status_and_delete_are_idempotent(transport: list[Any]) -> None:
    state = {"exists": False, "puts": 0, "deletes": 0}
    vm_url = (
        f"https://management.azure.com/subscriptions/{SUB}/resourceGroups/rg1/"
        f"providers/Microsoft.Compute/virtualMachines/node-1?api-version=2024-07-01"
    )
    resource_id = vm_url.split("?", 1)[0]

    def api(request: HttpRequest) -> HttpResponse | None:
        if request.url == f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token":
            return azure_api(request)
        if request.url.startswith(vm_url):
            if request.method == "PUT":
                body = json.loads(request.body or b"{}")
                state["puts"] += 1
                assert body["tags"]["kubernetes-platform-managed"] == "true"
                assert (
                    body["properties"]["osProfile"]["linuxConfiguration"]["ssh"]["publicKeys"][0]["keyData"]
                    == PUBLIC_KEY
                )
                state["exists"] = True
                return _json(200, {"id": resource_id})
            if request.method == "DELETE":
                state["deletes"] += 1
                state["exists"] = False
                return HttpResponse(202, {}, b"")
            if not state["exists"]:
                return _json(404, {"error": {"code": "NotFound"}})
            return _json(
                200,
                {
                    "id": resource_id,
                    "name": "node-1",
                    "location": "eastus",
                    "tags": {"kubernetes-platform-managed": "true"},
                    "properties": {
                        "hardwareProfile": {"vmSize": "Standard_B2s"},
                        "instanceView": {"statuses": [{"code": "PowerState/running"}]},
                        "networkProfile": {"networkInterfaces": [{"id": "/x/nic1"}]},
                    },
                },
            )
        if request.url == "/x/nic1?api-version=2024-05-01":
            return _json(200, {"properties": {"ipConfigurations": [{"properties": {"privateIPAddress": "10.0.0.7"}}]}})
        return None

    transport.extend([api, azure_api])
    provider = _azure()
    machine = provider.provision(
        MachineSpecRequest(
            name="node-1",
            cpu_cores=2,
            memory_mb=4096,
            disk_gb=30,
            ssh_username="platform",
            ssh_public_key=PUBLIC_KEY,
            provider_options={
                "resource_group": "rg1",
                "location": "eastus",
                "vm_size": "Standard_B2s",
                "network_interface_id": "/x/nic1",
                "image": "Canonical:UbuntuServer:22_04-lts:latest",
            },
        )
    )
    assert machine.external_id == resource_id and machine.power_state == "running"
    assert machine.addresses[0].address == "10.0.0.7"
    assert provider.get_status(MachineRef(external_id=resource_id)).exists
    provider.delete(MachineRef(external_id=resource_id))
    provider.delete(MachineRef(external_id=resource_id))
    assert state == {"exists": False, "puts": 1, "deletes": 1}


# ---- GCP -----------------------------------------------------------------------------


@pytest.fixture(scope="module")
def gcp_key() -> tuple[dict[str, Any], Any]:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    key = {
        "type": "service_account",
        "project_id": "lab-project",
        "private_key_id": "kid1",
        "private_key": pem,
        "client_email": "platform@lab-project.iam.gserviceaccount.com",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
    return key, private.public_key()


def test_gcp_validate_and_discover(transport: list[Any], gcp_key: tuple[dict[str, Any], Any]) -> None:
    key, public_key = gcp_key

    def gcp_api(request: HttpRequest) -> HttpResponse | None:
        if request.url == "https://oauth2.googleapis.com/token":
            assertion = dict(parse_qsl((request.body or b"").decode()))["assertion"]
            header, claims, signature = assertion.split(".")
            pad = lambda s: base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))  # noqa: E731
            public_key.verify(pad(signature), f"{header}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256())
            body = json.loads(pad(claims))
            assert body["iss"] == key["client_email"] and body["scope"].endswith("/compute")
            return _json(200, {"access_token": "gtok", "expires_in": 3600})
        assert request.headers["Authorization"] == "Bearer gtok"
        base = "https://compute.googleapis.com/compute/v1/projects/lab-project"
        if request.url == base:
            return _json(200, {"name": "lab-project"})
        if request.url.startswith(f"{base}/aggregated/instances"):
            return _json(
                200,
                {
                    "items": {
                        "zones/europe-west1-b": {
                            "instances": [
                                {
                                    "id": "123",
                                    "name": "gce-1",
                                    "status": "RUNNING",
                                    "zone": f"{base}/zones/europe-west1-b",
                                    "machineType": f"{base}/zones/europe-west1-b/machineTypes/e2-medium",
                                    "networkInterfaces": [
                                        {"networkIP": "10.3.0.2", "accessConfigs": [{"natIP": "34.1.1.1"}]}
                                    ],
                                }
                            ]
                        },
                        "zones/us-east1-b": {"warning": {"code": "NO_RESULTS_ON_PAGE"}},
                    }
                },
            )
        if request.url == f"{base}/zones/europe-west1-b/machineTypes/e2-medium":
            return _json(200, {"guestCpus": 2, "memoryMb": 4096})
        return None

    transport.append(gcp_api)
    provider = provider_registry.create(ProviderType.GCP, {}, json.dumps(key).encode())
    assert provider.validate_connection().details["service_account"] == key["client_email"]
    [vm] = provider.discover()
    assert vm.external_id == "europe-west1-b/gce-1"
    assert [a.address for a in vm.addresses] == ["10.3.0.2", "34.1.1.1"]
    assert vm.hardware and vm.hardware.cpu_cores == 2


def test_gcp_provision_status_and_delete_are_idempotent(
    transport: list[Any], gcp_key: tuple[dict[str, Any], Any]
) -> None:
    key, _public_key = gcp_key
    state: dict[str, Any] = {"exists": False, "creates": 0, "deletes": 0}
    base = "https://compute.googleapis.com/compute/v1/projects/lab-project"
    instance_url = f"{base}/zones/us-central1-a/instances/node-1"

    def api(request: HttpRequest) -> HttpResponse | None:
        if request.url == "https://oauth2.googleapis.com/token":
            form = dict(parse_qsl((request.body or b"").decode()))
            payload = form["assertion"].split(".")[1]
            payload += "=" * (-len(payload) % 4)
            state["scope"] = json.loads(base64.urlsafe_b64decode(payload))["scope"]
            return _json(200, {"access_token": "gtok", "expires_in": 3600})
        assert request.headers["Authorization"] == "Bearer gtok"
        if request.url.startswith(f"{base}/zones/us-central1-a/instances?"):
            body = json.loads(request.body or b"{}")
            assert body["labels"]["kplatform_managed"] == "true"
            assert (
                body["disks"][0]["initializeParams"]["sourceImage"] == "projects/debian-cloud/global/images/debian-12"
            )
            assert PUBLIC_KEY in body["metadata"]["items"][0]["value"]
            assert "requestId=" in request.url
            state["creates"] += 1
            state["exists"] = True
            return _json(200, {"name": "operation-1", "status": "DONE"})
        if request.url == instance_url and request.method == "GET":
            if not state["exists"]:
                return _json(404, {"error": {"message": "not found"}})
            return _json(
                200,
                {
                    "name": "node-1",
                    "status": "RUNNING",
                    "machineType": "zones/us-central1-a/machineTypes/e2-medium",
                    "labels": {"kplatform_managed": "true"},
                    "networkInterfaces": [{"networkIP": "10.0.0.5", "accessConfigs": [{"natIP": "34.0.0.5"}]}],
                },
            )
        if request.url == instance_url and request.method == "DELETE":
            state["deletes"] += 1
            state["exists"] = False
            return _json(200, {"name": "delete-1", "status": "DONE"})
        return None

    transport.append(api)
    provider = provider_registry.create(ProviderType.GCP, {}, json.dumps(key).encode())
    request = MachineSpecRequest(
        name="node-1",
        cpu_cores=2,
        memory_mb=4096,
        disk_gb=30,
        provider_options={
            "zone": "us-central1-a",
            "image": "projects/debian-cloud/global/images/debian-12",
            "machine_type": "e2-medium",
            "network": "global/networks/default",
        },
        ssh_username="platform",
        ssh_public_key=PUBLIC_KEY,
    )
    machine = provider.provision(request)
    assert machine.external_id == "us-central1-a/node-1"
    assert [address.address for address in machine.addresses] == ["10.0.0.5", "34.0.0.5"]
    assert provider.get_status(MachineRef(external_id=machine.external_id)).exists
    provider.delete(MachineRef(external_id=machine.external_id))
    provider.delete(MachineRef(external_id=machine.external_id))
    assert state["scope"] == "https://www.googleapis.com/auth/compute"
    assert (state["creates"], state["deletes"]) == (1, 1)


# ---- Proxmox -------------------------------------------------------------------------

PROXMOX_CONFIG = {"api_url": "https://pve.lab:8006", "token_id": TOKEN_ID, "node": "pve1", "template_vmid": 9000}


@pytest.fixture
def pve() -> Iterator[FakeProxmox]:
    fake = FakeProxmox()
    http.set_transport(fake)
    yield fake
    http.set_transport(None)


def _proxmox(**config: Any) -> Any:
    provider = provider_registry.create(ProviderType.PROXMOX, {**PROXMOX_CONFIG, **config}, TOKEN_SECRET.encode())
    provider.poll_interval_seconds = 0
    return provider


def _request(name: str = "node-1") -> MachineSpecRequest:
    return MachineSpecRequest(
        name=name, cpu_cores=2, memory_mb=4096, disk_gb=30, ssh_username="platform", ssh_public_key=PUBLIC_KEY
    )


def test_proxmox_config_requires_https() -> None:
    with pytest.raises(ValidationFailedError):
        provider_registry.validate_config(ProviderType.PROXMOX, {**PROXMOX_CONFIG, "api_url": "http://pve:8006"})


def test_proxmox_validate(pve: FakeProxmox) -> None:
    result = _proxmox().validate_connection()
    assert result.ok, result.message
    assert result.details["version"] == "8.3.2"
    assert _proxmox(node="pve2").validate_connection().message == "Node 'pve2' is offline"
    del pve.vms[9000].config["agent"]
    assert "guest agent" in _proxmox().validate_connection().message


def test_proxmox_rejects_bad_token(pve: FakeProxmox) -> None:
    provider = provider_registry.create(ProviderType.PROXMOX, PROXMOX_CONFIG, b"wrong")
    with pytest.raises(ProviderAuthError):
        provider.validate_connection()


def test_proxmox_provision_is_complete_and_idempotent(pve: FakeProxmox) -> None:
    provider = _proxmox(tls_fingerprint=":".join(["AB"] * 32))
    first = provider.provision(_request())
    vmid = int(first.external_id.split("/")[1])
    vm = pve.vms[vmid]
    assert first.external_id == f"pve1/{vmid}" and vm.status == "running"
    assert vm.config["cores"] == "2" and vm.config["memory"] == "4096"
    assert vm.config["ciuser"] == "platform" and vm.config["ipconfig0"] == "ip=dhcp"
    assert unquote(vm.config["sshkeys"]).strip() == PUBLIC_KEY  # URL-encoded as Proxmox requires
    assert "size=30G" in vm.config["scsi0"]
    assert "managed-by: kubernetes-platform" in vm.config["description"]
    assert [a.address for a in first.addresses] == ["192.168.10.50"]
    assert pve.tls_seen[-1].fingerprint_sha256 == ":".join(["AB"] * 32)

    clones = [c for c in pve.calls if c[1].endswith("/clone")]
    again = provider.provision(_request())  # a retried step must not clone twice
    assert again.external_id == first.external_id
    assert [c for c in pve.calls if c[1].endswith("/clone")] == clones


def test_proxmox_never_adopts_or_deletes_foreign_vms(pve: FakeProxmox) -> None:
    from tests.fake_proxmox import Vm

    pve.vms[120] = Vm(120, "node-1", config={"name": "node-1", "scsi0": "local-lvm:vm-120-disk-0,size=10G"})
    provider = _proxmox()
    with pytest.raises(ProviderApiError, match="not managed by the platform"):
        provider.provision(_request())
    with pytest.raises(ProviderApiError, match="refusing to delete"):
        provider.delete(MachineRef(external_id="pve1/120"))
    assert 120 in pve.vms


def test_proxmox_failed_task_is_reported(pve: FakeProxmox) -> None:
    pve.fail_tasks.add("qmclone")
    with pytest.raises(ProviderApiError, match="simulated error"):
        _proxmox().provision(_request())


def test_proxmox_power_status_delete(pve: FakeProxmox) -> None:
    provider = _proxmox()
    ref = MachineRef(external_id=provider.provision(_request()).external_id)
    assert provider.set_power_state(ref, PowerAction.STOP).power_state == "stopped"
    assert any(c[1].endswith("/status/shutdown") for c in pve.calls)
    assert provider.set_power_state(ref, PowerAction.START).power_state == "running"
    spec = provider.get_specifications(ref)
    assert spec.cpu_cores == 2 and spec.disks[0].size_gb == 30
    provider.delete(ref)  # stops the running VM first
    assert not provider.get_status(ref).exists
    provider.delete(ref)  # already gone: still succeeds


def test_proxmox_discover_skips_templates_and_containers(pve: FakeProxmox) -> None:
    provider = _proxmox()
    provider.provision(_request("node-1"))
    names = [m.name for m in provider.discover()]
    assert names == ["node-1"]


# ---- Local (Docker) ---------------------------------------------------------------------


@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch) -> FakeDocker:
    fake = FakeDocker()
    ports = iter(range(40022, 40100))
    monkeypatch.setattr(local_mod.LocalProvider, "_docker", lambda self: fake)
    monkeypatch.setattr(local_mod.LocalProvider, "_free_port", staticmethod(lambda: next(ports)))
    return fake


def test_local_provision_builds_image_and_creates_node(engine: FakeDocker) -> None:
    provider = provider_registry.create(ProviderType.LOCAL, {}, None)
    assert provider.validate_connection().details["node_image"].startswith("built on first provision")
    machine = provider.provision(_request("dev-1"))
    assert engine.builds == [local_mod.node_image_tag()]
    run = engine.runs[0]
    assert run["name"] == "kplatform-dev-1" and run["hostname"] == "dev-1"
    assert run["privileged"] and run["cgroupns"] == "private"
    assert run["environment"] == {"AUTHORIZED_KEY": PUBLIC_KEY, "NODE_USER": "platform"}
    assert run["nano_cpus"] == 2_000_000_000 and run["mem_limit"] == "4096m"
    # A free subnet (172.28.0.0/24 is taken) and a fixed address in it.
    assert engine.network_map["kplatform-local"].subnet == "172.28.1.0/24"
    assert machine.addresses[0].address == "172.28.1.10"
    assert machine.ssh_endpoint and (machine.ssh_endpoint.host, machine.ssh_endpoint.port) == ("127.0.0.1", 40022)
    assert machine.kube_api_endpoint and machine.kube_api_endpoint.port == 40023

    second = provider.provision(_request("dev-2"))
    assert second.addresses[0].address == "172.28.1.11"
    assert provider.provision(_request("dev-1")).external_id == "kplatform-dev-1"  # idempotent
    assert len(engine.runs) == 2 and len(engine.builds) == 1


def test_local_retries_when_a_parallel_provision_took_the_address(
    engine: FakeDocker, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = provider_registry.create(ProviderType.LOCAL, {}, None)
    provider.provision(_request("dev-1"))  # holds 172.28.1.10
    real = provider._free_ip
    stale = iter(["172.28.1.10"])  # what a concurrent provision saw a moment earlier

    def racing(network: Any, exclude: set[str] | None = None) -> str:
        return next(stale, None) or real(network, exclude)

    monkeypatch.setattr(provider, "_free_ip", racing)
    machine = provider.provision(_request("dev-2"))
    assert machine.addresses[0].address == "172.28.1.11"
    assert engine.containers_by_name["kplatform-dev-2"].status == "running"


def test_local_addresses_leave_room_for_virtual_ips(engine: FakeDocker) -> None:
    provider = provider_registry.create(ProviderType.LOCAL, {}, None)
    provider.provision(_request("dev-1"))
    network = engine.network_map["kplatform-local"]
    taken = {f"172.28.1.{i}" for i in range(10, 200)}
    with pytest.raises(ProviderApiError):
        provider._free_ip(network, exclude=taken)


def test_local_power_discover_delete(engine: FakeDocker) -> None:
    provider = provider_registry.create(ProviderType.LOCAL, {}, None)
    provider.provision(_request("dev-1"))
    ref = MachineRef(external_id="kplatform-dev-1")
    assert provider.set_power_state(ref, PowerAction.STOP).power_state == "stopped"
    # A stopped node keeps its reserved address: the next machine must not take it.
    provider.provision(_request("dev-2"))
    assert engine.containers_by_name["kplatform-dev-2"].ip == "172.28.1.11"
    assert {m.name for m in provider.discover()} == {"dev-1", "dev-2"}
    engine.volume_names.add("kplatform-dev-1-var")
    provider.delete(ref)
    provider.delete(ref)  # idempotent
    assert "kplatform-dev-1" not in engine.containers_by_name
    assert engine.removed_volumes == ["kplatform-dev-1-var"]


def test_local_refuses_unmanaged_containers(engine: FakeDocker) -> None:
    from tests.fake_docker import FakeContainer

    engine.containers_by_name["kplatform-db"] = FakeContainer(engine, "kplatform-db", "postgres", {"labels": {}})
    provider = provider_registry.create(ProviderType.LOCAL, {}, None)
    with pytest.raises(ProviderApiError, match="not a platform-managed"):
        provider.delete(MachineRef(external_id="kplatform-db"))


# ---- Network discovery -------------------------------------------------------------------


def test_parse_targets() -> None:
    assert discovery.parse_targets(["10.0.0.0/30"], allow_public=False) == ["10.0.0.1", "10.0.0.2"]
    assert discovery.parse_targets(["192.168.1.10-12", "192.168.1.11"], allow_public=False) == [
        "192.168.1.10",
        "192.168.1.11",
        "192.168.1.12",
    ]
    assert discovery.parse_targets(["127.0.0.1"], allow_public=False) == ["127.0.0.1"]
    with pytest.raises(ValidationFailedError, match="public address"):
        discovery.parse_targets(["8.8.8.8"], allow_public=False)
    assert discovery.parse_targets(["8.8.8.8"], allow_public=True) == ["8.8.8.8"]
    with pytest.raises(ValidationFailedError, match="too large"):
        discovery.parse_targets(["10.0.0.0/8"], allow_public=False)
    with pytest.raises(ValidationFailedError, match="not an IP"):
        discovery.parse_targets(["10.0.0.20-10"], allow_public=False)


def test_os_hints() -> None:
    assert discovery.os_hint("SSH-2.0-OpenSSH_9.6p1 Ubuntu-3ubuntu13.5") == "Ubuntu"
    assert discovery.os_hint("SSH-2.0-OpenSSH_9.2p1 Debian-2+deb12u3") == "Debian"
    assert discovery.os_hint("SSH-2.0-OpenSSH_8.7") == "Linux/Unix (OpenSSH)"


def _server(payload: bytes) -> tuple[socket.socket, int]:
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()

    def serve() -> None:
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            with conn:
                conn.sendall(payload)

    threading.Thread(target=serve, daemon=True).start()
    return server, server.getsockname()[1]


def test_scan_finds_ssh_servers_only() -> None:
    ssh, ssh_port = _server(b"SSH-2.0-OpenSSH_9.6p1 Ubuntu-3ubuntu13.5\r\n")
    web, web_port = _server(b"HTTP/1.1 400 Bad Request\r\n\r\n")
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    closed_port = closed.getsockname()[1]
    closed.close()
    try:
        progress: list[tuple[int, int]] = []
        hosts = discovery.scan(
            ["127.0.0.1"],
            [ssh_port, web_port, closed_port],
            timeout=1,
            on_progress=lambda d, t: progress.append((d, t)),
        )
    finally:
        ssh.close()
        web.close()
    assert [(h["port"], h["os_hint"]) for h in hosts] == [(ssh_port, "Ubuntu")]
    assert progress[-1] == (3, 3)
