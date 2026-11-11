# Local testing environment

The local test stack is separate from the default development Compose project.
It binds only to loopback, has its own named volumes, and uses generated passwords.
Docker's built-in Kubernetes is not needed for automated tests or OIDC validation.

## Installed tooling on this checkout

- Docker Desktop is installed in `/Applications/Docker.app`.
- Python 3.13 lives in `.tools/runtime/`; the official Node 24 distribution lives
  in `.tools/node/`. Both directories are ignored by Git.
- Backend development dependencies and Playwright live in `backend/.venv/`.
- Playwright Chromium lives in `.tools/playwright-browsers/`.
- Frontend dependencies live in `frontend/node_modules/`.

From the repository root, use Bash and activate the project tools:

```sh
source scripts/dev-env.sh
python scripts/configure-local.py
bash scripts/test-services.sh up -d --wait
bash scripts/test-services.sh ps
```

`configure-local.py` creates `.env.test-services`, `backend/.env`, and
`frontend/.env.local` with private file permissions, preserving existing files.
These files are ignored by Git. Keep `backend/.env` and its encryption key when
retaining application data; generating a different key cannot decrypt old data.
To recreate the setup on another machine, install Python 3.13/Node 24/Docker,
create `backend/.venv`, install `backend[dev,browser]`, and run `npm ci` in
`frontend/`. On this Intel Mac the cryptography wheel was selected with
`pip install --only-binary=cryptography -e './backend[dev,browser]'`.

| Service | Host address | Purpose |
|---|---|---|
| PostgreSQL 17 | `127.0.0.1:55432` | Separate `kplatform` application and `kplatform_test` test databases |
| Redis 7 | `127.0.0.1:56379` | Application DB 0; automated tests DB 15 |
| Keycloak | `http://localhost:18080` | Local identity provider; realm `kplatform` |

The service limits total approximately 1.6 GiB. On an 8 GiB Mac, keep Docker's
Kubernetes disabled and avoid running local multi-node clusters during builds.

## Automated checks

```sh
source scripts/dev-env.sh
bash scripts/checks.sh
```

This starts the test services and runs backend tests/Ruff/Mypy and frontend
type checking/ESLint/Vitest/build. On macOS the frontend checks default to the
Linux Docker image because the host Turbopack worker hit a socket permission
error; host type checking, lint and tests still work. To use the native frontend
build explicitly, set `KPLATFORM_FRONTEND_CHECKS=native` before running the script.
Backend tests deliberately recreate the test
database schema; the check script explicitly selects `kplatform_test`.
Never point them at a development or production database.

For individual backend tests:

```sh
source scripts/dev-env.sh
cd backend
DATABASE_URL="$TEST_DATABASE_URL" REDIS_URL="$TEST_REDIS_URL" python -m pytest tests/test_identity.py
```

An optional Linux frontend check image runs the same checks and production build
without using macOS native dependencies. Its context excludes local environment
files and installed dependencies:

```sh
docker build -f infrastructure/containers/frontend-checks.Dockerfile -t kplatform-frontend-checks frontend
```

On Docker Desktop, this image can also serve the verified UI against the host API:

```sh
docker run --rm --name kplatform-test-ui --memory=512m -p 127.0.0.1:3000:3000 kplatform-frontend-checks
```

Its build-time backend URL defaults to `http://host.docker.internal:8000` and can
be overridden with `--build-arg BACKEND_URL=...`. Do not run a native frontend on
port 3000 at the same time.

## Run the application with real OIDC

Start the services, activate the tools, and use separate terminals for:

```sh
# API
cd backend
alembic upgrade head
uvicorn app.main:app --reload --port 8000

# Worker (another terminal, with scripts/dev-env.sh sourced)
cd backend
celery -A app.orchestration.celery_app worker --pool=threads --concurrency=2 --loglevel=INFO

# Scheduler (another terminal)
cd backend
celery -A app.orchestration.celery_app beat --loglevel=INFO --schedule ../.tools/celerybeat-schedule

# UI (another terminal)
cd frontend
npm run dev
```

Open `http://localhost:3000` (use `localhost` consistently, matching the OIDC
redirect and cookie origin). The public PKCE client is `kubernetes-platform-web`;
access tokens have the separate audience `kubernetes-platform-api`.

Imported realm users are `platform-admin`, `operator`, and `viewer`. Their shared
test-only password is `KPLATFORM_TEST_USER_PASSWORD` in `.env.test-services`.
Keycloak's management console uses `admin` and the separate
`KPLATFORM_KEYCLOAK_ADMIN_PASSWORD`. View passwords locally; do not paste them into
issues or commit them. `sslRequired=none` is restricted to this loopback development
realm and is not a production identity-provider configuration.

The generated backend configuration bootstraps only the fixed `platform-admin`
subject on its first login. Remove `OIDC_BOOTSTRAP_SUBJECT` after initial setup.
Other users must sign in once, then receive explicit memberships through the
administration UI. Their Keycloak usernames/claims do not automatically grant
application roles. See [enterprise administration](enterprise-administration.md).

The realm is imported only if absent. Changing its JSON does not overwrite an
existing realm. Named volumes retain test data across service restarts.

With the API and UI running, execute the real browser check:

```sh
source scripts/dev-env.sh
python scripts/check-oidc.py
```

This signs in all three fixture users, runs the real worker self-check, grants the local operator/viewer their
documented memberships, verifies cookie flags and role boundaries (including the
credential UI), creates/deletes one disposable operator credential, and signs the
administrator out through the UI. It does not touch infrastructure or clusters.

## Stop services

```sh
bash scripts/test-services.sh stop
```

Use `logs --tail=100` or `ps` with the same script for diagnostics. Do not delete
volumes unless you intend to discard the application and identity-provider data.

## Verified on 2026-11-11

On this Intel Mac running macOS 14.8.9:

- Docker Desktop 4.99.0, Docker Engine 29.9.3, Compose 5.7.0; all three test
  services healthy. Docker reports approximately 4 GiB of VM memory.
- Python 3.13.18, official Node 24.22.2, npm 11.19.1, Playwright 1.65.0 and
  downloaded Chromium; backend dependency consistency checked with `pip check`.
- Backend: **305 tests passed**, including 35 dedicated identity/authorization
  cases; Ruff lint/format and Mypy passed. One upstream Starlette/httpx
  deprecation warning remains.
- Frontend: type checking, ESLint, **27 tests**, and the production build passed
  in the Node 24 Linux check image. Native type checking/lint/tests also passed;
  native production build encountered the Turbopack worker socket restriction
  described above.
- Live Keycloak browser check passed for platform administrator, viewer and
  operator logins, cookie flags, credential permissions/UI visibility, and UI
  logout/session revocation. The real API/Redis/Celery operation self-check passed.
- The API's CLI dependency check reports PostgreSQL, Redis, worker and credential
  encryption as healthy; the scheduler runs recovery and health-probe tasks.

These are local development checks, not production certification or live cloud,
hypervisor, SSH installer, or managed-cluster integration validation.
