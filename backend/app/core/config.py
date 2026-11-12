"""Application configuration.

All configuration comes from environment variables (optionally a ``.env`` file).
Settings are validated at startup; unsafe combinations (for example disabled
authentication in production) cause the process to refuse to start rather than
silently running insecurely.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from typing import Annotated

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from app import __version__


class Environment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


class AuthMode(StrEnum):
    # Development only: every request is attributed to a fixed local principal.
    DEV = "dev"
    # OIDC authorization-code login and JWT access tokens (Keycloak or compatible).
    OIDC = "oidc"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Kubernetes Platform"
    environment: Environment = Environment.DEVELOPMENT
    log_level: str = "INFO"
    log_json: bool = False

    # --- Data services -------------------------------------------------------
    database_url: str = "postgresql+psycopg://kplatform:kplatform-dev-only@localhost:5432/kplatform"
    database_pool_size: int = Field(default=10, ge=1)
    database_echo: bool = False
    redis_url: str = "redis://localhost:6379/0"

    # --- HTTP ----------------------------------------------------------------
    api_prefix: str = "/api/v1"
    cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["http://localhost:3000"])

    # --- Authentication ------------------------------------------------------
    auth_mode: AuthMode = AuthMode.DEV
    oidc_issuer_url: str | None = None
    oidc_audience: str | None = None
    oidc_client_id: str | None = None
    oidc_client_secret: SecretStr | None = None
    oidc_bootstrap_subject: str | None = None
    public_url: str = "http://localhost:3000"
    auth_session_seconds: int = Field(default=28_800, ge=300, le=86_400)

    # --- Secrets -------------------------------------------------------------
    # Comma-separated "key_id:fernet_key" pairs. The FIRST entry is used for new
    # encryptions; the remaining entries are kept so older ciphertexts can still be
    # decrypted during key rotation.
    secret_encryption_keys: SecretStr | None = None

    # --- Infrastructure --------------------------------------------------------
    # Network discovery only scans private, loopback and link-local ranges unless enabled.
    discovery_allow_public_ranges: bool = False
    machine_probe_interval_seconds: int = Field(default=30, ge=10, le=300)

    # --- Orchestration -------------------------------------------------------
    # When true, operations execute in-process instead of via Celery (tests only).
    orchestration_inline: bool = False
    operation_heartbeat_seconds: int = Field(default=15, ge=1)
    operation_stale_after_seconds: int = Field(default=120, ge=10)
    operation_pending_redispatch_seconds: int = Field(default=60, ge=5)
    operation_max_recoveries: int = Field(default=3, ge=0)

    @model_validator(mode="before")
    @classmethod
    def _split_cors(cls, data: dict) -> dict:
        value = data.get("cors_origins") if isinstance(data, dict) else None
        if isinstance(value, str):
            data["cors_origins"] = [o.strip() for o in value.split(",") if o.strip()]
        return data

    @model_validator(mode="after")
    def _enforce_safe_configuration(self) -> Settings:
        protected = self.environment in (Environment.STAGING, Environment.PRODUCTION)
        if protected and self.auth_mode is AuthMode.DEV:
            raise ValueError("AUTH_MODE=dev is not permitted when ENVIRONMENT is staging or production")
        if self.secret_encryption_keys is None:
            raise ValueError("SECRET_ENCRYPTION_KEYS must be set (generate one with: python -m app.cli generate-key)")
        if protected and self.orchestration_inline:
            raise ValueError("ORCHESTRATION_INLINE is a test-only setting")
        if self.auth_mode is AuthMode.OIDC:
            from urllib.parse import urlsplit

            if not (self.oidc_issuer_url and self.oidc_audience and self.oidc_client_id):
                raise ValueError("AUTH_MODE=oidc requires OIDC_ISSUER_URL, OIDC_AUDIENCE and OIDC_CLIENT_ID")
            for value in (self.oidc_issuer_url, self.public_url):
                url = urlsplit(value)
                local_http = (
                    not protected and url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"}
                )
                if (
                    (url.scheme != "https" and not local_http)
                    or not url.hostname
                    or url.username
                    or url.password
                    or url.query
                    or url.fragment
                ):
                    raise ValueError(
                        "OIDC URLs require HTTPS (loopback HTTP is development-only), "
                        "without credentials, query or fragment"
                    )
            if urlsplit(self.public_url).path not in {"", "/"}:
                raise ValueError("PUBLIC_URL must be an origin without a path")
            if self.oidc_audience == self.oidc_client_id:
                raise ValueError("OIDC_AUDIENCE must identify the API separately from OIDC_CLIENT_ID")
        return self

    @property
    def version(self) -> str:
        return __version__

    @property
    def is_production_like(self) -> bool:
        return self.environment in (Environment.STAGING, Environment.PRODUCTION)


@lru_cache
def get_settings() -> Settings:
    return Settings()
