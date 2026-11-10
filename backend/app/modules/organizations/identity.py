"""Persist identities and resolve database-controlled tenant memberships."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import AuthenticationRequiredError, PermissionDeniedError
from app.core.principal import Principal
from app.modules.organizations.models import Organization, OrganizationMembership, Project, ProjectMembership, User


def identity_user(session: Session, settings: Settings, claims: dict[str, Any]) -> User:
    subject = f"{settings.oidc_issuer_url}|{claims['sub']}"
    name = str(claims.get("name") or claims.get("preferred_username") or claims["sub"])[:255]
    email = claims.get("email") if claims.get("email_verified") is True else None
    session.execute(
        insert(User)
        .values(
            subject=subject,
            display_name=name,
            email=str(email)[:320] if email else None,
            is_platform_admin=bool(
                settings.oidc_bootstrap_subject and claims["sub"] == settings.oidc_bootstrap_subject
            ),
        )
        .on_conflict_do_nothing(index_elements=["subject"])
    )
    user = session.scalars(select(User).where(User.subject == subject)).one()
    if not user.is_active:
        raise AuthenticationRequiredError("This platform account is disabled")
    user.display_name = name
    user.email = str(email)[:320] if email else None
    user.last_seen_at = datetime.now(UTC)
    session.commit()
    return user


def resolve_principal(
    session: Session, user: User, organization_id: uuid.UUID | None, project_id: uuid.UUID | None
) -> Principal:
    if organization_id is None:
        query = select(Organization)
        if not user.is_platform_admin:
            query = query.join(OrganizationMembership, OrganizationMembership.organization_id == Organization.id).where(
                OrganizationMembership.user_id == user.id
            )
        org = session.scalars(query.order_by(Organization.slug).limit(1)).first()
    else:
        org = session.get(Organization, organization_id)
    if org is None:
        raise PermissionDeniedError(
            "An administrator must grant organization membership before you can use the platform"
        )
    membership = session.scalars(
        select(OrganizationMembership).where(
            OrganizationMembership.organization_id == org.id, OrganizationMembership.user_id == user.id
        )
    ).first()
    if not user.is_platform_admin and membership is None:
        raise PermissionDeniedError("You are not a member of this organization")
    org_role = "platform-admin" if user.is_platform_admin else membership.role  # type: ignore[union-attr]
    role = org_role
    if project_id is not None:
        project = session.get(Project, project_id)
        if project is None or project.organization_id != org.id:
            raise PermissionDeniedError("Project is not in this organization")
        if org_role == "member":
            grant = session.scalars(
                select(ProjectMembership).where(
                    ProjectMembership.project_id == project_id, ProjectMembership.user_id == user.id
                )
            ).first()
            if grant is None:
                raise PermissionDeniedError("You are not a member of this project")
            role = grant.role
    return Principal(
        user_id=user.id,
        subject=user.subject,
        display_name=user.display_name,
        organization_id=org.id,
        roles=frozenset({role}),
        project_id=project_id,
        organization_role=org_role,
    )


def available_contexts(session: Session, user: User) -> list[dict[str, Any]]:
    query = select(Organization)
    if not user.is_platform_admin:
        query = query.join(OrganizationMembership, OrganizationMembership.organization_id == Organization.id).where(
            OrganizationMembership.user_id == user.id
        )
    result = []
    for org in session.scalars(query.order_by(Organization.slug)):
        membership = session.scalars(
            select(OrganizationMembership).where(
                OrganizationMembership.organization_id == org.id, OrganizationMembership.user_id == user.id
            )
        ).first()
        role = "platform-admin" if user.is_platform_admin else membership.role  # type: ignore[union-attr]
        projects = select(Project).where(Project.organization_id == org.id)
        if role == "member":
            projects = projects.join(ProjectMembership, ProjectMembership.project_id == Project.id).where(
                ProjectMembership.user_id == user.id
            )
        result.append(
            {
                "id": str(org.id),
                "name": org.name,
                "slug": org.slug,
                "role": role,
                "projects": [
                    {"id": str(p.id), "name": p.name, "slug": p.slug}
                    for p in session.scalars(projects.order_by(Project.slug))
                ],
            }
        )
    return result
