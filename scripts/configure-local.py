"""Create private local testing configuration without overwriting existing files."""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parents[1]


def create_private(path: Path, content: str) -> None:
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        print(f"Preserved {path.relative_to(ROOT)}")
        return
    with os.fdopen(descriptor, "w") as stream:
        stream.write(content)
    print(f"Created {path.relative_to(ROOT)}")


def main() -> None:
    service_env = ROOT / ".env.test-services"
    create_private(
        service_env,
        "".join(
            f"{name}={secrets.token_hex(24)}\n"
            for name in (
                "KPLATFORM_TEST_DB_PASSWORD",
                "KPLATFORM_KEYCLOAK_ADMIN_PASSWORD",
                "KPLATFORM_TEST_USER_PASSWORD",
            )
        ),
    )
    values = dict(
        line.split("=", 1)
        for line in service_env.read_text().splitlines()
        if line and not line.startswith("#")
    )
    key = Fernet.generate_key().decode()
    backend_env = (
        "ENVIRONMENT=development\n"
        f"DATABASE_URL=postgresql+psycopg://kplatform:{values['KPLATFORM_TEST_DB_PASSWORD']}@127.0.0.1:55432/kplatform\n"
        "REDIS_URL=redis://127.0.0.1:56379/0\n"
        f"SECRET_ENCRYPTION_KEYS=local:{key}\n"
        "AUTH_MODE=oidc\n"
        "PUBLIC_URL=http://localhost:3000\n"
        "CORS_ORIGINS=http://localhost:3000\n"
        "OIDC_ISSUER_URL=http://localhost:18080/realms/kplatform\n"
        "OIDC_CLIENT_ID=kubernetes-platform-web\n"
        "OIDC_AUDIENCE=kubernetes-platform-api\n"
        "OIDC_BOOTSTRAP_SUBJECT=11111111-1111-4111-8111-111111111111\n"
    )
    create_private(ROOT / "backend" / ".env", backend_env)
    create_private(
        ROOT / "frontend" / ".env.local", "BACKEND_URL=http://localhost:8000\n"
    )


if __name__ == "__main__":
    main()
