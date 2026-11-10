"""Request-session tenant constraints; workers retain their explicit service scopes.

An active project is an isolation boundary, not just a list filter. Every tenant
model read through the API session is constrained, including ORM primary-key
lookups and aggregate queries. New resources inherit that project at flush time.
"""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy import Table, event, select
from sqlalchemy.orm import ORMExecuteState, Session, with_loader_criteria

from app.core.errors import PermissionDeniedError
from app.core.principal import Principal
from app.db.base import Base, OrganizationScoped
from app.modules.organizations.models import Project


def scope_session(session: Session, principal: Principal) -> Session:
    session.info["principal"] = principal
    return session


@event.listens_for(Session, "do_orm_execute")
def _scope_reads(state: ORMExecuteState) -> None:
    principal = state.session.info.get("principal")
    if principal is None or not state.is_select:
        return
    org_id = principal.organization_id
    state.statement = state.statement.options(
        with_loader_criteria(OrganizationScoped, lambda row: row.organization_id == org_id, include_aliases=True)
    )
    if principal.project_id is not None:
        project_id = principal.project_id
        state.statement = state.statement.options(
            with_loader_criteria(OrganizationScoped, lambda row: row.project_id == project_id, include_aliases=True)
        )


@event.listens_for(Session, "before_flush")
def _scope_writes(session: Session, _context: object, _instances: object) -> None:
    principal = session.info.get("principal")
    if principal is None:
        return
    for row in session.new | session.dirty | session.deleted:
        if not isinstance(row, OrganizationScoped):
            continue
        if row.organization_id != principal.organization_id:
            raise PermissionDeniedError("Resource belongs to a different organization")
        if principal.project_id is not None:
            if row in session.new and row.project_id is None:
                row.project_id = principal.project_id
            if row.project_id != principal.project_id:
                raise PermissionDeniedError("Resource belongs to a different project")
        if row.project_id is not None:
            exists = session.scalar(
                select(Project.id).where(
                    Project.id == row.project_id, Project.organization_id == principal.organization_id
                )
            )
            if exists is None:
                raise PermissionDeniedError("Project is not in the active organization")
    # References cannot silently bind resources from different projects, even
    # when an administrator is viewing the entire organization.
    scoped_models: dict[str, type[Any]] = {
        cast(Table, mapper.local_table).name: mapper.class_
        for mapper in Base.registry.mappers
        if issubclass(mapper.class_, OrganizationScoped)
    }
    for row in session.new | session.dirty:
        if isinstance(row, OrganizationScoped):
            for column in cast(Any, row).__table__.columns:
                for fk in column.foreign_keys:
                    target_model = scoped_models.get(fk.column.table.name)
                    target_id = getattr(row, column.key, None)
                    if target_model is None or target_id is None:
                        continue
                    target = next(
                        (new for new in session.new if isinstance(new, target_model) and new.id == target_id),
                        None,
                    )
                    if target is None:
                        target = session.get(target_model, target_id)
                    if (
                        target is None
                        or target.organization_id != row.organization_id
                        or target.project_id != row.project_id
                    ):
                        raise PermissionDeniedError("Linked resources must belong to the same organization and project")
        elif row.__table__.name == "cluster_machines":
            from app.modules.cluster_lifecycle.models import Cluster
            from app.modules.infrastructure.models import Machine

            cluster = session.get(Cluster, row.cluster_id)
            machine = session.get(Machine, row.machine_id)
            if (
                cluster is None
                or machine is None
                or cluster.organization_id != machine.organization_id
                or cluster.project_id != machine.project_id
            ):
                raise PermissionDeniedError("Cluster and machine must belong to the same organization and project")
