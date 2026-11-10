"""User administration, tenant records, and explicit role assignments."""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from app.api.auth import get_principal
from app.core.errors import ConfirmationRequiredError, ConflictError, NotFoundError, PermissionDeniedError
from app.core.principal import Principal
from app.db.session import get_session_factory
from app.modules.auditing import service as audit
from app.modules.organizations.identity import available_contexts
from app.modules.organizations.models import (
    BrowserSession,
    Organization,
    OrganizationMembership,
    Project,
    ProjectMembership,
    User,
)
from app.modules.organizations.policy import ROLE_PERMISSIONS, permissions, require
from app.modules.organizations.service import DEV_SUBJECT

router = APIRouter(prefix="/administration", tags=["administration"])
SLUG = r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$"


class TenantBody(BaseModel):
    slug: str = Field(pattern=SLUG)
    name: str = Field(min_length=1, max_length=255)


class RenameBody(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class UserUpdate(BaseModel):
    is_active: bool
    is_platform_admin: bool


class MemberBody(BaseModel):
    subject: str = Field(min_length=1, max_length=512)
    role: Literal["org-admin", "operator", "viewer", "member"]


class ProjectMemberBody(BaseModel):
    subject: str = Field(min_length=1, max_length=512)
    role: Literal["operator", "viewer"]


def _user(user: User) -> dict:
    return {
        "id": str(user.id),
        "subject": user.subject,
        "display_name": user.display_name,
        "email": user.email,
        "is_active": user.is_active,
        "is_platform_admin": user.is_platform_admin,
        "last_seen_at": user.last_seen_at,
    }


def _commit(session) -> None:
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise ConflictError("This name or membership already exists, or the record is still referenced") from exc


@router.get("/permissions")
def role_permissions(principal: Principal = Depends(get_principal)) -> dict:
    return {
        "roles": {name: sorted(values) for name, values in ROLE_PERMISSIONS.items()},
        "effective_permissions": sorted(permissions(principal)),
        "organization_id": str(principal.organization_id),
        "project_id": str(principal.project_id) if principal.project_id else None,
    }


@router.get("/users")
def users(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    principal: Principal = Depends(get_principal),
) -> list[dict]:
    require(principal, "platform.manage")
    with get_session_factory()() as session:
        return [
            _user(user)
            for user in session.scalars(select(User).order_by(User.created_at, User.id).limit(limit).offset(offset))
        ]


@router.put("/users/{user_id}")
def update_user(user_id: uuid.UUID, body: UserUpdate, principal: Principal = Depends(get_principal)) -> dict:
    require(principal, "platform.manage")
    with get_session_factory()() as session:
        # Serialize platform-admin changes so concurrent demotions cannot remove all admins.
        admins = list(
            session.scalars(
                select(User)
                .where(User.is_platform_admin.is_(True), User.is_active.is_(True), User.subject != DEV_SUBJECT)
                .order_by(User.id)
                .with_for_update()
            )
        )
        user = session.get(User, user_id, with_for_update=True)
        if user is None:
            raise NotFoundError("User not found")
        if user.subject == DEV_SUBJECT:
            raise PermissionDeniedError("The development administrator is controlled by AUTH_MODE")
        if user in admins and len(admins) <= 1 and not (body.is_active and body.is_platform_admin):
            raise ConflictError("At least one active platform administrator must remain")
        user.is_active, user.is_platform_admin = body.is_active, body.is_platform_admin
        if not body.is_active:
            session.execute(delete(BrowserSession).where(BrowserSession.user_id == user.id))
        audit.record(
            session,
            principal=principal,
            action="user.update",
            resource_type="user",
            resource_id=user.id,
            details=body.model_dump(),
        )
        _commit(session)
        return _user(user)


@router.get("/organizations")
def organizations(principal: Principal = Depends(get_principal)) -> list[dict]:
    with get_session_factory()() as session:
        user = session.get(User, principal.user_id)
        assert user is not None
        return available_contexts(session, user)


@router.post("/organizations", status_code=201)
def create_organization(body: TenantBody, principal: Principal = Depends(get_principal)) -> dict:
    require(principal, "platform.manage")
    with get_session_factory()() as session:
        org = Organization(slug=body.slug, name=body.name)
        session.add(org)
        try:
            session.flush()
        except IntegrityError as exc:
            session.rollback()
            raise ConflictError("An organization with this slug already exists") from exc
        session.add(Project(organization_id=org.id, slug="default", name="Default project"))
        audit.record(
            session,
            principal=principal,
            action="organization.create",
            resource_type="organization",
            resource_id=org.id,
            resource_name=org.name,
        )
        _commit(session)
        return {"id": str(org.id), "slug": org.slug, "name": org.name}


@router.put("/organization")
def rename_organization(body: RenameBody, principal: Principal = Depends(get_principal)) -> dict:
    with get_session_factory()() as session:
        org = session.get(Organization, principal.organization_id)
        assert org is not None
        org.name = body.name
        audit.record(
            session,
            principal=principal,
            action="organization.rename",
            resource_type="organization",
            resource_id=org.id,
            resource_name=org.name,
        )
        _commit(session)
        return {"id": str(org.id), "slug": org.slug, "name": org.name}


@router.post("/projects", status_code=201)
def create_project(body: TenantBody, principal: Principal = Depends(get_principal)) -> dict:
    with get_session_factory()() as session:
        project = Project(organization_id=principal.organization_id, slug=body.slug, name=body.name)
        session.add(project)
        audit.record(
            session, principal=principal, action="project.create", resource_type="project", resource_name=body.name
        )
        _commit(session)
        return {"id": str(project.id), "slug": project.slug, "name": project.name}


def _project(session, principal: Principal, project_id: uuid.UUID) -> Project:
    project = session.get(Project, project_id)
    if project is None or project.organization_id != principal.organization_id:
        raise NotFoundError("Project not found")
    return project


@router.put("/projects/{project_id}")
def rename_project(project_id: uuid.UUID, body: RenameBody, principal: Principal = Depends(get_principal)) -> dict:
    with get_session_factory()() as session:
        project = _project(session, principal, project_id)
        project.name = body.name
        audit.record(
            session,
            principal=principal,
            action="project.rename",
            resource_type="project",
            resource_id=project.id,
            resource_name=project.name,
        )
        _commit(session)
        return {"id": str(project.id), "slug": project.slug, "name": project.name}


@router.delete("/projects/{project_id}", status_code=204)
def delete_project(
    project_id: uuid.UUID, confirm: str | None = None, principal: Principal = Depends(get_principal)
) -> Response:
    with get_session_factory()() as session:
        project = _project(session, principal, project_id)
        if confirm != project.slug:
            raise ConfirmationRequiredError("Repeat the project slug to confirm deletion")
        if project.slug == "default":
            raise ConflictError("The default project cannot be deleted")
        audit.record(
            session,
            principal=principal,
            action="project.delete",
            resource_type="project",
            resource_id=project.id,
            resource_name=project.name,
        )
        session.delete(project)
        _commit(session)
    return Response(status_code=204)


@router.get("/members")
def members(principal: Principal = Depends(get_principal)) -> dict:
    with get_session_factory()() as session:
        org_rows = session.execute(
            select(OrganizationMembership, User)
            .join(User, User.id == OrganizationMembership.user_id)
            .where(OrganizationMembership.organization_id == principal.organization_id)
            .order_by(User.display_name)
        ).all()
        project_rows = session.execute(
            select(ProjectMembership, User, Project)
            .join(User, User.id == ProjectMembership.user_id)
            .join(Project, Project.id == ProjectMembership.project_id)
            .where(Project.organization_id == principal.organization_id)
        ).all()
        return {
            "organization": [{"user": _user(user), "role": member.role} for member, user in org_rows],
            "projects": [
                {"user": _user(user), "role": member.role, "project_id": str(project.id), "project_name": project.name}
                for member, user, project in project_rows
            ],
        }


@router.put("/members")
def grant_membership(body: MemberBody, principal: Principal = Depends(get_principal)) -> dict:
    with get_session_factory()() as session:
        # Lock the organization as well as its memberships, including an initially empty set.
        session.get(Organization, principal.organization_id, with_for_update=True)
        user = session.scalars(select(User).where(User.subject == body.subject)).first()
        if user is None:
            raise NotFoundError("User must sign in once before membership can be granted")
        rows = list(
            session.scalars(
                select(OrganizationMembership)
                .where(OrganizationMembership.organization_id == principal.organization_id)
                .with_for_update()
            )
        )
        member = next((row for row in rows if row.user_id == user.id), None)
        if (
            member
            and member.role == "org-admin"
            and body.role != "org-admin"
            and sum(row.role == "org-admin" for row in rows) <= 1
        ):
            raise ConflictError("At least one organization administrator must remain")
        if member is None:
            member = OrganizationMembership(organization_id=principal.organization_id, user_id=user.id, role=body.role)
            session.add(member)
        else:
            member.role = body.role
        audit.record(
            session,
            principal=principal,
            action="membership.grant",
            resource_type="user",
            resource_id=user.id,
            details={"role": body.role},
        )
        _commit(session)
        return {"user": _user(user), "role": member.role}


@router.delete("/members/{user_id}", status_code=204)
def revoke_membership(user_id: uuid.UUID, principal: Principal = Depends(get_principal)) -> Response:
    with get_session_factory()() as session:
        session.get(Organization, principal.organization_id, with_for_update=True)
        rows = list(
            session.scalars(
                select(OrganizationMembership)
                .where(OrganizationMembership.organization_id == principal.organization_id)
                .with_for_update()
            )
        )
        member = next((row for row in rows if row.user_id == user_id), None)
        if member is None:
            raise NotFoundError("Membership not found")
        if member.role == "org-admin" and sum(row.role == "org-admin" for row in rows) <= 1:
            raise ConflictError("At least one organization administrator must remain")
        projects = select(Project.id).where(Project.organization_id == principal.organization_id)
        session.execute(
            delete(ProjectMembership).where(
                ProjectMembership.user_id == user_id, ProjectMembership.project_id.in_(projects)
            )
        )
        session.delete(member)
        audit.record(
            session, principal=principal, action="membership.revoke", resource_type="user", resource_id=user_id
        )
        _commit(session)
    return Response(status_code=204)


@router.put("/projects/{project_id}/members")
def grant_project_membership(
    project_id: uuid.UUID, body: ProjectMemberBody, principal: Principal = Depends(get_principal)
) -> dict:
    with get_session_factory()() as session:
        _project(session, principal, project_id)
        session.get(Organization, principal.organization_id, with_for_update=True)
        user = session.scalars(select(User).where(User.subject == body.subject)).first()
        org_member = (
            session.scalars(
                select(OrganizationMembership).where(
                    OrganizationMembership.organization_id == principal.organization_id,
                    OrganizationMembership.user_id == user.id,
                )
            ).first()
            if user
            else None
        )
        if user is None or org_member is None:
            raise NotFoundError("Grant organization membership before project membership")
        grant = session.scalars(
            select(ProjectMembership).where(
                ProjectMembership.project_id == project_id, ProjectMembership.user_id == user.id
            )
        ).first()
        if grant is None:
            session.add(ProjectMembership(project_id=project_id, user_id=user.id, role=body.role))
        else:
            grant.role = body.role
        audit.record(
            session,
            principal=principal,
            action="project.membership.grant",
            resource_type="project",
            resource_id=project_id,
            details={"user_id": str(user.id), "role": body.role},
        )
        _commit(session)
        return {"user": _user(user), "role": body.role, "project_id": str(project_id)}


@router.delete("/projects/{project_id}/members/{user_id}", status_code=204)
def revoke_project_membership(
    project_id: uuid.UUID, user_id: uuid.UUID, principal: Principal = Depends(get_principal)
) -> Response:
    with get_session_factory()() as session:
        _project(session, principal, project_id)
        session.get(Organization, principal.organization_id, with_for_update=True)
        session.execute(
            delete(ProjectMembership).where(
                ProjectMembership.project_id == project_id, ProjectMembership.user_id == user_id
            )
        )
        audit.record(
            session,
            principal=principal,
            action="project.membership.revoke",
            resource_type="project",
            resource_id=project_id,
            details={"user_id": str(user_id)},
        )
        _commit(session)
    return Response(status_code=204)
