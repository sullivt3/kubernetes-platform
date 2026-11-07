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
    # OpenID Connect bearer-token validation (Keycloak or compatible). Planned for Phase 7.
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

    # --- Secrets -------------------------------------------------------------
    # Comma-separated "key_id:fernet_key" pairs. The FIRST entry is used for new
    # encryptions; the remaining entries are kept so older ciphertexts can still be
    # decrypted during key rotation.
    secret_encryption_keys: SecretStr | None = None

    # --- Infrastructure --------------------------------------------------------
    # Network discovery only scans private, loopback and link-local ranges unless enabled.
    discovery_allow_public_ranges: bool = False

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
        if self.auth_mode is AuthMode.OIDC and not (self.oidc_issuer_url and self.oidc_audience):
            raise ValueError("AUTH_MODE=oidc requires OIDC_ISSUER_URL and OIDC_AUDIENCE")
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
