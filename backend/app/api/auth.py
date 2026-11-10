"""Shared authentication and permission enforcement for HTTP and WebSockets."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from urllib.parse import urlsplit

from starlette.requests import HTTPConnection

from app.core.config import AuthMode, Settings
from app.core.errors import AuthenticationRequiredError, PermissionDeniedError, PlatformError
from app.core.principal import Principal
from app.db.session import get_session_factory
from app.modules.auditing import service as audit
from app.modules.auditing.models import AuditOutcome
from app.modules.organizations.identity import identity_user, resolve_principal
from app.modules.organizations.models import BrowserSession, User
from app.modules.organizations.oidc import OIDCClient
from app.modules.organizations.policy import request_permission, require

SESSION_COOKIE = "kplatform_session"
CSRF_COOKIE = "kplatform_csrf"
ORG_COOKIE = "kplatform_organization"
PROJECT_COOKIE = "kplatform_project"
LOGIN_COOKIE = "kplatform_login"


def token_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def context_id(request: HTTPConnection, header: str, cookie: str) -> uuid.UUID | None:
    value = request.headers.get(header) or request.cookies.get(cookie)
    if not value:
        return None
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise PermissionDeniedError("Invalid organization or project context; select a valid context") from exc


class Authenticator(ABC):
    mode: AuthMode

    @abstractmethod
    def identity(self, request: HTTPConnection) -> User: ...

    def authenticate(self, request: HTTPConnection) -> Principal:
        user = self.identity(request)
        with get_session_factory()() as session:
            return resolve_principal(
                session,
                user,
                context_id(request, "X-Organization-ID", ORG_COOKIE),
                context_id(request, "X-Project-ID", PROJECT_COOKIE),
            )


class DevAuthenticator(Authenticator):
    mode = AuthMode.DEV

    def __init__(self, principal: Principal) -> None:
        self._principal = principal

    def identity(self, request: HTTPConnection) -> User:
        with get_session_factory()() as session:
            user = session.get(User, self._principal.user_id)
            if user is None or not user.is_active:
                raise AuthenticationRequiredError("Development administrator is unavailable")
            return user


class OIDCAuthenticator(Authenticator):
    mode = AuthMode.OIDC

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = OIDCClient(settings)

    def identity(self, request: HTTPConnection) -> User:
        authorization = request.headers.get("authorization")
        with get_session_factory()() as session:
            if authorization is not None:
                scheme, _, value = authorization.partition(" ")
                if scheme.lower() != "bearer" or not value:
                    raise AuthenticationRequiredError("A valid bearer access token is required")
                claims = self.client.decode(value, audience=str(self.settings.oidc_audience))
                return identity_user(session, self.settings, claims)
            cookie = request.cookies.get(SESSION_COOKIE, "")
            login = session.get(BrowserSession, token_hash(cookie)) if cookie else None
            if login is None or login.expires_at <= datetime.now(UTC):
                raise AuthenticationRequiredError("Sign in to continue")
            user = session.get(User, login.user_id)
            if user is None or not user.is_active:
                raise AuthenticationRequiredError("This platform account is disabled")
            if request.scope["type"] == "http" and request.scope["method"] not in {"GET", "HEAD", "OPTIONS"}:
                csrf = request.headers.get("x-csrf-token", "")
                origin = request.headers.get("origin")
                if (
                    not csrf
                    or not secrets.compare_digest(token_hash(csrf), login.csrf_hash)
                    or (origin is not None and origin != self.settings.public_url.rstrip("/"))
                ):
                    raise PermissionDeniedError("Session mutation requires a valid CSRF token and platform origin")
            if request.scope["type"] == "websocket":
                origin = request.headers.get("origin")
                expected = urlsplit(self.settings.public_url)
                if origin != f"{expected.scheme}://{expected.netloc}":
                    raise PermissionDeniedError("Browser terminal requires the platform origin")
            return user


def build_authenticator(settings: Settings, dev_principal: Principal | None) -> Authenticator:
    if settings.auth_mode is AuthMode.DEV:
        assert dev_principal is not None
        return DevAuthenticator(dev_principal)
    return OIDCAuthenticator(settings)


def get_principal(request: HTTPConnection) -> Principal:
    cached = getattr(request.state, "principal", None)
    if cached is not None:
        return cached
    try:
        principal = request.app.state.authenticator.authenticate(request)
        require(principal, request_permission(request))
    except PermissionDeniedError:
        if "principal" in locals():
            with get_session_factory()() as session:
                audit.record(
                    session,
                    principal=principal,
                    action="access.denied",
                    resource_type="api",
                    outcome=AuditOutcome.DENIED,
                    details={"permission": request_permission(request)},
                )
                session.commit()
        if request.scope["type"] == "websocket":
            from starlette.exceptions import WebSocketException

            raise WebSocketException(4403, "Permission denied") from None
        raise
    except PlatformError as exc:
        if request.scope["type"] == "websocket":
            from starlette.exceptions import WebSocketException

            raise WebSocketException(4401, "Authentication required") from exc
        raise
    request.state.principal = principal
    return principal
