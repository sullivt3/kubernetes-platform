"""Encrypt delivered terminal output, never raw input, and audit explicit exports."""

from datetime import timedelta
from typing import cast

from sqlalchemy import delete, select
from sqlalchemy.engine import CursorResult

from app.core.errors import InvalidStateError, NotFoundError
from app.core.secrets import SealedSecret, get_secret_store
from app.db.session import get_session_factory
from app.modules.auditing import service as audit
from app.modules.command_center import access
from app.modules.command_center.session_models import CommandSession, CommandTranscriptChunk
from app.modules.organizations.policy import require

MAX_BYTES = 2 * 1024 * 1024
MAX_FRAMES = 4096


def stamp():
    from app.modules.command_center.sessions import now

    return now()


def append(session_id, data):
    with get_session_factory()() as db:
        row = db.scalar(select(CommandSession).where(CommandSession.id == session_id).with_for_update())
        if not row or not row.specification.get("record_transcript"):
            return
        if row.status != "active" or row.specification.get("transcript_deleted"):
            raise InvalidStateError("Transcript recording is no longer available")
        size = row.specification.get("transcript_bytes", 0) + len(data.encode())
        sequence = row.specification.get("transcript_frames", 0)
        expiry = row.created_at + timedelta(hours=24)
        if size > MAX_BYTES or sequence >= MAX_FRAMES or expiry <= stamp():
            raise InvalidStateError("Transcript retention or size limit reached")
        sealed = get_secret_store().seal(data.encode())
        db.add(
            CommandTranscriptChunk(
                session_id=row.id,
                sequence=sequence,
                key_id=sealed.key_id,
                ciphertext=sealed.ciphertext,
                created_at=stamp(),
                expires_at=expiry,
            )
        )
        row.specification = {**row.specification, "transcript_bytes": size, "transcript_frames": sequence + 1}
        db.commit()


def authorized(service, principal, cluster_id, session_id):
    require(principal, "terminal.exec")
    require(principal, "secrets.reveal")
    service.cluster(principal, cluster_id)
    row = service.session.get(CommandSession, session_id)
    if (
        not row
        or row.cluster_id != cluster_id
        or (row.created_by != principal.subject and not access.is_admin(principal))
    ):
        raise NotFoundError("Transcript not found")
    if (
        not row.specification.get("record_transcript")
        or row.specification.get("transcript_deleted")
        or row.created_at + timedelta(hours=24) <= stamp()
    ):
        raise NotFoundError("Transcript unavailable, deleted or expired")
    return row


def export(service, principal, cluster_id, session_id):
    row = authorized(service, principal, cluster_id, session_id)
    chunks = list(
        service.session.scalars(
            select(CommandTranscriptChunk)
            .where(CommandTranscriptChunk.session_id == row.id, CommandTranscriptChunk.expires_at > stamp())
            .order_by(CommandTranscriptChunk.sequence)
            .limit(MAX_FRAMES + 1)
        )
    )
    if len(chunks) > MAX_FRAMES:
        raise InvalidStateError("Transcript exceeds the export bound")
    frames = []
    size = 0
    for chunk in chunks:
        data = service.secret_store.unseal(SealedSecret(chunk.key_id, chunk.ciphertext))
        size += len(data)
        if size > MAX_BYTES:
            raise InvalidStateError("Transcript exceeds the export bound")
        frames.append({"sequence": chunk.sequence, "recorded_at": chunk.created_at.isoformat(), "data": data.decode()})
    audit.record(
        service.session,
        principal=principal,
        action="command_session.transcript_exported",
        resource_type="command_session",
        resource_id=row.id,
        details={"frames": len(frames), "bytes": size},
    )
    service.session.commit()
    return {
        "session_id": str(row.id),
        "capture": "terminal output; includes any echoed input",
        "session_status": row.status,
        "termination_reason": row.specification.get("outcome"),
        "expires_at": (row.created_at + timedelta(hours=24)).isoformat(),
        "frames": frames,
    }


def inventory(service, principal, cluster_id, offset=0, all_users=False):
    require(principal, "terminal.exec")
    require(principal, "secrets.reveal")
    if all_users:
        require(principal, "platform.manage")
    service.cluster(principal, cluster_id)
    query = select(CommandSession).where(
        CommandSession.cluster_id == cluster_id,
        CommandSession.created_at > stamp() - timedelta(hours=24),
        CommandSession.specification["record_transcript"].as_boolean().is_(True),
        CommandSession.specification["transcript_deleted"].as_boolean().is_not(True),
    )
    if not all_users:
        query = query.where(CommandSession.created_by == principal.subject)
    from app.modules.command_center.sessions import public

    rows = list(service.session.scalars(query.order_by(CommandSession.created_at.desc()).offset(offset).limit(21)))
    return {"items": [{**public(row), "owner": row.created_by} for row in rows[:20]], "has_more": len(rows) > 20}


def remove(service, principal, cluster_id, session_id):
    row = authorized(service, principal, cluster_id, session_id)
    service.session.refresh(row, with_for_update=True)
    if row.status != "closed":
        raise InvalidStateError("End the session and finish cleanup before deleting its transcript")
    row.specification = {**row.specification, "transcript_deleted": True}
    service.session.execute(delete(CommandTranscriptChunk).where(CommandTranscriptChunk.session_id == row.id))
    audit.record(
        service.session,
        principal=principal,
        action="command_session.transcript_deleted",
        resource_type="command_session",
        resource_id=row.id,
    )
    service.session.commit()


def purge():
    with get_session_factory()() as db:
        result = db.execute(delete(CommandTranscriptChunk).where(CommandTranscriptChunk.expires_at <= stamp()))
        db.commit()
    return {"expired_transcript_chunks_deleted": cast(CursorResult, result).rowcount}
