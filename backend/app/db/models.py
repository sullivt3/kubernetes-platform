"""Import every model so ``Base.metadata`` is complete (used by Alembic and tests)."""

from app.db.base import Base
from app.modules.auditing.models import AuditEvent
from app.modules.cluster_lifecycle.models import Cluster, ClusterMachine
from app.modules.credentials.models import Credential
from app.modules.infrastructure.models import Machine, ProviderConnection
from app.modules.organizations.models import Organization, Project, User
from app.orchestration.models import Operation, OperationLog, OperationStep

__all__ = [
    "AuditEvent",
    "Base",
    "Cluster",
    "ClusterMachine",
    "Credential",
    "Machine",
    "Operation",
    "OperationLog",
    "OperationStep",
    "Organization",
    "Project",
    "ProviderConnection",
    "User",
]
