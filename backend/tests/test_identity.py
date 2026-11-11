"""Phase 7 boundaries through real authentication, API dependencies and PostgreSQL.

Only the identity provider HTTP transport is simulated. JWT signing/verification,
sessions, CSRF, memberships, scoped database access and API policies are real.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import select
from starlette.websockets import WebSocketDisconnect

from app.api.auth import CSRF_COOKIE, SESSION_COOKIE, OIDCAuthenticator, token_hash
from app.core.config import Settings, get_settings
from app.core.errors import AuthenticationRequiredError
from app.db.session import get_session_factory
from app.main import create_app
from app.modules.organizations.models import (
    BrowserSession,
    Organization,
    OrganizationMembership,
    Project,
    ProjectMembership,
    User,
)
from app.modules.organizations.service import ensure_default_tenancy

API = "/api/v1"
ISSUER = "http://127.0.0.1:19090/realms/test"


@dataclass
class IdentityHarness:
    client: TestClient
    settings: Settings
    key: rsa.RSAPrivateKey
    organization_id: uuid.UUID
    project_id: uuid.UUID
    user_id: uuid.UUID

    def token(self, **overrides: object) -> str:
        claims = {
            "iss": ISSUER,
            "sub": "alice",
            "aud": "platform-api",
            "iat": int(time.time()),
            "exp": int(time.time()) + 300,
            **overrides,
        }
        return jwt.encode(claims, self.key, algorithm="RS256", headers={"kid": "test-key"})

    def bearer(self, **overrides: object) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token(**overrides)}"}

    def role(self, role: str) -> None:
        with get_session_factory()() as session:
            member = session.scalar(
                select(OrganizationMembership).where(OrganizationMembership.user_id == self.user_id)
            )
            assert member is not None
            member.role = role
            session.commit()

    def browser(self) -> dict[str, str]:
        token, csrf = "browser-session-test-token", "browser-csrf-test-token"
        with get_session_factory()() as session:
            session.add(
                BrowserSession(
                    token_hash=token_hash(token),
                    csrf_hash=token_hash(csrf),
                    user_id=self.user_id,
                    expires_at=datetime.now(UTC) + timedelta(minutes=10),
                )
            )
            session.commit()
        self.client.cookies.set(SESSION_COOKIE, token, domain="localhost.local", path="/")
        self.client.cookies.set(CSRF_COOKIE, csrf, domain="localhost.local", path="/")
        return {"X-CSRF-Token": csrf, "Origin": self.settings.public_url}


@pytest.fixture
def identity(monkeypatch: pytest.MonkeyPatch) -> Iterator[IdentityHarness]:
    monkeypatch.setenv("AUTH_MODE", "oidc")
    monkeypatch.setenv("OIDC_ISSUER_URL", ISSUER)
    monkeypatch.setenv("OIDC_CLIENT_ID", "platform-web")
    monkeypatch.setenv("OIDC_AUDIENCE", "platform-api")
    get_settings.cache_clear()
    settings = get_settings()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = {
        **json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key())),
        "kid": "test-key",
        "use": "sig",
        "alg": "RS256",
    }

    with get_session_factory()() as session:
        tenancy = ensure_default_tenancy(session)
        user = User(subject=f"{ISSUER}|alice", display_name="Alice")
        session.add(user)
        session.flush()
        session.add(OrganizationMembership(organization_id=tenancy.organization.id, user_id=user.id, role="operator"))
        session.commit()
        ids = tenancy.organization.id, tenancy.project.id, user.id

    def provider(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(
                200,
                json={
                    "issuer": ISSUER,
                    "authorization_endpoint": ISSUER + "/authorize",
                    "token_endpoint": ISSUER + "/token",
                    "jwks_uri": ISSUER + "/keys",
                },
            )
        if request.url.path.endswith("/keys"):
            return httpx.Response(200, json={"keys": [jwk]})
        raise AssertionError(f"Unexpected identity-provider request: {request.method} {request.url}")

    original_client = httpx.Client
    try:
        with TestClient(create_app(), base_url=settings.public_url) as client:
            monkeypatch.setattr(
                httpx,
                "Client",
                lambda **kwargs: original_client(
                    transport=httpx.MockTransport(provider),
                    **kwargs,
                ),
            )
            yield IdentityHarness(client, settings, key, *ids)
    finally:
        get_settings.cache_clear()


def test_unauthenticated_api_and_terminal_are_rejected(identity: IdentityHarness) -> None:
    assert identity.client.get(f"{API}/auth/session").json()["authenticated"] is False
    assert identity.client.get(f"{API}/clusters").status_code == 401
    with (
        pytest.raises(WebSocketDisconnect) as closed,
        identity.client.websocket_connect(f"{API}/clusters/{uuid.uuid4()}/pods/default/web/exec"),
    ):
        pass
    assert closed.value.code == 4401


@pytest.mark.parametrize(
    "claims",
    [
        {"aud": "platform-web"},
        {"iss": "https://untrusted.example"},
        {"exp": 1},
        {"iat": int(time.time()) + 3600},
        {"token_use": "id"},
        {"sub": ""},
    ],
)
def test_invalid_access_tokens_are_rejected(identity: IdentityHarness, claims: dict) -> None:
    assert identity.client.get(f"{API}/clusters", headers=identity.bearer(**claims)).status_code == 401


def test_signature_and_algorithm_cannot_be_chosen_by_attacker(identity: IdentityHarness) -> None:
    token = jwt.encode(
        {"iss": ISSUER, "sub": "alice", "aud": "platform-api", "exp": time.time() + 300},
        "attacker-controlled-secret-long-enough",
        algorithm="HS256",
        headers={"kid": "test-key"},
    )
    assert identity.client.get(f"{API}/clusters", headers={"Authorization": f"Bearer {token}"}).status_code == 401
    stranger = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = jwt.encode(
        jwt.decode(identity.token(), options={"verify_signature": False}),
        stranger,
        algorithm="RS256",
        headers={"kid": "test-key"},
    )
    assert identity.client.get(f"{API}/clusters", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_identity_claims_do_not_grant_membership_or_admin(identity: IdentityHarness) -> None:
    headers = identity.bearer(sub="new-user", roles=["platform-admin"], email="admin@example.com", email_verified=False)
    response = identity.client.get(f"{API}/auth/session", headers=headers).json()
    assert response["authenticated"] is True
    assert response["user"]["is_platform_admin"] is False
    assert response["context"] is None
    assert identity.client.get(f"{API}/clusters", headers=headers).status_code == 403
    with get_session_factory()() as session:
        user = session.scalar(select(User).where(User.subject == f"{ISSUER}|new-user"))
        assert user is not None and user.email is None


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/credentials", {"name": "forbidden", "kind": "ssh_password", "secret_value": "hidden"}),
        ("POST", "/clusters", {}),
        ("POST", "/machines", {}),
        ("POST", "/system/self-check", None),
        ("POST", "/operations/00000000-0000-0000-0000-000000000001/cancel", None),
        ("PUT", "/administration/members", {"subject": f"{ISSUER}|alice", "role": "org-admin"}),
        ("GET", "/administration/users", None),
        ("GET", "/audit-events", None),
        ("POST", "/clusters/00000000-0000-0000-0000-000000000001/secrets/default/password/reveal", None),
        ("POST", "/clusters/00000000-0000-0000-0000-000000000001/apply", {"manifest": "", "dry_run": False}),
    ],
)
def test_viewer_cannot_write_administer_or_reveal(
    identity: IdentityHarness, method: str, path: str, body: dict | None
) -> None:
    identity.role("viewer")
    response = identity.client.request(method, API + path, headers=identity.bearer(), json=body)
    assert response.status_code == 403, response.text
    assert identity.client.get(f"{API}/clusters", headers=identity.bearer()).status_code == 200


def test_viewer_terminal_denied_before_cluster_access(identity: IdentityHarness) -> None:
    identity.role("viewer")
    with (
        pytest.raises(WebSocketDisconnect) as closed,
        identity.client.websocket_connect(
            f"{API}/clusters/{uuid.uuid4()}/pods/default/web/exec",
            headers=identity.bearer(),
        ),
    ):
        pass
    assert closed.value.code == 4403


@pytest.mark.parametrize(
    "headers",
    [{}, {"X-CSRF-Token": "wrong"}, {"X-CSRF-Token": "browser-csrf-test-token", "Origin": "https://evil.example"}],
)
def test_browser_mutations_require_csrf_and_trusted_origin(identity: IdentityHarness, headers: dict) -> None:
    identity.browser()
    assert identity.client.post(f"{API}/auth/logout", headers=headers).status_code == 403
    assert identity.client.get(f"{API}/auth/session").json()["authenticated"] is True


def test_logout_revokes_session_and_clears_cookies(identity: IdentityHarness) -> None:
    headers = identity.browser()
    assert identity.client.post(f"{API}/auth/logout", headers=headers).status_code == 204
    assert SESSION_COOKIE not in identity.client.cookies
    identity.client.cookies.set(SESSION_COOKIE, "browser-session-test-token")
    assert identity.client.get(f"{API}/clusters").status_code == 401


def test_expired_session_and_disabled_user_are_rejected(identity: IdentityHarness) -> None:
    identity.browser()
    with get_session_factory()() as session:
        login = session.get(BrowserSession, token_hash("browser-session-test-token"))
        assert login is not None
        login.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()
    assert identity.client.get(f"{API}/clusters").status_code == 401
    with get_session_factory()() as session:
        user = session.get(User, identity.user_id)
        assert user is not None
        user.is_active = False
        session.commit()
    assert identity.client.get(f"{API}/clusters", headers=identity.bearer()).status_code == 401


def test_membership_changes_take_effect_on_next_request(identity: IdentityHarness) -> None:
    identity.browser()
    assert identity.client.get(f"{API}/clusters").status_code == 200
    with get_session_factory()() as session:
        member = session.scalar(
            select(OrganizationMembership).where(OrganizationMembership.user_id == identity.user_id)
        )
        assert member is not None
        session.delete(member)
        session.commit()
    assert identity.client.get(f"{API}/clusters").status_code == 403


def test_project_grants_and_context_headers_are_selectors_not_authority(identity: IdentityHarness) -> None:
    identity.role("member")
    headers = identity.bearer()
    assert identity.client.get(f"{API}/clusters", headers=headers).status_code == 403
    headers["X-Project-ID"] = str(identity.project_id)
    assert identity.client.get(f"{API}/clusters", headers=headers).status_code == 403
    with get_session_factory()() as session:
        session.add(ProjectMembership(project_id=identity.project_id, user_id=identity.user_id, role="viewer"))
        session.commit()
    assert identity.client.get(f"{API}/clusters", headers=headers).status_code == 200
    headers["X-Organization-ID"] = str(uuid.uuid4())
    assert identity.client.get(f"{API}/clusters", headers=headers).status_code == 403


def test_context_switch_validates_membership_and_csrf(identity: IdentityHarness) -> None:
    headers = identity.browser()
    body = {"organization_id": str(identity.organization_id), "project_id": str(identity.project_id)}
    assert identity.client.post(f"{API}/auth/context", json=body).status_code == 403
    assert identity.client.post(f"{API}/auth/context", json=body, headers=headers).status_code == 204
    assert identity.client.get(f"{API}/auth/session").json()["context"]["project_id"] == str(identity.project_id)
    body["project_id"] = str(uuid.uuid4())
    assert identity.client.post(f"{API}/auth/context", json=body, headers=headers).status_code == 403


def test_project_lists_details_and_counts_are_isolated(identity: IdentityHarness) -> None:
    headers = identity.bearer()
    with get_session_factory()() as session:
        sibling = Project(organization_id=identity.organization_id, slug="sibling", name="Sibling")
        session.add(sibling)
        session.commit()
        sibling_id = sibling.id
    created = []
    for name, project_id in [("one", identity.project_id), ("two", sibling_id), ("wide", None)]:
        scoped = {**headers, **({"X-Project-ID": str(project_id)} if project_id else {})}
        response = identity.client.post(
            f"{API}/credentials",
            headers=scoped,
            json={
                "name": name,
                "kind": "ssh_password",
                "secret_value": "never-return-this",
            },
        )
        assert response.status_code == 201, response.text
        created.append(response.json()["id"])
    scoped = {**headers, "X-Project-ID": str(identity.project_id)}
    listing = identity.client.get(f"{API}/credentials", headers=scoped).json()
    assert listing["total"] == 1
    assert [row["id"] for row in listing["items"]] == [created[0]]
    assert identity.client.get(f"{API}/credentials/{created[0]}", headers=scoped).status_code == 200
    for hidden in created[1:]:
        assert identity.client.get(f"{API}/credentials/{hidden}", headers=scoped).status_code == 404
        assert identity.client.delete(f"{API}/credentials/{hidden}?confirm=two", headers=scoped).status_code == 404
    assert identity.client.get(f"{API}/credentials", headers=headers).json()["total"] == 3


def test_organization_isolation_and_project_credential_links(identity: IdentityHarness) -> None:
    from app.core.errors import PermissionDeniedError
    from app.modules.infrastructure.models import ProviderConnection
    from app.modules.organizations.identity import resolve_principal
    from app.modules.organizations.scope import scope_session
    from app.modules.providers.types import ProviderType

    headers = {**identity.bearer(), "X-Project-ID": str(identity.project_id)}
    credential = identity.client.post(
        f"{API}/credentials",
        headers=headers,
        json={
            "name": "project-key",
            "kind": "ssh_password",
            "secret_value": "hidden",
        },
    ).json()
    with get_session_factory()() as session:
        org = Organization(slug="foreign", name="Foreign")
        sibling = Project(organization_id=identity.organization_id, slug="sibling", name="Sibling")
        session.add_all([org, sibling])
        session.flush()
        session.add(OrganizationMembership(organization_id=org.id, user_id=identity.user_id, role="operator"))
        session.commit()
        foreign_id, sibling_id = org.id, sibling.id
    foreign = {**identity.bearer(), "X-Organization-ID": str(foreign_id)}
    assert identity.client.get(f"{API}/credentials", headers=foreign).json()["total"] == 0
    assert identity.client.get(f"{API}/credentials/{credential['id']}", headers=foreign).status_code == 404
    # Even an organization-wide session cannot link resources belonging to different projects.
    with get_session_factory()() as session:
        user = session.get(User, identity.user_id)
        assert user is not None
        principal = resolve_principal(session, user, identity.organization_id, None)
        scope_session(session, principal)
        session.add(
            ProviderConnection(
                organization_id=identity.organization_id,
                project_id=sibling_id,
                name="cross-project",
                provider_type=ProviderType.LOCAL,
                config={},
                credential_id=uuid.UUID(credential["id"]),
            )
        )
        with pytest.raises(PermissionDeniedError, match="same organization and project"):
            session.commit()


def test_admin_changes_revoke_sessions_and_preserve_last_admin(identity: IdentityHarness) -> None:
    headers = identity.browser()
    with get_session_factory()() as session:
        alice = session.get(User, identity.user_id)
        assert alice is not None
        alice.is_platform_admin = True
        bob = User(subject=f"{ISSUER}|bob", display_name="Bob", is_platform_admin=True)
        session.add(bob)
        session.commit()
        bob_id = bob.id
    # A second administrator may disable Alice; both cookie and bearer access must stop.
    response = identity.client.put(
        f"{API}/administration/users/{identity.user_id}",
        headers=identity.bearer(sub="bob"),
        json={"is_active": False, "is_platform_admin": False},
    )
    assert response.status_code == 200, response.text
    assert identity.client.get(f"{API}/clusters").status_code == 401
    assert identity.client.get(f"{API}/clusters", headers=identity.bearer()).status_code == 401
    response = identity.client.put(
        f"{API}/administration/users/{bob_id}",
        headers=identity.bearer(sub="bob"),
        json={"is_active": False, "is_platform_admin": False},
    )
    assert response.status_code == 409
    assert identity.client.post(f"{API}/auth/logout", headers=headers).status_code == 401


def test_project_operation_details_logs_and_streams_are_isolated(identity: IdentityHarness) -> None:
    from app.orchestration.models import LogLevel, Operation, OperationLog, OperationStatus

    with get_session_factory()() as session:
        sibling = Project(organization_id=identity.organization_id, slug="sibling", name="Sibling")
        session.add(sibling)
        session.flush()
        operations = [
            Operation(
                organization_id=identity.organization_id,
                project_id=project_id,
                type="test.completed",
                title=title,
                status=OperationStatus.SUCCEEDED,
                requested_by_id=identity.user_id,
                requested_by_subject=f"{ISSUER}|alice",
                params={},
            )
            for title, project_id in [("allowed", identity.project_id), ("hidden", sibling.id)]
        ]
        session.add_all(operations)
        session.flush()
        for op in operations:
            session.add(OperationLog(operation_id=op.id, level=LogLevel.INFO, message=op.title))
        session.commit()
        allowed_id, hidden_id = (op.id for op in operations)
    headers = {**identity.bearer(), "X-Project-ID": str(identity.project_id)}
    listed = identity.client.get(f"{API}/operations", headers=headers).json()
    assert listed["total"] == 1 and listed["items"][0]["id"] == str(allowed_id)
    for suffix in ["", "/logs", "/events"]:
        assert identity.client.get(f"{API}/operations/{hidden_id}{suffix}", headers=headers).status_code == 404
    stream = identity.client.get(f"{API}/operations/{allowed_id}/events", headers=headers)
    assert stream.status_code == 200 and "allowed" in stream.text and "hidden" not in stream.text


def test_worker_session_inherits_operation_project(identity: IdentityHarness) -> None:
    from pydantic import BaseModel

    from app.core.secrets import get_secret_store
    from app.modules.credentials.models import Credential, CredentialKind
    from app.orchestration.context import StepContext
    from app.orchestration.models import Operation, OperationStatus

    class WorkerParams(BaseModel):
        pass

    with get_session_factory()() as session:
        operation = Operation(
            organization_id=identity.organization_id,
            project_id=identity.project_id,
            type="test.worker",
            title="Worker scope",
            status=OperationStatus.RUNNING,
            requested_by_id=identity.user_id,
            requested_by_subject=f"{ISSUER}|alice",
            params={},
        )
        session.add(operation)
        session.commit()
        operation_id = operation.id
    context = StepContext(
        operation_id=operation_id,
        organization_id=identity.organization_id,
        step_key="create",
        attempt=1,
        params=WorkerParams(),
        outputs={},
        session_factory=get_session_factory(),
        settings=identity.settings,
        secret_store=get_secret_store(),
        redactions=set(),
        log_fn=lambda *_: None,
        progress_fn=lambda _: None,
    )
    with context.db() as session:
        sealed = get_secret_store().seal(b"worker-secret")
        credential = Credential(
            organization_id=identity.organization_id,
            name="worker-created",
            kind=CredentialKind.SSH_PASSWORD,
            secret_backend="fernet",
            key_id=sealed.key_id,
            ciphertext=sealed.ciphertext,
        )
        session.add(credential)
        session.flush()
        assert credential.project_id == identity.project_id
        credential_id = credential.id
    with get_session_factory()() as session:
        assert session.get(Credential, credential_id).project_id == identity.project_id


def test_oidc_nonce_binding(identity: IdentityHarness) -> None:
    authenticator = identity.client.app.state.authenticator
    assert isinstance(authenticator, OIDCAuthenticator)
    token = identity.token(aud="platform-web", nonce="expected")
    assert authenticator.client.decode(token, audience="platform-web", nonce="expected")["sub"] == "alice"
    with pytest.raises(AuthenticationRequiredError):
        authenticator.client.decode(token, audience="platform-web", nonce="different")


def test_login_uses_pkce_and_callback_cannot_be_replayed(
    identity: IdentityHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    response = identity.client.get(f"{API}/auth/login", follow_redirects=False)
    assert response.status_code == 303
    query = parse_qs(urlsplit(response.headers["location"]).query)
    assert query["code_challenge_method"] == ["S256"]
    assert query["redirect_uri"] == [identity.settings.public_url + API + "/auth/callback"]
    authenticator = identity.client.app.state.authenticator
    assert isinstance(authenticator, OIDCAuthenticator)

    def exchange(code: str, verifier: str) -> dict:
        import base64
        import hashlib

        assert code == "authorization-code"
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        assert query["code_challenge"] == [challenge]
        return {"id_token": identity.token(aud="platform-web", nonce=query["nonce"][0])}

    monkeypatch.setattr(authenticator.client, "exchange", exchange)
    params = {"code": "authorization-code", "state": query["state"][0]}
    assert identity.client.get(f"{API}/auth/callback", params={**params, "state": "wrong"}).status_code == 401
    callback = identity.client.get(f"{API}/auth/callback", params=params, follow_redirects=False)
    assert callback.status_code == 303
    assert identity.client.get(f"{API}/auth/session").json()["authenticated"] is True
    identity.client.cookies.set("kplatform_login", params["state"])
    assert identity.client.get(f"{API}/auth/callback", params=params).status_code == 401
    with get_session_factory()() as session:
        stored = session.scalar(select(BrowserSession).where(BrowserSession.user_id == identity.user_id))
        assert stored is not None
        assert stored.token_hash != identity.client.cookies.get(SESSION_COOKIE)
