"""End-to-end encrypted PostgreSQL recovery into a disposable, uniquely named database."""

from __future__ import annotations

import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from sqlalchemy.engine import make_url

from app.core.config import get_settings
from app.core.errors import InvalidStateError
from app.modules.backups import platform


@pytest.mark.skipif(
    any(shutil.which(tool) is None for tool in ("age-keygen", "age", "pg_dump", "pg_restore")),
    reason="Recovery drill requires age and PostgreSQL client tools",
)
def test_recovery_drill(client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    identity = tmp_path / "identity"
    subprocess.run(["age-keygen", "-o", str(identity)], check=True, capture_output=True)
    recipient = subprocess.check_output(["age-keygen", "-y", str(identity)], text=True).strip()
    archives, recovered_keys = tmp_path / "archives", tmp_path / "keys"
    for field, value in [
        ("backup_directory", archives),
        ("recovery_key_directory", recovered_keys),
        ("backup_identity_file", identity),
        ("backup_recipient", recipient),
    ]:
        monkeypatch.setattr(settings, field, str(value))
    api = "/api/v1"
    cred = client.post(
        api + "/credentials", json={"name": "drill", "kind": "ssh_password", "secret_value": "test-fixture"}
    )
    assert cred.status_code == 201
    op = client.post(api + "/backups")
    assert op.status_code == 202, op.text
    ident = op.json()["id"]
    assert client.get(api + "/operations/" + ident).json()["status"] == "succeeded"
    assert client.get(api + "/backups").json()["items"][0]["id"] == ident
    archive = archives / (ident + ".age")
    downloaded = client.get(api + "/backups/" + ident + "/archive")
    assert downloaded.content == archive.read_bytes()
    assert downloaded.content.startswith(b"age-encryption.org/")
    url = api + "/backups/" + ident + "/restore"
    assert client.post(url, json={"confirm": "incorrect"}).status_code == 428
    restored = client.post(url, json={"confirm": ident})
    assert restored.status_code == 202, restored.text
    recovered_op = client.get(api + "/operations/" + restored.json()["id"]).json()
    assert recovered_op["status"] == "succeeded", recovered_op["error"]
    name = "kplatform_recovery_" + uuid.UUID(recovered_op["id"]).hex
    assert name == recovered_op["result"]["database"]
    connection = make_url(settings.database_url).set(drivername="postgresql")
    offline_name = f"kplatform_offline_{uuid.uuid4().hex}"
    try:
        key_file = recovered_keys / recovered_op["result"]["key_file"]
        assert key_file.stat().st_mode & 0o777 == 0o600
        assert (
            key_file.read_text().rstrip()
            == "SECRET_ENCRYPTION_KEYS=" + settings.secret_encryption_keys.get_secret_value()
        )
        with psycopg.connect(connection.set(database=name).render_as_string(hide_password=False)) as db:
            assert db.execute("select count(*) from credentials where name='drill'").fetchone() == (1,)
            assert db.execute("select count(*) from operations where status='running'").fetchone() == (0,)
            assert db.execute("select count(*) from browser_sessions").fetchone() == (0,)
        with pytest.raises(InvalidStateError, match="already exists"):
            platform.restore(archive, settings.database_url, name, identity, tmp_path / "second.env")
        resume_key = tmp_path / "resumed.env"
        platform.restore(archive, settings.database_url, name, identity, resume_key, resume=True)
        assert resume_key.read_bytes() == key_file.read_bytes()
        platform.restore(archive, settings.database_url, name, identity, resume_key, resume=True)
        offline_keys = tmp_path / "offline.env"
        command = subprocess.run(
            [
                sys.executable,
                "-m",
                "app.cli",
                "restore-backup",
                str(archive),
                "--database",
                offline_name,
                "--identity",
                str(identity),
                "--key-output",
                str(offline_keys),
            ],
            capture_output=True,
            text=True,
        )
        assert command.returncode == 0, command.stderr
        assert offline_keys.read_bytes() == key_file.read_bytes()
        damaged = tmp_path / "damaged.age"
        damaged.write_bytes(archive.read_bytes()[:-8])
        with pytest.raises(InvalidStateError, match="age failed"):
            platform.restore(damaged, settings.database_url, name, identity, tmp_path / "other.env")
    finally:
        with psycopg.connect(
            connection.set(database="postgres").render_as_string(hide_password=False), autocommit=True
        ) as db:
            for restored_name in (name, offline_name):
                db.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(restored_name)))
