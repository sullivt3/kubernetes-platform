# Kubernetes Platform

A platform for provisioning infrastructure and managing Kubernetes clusters. It aims to cover provisioning,
installation, cluster administration, workload management and monitoring from a single web interface.

**Status: the platform can create Kubernetes clusters.**
* **Machines:** register existing Linux servers over SSH. The platform pins their host keys and inspects OS,
  hardware and prerequisites.
* **Clusters:** a wizard installs Kubernetes with kubeadm (containerd, Calico or Flannel, single or HA control
  plane, worker pools with labels and taints), stores the kubeconfig encrypted, and can delete the cluster again.
* **Not implemented yet:** VM/cloud provisioning, other distributions and live cluster management. The UI and API
  say so explicitly rather than simulating them. See [documentation/architecture.md](documentation/architecture.md).

## Stack

| Layer | Technology |
|---|---|
| Frontend | Next.js 16 (App Router), React 19, TypeScript, Tailwind CSS 4, shadcn/ui (Radix), TanStack Query, Lucide |
| Backend | Python 3.12+, FastAPI, Pydantic 2, SQLAlchemy 2, Alembic, PostgreSQL 17 |
| Orchestration | Celery 5 + Redis 7, Server-Sent Events for live updates |
| Security | Fernet-encrypted credentials with key rotation, audit trail, OIDC seam (Phase 7) |

## Prerequisites

* Docker (for PostgreSQL and Redis)
* Python 3.12+
* Node.js 20.9+ (developed with Node 24)

## Running locally

```bash
# 1. Data services (also creates the kplatform_test database)
docker compose -f infrastructure/deployment/docker-compose.yml up -d

# 2. Backend
cd backend
python -m venv .venv
.venv/Scripts/activate            # Windows (Git Bash: source .venv/Scripts/activate); Linux/macOS: source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
python -m app.cli generate-key    # paste the output into SECRET_ENCRYPTION_KEYS in .env
alembic upgrade head
uvicorn app.main:app --reload --port 8000

# 3. Orchestration worker (separate terminal, same venv)
celery -A app.orchestration.celery_app worker --loglevel=INFO --pool=threads --concurrency=4   # prefork is unavailable on Windows
# Optional: recovery scheduler (re-dispatches/resumes orphaned operations every 30s)
celery -A app.orchestration.celery_app beat --loglevel=INFO

# 4. Frontend (separate terminal)
cd frontend
npm install
npm run dev                       # http://localhost:3000
```

Open http://localhost:3000 and choose **Run platform self-check**. This sends a real operation through the API,
broker, worker, checkpointed steps and live log stream. `python -m app.cli check` verifies dependencies from
the command line.

### Creating a cluster

1. **Security → Credentials:** add the SSH private key (or password) for your servers.
2. **Infrastructure → Machines:** register each server. Requirements: Ubuntu 22.04/24.04, Debian 12/13 or
   RHEL/Rocky/AlmaLinux 8/9; 2+ CPUs for control planes; 2 GB+ RAM; root or passwordless sudo; outbound access
   to pkgs.k8s.io, download.docker.com and registry.k8s.io.
3. **Kubernetes → Create cluster:** follow the wizard. The review step validates against the inventory and shows
   the exact plan. Installation then streams live and can be retried from the failed step.

No spare servers? [infrastructure/testing/linux-nodes](infrastructure/testing/linux-nodes/README.md) starts two
disposable Linux nodes in Docker for a full end-to-end run.

To run the whole backend in containers instead, run
`docker compose -f infrastructure/deployment/docker-compose.yml --profile app up -d --build`. This needs
`backend/.env` with the encryption key.

## Quality checks

```bash
# Backend (needs the compose services running)
cd backend
pytest                 # engine, recovery, API, security, migrations, machines, cluster lifecycle
ruff check app tests && ruff format --check app tests
mypy app

# Frontend
cd frontend
npm run typecheck      # next typegen + tsc
npm run lint
npm test               # vitest
npm run build
```

## Documentation

* [Architecture](documentation/architecture.md): module boundaries, orchestration engine, security model, structure decisions
* [Configuration](documentation/configuration.md): every environment variable, key rotation
* [API](documentation/api.md): endpoints and conventions (OpenAPI at `/api/v1/docs`)

## Roadmap

| Phase | Scope |
|---|---|
| 0 | Foundation ✅ |
| 3 + 4 (core) | SSH machines, inspection, kubeadm installation engine, cluster create/delete ✅ |
| 1 | Dashboard and existing cluster connectivity (live cluster data, import) |
| 2 | Kubernetes workload management |
| 3 (rest) | Proxmox/local providers, network discovery |
| 4 (rest) | kube-vip HA, node add/remove |
| 5 | Full cluster lifecycle, HA, managed Kubernetes |
| 6 | Networking and storage |
| 7 | Enterprise administration (OIDC, RBAC, organizations, backup) |
| 8 | Advanced operations (Prometheus, Grafana, Loki, Helm, GitOps, CRDs) |
