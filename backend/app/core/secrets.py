"""Secret storage.

Credentials (SSH keys, kubeconfigs, cloud API keys...) are never stored in
plaintext and never returned to API clients. Callers interact with a
``SecretStore`` that turns plaintext into an opaque, self-describing ciphertext
reference and back.

The default ``FernetSecretStore`` uses authenticated symmetric encryption
(AES-128-CBC + HMAC-SHA256) with key IDs so keys can be rotated: new data is
encrypted with the primary key, while older keys remain available for
decryption. A Vault-backed store can implement the same interface later.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import Settings, get_settings
from app.core.errors import SecretStoreError


@dataclass(frozen=True, slots=True)
class SealedSecret:
    """Ciphertext plus the identifier of the key that produced it."""

    key_id: str
    ciphertext: bytes


class SecretStore(ABC):
    backend_name: str

    @abstractmethod
    def seal(self, plaintext: bytes) -> SealedSecret: ...

    @abstractmethod
    def unseal(self, sealed: SealedSecret) -> bytes: ...


class FernetSecretStore(SecretStore):
    backend_name = "fernet"

    def __init__(self, keys: dict[str, bytes], primary_key_id: str) -> None:
        if primary_key_id not in keys:
            raise SecretStoreError("Primary key id is not present in the key set")
        self._fernets = {key_id: Fernet(key) for key_id, key in keys.items()}
        self._primary = primary_key_id

    @classmethod
    def from_config(cls, raw: str) -> FernetSecretStore:
        """Parse ``"id1:key1,id2:key2"``; the first entry is the primary key."""
        keys: dict[str, bytes] = {}
        order: list[str] = []
        for entry in (e.strip() for e in raw.split(",") if e.strip()):
            key_id, sep, key = entry.partition(":")
            if not sep or not key_id or not key:
                raise SecretStoreError("SECRET_ENCRYPTION_KEYS entries must look like 'key_id:fernet_key'")
            try:
                Fernet(key.encode())
            except ValueError as exc:
                raise SecretStoreError(f"Encryption key '{key_id}' is not a valid Fernet key") from exc
            keys[key_id] = key.encode()
            order.append(key_id)
        if not order:
            raise SecretStoreError("SECRET_ENCRYPTION_KEYS is empty")
        return cls(keys, order[0])

    @property
    def primary_key_id(self) -> str:
        return self._primary

    def seal(self, plaintext: bytes) -> SealedSecret:
        return SealedSecret(self._primary, self._fernets[self._primary].encrypt(plaintext))

    def unseal(self, sealed: SealedSecret) -> bytes:
        fernet = self._fernets.get(sealed.key_id)
        if fernet is None:
            raise SecretStoreError(f"Encryption key '{sealed.key_id}' is not configured; cannot decrypt")
        try:
            return fernet.decrypt(sealed.ciphertext)
        except InvalidToken as exc:
            raise SecretStoreError("Secret failed integrity verification") from exc


def build_secret_store(settings: Settings) -> SecretStore:
    # Settings validation guarantees a key is configured. There is deliberately no
    # "generate a random key" fallback: the API and workers are separate processes
    # and must share keys, and silently losing access to stored credentials after a
    # restart would be far worse than refusing to start.
    assert settings.secret_encryption_keys is not None
    return FernetSecretStore.from_config(settings.secret_encryption_keys.get_secret_value())


@lru_cache
def get_secret_store() -> SecretStore:
    return build_secret_store(get_settings())
