"""Workflow modules. Importing this package registers every workflow."""

from app.modules.backups import workflows as backup_workflows  # noqa: F401
from app.modules.cluster_lifecycle import workflows as cluster_workflows  # noqa: F401
from app.modules.command_center import service as command_workflows  # noqa: F401
from app.modules.custom_workflows import service as custom_workflows  # noqa: F401
from app.modules.gitops import automation as gitops_automation  # noqa: F401
from app.modules.gitops import service as gitops_workflows  # noqa: F401
from app.modules.helm import service as helm_workflows  # noqa: F401
from app.modules.infrastructure import provider_workflows  # noqa: F401
from app.modules.infrastructure import workflows as machine_workflows  # noqa: F401
from app.modules.kubernetes import (  # noqa: F401
    crd_operations,
    dns,
    log_collection,
    remediation,
    storage,
)
from app.modules.kubernetes import workflows as kubernetes_workflows  # noqa: F401
from app.orchestration.workflows import system  # noqa: F401
