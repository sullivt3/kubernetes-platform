"""Authentication seam.

Every route obtains its :class:`Principal` from :func:`get_principal`. Swapping the
development authenticator for OIDC (Phase 7) changes only the ``Authenticator``
built at startup — no route changes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from starlette.requests import HTTPConnection

from app.core.config import AuthMode, Settings
from app.core.errors import NotImplementedYetError
from app.core.principal import Principal


class Authenticator(ABC):
    mode: AuthMode

    @abstractmethod
    def authenticate(self, request: HTTPConnection) -> Principal: ...


class DevAuthenticator(Authenticator):
    """Attributes every request to the local development administrator.

    Settings validation makes it impossible to enable in staging or production.
    """

    mode = AuthMode.DEV

    def __init__(self, principal: Principal) -> None:
        self._principal = principal

    def authenticate(self, request: HTTPConnection) -> Principal:
        return self._principal


def build_authenticator(settings: Settings, dev_principal: Principal | None) -> Authenticator:
    if settings.auth_mode is AuthMode.DEV:
        assert dev_principal is not None
        return DevAuthenticator(dev_principal)
    raise NotImplementedYetError(
        "AUTH_MODE=oidc is not implemented yet (planned for Phase 7). Use AUTH_MODE=dev for local development."
    )


def get_principal(request: HTTPConnection) -> Principal:
    """Resolves the caller for HTTP requests and WebSockets alike."""
    authenticator: Authenticator = request.app.state.authenticator
    return authenticator.authenticate(request)
