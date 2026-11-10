"""FastAPI dependencies."""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import Depends
from sqlalchemy.orm import Session

from app.api.auth import get_principal
from app.core.principal import Principal
from app.core.secrets import get_secret_store
from app.db.session import get_session_factory
from app.modules.cluster_lifecycle.service import ClusterService
from app.modules.credentials.service import CredentialService
from app.modules.infrastructure.connections import ProviderConnectionService
from app.modules.infrastructure.service import MachineService
from app.modules.kubernetes.service import KubernetesService
from app.modules.organizations.scope import scope_session
from app.modules.workloads.service import WorkloadService
from app.orchestration.runtime import get_dispatcher, get_publisher, load_workflows
from app.orchestration.service import OperationService
from app.orchestration.workflow import workflow_registry


def get_db(principal: Principal = Depends(get_principal)) -> Iterator[Session]:
    session = scope_session(get_session_factory()(), principal)
    try:
        yield session
    finally:
        session.close()


def get_operation_service(session: Session = Depends(get_db)) -> OperationService:
    load_workflows()
    return OperationService(session, registry=workflow_registry, dispatcher=get_dispatcher(), publisher=get_publisher())


def get_credential_service(session: Session = Depends(get_db)) -> CredentialService:
    return CredentialService(session, get_secret_store())


def get_machine_service(
    session: Session = Depends(get_db), operations: OperationService = Depends(get_operation_service)
) -> MachineService:
    return MachineService(session, operations)


def get_connection_service(
    session: Session = Depends(get_db), operations: OperationService = Depends(get_operation_service)
) -> ProviderConnectionService:
    return ProviderConnectionService(session, operations)


def get_kubernetes_service(
    session: Session = Depends(get_db), operations: OperationService = Depends(get_operation_service)
) -> KubernetesService:
    return KubernetesService(session, get_secret_store(), operations)


def get_workload_service(kubernetes: KubernetesService = Depends(get_kubernetes_service)) -> WorkloadService:
    return WorkloadService(kubernetes)


def get_cluster_service(
    session: Session = Depends(get_db), operations: OperationService = Depends(get_operation_service)
) -> ClusterService:
    return ClusterService(session, operations)
