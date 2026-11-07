from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from app.core.config import AuthMode, Environment, Settings
from app.core.errors import SecretStoreError
from app.core.secrets import FernetSecretStore, SealedSecret

KEY = f"k1:{Fernet.generate_key().decode()}"


def make_settings(**overrides: object) -> Settings:
    base: dict[str, object] = {"secret_encryption_keys": KEY, "_env_file": None}
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


class TestSettings:
    def test_environment_variables_are_applied(self) -> None:
        settings = make_settings()  # conftest sets ENVIRONMENT=test, AUTH_MODE=dev
        assert settings.environment is Environment.TEST
        assert settings.auth_mode is AuthMode.DEV
        assert make_settings(environment="development").environment is Environment.DEVELOPMENT

    @pytest.mark.parametrize("env", ["staging", "production"])
    def test_dev_auth_refused_outside_development(self, env: str) -> None:
        with pytest.raises(ValidationError, match="AUTH_MODE=dev is not permitted"):
            make_settings(environment=env)

    def test_encryption_key_required(self) -> None:
        with pytest.raises(ValidationError, match="SECRET_ENCRYPTION_KEYS must be set"):
            Settings(_env_file=None, secret_encryption_keys=None)  # type: ignore[call-arg]

    def test_oidc_requires_issuer_and_audience(self) -> None:
        with pytest.raises(ValidationError, match="OIDC_ISSUER_URL"):
            make_settings(auth_mode="oidc")

    def test_inline_orchestration_refused_in_production(self) -> None:
        with pytest.raises(ValidationError):
            make_settings(
                environment="production",
                auth_mode="oidc",
                oidc_issuer_url="https://idp",
                oidc_audience="x",
                orchestration_inline=True,
            )

    def test_cors_origins_parsed_from_comma_list(self) -> None:
        settings = make_settings(cors_origins="http://a.test, http://b.test")
        assert settings.cors_origins == ["http://a.test", "http://b.test"]

    def test_secret_value_not_in_repr(self) -> None:
        assert KEY.split(":", 1)[1] not in repr(make_settings())


class TestFernetSecretStore:
    def test_round_trip(self) -> None:
        store = FernetSecretStore.from_config(KEY)
        sealed = store.seal(b"super-secret")
        assert sealed.key_id == "k1"
        assert b"super-secret" not in sealed.ciphertext
        assert store.unseal(sealed) == b"super-secret"

    def test_rotation_keeps_old_ciphertexts_readable(self) -> None:
        old_store = FernetSecretStore.from_config(KEY)
        sealed_old = old_store.seal(b"legacy")
        new_key = f"k2:{Fernet.generate_key().decode()}"
        rotated = FernetSecretStore.from_config(f"{new_key},{KEY}")
        assert rotated.primary_key_id == "k2"
        assert rotated.seal(b"fresh").key_id == "k2"
        assert rotated.unseal(sealed_old) == b"legacy"

    def test_unknown_key_id_fails_clearly(self) -> None:
        store = FernetSecretStore.from_config(KEY)
        with pytest.raises(SecretStoreError, match="not configured"):
            store.unseal(SealedSecret("missing", b"x"))

    def test_tampered_ciphertext_rejected(self) -> None:
        store = FernetSecretStore.from_config(KEY)
        sealed = store.seal(b"value")
        tampered = SealedSecret(sealed.key_id, sealed.ciphertext[:-4] + b"AAAA")
        with pytest.raises(SecretStoreError, match="integrity"):
            store.unseal(tampered)

    @pytest.mark.parametrize("raw", ["", "nokeyid", "k1:not-a-fernet-key", ":abc"])
    def test_invalid_configuration_rejected(self, raw: str) -> None:
        with pytest.raises(SecretStoreError):
            FernetSecretStore.from_config(raw)
