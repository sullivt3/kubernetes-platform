"""Import every model so ``Base.metadata`` is complete (used by Alembic and tests)."""

from app.db.base import Base
from app.modules.auditing.models import AuditEvent
from app.modules.cluster_lifecycle.models import Cluster, ClusterMachine
from app.modules.credentials.models import Credential
from app.modules.infrastructure.models import Machine, ProviderConnection
from app.modules.organizations.models import (
    BrowserSession,
    LoginAttempt,
    Organization,
    OrganizationMembership,
    Project,
    ProjectMembership,
    User,
)
from app.orchestration.models import Operation, OperationLog, OperationStep

__all__ = [
    "AuditEvent",
    "Base",
    "BrowserSession",
    "Cluster",
    "ClusterMachine",
    "Credential",
    "LoginAttempt",
    "Machine",
    "Operation",
    "OperationLog",
    "OperationStep",
    "Organization",
    "OrganizationMembership",
    "Project",
    "ProjectMembership",
    "ProviderConnection",
    "User",
]
