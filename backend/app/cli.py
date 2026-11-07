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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    gen = sub.add_parser("generate-key", help="Generate a credential encryption key entry")
    gen.add_argument("--key-id", default="k1")
    gen.set_defaults(func=_generate_key)
    sub.add_parser("bootstrap", help="Create default tenancy records").set_defaults(func=_bootstrap)
    sub.add_parser("check", help="Check configuration and dependencies").set_defaults(func=_check)
    sub.add_parser("recover", help="Run one operation recovery pass").set_defaults(func=_recover)
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
