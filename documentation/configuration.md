# Configuration

## Backend (`backend/.env`, or environment variables)

All settings are validated at startup. Unsafe combinations make the process refuse to start.

| Variable | Default | Description |
|---|---|---|
| `ENVIRONMENT` | `development` | `development`, `test`, `staging` or `production`. |
| `LOG_LEVEL` | `INFO` | Python log level. |
| `LOG_JSON` | `false` | Emit JSON log lines (recommended in production). |
| `DATABASE_URL` | `postgresql+psycopg://kplatform:kplatform-dev-only@localhost:5432/kplatform` | SQLAlchemy URL. Must use the `postgresql+psycopg` driver. |
| `DATABASE_POOL_SIZE` | `10` | Connection pool size per process. |
| `REDIS_URL` | `redis://localhost:6379/0` | Celery broker and change-notification channel. |
| `CORS_ORIGINS` | `http://localhost:3000` | Comma-separated list. Browsers normally reach the API through the Next.js proxy, so CORS mainly matters for direct API clients. |
| `API_PREFIX` | `/api/v1` | API mount path. |
| `AUTH_MODE` | `dev` | `dev`: local administrator, rejected in staging/production. `oidc`: authorization-code login, opaque browser sessions and JWT API access tokens. See [enterprise administration](enterprise-administration.md). |
| `OIDC_ISSUER_URL`, `OIDC_AUDIENCE`, `OIDC_CLIENT_ID` | — | Required in OIDC mode. API audience must differ from browser client ID. |
| `OIDC_CLIENT_SECRET` | — | Confidential OIDC client secret; omit for a public client using PKCE. |
| `OIDC_BOOTSTRAP_SUBJECT` | — | Exact trusted OIDC `sub` granted platform administration only when its user is first created. Remove after setup. |
| `PUBLIC_URL` | `http://localhost:3000` | Browser-facing origin used for callbacks and CSRF Origin checks. HTTPS required in staging/production. |
| `AUTH_SESSION_SECONDS` | `28800` | Absolute local browser session lifetime (300–86400 seconds); no silent provider-token refresh. |
| `SECRET_ENCRYPTION_KEYS` | — (**required**) | `key_id:fernet_key[,key_id:fernet_key…]`. The first key encrypts new secrets; the others only decrypt. Generate with `python -m app.cli generate-key --key-id k1`. |
| `DISCOVERY_ALLOW_PUBLIC_RANGES` | `false` | Network discovery only scans private, loopback and link-local addresses unless this is enabled. |
| `DOCKER_HOST` | system default | Docker daemon used by Local provider connections that do not set `docker_host` themselves. Workers need access to it; access to the Docker daemon is equivalent to root on that host. |
| `ORCHESTRATION_INLINE` | `false` | Run operations in-process instead of via Celery. **Test only**; rejected in staging/production. |
| `OPERATION_HEARTBEAT_SECONDS` | `15` | How often a running operation records liveness. |
| `OPERATION_STALE_AFTER_SECONDS` | `120` | Heartbeat age after which an operation counts as orphaned. |
| `OPERATION_PENDING_REDISPATCH_SECONDS` | `60` | Age after which an undispatched operation is re-sent to the broker. |
| `OPERATION_MAX_RECOVERIES` | `3` | Automatic resumes after worker loss before the operation fails. |

### Encryption key rotation

1. Generate a new key: `python -m app.cli generate-key --key-id k2`.
2. Prepend it: `SECRET_ENCRYPTION_KEYS=k2:<new>,k1:<old>` and restart the API and workers.
   New and rotated secrets use `k2`; secrets sealed with `k1` stay readable.
3. Re-seal older credentials (`PUT /api/v1/credentials/{id}/secret`). A bulk re-encryption command is planned.
4. Remove `k1` once no credential reports `key_id = k1`.

**Losing every configured key makes stored credentials unrecoverable.** Back keys up separately from database
backups.

## Workers

Run at least one Celery worker:
`celery -A app.orchestration.celery_app worker --pool=threads --concurrency=4`. The `threads` pool works on every OS
and runs several operations at once; `prefork` is unavailable on Windows. Workers need SSH access (TCP 22 or the
configured port) to registered machines. The machines themselves need outbound HTTPS to `pkgs.k8s.io`,
`download.docker.com`, `registry.k8s.io` and the CNI manifest hosts (`raw.githubusercontent.com` for Calico,
`github.com` for Flannel).

Pinned CNI versions live in `app/modules/cluster_lifecycle/distributions/kubeadm/config.py` (`CNI_MANIFESTS`).

## Frontend (environment of `next start` / `next dev`)

| Variable | Default | Description |
|---|---|---|
| `BACKEND_URL` | `http://localhost:8000` | Origin of the FastAPI service that `/api/*` is proxied to. Server-side only; never exposed to the browser. |

## Development services (`infrastructure/deployment/docker-compose.yml`)

| Variable | Default | Description |
|---|---|---|
| `POSTGRES_PASSWORD` | `kplatform-dev-only` | Development database password. |
| `POSTGRES_PORT` | `5432` | Host port for PostgreSQL. |
| `REDIS_PORT` | `6379` | Host port for Redis. |

The compose stack also creates the `kplatform_test` database that the backend test suite uses. Tests refuse to
run against any database whose name doesn't contain `test`.

## Additional cluster integrations

See [Cluster integrations](cluster-integrations.md) for vSphere credentials/templates, native K3s/RKE2 prerequisites, local Docker access, pinned tool caches, cloud VM inputs, managed lifecycle permissions and creation-time storage. Install backend dependencies after updating: `python -m pip install -e ".[dev]"` includes the vSphere SDK.
