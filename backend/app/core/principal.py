"""The authenticated actor on whose behalf work is performed."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Principal:
    user_id: uuid.UUID | None
    subject: str
    display_name: str
    organization_id: uuid.UUID
    roles: frozenset[str] = field(default_factory=frozenset)
    is_system: bool = False
    project_id: uuid.UUID | None = None
    organization_role: str | None = None

    @classmethod
    def system(cls, organization_id: uuid.UUID, component: str) -> Principal:
        """Internal actor for background components (recovery job, scheduler...)."""
        return cls(
            user_id=None,
            subject=f"system|{component}",
            display_name=f"System ({component})",
            organization_id=organization_id,
            roles=frozenset({"system"}),
            is_system=True,
        )
