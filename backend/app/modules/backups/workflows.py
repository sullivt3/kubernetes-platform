"""Platform recovery work runs outside the database being recovered."""

import uuid
from pathlib import Path

from pydantic import BaseModel

from app.core.errors import InvalidStateError
from app.modules.backups import platform
from app.orchestration.context import StepContext
from app.orchestration.workflow import OperationTarget, StepDefinition, WorkflowDefinition, workflow_registry


class BackupParams(BaseModel):
    pass


class RestoreParams(BaseModel):
    backup_id: uuid.UUID


def backup(ctx: StepContext) -> dict:
    settings = ctx.settings
    if not settings.backup_directory or not settings.backup_recipient or not settings.secret_encryption_keys:
        raise InvalidStateError("Configure BACKUP_DIRECTORY and BACKUP_RECIPIENT on the API and worker")
    archive = Path(settings.backup_directory) / f"{ctx.operation_id}.age"
    ctx.check_cancelled()
    if not archive.is_file():
        platform.create(
            archive,
            settings.database_url,
            settings.secret_encryption_keys.get_secret_value(),
            settings.backup_recipient,
        )
    result = {"backup_id": str(ctx.operation_id), "bytes": archive.stat().st_size}
    ctx.set_result(result)
    return result


def restore(ctx: StepContext) -> dict:
    settings = ctx.settings
    if not settings.backup_directory or not settings.backup_identity_file or not settings.recovery_key_directory:
        raise InvalidStateError("Configure the recovery identity and private recovery key directory on the worker")
    assert isinstance(ctx.params, RestoreParams)
    archive = Path(settings.backup_directory) / f"{ctx.params.backup_id}.age"
    database = f"kplatform_recovery_{ctx.operation_id.hex}"
    key_output = Path(settings.recovery_key_directory) / f"{database}.env"
    ctx.check_cancelled()
    platform.restore(
        archive, settings.database_url, database, Path(settings.backup_identity_file), key_output, resume=True
    )
    result = {"database": database, "key_file": key_output.name}
    ctx.set_result(result)
    return result


workflow_registry.register(
    WorkflowDefinition(
        type="platform.backup",
        title="Back up the platform",
        description="Encrypt the database and credential keyring.",
        params_model=BackupParams,
        plan=lambda _: [StepDefinition("backup", "Write encrypted archive", backup)],
        target=lambda _: OperationTarget("platform", "backup", "Platform"),
    )
)
workflow_registry.register(
    WorkflowDefinition(
        type="platform.restore",
        title="Restore a platform recovery copy",
        description="Restore into a new database.",
        params_model=RestoreParams,
        plan=lambda _: [StepDefinition("restore", "Recover and verify database", restore)],
        target=lambda p: OperationTarget("platform", "restore", str(p.backup_id)),
        destructive=True,
    )
)
