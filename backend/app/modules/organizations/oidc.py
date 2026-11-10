"""OIDC discovery, bounded JWKS caching, and strict JWT validation.

Only a configured issuer and asymmetric signature algorithms are trusted. JWT
headers cannot supply key URLs or choose an arbitrary validation algorithm.
"""

from __future__ import annotations

import threading
import time
from typing import Any
from urllib.parse import urlsplit

import httpx
import jwt

from app.core.config import Settings
from app.core.errors import AuthenticationRequiredError, PlatformError


class IdentityProviderUnavailable(PlatformError):
    status_code = 503
    code = "identity_provider_unavailable"


class OIDCClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.issuer = str(settings.oidc_issuer_url)
        self._lock = threading.Lock()
        self._metadata: dict[str, Any] = {}
        self._keys: list[jwt.PyJWK] = []
        self._metadata_at = 0.0
        self._keys_at = 0.0

    def _safe_url(self, value: str) -> str:
        parsed = urlsplit(value)
        local = (
            not self.settings.is_production_like
            and parsed.scheme == "http"
            and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        )
        if (
            (parsed.scheme != "https" and not local)
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.fragment
        ):
            raise IdentityProviderUnavailable("Identity provider returned an unsafe endpoint")
        return value

    def _json(self, url: str, data: dict[str, str] | None = None) -> dict[str, Any]:
        try:
            with httpx.Client(timeout=10, follow_redirects=False) as client:
                response = (
                    client.get(self._safe_url(url)) if data is None else client.post(self._safe_url(url), data=data)
                )
                response.raise_for_status()
                if len(response.content) > 1_000_000:
                    raise ValueError("response too large")
                result = response.json()
                if not isinstance(result, dict):
                    raise ValueError("object required")
                return result
        except (httpx.HTTPError, ValueError) as exc:
            # Provider bodies may contain authorization codes or tokens.
            raise IdentityProviderUnavailable(
                "Identity provider request failed; check its configuration and availability"
            ) from exc

    def metadata(self) -> dict[str, Any]:
        with self._lock:
            if not self._metadata or time.monotonic() - self._metadata_at > 3600:
                metadata = self._json(self.issuer.rstrip("/") + "/.well-known/openid-configuration")
                if metadata.get("issuer") != self.issuer:
                    raise IdentityProviderUnavailable("Discovery issuer does not match OIDC_ISSUER_URL")
                for name in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
                    self._safe_url(str(metadata.get(name, "")))
                self._metadata, self._metadata_at = metadata, time.monotonic()
            return self._metadata

    def decode(self, token: str, *, audience: str, nonce: str | None = None) -> dict[str, Any]:
        try:
            if len(token) > 32_768:
                raise jwt.InvalidTokenError("oversize")
            header = jwt.get_unverified_header(token)
            algorithm = header.get("alg")
            kid = header.get("kid")
            if (
                algorithm not in {"RS256", "RS384", "RS512", "ES256", "ES384", "PS256"}
                or not isinstance(kid, str)
                or not kid
                or header.get("crit")
            ):
                raise jwt.InvalidTokenError("unsupported header")
            metadata = self.metadata()
            with self._lock:
                matching = [key for key in self._keys if key.key_id == kid]
                age = time.monotonic() - self._keys_at
                if not self._keys or age > 300 or (not matching and age > 5):
                    keyset = self._json(metadata["jwks_uri"])
                    self._keys = jwt.PyJWKSet.from_dict(keyset).keys
                    self._keys_at = time.monotonic()
                    matching = [key for key in self._keys if key.key_id == kid]
                matching = [
                    key for key in matching if key.public_key_use in {None, "sig"} and key.algorithm_name == algorithm
                ]
                if len(matching) != 1:
                    raise jwt.InvalidTokenError("unknown key")
                claims: dict[str, Any] = jwt.decode(
                    token,
                    matching[0].key,
                    algorithms=[algorithm],
                    audience=audience,
                    issuer=self.issuer,
                    leeway=30,
                    options={"require": ["iss", "sub", "aud", "exp", "iat"]},
                )
            subject = claims.get("sub")
            if not isinstance(subject, str) or not subject or len(self.issuer + "|" + subject) > 512:
                raise jwt.InvalidTokenError("invalid subject")
            if nonce is not None:
                if claims.get("nonce") != nonce or (
                    isinstance(claims["aud"], list) and len(claims["aud"]) > 1 and claims.get("azp") != audience
                ):
                    raise jwt.InvalidTokenError("invalid login binding")
                if claims.get("azp") is not None and claims["azp"] != audience:
                    raise jwt.InvalidTokenError("invalid authorized party")
            elif claims.get("token_use", "access") != "access":
                raise jwt.InvalidTokenError("not an access token")
            return claims
        except (jwt.PyJWTError, KeyError, TypeError, ValueError) as exc:
            raise AuthenticationRequiredError("The identity token is invalid or expired") from exc

    @property
    def callback_url(self) -> str:
        return self.settings.public_url.rstrip("/") + self.settings.api_prefix + "/auth/callback"

    def exchange(self, code: str, verifier: str) -> dict[str, Any]:
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.callback_url,
            "client_id": str(self.settings.oidc_client_id),
            "code_verifier": verifier,
        }
        if self.settings.oidc_client_secret:
            data["client_secret"] = self.settings.oidc_client_secret.get_secret_value()
        return self._json(self.metadata()["token_endpoint"], data)
