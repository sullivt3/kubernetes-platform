"""Browser OIDC login, local session logout, and validated tenant selection."""

from __future__ import annotations

import base64
import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import delete

from app.api.auth import (
    CSRF_COOKIE,
    LOGIN_COOKIE,
    ORG_COOKIE,
    PROJECT_COOKIE,
    SESSION_COOKIE,
    OIDCAuthenticator,
    context_id,
    token_hash,
)
from app.core.config import get_settings
from app.core.errors import AuthenticationRequiredError, PermissionDeniedError
from app.core.secrets import SealedSecret, get_secret_store
from app.db.session import get_session_factory
from app.modules.auditing import service as audit
from app.modules.organizations.identity import available_contexts, identity_user, resolve_principal
from app.modules.organizations.models import BrowserSession, LoginAttempt
from app.modules.organizations.policy import permissions

router = APIRouter(prefix="/auth", tags=["authentication"])


def _cookie(response: Response, name: str, value: str, seconds: int, *, http_only: bool = True) -> None:
    response.set_cookie(
        name,
        value,
        max_age=seconds,
        httponly=http_only,
        secure=get_settings().public_url.startswith("https://"),
        samesite="lax",
        path="/",
    )


@router.get("/session")
def current_session(request: Request) -> dict[str, Any]:
    settings = get_settings()
    authenticator = request.app.state.authenticator
    base: dict[str, Any] = {"auth_mode": settings.auth_mode.value, "login_url": settings.api_prefix + "/auth/login"}
    try:
        user = authenticator.identity(request)
    except AuthenticationRequiredError:
        return {**base, "authenticated": False}
    with get_session_factory()() as session:
        contexts = available_contexts(session, user)
        selected = None
        error = None
        try:
            principal = resolve_principal(
                session,
                user,
                context_id(request, "X-Organization-ID", ORG_COOKIE),
                context_id(request, "X-Project-ID", PROJECT_COOKIE),
            )
            selected = {
                "organization_id": str(principal.organization_id),
                "project_id": str(principal.project_id) if principal.project_id else None,
                "roles": sorted(principal.roles),
                "permissions": sorted(permissions(principal)),
            }
        except PermissionDeniedError as exc:
            error = exc.message
    return {
        **base,
        "authenticated": True,
        "user": {
            "id": str(user.id),
            "subject": user.subject,
            "display_name": user.display_name,
            "is_platform_admin": user.is_platform_admin,
        },
        "organizations": contexts,
        "context": selected,
        "context_error": error,
    }


@router.get("/login")
def login(request: Request) -> RedirectResponse:
    authenticator = request.app.state.authenticator
    if not isinstance(authenticator, OIDCAuthenticator):
        return RedirectResponse("/", status_code=303)
    # Rate-limit login starts using shared Redis, including across API workers.
    from redis import Redis
    from redis.exceptions import RedisError

    from app.modules.organizations.oidc import IdentityProviderUnavailable

    client_ip = request.client.host if request.client else "unknown"
    bucket = f"auth:login:{token_hash(client_ip)}:{int(datetime.now(UTC).timestamp()) // 60}"
    try:
        with Redis.from_url(get_settings().redis_url, socket_connect_timeout=2, socket_timeout=2) as redis:
            with redis.pipeline(transaction=True) as pipe:
                count, _ = pipe.incr(bucket).expire(bucket, 120).execute()
            if count > 30:
                from fastapi import HTTPException

                raise HTTPException(429, "Too many login attempts; retry in a minute")
    except RedisError as exc:
        raise IdentityProviderUnavailable("Login rate limiter is unavailable") from exc
    metadata = authenticator.client.metadata()
    state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
    sealed = get_secret_store().seal(verifier.encode())
    now = datetime.now(UTC)
    with get_session_factory()() as session:
        session.execute(delete(LoginAttempt).where(LoginAttempt.expires_at <= now))
        session.execute(delete(BrowserSession).where(BrowserSession.expires_at <= now))
        session.add(
            LoginAttempt(
                state_hash=token_hash(state),
                nonce=nonce,
                verifier_key_id=sealed.key_id,
                verifier_ciphertext=sealed.ciphertext,
                expires_at=now + timedelta(minutes=5),
            )
        )
        session.commit()
    params = {
        "response_type": "code",
        "client_id": str(get_settings().oidc_client_id),
        "redirect_uri": authenticator.client.callback_url,
        "scope": "openid profile email",
        "state": state,
        "nonce": nonce,
        "code_challenge": base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode(),
        "code_challenge_method": "S256",
    }
    endpoint = metadata["authorization_endpoint"]
    response = RedirectResponse(endpoint + ("&" if "?" in endpoint else "?") + urlencode(params), status_code=303)
    _cookie(response, LOGIN_COOKIE, state, 300)
    return response


