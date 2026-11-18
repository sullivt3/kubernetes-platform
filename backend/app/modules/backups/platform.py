"""Encrypted, host-side recovery archives. Only archive metadata leaves the backend."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import uuid
from pathlib import Path

import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url

from app.core.errors import InvalidStateError
from app.core.secrets import FernetSecretStore, SealedSecret


def _run(args: list[str], **kwargs) -> None:
    try:
        subprocess.run(args, stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, **kwargs)
    except FileNotFoundError:
        raise InvalidStateError(f"Install {args[0]} on the backup host") from None
    except subprocess.CalledProcessError:
        raise InvalidStateError(
            f"{args[0]} failed. Check the tool version, access, key, and available disk space."
        ) from None


def _connection(url: str, database: str | None = None) -> tuple[str, dict[str, str]]:
    target = make_url(url).set(drivername="postgresql", database=database)
    # libpq receives the password through its environment, never process arguments.
    password = target.password
    if target.query.keys() & {"password", "sslpassword"}:
        raise InvalidStateError("Use a password in the database URL authority or a libpq password file")
    env = os.environ.copy()
    if password is not None:
        env["PGPASSWORD"] = password
    return target._replace(password=None).render_as_string(hide_password=False), env


def create(archive: Path, database_url: str, keyring: str, recipient: str) -> None:
    FernetSecretStore.from_config(keyring)
    if not re.fullmatch(r"age1[0-9a-z]{58}", recipient):
        raise InvalidStateError("Configure an age X25519 recovery recipient")
    if archive.exists():
        raise InvalidStateError("Backup archive already exists")
    archive.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    conn, env = _connection(database_url)
    with tempfile.TemporaryDirectory() as work, tempfile.TemporaryDirectory(dir=archive.parent) as publication:
        root = Path(work)
        dump = root / "database.dump"
        _run(
            [
                "pg_dump",
                "--format=custom",
                "--no-password",
                "--exclude-table-data=public.browser_sessions",
                "--exclude-table-data=public.login_attempts",
                "--file",
                str(dump),
                "--dbname",
                conn,
            ],
            env=env,
        )
        ring = root / "credential-keys"
        ring.write_text(keyring)
        with tarfile.open(root / "platform.tar", "w") as tar:
            for file in (dump, ring):
                tar.add(file, arcname=file.name, recursive=False)
        sealed = Path(publication) / "sealed"
        with sealed.open("xb") as out:
            os.fchmod(out.fileno(), 0o600)
            _run(["age", "-r", recipient, str(root / "platform.tar")], stdout=out)
            os.fsync(out.fileno())
        os.link(sealed, archive)  # atomic publication, never overwrite an existing backup


def _restore_database(
    admin: psycopg.Connection, admin_url: str, database: str, dump: Path, keys: FernetSecretStore, marker: str
) -> None:
    scratch = f"kplatform_stage_{uuid.uuid4().hex}"
    conn, env = _connection(admin_url, scratch)
    admin.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(scratch)))
    try:
        _run(
            [
                "pg_restore",
                "--single-transaction",
                "--exit-on-error",
                "--no-password",
                "--no-owner",
                "--no-privileges",
                "--dbname",
                conn,
                str(dump),
            ],
            env=env,
        )
        target_url = make_url(admin_url).set(database=scratch)
        with psycopg.connect(target_url.render_as_string(hide_password=False)) as db:
            with db.cursor(name="verify_credentials") as cursor:
                cursor.execute("SELECT key_id, ciphertext FROM public.credentials")
                for key_id, ciphertext in cursor:
                    keys.unseal(SealedSecret(key_id, bytes(ciphertext)))
            db.execute("DELETE FROM public.browser_sessions")
            db.execute("DELETE FROM public.login_attempts")
            db.execute("""UPDATE public.operation_steps SET status='cancelled', finished_at=now()
                          WHERE status IN ('pending','running') AND operation_id IN
                          (SELECT id FROM public.operations
                           WHERE status IN ('pending','queued','running','cancelling'))""")
            db.execute("""UPDATE public.operations SET status='cancelled', finished_at=now(), current_step=NULL,
                          error='{"code":"restored_snapshot","message":"Interrupted by platform recovery"}'
                          WHERE status IN ('pending','queued','running','cancelling')""")
        admin.execute(sql.SQL("COMMENT ON DATABASE {} IS {}").format(sql.Identifier(scratch), sql.Literal(marker)))
        admin.execute(
            sql.SQL("ALTER DATABASE {} RENAME TO {}").format(sql.Identifier(scratch), sql.Identifier(database))
        )
    except Exception:
        admin.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(scratch)))
        raise


def restore(
    archive: Path, database_url: str, database: str, identity: Path, key_output: Path, *, resume: bool = False
) -> None:
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", database) or database in {"postgres", "template0", "template1"}:
        raise InvalidStateError("Use a new lowercase PostgreSQL database name")
    if key_output.exists() and not resume:
        raise InvalidStateError("The recovery key output already exists")
    key_output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as work, tempfile.TemporaryDirectory(dir=key_output.parent) as publication:
        root = Path(work)
        with (root / "platform.tar").open("xb") as plain:
            _run(["age", "-d", "-i", str(identity), str(archive)], stdout=plain)
        with tarfile.open(root / "platform.tar", "r:") as tar:
            members = tar.getmembers()
            if sorted(m.name for m in members) != ["credential-keys", "database.dump"] or not all(
                m.isfile() for m in members
            ):
                raise InvalidStateError("Unexpected platform recovery archive contents")
            for m in members:
                stream = tar.extractfile(m)
                if stream is None or (m.name == "credential-keys" and m.size > 1048576):
                    raise InvalidStateError("Invalid platform recovery archive entry")
                with (root / m.name).open("xb") as target:
                    shutil.copyfileobj(stream, target)
        ring = (root / "credential-keys").read_text()
        keys = FernetSecretStore.from_config(ring)
        with (root / "platform.tar").open("rb") as payload:
            marker = "Kubernetes Platform recovery " + hashlib.file_digest(payload, "sha256").hexdigest()
        content = "SECRET_ENCRYPTION_KEYS=" + ring + "\n"
        admin_url = make_url(database_url).set(drivername="postgresql", database="postgres")
        dsn = admin_url.render_as_string(hide_password=False)
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (f"kplatform-restore:{database}",))
            existing = admin.execute(
                "SELECT shobj_description(oid, 'pg_database') FROM pg_database WHERE datname=%s", (database,)
            ).fetchone()
            if existing is not None and not (resume and existing[0] == marker):
                raise InvalidStateError("The recovery database already exists")
            if key_output.exists():
                if (
                    resume
                    and existing is not None
                    and not key_output.is_symlink()
                    and key_output.read_text() == content
                ):
                    return
                raise InvalidStateError("The recovery key output already exists")
            recovered = Path(publication) / "recovered.env"
            with recovered.open("x") as output:
                os.fchmod(output.fileno(), 0o600)
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            if existing is None:
                _restore_database(admin, dsn, database, root / "database.dump", keys, marker)
            os.link(recovered, key_output)
