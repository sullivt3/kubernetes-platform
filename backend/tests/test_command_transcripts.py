import asyncio
import json
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.api.routes import command_sessions
from app.core.errors import InvalidStateError
from app.db.session import get_session_factory
from app.modules.command_center import sessions, transcripts
from app.modules.command_center.session_models import CommandSession, CommandTranscriptChunk
from app.orchestration.models import OperationLog
from tests.test_command_sessions import base, env, get_row, oidc_env, revoke, start  # noqa: F401
from tests.test_identity import identity  # noqa: F401

OUTPUT = "sensitive-output-test-sentinel"


def recorded(env):
    result = env[0].post(
        base(env),
        json={
            "confirm": "session-test",
            "acknowledge_cluster_admin": True,
            "record_transcript": True,
            "acknowledge_transcript": True,
        },
    )
    assert result.status_code == 202, result.text
    return result.json()


def make_active(row):
    with get_session_factory()() as db:
        record = db.get(CommandSession, uuid.UUID(row["id"]))
        record.status = "active"
        db.commit()


def test_recording_off_by_default_and_requires_consent(env):
    row = start(env)
    assert not row["record_transcript"]
    assert env[0].get(base(env) + "/" + row["id"] + "/transcript").status_code == 404
    result = env[0].post(
        base(env), json={"confirm": "session-test", "acknowledge_cluster_admin": True, "record_transcript": True}
    )
    assert result.status_code == 428


def test_stream_is_encrypted_before_delivery_export_is_audited_and_delete_is_final(env, monkeypatch):
    async def bridge(browser, *args, **kwargs):
        await browser.send_json({"type": "output", "data": OUTPUT})
        await browser.receive_json()
        return "closed"

    monkeypatch.setattr(command_sessions.terminal, "bridge", bridge)
    row = recorded(env)
    root = base(env) + "/" + row["id"]
    with env[0].websocket_connect(root + "/attach", headers={"Origin": "http://localhost:3000"}) as ws:
        assert ws.receive_json()["data"] == OUTPUT
        with get_session_factory()() as db:
            chunk = db.scalars(select(CommandTranscriptChunk)).one()
            assert OUTPUT.encode() not in bytes(chunk.ciphertext)
        ws.send_json({"type": "input", "data": "not-recorded-input"})
        assert ws.receive()["type"] == "websocket.close"
    result = env[0].get(root + "/transcript")
    assert result.status_code == 200 and result.headers["cache-control"] == "no-store"
    assert result.json()["frames"][0]["data"] == OUTPUT
    assert "not-recorded-input" not in result.text
    inventory = env[0].get(base(env) + "/transcripts")
    assert inventory.status_code == 200 and inventory.headers["cache-control"] == "no-store"
    assert OUTPUT not in inventory.text and inventory.json()["items"][0]["id"] == row["id"]
    assert OUTPUT not in json.dumps(get_row(row).specification)
    with get_session_factory()() as db:
        assert all(OUTPUT not in str(log.message) for log in db.scalars(select(OperationLog)))
    assert env[0].delete(root + "/transcript").status_code == 204
    assert env[0].get(root + "/transcript").status_code == 404
    with get_session_factory()() as db:
        assert not list(db.scalars(select(CommandTranscriptChunk)))


def test_expired_recordings_are_unavailable_and_purged(env):
    row = recorded(env)
    make_active(row)
    transcripts.append(uuid.UUID(row["id"]), OUTPUT)
    with get_session_factory()() as db:
        record = db.get(CommandSession, uuid.UUID(row["id"]))
        record.created_at = sessions.now() - timedelta(hours=25)
        chunk = db.scalars(select(CommandTranscriptChunk)).one()
        chunk.expires_at = sessions.now() - timedelta(seconds=1)
        db.commit()
    assert env[0].get(base(env) + "/" + row["id"] + "/transcript").status_code == 404
    assert transcripts.purge()["expired_transcript_chunks_deleted"] == 1


def test_recording_size_frame_limits_and_active_deletion_refused(env, monkeypatch):
    row = recorded(env)
    make_active(row)
    monkeypatch.setattr(transcripts, "MAX_BYTES", 4)
    with pytest.raises(InvalidStateError):
        transcripts.append(uuid.UUID(row["id"]), "too long")
    monkeypatch.setattr(transcripts, "MAX_FRAMES", 0)
    with pytest.raises(InvalidStateError):
        transcripts.append(uuid.UUID(row["id"]), "x")
    assert env[0].delete(base(env) + "/" + row["id"] + "/transcript").status_code == 409


def test_recording_failure_never_delivers_unrecorded_output(monkeypatch):
    class Socket:
        def __init__(self):
            self.sent = []

        async def send_json(self, message):
            self.sent.append(message)

    def broken(*args):
        raise RuntimeError("private error")

    monkeypatch.setattr(transcripts, "append", broken)
    socket = Socket()
    adapter = command_sessions.SessionSocket(socket, uuid.uuid4())
    with pytest.raises(InvalidStateError, match="Transcript recording failed"):
        asyncio.run(adapter.send_json({"type": "output", "data": OUTPUT}))
    assert not socket.sent and adapter.outcome == "recording_failed"


def test_transcript_author_and_admin_boundaries(oidc_env):
    actor, cid, _ = oidc_env
    base_path = f"/api/v1/clusters/{cid}/command-sessions"
    result = actor.client.post(
        base_path,
        headers=actor.bearer(),
        json={
            "confirm": "session-test",
            "acknowledge_cluster_admin": True,
            "record_transcript": True,
            "acknowledge_transcript": True,
        },
    )
    assert result.status_code == 202
    row = result.json()
    make_active(row)
    transcripts.append(uuid.UUID(row["id"]), OUTPUT)
    path = base_path + "/" + row["id"] + "/transcript"
    with get_session_factory()() as db:
        record = db.get(CommandSession, uuid.UUID(row["id"]))
        record.created_by = "another-user"
        db.commit()
    assert actor.client.get(path, headers=actor.bearer()).status_code == 200
    revoke(actor)
    assert actor.client.get(path, headers=actor.bearer()).status_code == 404
    assert not actor.client.get(base_path + "/transcripts", headers=actor.bearer()).json()["items"]
    assert actor.client.get(base_path + "/transcripts?all_users=true", headers=actor.bearer()).status_code == 403
    actor.role("viewer")
    assert actor.client.get(path, headers=actor.bearer()).status_code == 403
