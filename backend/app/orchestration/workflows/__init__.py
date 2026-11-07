"""Workflow modules. Importing this package registers every workflow."""

from app.modules.cluster_lifecycle import workflows as cluster_workflows  # noqa: F401
from app.modules.infrastructure import provider_workflows  # noqa: F401
from app.modules.infrastructure import workflows as machine_workflows  # noqa: F401
from app.modules.kubernetes import workflows as kubernetes_workflows  # noqa: F401
from app.orchestration.workflows import system  # noqa: F401
