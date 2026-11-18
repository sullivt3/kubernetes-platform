"""Operational command line.

python -m app.cli generate-key [--key-id k2]   print a new SECRET_ENCRYPTION_KEYS entry
python -m app.cli bootstrap                     ensure default organization/project exist
python -m app.cli check                         verify configuration and dependencies
python -m app.cli recover                       run one operation-recovery pass
"""

from __future__ import annotations

import argparse
import json
import sys


def _generate_key(args: argparse.Namespace) -> int:
    from cryptography.fernet import Fernet

    print(f"{args.key_id}:{Fernet.generate_key().decode()}")
    return 0


def _bootstrap(_: argparse.Namespace) -> int:
    from app.db.session import get_session_factory
    from app.modules.organizations.service import ensure_default_tenancy

    with get_session_factory()() as session:
        tenancy = ensure_default_tenancy(session)
    print(f"Organization '{tenancy.organization.slug}' and project '{tenancy.project.slug}' are ready")
    return 0


def _check(_: argparse.Namespace) -> int:
    from app.api.routes.health import check_database, check_redis, check_secret_store, check_workers
    from app.core.config import get_settings

    settings = get_settings()
    print(f"Environment: {settings.environment.value}  Auth: {settings.auth_mode.value}")
    redis_health = check_redis()
    results = [
        check_database(),
        redis_health,
        check_workers(redis_health.status == "ok"),
        check_secret_store(),
    ]
    for r in results:
        print(f"  [{r.status:^11}] {r.name:<13} {r.message}")
    return 1 if any(r.status == "unavailable" for r in results) else 0


def _recover(_: argparse.Namespace) -> int:
    from app.orchestration.celery_app import recover

    print(json.dumps(recover(), indent=2))
    return 0


def _grant_admin(args: argparse.Namespace) -> int:
    from sqlalchemy import select

    from app.core.principal import Principal
    from app.db.session import get_session_factory
    from app.modules.auditing import service as audit
    from app.modules.organizations.models import User
    from app.modules.organizations.service import ensure_default_tenancy

    with get_session_factory()() as session:
        tenancy = ensure_default_tenancy(session)
        user = session.scalars(select(User).where(User.subject == args.subject)).first()
        if user is None:
            print(
                "User not found. Sign in once, then use the exact issuer|sub identity shown on the access page.",
                file=sys.stderr,
            )
            return 1
        user.is_platform_admin = True
        user.is_active = True
        audit.record(
            session,
            principal=Principal.system(tenancy.organization.id, "admin-recovery-cli"),
            action="user.admin.recover",
            resource_type="user",
            resource_id=user.id,
        )
        session.commit()
    print("Platform administrator access granted; this change was audited.")
    return 0


def _restore_backup(args: argparse.Namespace) -> int:
    import os
    from pathlib import Path

    from app.modules.backups.platform import restore

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("Set DATABASE_URL for the recovery PostgreSQL server.", file=sys.stderr)
        return 1
    try:
        restore(Path(args.archive), database_url, args.database, Path(args.identity), Path(args.key_output))
    except Exception as exc:
        print(
            f"Recovery failed ({type(exc).__name__}). Check the archive, identity, database privileges, "
            "tool versions and whether the destination database or key file already exists.",
            file=sys.stderr,
        )
        return 1
    print(f"Verified recovery database {args.database}. Recovered credential keys are in {args.key_output}.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    gen = sub.add_parser("generate-key", help="Generate a credential encryption key entry")
    gen.add_argument("--key-id", default="k1")
    gen.set_defaults(func=_generate_key)
    sub.add_parser("bootstrap", help="Create default tenancy records").set_defaults(func=_bootstrap)
    sub.add_parser("check", help="Check configuration and dependencies").set_defaults(func=_check)
    sub.add_parser("recover", help="Run one operation recovery pass").set_defaults(func=_recover)
    admin = sub.add_parser("grant-admin", help="Recover platform administrator access for an existing OIDC identity")
    admin.add_argument("--subject", required=True)
    admin.set_defaults(func=_grant_admin)
    restore = sub.add_parser("restore-backup", help="Recover a platform archive into a new database")
    restore.add_argument("archive")
    restore.add_argument("--database", required=True)
    restore.add_argument("--identity", required=True)
    restore.add_argument("--key-output", required=True)
    restore.set_defaults(func=_restore_backup)
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
