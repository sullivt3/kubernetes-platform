# Kubernetes Platform

A platform for provisioning infrastructure and managing Kubernetes clusters. It aims to cover provisioning,
installation, cluster administration, workload management and monitoring from a single web interface.

**Status: the platform can create Kubernetes clusters.**
* **Machines:** register existing Linux servers over SSH, find them with network discovery, or provision new
  ones: as Docker containers on this computer (Local provider) or as VMs on Proxmox VE (preview). The platform
  pins host keys and inspects OS, hardware and prerequisites, and can start, stop, reboot and destroy the machines
  it provisioned.
* **Cloud accounts (preview):** validate AWS, Azure and Google Cloud credentials and inventory their instances
  (read-only).
* **Clusters:** a wizard installs Kubernetes with kubeadm (containerd, Calico or Flannel, single or HA control
  plane, worker pools with labels and taints), stores the kubeconfig encrypted, and can delete the cluster again.
* **Visibility:** import existing clusters by kubeconfig and see live health, nodes, namespaces, pods, events,
  topology and CPU/memory utilization (metrics-server can be installed from the UI). Health is observed every minute.
* **Workloads:** browse and manage pods, deployments, ReplicaSets, StatefulSets, DaemonSets, Jobs, CronJobs,
  Services, ConfigMaps and Secrets. Scale, restart, roll back, trigger and suspend CronJobs, edit YAML with a
  server-side dry run and diff before applying, follow pod logs live and open an audited shell in a container.
  A guided form builds new deployments (with an optional Service).
* **Not implemented yet:** cloud provisioning and managed Kubernetes, vSphere, other distributions,
  networking/storage management. "Preview" providers are implemented against the vendor's documented API and
  tested with simulators, but not yet verified against a live system. The UI and API say
  so explicitly rather than simulating them. See [documentation/architecture.md](documentation/architecture.md).

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
# Scheduler (separate terminal): cluster health probe every 60s + recovery of orphaned operations every 30s
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

No spare servers? Add a **Local (Docker)** connection under **Infrastructure → Providers**, then **Provision
machines**: the platform creates Linux machines as privileged containers on this computer (like Kind nodes) and
the cluster wizard installs Kubernetes on them. The settings container nodes need (swap, conntrack, DNS) are applied
automatically, and the Kubernetes API is reached through a port published on 127.0.0.1. This needs the Docker
daemon the workers can reach (Docker Desktop on Windows/macOS) and about 2 GB of free memory per node.

To run the whole backend in containers instead, run
`docker compose -f infrastructure/deployment/docker-compose.yml --profile app up -d --build`. This needs
`backend/.env` with the encryption key.

## Quality checks

```bash
# Backend (needs the compose services running)
cd backend
pytest                 # engine, recovery, API, security, migrations, machines, cluster lifecycle, kubernetes, workloads
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
| 3 | SSH machines, inspection, network discovery, local Docker provider, Proxmox VE (preview), cloud foundations (preview) ✅ |
| 4 (core) | kubeadm installation engine, cluster create/delete ✅ |
| 1 | Dashboard and cluster connectivity: import, live health/nodes/namespaces/pods/events, utilization ✅ |
| 2 | Kubernetes workload management: resources, YAML apply with dry-run diff, logs, terminal ✅ |
| 4 (rest) | kube-vip HA, node add/remove |
| 5 | Full cluster lifecycle, HA, managed Kubernetes |
| 6 | Networking and storage |
| 7 | Enterprise administration (OIDC, RBAC, organizations, backup) |
| 8 | Advanced operations (Prometheus, Grafana, Loki, Helm, GitOps, CRDs) |
