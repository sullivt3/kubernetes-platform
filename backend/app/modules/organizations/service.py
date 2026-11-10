"""Tenancy bootstrap.

A ``default`` organization and project are ensured at startup for existing data
and initial administration. OIDC identities require explicit membership grants;
only development mode creates a local platform administrator.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.principal import Principal
from app.modules.organizations.models import Organization, Project, User

DEFAULT_ORG_SLUG = "default"
DEFAULT_PROJECT_SLUG = "default"
DEV_SUBJECT = "dev|local-admin"


@dataclass(frozen=True)
class Tenancy:
    organization: Organization
    project: Project


def ensure_default_tenancy(session: Session) -> Tenancy:
    session.execute(
        insert(Organization)
        .values(slug=DEFAULT_ORG_SLUG, name="Default organization")
        .on_conflict_do_nothing(index_elements=["slug"])
    )
    org = session.scalars(select(Organization).where(Organization.slug == DEFAULT_ORG_SLUG)).one()
    session.execute(
        insert(Project)
        .values(organization_id=org.id, slug=DEFAULT_PROJECT_SLUG, name="Default project")
        .on_conflict_do_nothing(index_elements=["organization_id", "slug"])
    )
    project = session.scalars(
        select(Project).where(Project.organization_id == org.id, Project.slug == DEFAULT_PROJECT_SLUG)
    ).one()
    session.commit()
    return Tenancy(org, project)


def ensure_dev_principal(session: Session, tenancy: Tenancy) -> Principal:
    """Create/load the local development administrator (AUTH_MODE=dev only)."""
    session.execute(
        insert(User)
        .values(subject=DEV_SUBJECT, display_name="Local administrator (dev mode)", email=None)
        .on_conflict_do_nothing(index_elements=["subject"])
    )
    user = session.scalars(select(User).where(User.subject == DEV_SUBJECT)).one()
    user.is_platform_admin = True
    session.commit()
    return Principal(
        user_id=user.id,
        subject=user.subject,
        display_name=user.display_name,
        organization_id=tenancy.organization.id,
        roles=frozenset({"platform-admin"}),
    )
