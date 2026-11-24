"""Import every model so ``Base.metadata`` is complete (used by Alembic and tests)."""

from app.db.base import Base
from app.modules.auditing.models import AuditEvent
from app.modules.cluster_lifecycle.models import Cluster, ClusterMachine
from app.modules.command_center.session_models import CommandNamespaceGrant, CommandSession, CommandTranscriptChunk
from app.modules.credentials.models import Credential
from app.modules.custom_workflows.models import CustomWorkflow
from app.modules.gitops.models import GitOpsApplication, GitOpsReview
from app.modules.helm.models import HelmPlan, HelmRepositoryCredential
from app.modules.infrastructure.models import Machine, ProviderConnection
from app.modules.kubernetes.custom_resource_models import CustomResourceReview
from app.modules.kubernetes.remediation_models import RemediationRequest
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
    "CommandNamespaceGrant",
    "CommandSession",
    "CommandTranscriptChunk",
    "Credential",
    "CustomResourceReview",
    "CustomWorkflow",
    "GitOpsApplication",
    "GitOpsReview",
    "HelmPlan",
    "HelmRepositoryCredential",
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
    "RemediationRequest",
    "User",
]
