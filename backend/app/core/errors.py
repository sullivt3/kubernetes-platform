"""Domain exceptions and their HTTP mapping.

Services raise these exceptions; the API layer converts them into consistent
JSON error responses (``{"error": {"code", "message", "details"}}``). Services
never import FastAPI.
"""

from __future__ import annotations

from typing import Any


class PlatformError(Exception):
    status_code = 500
    code = "internal_error"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class NotFoundError(PlatformError):
    status_code = 404
    code = "not_found"


class ConflictError(PlatformError):
    status_code = 409
    code = "conflict"


class ValidationFailedError(PlatformError):
    status_code = 422
    code = "validation_failed"


class ConfirmationRequiredError(PlatformError):
    """A destructive operation was requested without the explicit confirmation token."""

    status_code = 428
    code = "confirmation_required"


class PermissionDeniedError(PlatformError):
    status_code = 403
    code = "permission_denied"


class InvalidStateError(PlatformError):
    """The resource is not in a state that permits the requested action."""

    status_code = 409
    code = "invalid_state"


class CapabilityNotSupportedError(PlatformError):
    """A provider or distribution does not support the requested capability."""

    status_code = 400
    code = "capability_not_supported"


class NotImplementedYetError(PlatformError):
    """The integration exists in the catalog but has not been implemented yet."""

    status_code = 501
    code = "not_implemented"


class SecretStoreError(PlatformError):
    status_code = 500
    code = "secret_store_error"