@router.get("/callback")
def callback(
    request: Request, state: str = Query(max_length=256), code: str = Query(default="", max_length=4096)
) -> RedirectResponse:
    authenticator = request.app.state.authenticator
    expected = request.cookies.get(LOGIN_COOKIE, "")
    if (
        not isinstance(authenticator, OIDCAuthenticator)
        or not code
        or not expected
        or not secrets.compare_digest(state, expected)
    ):
        raise AuthenticationRequiredError("Login was cancelled or its browser binding is invalid; start again")
    with get_session_factory()() as session:
        # Lock and consume before the token exchange; parallel/replayed callbacks fail.
        attempt = session.get(LoginAttempt, token_hash(state), with_for_update=True)
        if attempt is None or attempt.expires_at <= datetime.now(UTC):
            raise AuthenticationRequiredError("Login expired or was already used; start again")
        verifier = (
            get_secret_store().unseal(SealedSecret(attempt.verifier_key_id, attempt.verifier_ciphertext)).decode()
        )
        nonce = attempt.nonce
        session.delete(attempt)
        session.commit()
        tokens = authenticator.client.exchange(code, verifier)
        claims = authenticator.client.decode(
            str(tokens.get("id_token", "")), audience=str(get_settings().oidc_client_id), nonce=nonce
        )
        user = identity_user(session, get_settings(), claims)
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        expires = get_settings().auth_session_seconds
        session.add(
            BrowserSession(
                token_hash=token_hash(token),
                csrf_hash=token_hash(csrf),
                user_id=user.id,
                expires_at=datetime.now(UTC) + timedelta(seconds=expires),
            )
        )
        # A user awaiting membership can log in without being granted tenant access.
        from app.core.principal import Principal
        from app.modules.organizations.service import ensure_default_tenancy

        tenancy = ensure_default_tenancy(session)
        actor = Principal(user.id, user.subject, user.display_name, tenancy.organization.id)
        audit.record(session, principal=actor, action="auth.login", resource_type="user", resource_id=user.id)
        session.commit()
    response = RedirectResponse("/", status_code=303)
    _cookie(response, SESSION_COOKIE, token, expires)
    _cookie(response, CSRF_COOKIE, csrf, expires, http_only=False)
    for name in (LOGIN_COOKIE, ORG_COOKIE, PROJECT_COOKIE):
        response.delete_cookie(name, path="/")
    return response


@router.post("/logout", status_code=204)
def logout(request: Request, response: Response) -> None:
    request.app.state.authenticator.identity(request)  # Includes CSRF validation.
    cookie = request.cookies.get(SESSION_COOKIE)
    if cookie:
        with get_session_factory()() as session:
            session.execute(delete(BrowserSession).where(BrowserSession.token_hash == token_hash(cookie)))
            session.commit()
    for name in (SESSION_COOKIE, CSRF_COOKIE, LOGIN_COOKIE, ORG_COOKIE, PROJECT_COOKIE):
        response.delete_cookie(name, path="/")


class ContextBody(BaseModel):
    organization_id: uuid.UUID
    project_id: uuid.UUID | None = None


@router.post("/context", status_code=204)
def select_context(body: ContextBody, request: Request, response: Response) -> None:
    user = request.app.state.authenticator.identity(request)
    with get_session_factory()() as session:
        resolve_principal(session, user, body.organization_id, body.project_id)
    _cookie(response, ORG_COOKIE, str(body.organization_id), get_settings().auth_session_seconds)
    if body.project_id:
        _cookie(response, PROJECT_COOKIE, str(body.project_id), get_settings().auth_session_seconds)
    else:
        response.delete_cookie(PROJECT_COOKIE, path="/")
