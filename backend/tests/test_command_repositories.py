import pytest
from pydantic import ValidationError

from app.core.errors import InvalidStateError
from app.modules.command_center import repositories
from tests.test_command_sessions import base, env, get_row  # noqa: F401


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "127.0.0.1",
        "*.example.com",
        "https://example.com",
        "example.com:443",
        "kubernetes.default.svc",
        "metadata.google.internal",
        "example.com/path",
    ],
)
def test_repository_hosts_are_exact_public_dns_names(host):
    with pytest.raises(ValidationError):
        repositories.Policy(hosts=[host])


@pytest.mark.parametrize(
    "address", ["127.0.0.1", "169.254.169.254", "192.168.18.11", "10.0.0.1", "::1", "::ffff:127.0.0.1", "224.0.0.1"]
)
def test_repository_resolution_refuses_private_metadata_and_mapped_addresses(monkeypatch, address):
    monkeypatch.setattr(repositories, "resolve", lambda host: [(None, None, None, None, (address, 443))])
    with pytest.raises(InvalidStateError):
        repositories.pinned(["charts.example.com"])


def test_repository_access_is_opt_in_pinned_and_revoked_on_policy_change(env, monkeypatch):
    client, _, store = env
    root = base(env)
    monkeypatch.setattr(repositories, "resolve", lambda host: [(None, None, None, None, ("93.184.216.34", 443))])
    result = client.post(
        root, json={"confirm": "session-test", "acknowledge_cluster_admin": True, "repository_access": True}
    )
    assert result.status_code == 409
    assert client.put(root + "/repository-policy", json={"hosts": ["charts.example.com"]}).status_code == 200
    result = client.post(
        root, json={"confirm": "session-test", "acknowledge_cluster_admin": True, "repository_access": True}
    )
    assert result.status_code == 202
    row = result.json()
    assert row["status"] == "ready"
    pod = store.objects["/api/v1/namespaces/" + row["namespace"] + "/pods/runtime"]
    assert pod["spec"]["hostAliases"] == [{"ip": "93.184.216.34", "hostnames": ["charts.example.com"]}]
    policy = store.objects["/apis/networking.k8s.io/v1/namespaces/" + row["namespace"] + "/networkpolicies/api-only"]
    assert policy["spec"]["egress"][-1] == {
        "to": [{"ipBlock": {"cidr": "93.184.216.34/32"}}],
        "ports": [{"protocol": "TCP", "port": 443}],
    }
    assert client.put(root + "/repository-policy", json={"hosts": []}).status_code == 200
    assert get_row(row).status == "closed"


def test_mixed_public_private_dns_answer_fails_before_credentials(env, monkeypatch):
    client, _, store = env
    root = base(env)
    client.put(root + "/repository-policy", json={"hosts": ["charts.example.com"]})
    monkeypatch.setattr(
        repositories,
        "resolve",
        lambda host: [(None, None, None, None, (ip, 443)) for ip in ["93.184.216.34", "127.0.0.1"]],
    )
    result = client.post(
        root, json={"confirm": "session-test", "acknowledge_cluster_admin": True, "repository_access": True}
    )
    assert result.status_code == 202 and result.json()["status"] == "closed"
    assert not store.tokens
