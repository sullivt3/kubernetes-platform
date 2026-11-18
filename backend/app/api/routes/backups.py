"""Platform archives and cluster-native Velero backup/restore requests."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse
from pydantic import BaseModel, StringConstraints

from app.api.auth import get_principal
from app.api.deps import get_kubernetes_service, get_operation_service
from app.api.routes.operations import OperationDetail
from app.core.config import get_settings
from app.core.errors import ConfirmationRequiredError, ConflictError, InvalidStateError, NotFoundError
from app.core.principal import Principal
from app.modules.auditing import service as audit
from app.modules.kubernetes import client as kube
from app.modules.kubernetes.service import KubernetesService
from app.orchestration.service import OperationService

router = APIRouter(tags=["backup and restore"])
BASE = "/apis/velero.io/v1/namespaces/velero"
Name = Annotated[str, StringConstraints(pattern=r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$", max_length=63)]


def archive_file(backup_id: uuid.UUID) -> Path:
    directory = get_settings().backup_directory
    if directory:
        path = Path(directory) / f"{backup_id}.age"
        if path.is_file() and not path.is_symlink():
            return path
    raise NotFoundError("Backup archive not found")


@router.get("/backups")
def platform_backups() -> dict[str, Any]:
    settings = get_settings()
    items = []
    if settings.backup_directory:
        for path in Path(settings.backup_directory).glob("*.age"):
            try:
                ident = uuid.UUID(path.stem)
                if str(ident) != path.stem or path.is_symlink() or not path.is_file():
                    continue
                stat = path.stat()
            except (ValueError, FileNotFoundError):
                continue
            items.append({"id": ident, "created_at": datetime.fromtimestamp(stat.st_mtime, UTC), "bytes": stat.st_size})
    return {
        "items": sorted(items, key=lambda row: row["created_at"], reverse=True),
        "can_backup": bool(settings.backup_directory and settings.backup_recipient),
        "can_restore": bool(
            settings.backup_directory and settings.backup_identity_file and settings.recovery_key_directory
        ),
    }


@router.get("/backups/{backup_id}/archive")
def download_backup(
    backup_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    service: OperationService = Depends(get_operation_service),
) -> FileResponse:
    path = archive_file(backup_id)
    audit.record(
        service.session,
        principal=principal,
        action="platform.backup.download",
        resource_type="backup",
        resource_id=backup_id,
    )
    service.session.commit()
    return FileResponse(
        path, filename=path.name, media_type="application/octet-stream", headers={"Cache-Control": "no-store"}
    )


@router.post("/backups", status_code=202)
def create_platform_backup(
    principal: Principal = Depends(get_principal), service: OperationService = Depends(get_operation_service)
) -> OperationDetail:
    if not (get_settings().backup_directory and get_settings().backup_recipient):
        raise InvalidStateError("Configure BACKUP_DIRECTORY and BACKUP_RECIPIENT")
    return OperationDetail.model_validate(service.submit(principal, "platform.backup", {}))


class Confirmation(BaseModel):
    confirm: str


@router.post("/backups/{backup_id}/restore", status_code=202)
def restore_platform(
    backup_id: uuid.UUID,
    body: Confirmation,
    principal: Principal = Depends(get_principal),
    service: OperationService = Depends(get_operation_service),
) -> OperationDetail:
    archive_file(backup_id)
    if not get_settings().backup_identity_file or not get_settings().recovery_key_directory:
        raise InvalidStateError("Configure a recovery identity and private output directory, or use offline recovery")
    return OperationDetail.model_validate(
        service.submit(principal, "platform.restore", {"backup_id": str(backup_id)}, confirm=body.confirm)
    )


def summary(obj: dict) -> dict:
    meta, status, spec = obj["metadata"], obj.get("status") or {}, obj.get("spec") or {}
    return {
        "name": meta["name"],
        "created_at": meta.get("creationTimestamp"),
        "phase": status.get("phase") or "New",
        "warnings": status.get("warnings", 0),
        "errors": status.get("errors", 0),
        "backup": spec.get("backupName"),
        "location": spec.get("storageLocation"),
    }


@router.get("/clusters/{cluster_id}/backups")
def cluster_backups(
    cluster_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    service: KubernetesService = Depends(get_kubernetes_service),
) -> dict:
    _, api = service.api(principal, cluster_id)
    result: dict[str, Any] = {}
    truncated = []
    for resource in ("backups", "restores", "backupstoragelocations"):
        objects, limited = kube.list_items(api, f"{BASE}/{resource}")
        if limited:
            truncated.append(resource)
        if resource == "backupstoragelocations":
            result["locations"] = [
                {
                    "name": o["metadata"]["name"],
                    "phase": (o.get("status") or {}).get("phase"),
                    "writable": (o.get("spec") or {}).get("accessMode") != "ReadOnly",
                }
                for o in objects
            ]
        else:
            result[resource] = sorted((summary(o) for o in objects), key=lambda o: o["created_at"] or "", reverse=True)
    return {**result, "truncated": truncated}


class VeleroBackup(BaseModel):
    name: Name
    location: Name


class VeleroRestore(BaseModel):
    name: Name
    backup: Name
    confirm: str


def submit_velero(api: kube.ClusterAPI, kind: Literal["Backup", "Restore"], name: str, spec: dict) -> dict:
    obj = {"apiVersion": "velero.io/v1", "kind": kind, "metadata": {"name": name, "namespace": "velero"}, "spec": spec}
    try:
        return summary(kube.send_json(api, "POST", f"{BASE}/{kind.lower()}s", obj))
    except kube.KubernetesAPIError as exc:
        if exc.http_status == 409:
            raise ConflictError(f"{kind} already exists. Refresh its status before retrying.") from exc
        raise


@router.post("/clusters/{cluster_id}/backups", status_code=202)
def cluster_backup(
    cluster_id: uuid.UUID,
    body: VeleroBackup,
    principal: Principal = Depends(get_principal),
    service: KubernetesService = Depends(get_kubernetes_service),
) -> dict:
    _, api = service.api(principal, cluster_id)
    location = kube.get_json(api, f"{BASE}/backupstoragelocations/{body.location}")
    if (location.get("status") or {}).get("phase") != "Available" or (location.get("spec") or {}).get(
        "accessMode"
    ) == "ReadOnly":
        raise InvalidStateError("Choose an available, writable Velero backup storage location")
    result = submit_velero(api, "Backup", body.name, {"storageLocation": body.location})
    audit.record(
        service.session,
        principal=principal,
        action="cluster.backup.create",
        resource_type="cluster",
        resource_id=cluster_id,
        details={"backup": body.name},
    )
    service.session.commit()
    return result


@router.post("/clusters/{cluster_id}/restores", status_code=202)
def cluster_restore(
    cluster_id: uuid.UUID,
    body: VeleroRestore,
    principal: Principal = Depends(get_principal),
    service: KubernetesService = Depends(get_kubernetes_service),
) -> dict:
    cluster, api = service.api(principal, cluster_id)
    if body.confirm != cluster.name:
        raise ConfirmationRequiredError("Enter the destination cluster name to confirm the restore")
    obj = kube.get_json(api, f"{BASE}/backups/{body.backup}")
    if (obj.get("status") or {}).get("phase") != "Completed":
        raise InvalidStateError("Select a completed Velero backup")
    result = submit_velero(api, "Restore", body.name, {"backupName": body.backup, "existingResourcePolicy": "none"})
    audit.record(
        service.session,
        principal=principal,
        action="cluster.restore.create",
        resource_type="cluster",
        resource_id=cluster_id,
        details={"restore": body.name, "backup": body.backup},
    )
    service.session.commit()
    return result
