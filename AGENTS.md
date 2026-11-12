# Kubernetes Platform — project context and working guidance

## Start here

This is an existing, substantially implemented project. **Do not restart, scaffold a replacement, or revert to Phase 0.** Continue from the current code, preserve working functionality, and scope implementation to the user's current request. The original master prompt's instruction to implement “Phase 0 only” was the historical starting instruction, not the current task.

This file applies throughout the repository. Also read any more specific `AGENTS.md` in the area being changed. In particular, `frontend/AGENTS.md` requires consulting the installed Next.js documentation before writing frontend code.

Before making changes:

1. Check Git status and preserve existing user changes.
2. Read this file, the relevant module, its tests, and applicable documentation.
3. Distinguish implemented behavior, simulator coverage, live validation, and planned work. Do not infer production readiness from a completed roadmap entry.
4. Continue the existing architecture and patterns; do not implement all remaining phases in one pass.

## Product goal

Build a production-capable, extensible infrastructure provisioning and Kubernetes management platform, comparable in scope to a combination of Rancher, Portainer, Kubernetes Dashboard, and infrastructure automation tools.

The primary interface is a unified web GUI: provision machines, prepare Linux, install Kubernetes, manage highly available control planes and workers, connect existing clusters, deploy workloads, configure networking/storage/security, monitor, troubleshoot, upgrade, and recover environments. Advanced users should eventually have kubectl and Helm access through a command center.

Target environments include existing physical Linux servers over SSH, local Docker environments, Proxmox, VMware vSphere, AWS, Azure, and GCP. Distribution paths include kubeadm, Kind, k3d, K3s, RKE2, EKS, AKS, and GKE. Support is capability-specific; consult the provider/distribution registry and integration documentation rather than assuming every combination works.

## Architecture and code map

- **Frontend:** Next.js 16 App Router, React 19, TypeScript, Tailwind CSS 4, shadcn/ui, Lucide, TanStack Query. `frontend/app/` contains thin routes; `features/` contains feature UI; `components/` contains shared UI, layout and providers; `lib/api/` contains typed clients/hooks. `frontend/lib/navigation.ts` drives navigation and planned-feature pages.
- **Backend:** Python 3.12+, FastAPI, Pydantic 2, SQLAlchemy 2, Alembic, PostgreSQL. Modular monolith under `backend/app/`; `main.py` wires routes and authentication, `api/` holds HTTP/WebSocket endpoints, and `core/` holds configuration, errors, principal and encrypted secret storage.
- **Domain modules:** `backend/app/modules/` contains organizations/identity, credentials, auditing, providers, infrastructure, cluster lifecycle, Kubernetes access and workloads. Networking/storage functionality currently shares the Kubernetes/workload modules; do not create parallel modules just to match a suggested directory tree.
- **Orchestration:** `backend/app/orchestration/` uses Celery and Redis with PostgreSQL-backed operations, steps, checkpoints, logs and recovery. API, worker and scheduler are separate process roles from the same backend codebase. SSE supplies operation/log updates; WebSockets bridge pod terminals.
- **Infrastructure:** provider implementations live in `modules/providers/implementations/`; SSH execution and inventory live in `modules/infrastructure/`. Distribution plans and installers live in `modules/cluster_lifecycle/distributions/`.
- **Persistence:** Alembic migrations live in `backend/migrations/`. Current migration head is `0006`, adding machine connection observations after the `0005` identity/session schema.
- **Deployment:** `infrastructure/deployment/docker-compose.yml` supplies PostgreSQL/Redis and optional backend API/worker/scheduler services. The frontend runs separately. `infrastructure/containers/backend.Dockerfile` packages the backend.

Keep infrastructure provisioning, host execution, and Kubernetes distribution installation separate. Add capabilities through the existing provider/distribution contracts and registries instead of scattering provider conditionals through services or UI.

Conventional Fedora 43/44/45 hosts are supported by inspection and the SSH installer.
Registered SSH machines can change their endpoint through the Machines **Update
address** action (`PATCH /machines/{id}/address`). This preserves the machine and
cluster binding and pinned SSH key, audits the change, and queues inspection in
the same transaction. Active machine/cluster operations block updates. This does
not migrate Kubernetes node networking, API endpoints or certificates, and is not
offered for provider-provisioned machines. Tests: `tests/test_machine_addresses.py`.
Machine connection health is observed separately from preparation and cluster
membership (`modules/infrastructure/health.py`, task `machines.probe`). Beat
defaults to 30 seconds, the UI polls at 10 seconds, and observations expire after
three intervals. Probes require a pinned host key and use an unprivileged SSH
`true`; they never accept keys or change the target. Offline machines stay in
inventory and show **Unavailable**; unassigned machines show **Unassigned**.
Tests: `tests/test_machine_health.py`. Apply migration `0006` and restart API,
worker and beat when deploying this change.
Kubeadm creation can opt in to local kubectl access with
`options.kubectl_username`. The account must exist on the first control plane.
After node/DNS/smoke checks, `kubectl_config.sh` merges administrator credentials
as that user into `~/.kube/config` (700 directory, 600 file), retaining the current
context and a private backup. The option is off by default; old operation plans
stay unchanged. It grants the shared bootstrap admin identity, not a separate
person's certificate. See `tests/test_kubectl_config.py` and the cluster integration
guide. Never merge a user's existing kubeconfig as root or print raw credentials.
Kubeadm now verifies the exact kubeadm/kubelet/kubectl versions and starts a real,
short-lived host-network CRI sandbox on every node before init/join. The packaged
`runtime_check.sh` uses systemd cgroups and cleans up only its own sandbox.
Containerd enables synchronized image extraction (and local pulls where exposed);
this prevents publishing undurable new layers but does not repair existing caches.
The initial CNI step precedes worker joins: its successful rollout alone cannot
prove worker networking works. `wait-ready` also requires a Ready CNI pod on every
selected node, reports init/container failures, and allows a 120-second startup
grace before failing a persistent fatal CNI error. Creation then runs a real DNS
pod on each selected node, including control planes, with cleanup on failure.
Tests: `tests/test_installation_verification.py`, `tests/test_linux_installers.py`.
A live Fedora worker incident exposed empty unpacked pause/Calico executables even
though compressed image hashes were valid. An explicitly approved cache rebuild
restored its runtime. Live pod DNS then passed on both nodes after an explicitly
approved peer-only protocol-4 allowance and pod-CIDR forwarding policy, preserving
libvirt interface bindings through a normal firewalld reload. Use numeric `4`,
not the ambiguous protocol name `ipip` (94 on Fedora). Firewalld preparation is now automated by `kubeadm/scripts/firewall.sh`, invoked
within the existing prepare/reset/release workflow keys. Each cluster owns a
host-input policy (`kh-<12 UUID hex>`) and pod-forwarding policy (`kp-...`) with
full UUID ownership metadata. Node add reconciles every joined host's peer list;
node removal revokes departed peers and reset removes only owned policies.
The manager uses the host's firewalld Python/D-Bus client, locks across reloads,
preserves runtime zone/policy settings and VM interface bindings, rolls back on
failure, and skips reloads when already correct. It refuses ownership collisions
and runtime-only zones/policies/direct rules that cannot be safely reloaded.
Inactive firewalls remain untouched; UFW retains the prior port preparation and
does not receive these firewalld policies. Tests: `tests/test_firewall_policies.py`
and `bash scripts/check-firewall.sh` (real isolated Fedora daemon, not a VM cluster). Validation of this automation passed 111 installer/lifecycle
regressions, 24 final policy/node tests, Ruff/mypy, and a real Fedora firewalld
integration run covering apply, retry without reload, peer replacement, runtime
bridge/port preservation and owned-policy removal. Subsequent live validation applied the owned policies to both Fedora hosts and
verified a second apply skipped reloading on each; full zone/bridge preservation
checks passed. The first attempt hit the preservation guard and rolled back; a
repeat succeeded without source changes. Control-plane DHCP had returned as .13 while Kubernetes expected .11; the user
restored a persistent manual .11/24 profile. A subsequent detached worker1 tap
(vnet0) was reattached to its libvirt-declared br0 bridge with explicit approval.
Final live validation passed on both hosts: pinned SSH, persistent owned policies,
repeat runs without reload, Node Ready, healthy Calico/CoreDNS, API /readyz, and a
real DNS pod per node (all probes cleaned up). Do not silently migrate the app
endpoint or Kubernetes certificates to follow DHCP. The existing
failed creation was resumed from its checkpoint and completed successfully with
real per-node DNS checks and encrypted kubeconfig registration. Validation: 99
installer/lifecycle tests plus 47 final verification/configuration/plan/API tests
(overlapping coverage), Ruff and mypy passed; local API/worker were restarted.
Containerd repositories are selected by OS ID (Fedora/RHEL/CentOS-compatible
Rocky/Alma/Ubuntu/Debian); DNF5 has separate Kubernetes query/exclude handling.
Shared swap preparation lives in `distributions/scripts/swap.sh` and masks Fedora's
zram generator when swap is disabled. See `documentation/fedora.md` and the real
package smoke test `bash scripts/check-fedora.sh 45`. Its Fedora 45 package/CRI
check passed (containerd 2.4.2; Kubernetes packages 1.36.6 → 1.37.2). Full Fedora VM
cluster/boot validation remains distinct; do not claim it from container tests.
The Fedora change passed a 343-test backend regression run and all 40 final
installer cases in a subsequent targeted run. The running local API/worker/UI
were updated, and the existing Fedora 45 Workstation machine passed a fresh
read-only SSH inspection with DNF5 detected and no failing checks.

The management plane must remain independent of the clusters it manages. An unavailable cluster must not make the management application inaccessible.

### Existing technology decisions

- kubeadm automation uses packaged, idempotent Bash scripts over SSH instead of Ansible, principally for Windows control-host compatibility. See `automation/README.md`.
- Provisioning uses provider APIs; Terraform/OpenTofu integration is not implemented.
- Kubernetes access uses a custom REST client rather than the Kubernetes Python SDK. Discovery supports generic YAML apply, but this is not yet a complete dynamic resource explorer.
- Secret storage uses encrypted Fernet payloads with key IDs and rotation behind an abstraction. A Vault adapter is not implemented.

These are existing decisions/deviations from the original prompt. Do not replace working implementations merely to match the originally suggested tools or directory names.

## Current status

Context reviewed **2026-11-11**. This handoff includes machine address/health management, optional user kubeconfigs, installer verification and automatic firewalld policies. Check current HEAD and update this file when milestones change.

| Phase | Implemented scope / remaining boundary |
|---|---|
| 0 — Foundation | API/UI foundations, database/migrations, provider contracts, encryption, audit, orchestration and test foundations implemented. |
| 1 — Connectivity | Cluster import, live health/nodes/namespaces/pods/events, dashboard, CPU/memory utilization and cluster detail views implemented. |
| 2 — Workloads | Dedicated resource lists/details, workload actions, YAML dry-run/diff/apply, deployment creation form, logs and pod terminals implemented. Broader forms and dynamic resource browsing remain incomplete. |
| 3 — Infrastructure | SSH registration/inspection/discovery, local Docker provisioning, Proxmox and cloud foundations implemented. Several providers remain preview. |
| 4 — Installation | kubeadm OS/runtime preparation, installation, CNI, validation, logs and recovery implemented; HA and node lifecycle were also implemented ahead of the original phase order. |
| 5 — Lifecycle | Node add/remove, HA, load balancing, upgrades/deletion, cloud VM provisioning, managed EKS/AKS/GKE lifecycle and additional distribution paths implemented, with preview boundaries. |
| 6 — Networking/storage | Services, Ingress, Gateway API, NetworkPolicies, DNS diagnostics, PV/PVC/StorageClasses/attachments/CSI inventory, NFS CSI lifecycle, quotas and limits implemented. Dedicated CSI integrations beyond NFS are absent. |
| 7 — Enterprise | **In progress.** OIDC, browser sessions, database-controlled organization/project memberships, application permissions, tenant scoping and administration UI implemented. Validation and further features remain pending. |
| 8 — Advanced operations | Mostly planned: Prometheus/Grafana/Loki, alerts, remediation, Helm releases, GitOps, dynamic CRD management, command center and custom workflows. |

Preview integrations perform real operations; they are not demonstrations. Documentation records live local Docker/kubeadm validation, including HA failover. Cloud providers, Proxmox, vSphere, newer distribution paths and NFS CSI still require relevant live validation. Simulator tests do not establish production readiness.

Initial Phase 7 implementation left automated testing and live OIDC validation
pending. The identity regression suite subsequently added 35 Phase 7 cases in
`backend/tests/test_identity.py`; the full backend suite passed 305 tests against
isolated Docker PostgreSQL/Redis. Ruff, Mypy, frontend type checking/lint and 27
frontend tests passed. The frontend production build passed in the Linux check
image; the host build hit a Turbopack worker socket permission error. Check
`documentation/local-testing.md` for the configured environment and browser harness.
The real local Keycloak browser harness also passed administrator/viewer/operator
login, role checks, cookie assertions, UI logout and the real worker self-check.
Do not confuse these local checks with production or live provider validation.

## Remaining work and priorities

Default recommendation, subject to the user's chosen task: validate the current baseline and finish Phase 7 before expanding Phase 8.

### Phase 7

- Authentication/session tests: OIDC login/callback/logout, token validation, expiry/revocation, CSRF and browser behavior, plus live identity-provider validation.
- Expand authorization/isolation coverage for organization/project selection, memberships, read/write roles, detail lookups, linked resources, workers, streams, secret reveals and terminal access. The original suite predominantly uses the development administrator; the new identity suite uses real OIDC authentication with a simulated provider transport.
- Finish hiding/disabling individual mutation controls for restricted roles; keep independent backend enforcement.
- Kubernetes Roles/ClusterRoles/RoleBindings/ClusterRoleBindings, ServiceAccounts and security-policy views. Application RBAC and Kubernetes RBAC are separate layers.
- Security posture reports, further audit filtering/reporting, credential access review and any selected external secrets-store integration.
- Platform/cluster backup and restore, including encryption-key recovery.
- General API rate limiting and production TLS/reverse-proxy deployment guidance. Login-start rate limiting already exists.

### Requirements not fully represented by “completed” earlier phases

- Global search currently covers pages and recent operations, not live clusters/nodes/pods/deployments/namespaces/services/infrastructure.
- YAML apply uses discovery, but generic discovery-driven resource lists/details/forms, CRD exploration and schema assistance remain incomplete.
- Deployment creation has a dedicated form; other workload creation primarily uses YAML/templates. Advanced scheduling and broader graphical creation need requirement-by-requirement review.
- Disk/network utilization, historical monitoring, broad alerting and troubleshooting recommendations remain unfinished.
- Creation-time storage is centered on the NFS CSI integration, not a broad CSI vendor catalog.
- Some UI descriptions are stale: the Cloud Resources page still describes managed Kubernetes as planned. Follow code and current integration/auth documentation when resolving inconsistencies. The backend example environment now includes the implemented OIDC settings.

### Delivery and integration validation

- Validate preview integrations against real target systems before promoting them to available/production-ready.
- Establish reproducible checks and CI; no tracked CI workflow was found at the snapshot.
- Complete deployment packaging/documentation and operational recovery validation before describing the product as production-capable.

## Rules to preserve

- Never simulate successful infrastructure actions or present sample data as live. Mark unavailable/planned/preview capabilities honestly; no inert action buttons.
- Use proper loading, error, empty and success states, validate input, and keep long-running work observable and recoverable through tracked operations.
- Keep steps idempotent, checkpointed and safe to retry; retain cancellation checks, target exclusivity and crash recovery.
- Never send infrastructure credentials, SSH private keys or unrestricted kubeconfigs to the browser. Kubernetes Secret values require explicit authorized, audited reveal.
- Encrypt secrets at rest; redact them from logs and operation outputs. SSH installer secrets travel through stdin rather than command-line arguments.
- Preserve SSH host-key pinning and TLS verification. Require explicit confirmation for destructive actions on the server as well as in the UI.
- Preserve ownership checks: provisioning integrations must not adopt or destroy unrelated resources.
- Enforce authorization and organization/project boundaries server-side, including detail access, linked resources, workflows and streams. UI visibility alone is insufficient.
- Preserve safe Kubernetes scheduling/drain semantics and etcd/control-plane sequencing.
- Add meaningful tests for important behavior and regressions; keep API/configuration documentation current. Do not call a phase complete solely because its UI routes exist.

## Local development and checks

Use `README.md` for full setup. Required tooling: Python 3.12+, Node.js 20.9+ (developed with Node 24), Docker for PostgreSQL/Redis, and installed project dependencies.

From the repository root, start development data services:

```sh
docker compose -f infrastructure/deployment/docker-compose.yml up -d
```

Backend setup uses a virtual environment, `pip install -e ".[dev]"`, `backend/.env`, an encryption key generated with `python -m app.cli generate-key`, and `alembic upgrade head`. Never commit real `.env` files or keys. For OIDC setup, use `documentation/enterprise-administration.md`; the example environment file is incomplete for that configuration.

Run from `backend/` with its environment active:

```sh
uvicorn app.main:app --reload --port 8000
celery -A app.orchestration.celery_app worker --loglevel=INFO --pool=threads --concurrency=4
celery -A app.orchestration.celery_app beat --loglevel=INFO
```

These are separate processes. Start the frontend with `npm install` then `npm run dev` from `frontend/`. Its server proxies `/api/*` to `BACKEND_URL` (default `http://localhost:8000`).

Backend checks, from `backend/`:

```sh
pytest
ruff check app tests
ruff format --check app tests
mypy app
```

**Backend tests use real PostgreSQL and Redis.** The test fixtures recreate the public schema and truncate tables in the test database. Check the test URL/environment and use a dedicated disposable test database, never development or production data. See `backend/tests/conftest.py`.

Frontend checks, from `frontend/`:

```sh
npm run typecheck
npm run lint
npm test
npm run build
```

For runtime verification, `python -m app.cli check` verifies dependencies; the UI's **Run platform self-check** exercises the operation pipeline. Report exactly which checks ran and their results. At the familiarization snapshot, dependencies were absent and Node/npm, Docker and Python test tools were not available on PATH; this was an environment observation, not a project failure or a permanent limitation.

Local tooling and an isolated Docker test stack were subsequently prepared. Source
`scripts/dev-env.sh` from Bash to use project-local Python/Node and `backend/.venv`.
`scripts/configure-local.py` generates private, ignored local configuration without
overwriting existing files. `bash scripts/test-services.sh up -d --wait` starts the
separate `kplatform-test` Compose project (PostgreSQL 17 on 55432, Redis 7 on 56379,
Keycloak on 18080); `bash scripts/checks.sh` selects the test database explicitly.
See `documentation/local-testing.md` for OIDC test accounts and startup commands.
Check actual service health before running tests; preparation is not proof that
Docker is running or that the full checks passed.

## Documentation entry points

- `README.md`: product status, setup, checks and phase roadmap.
- `documentation/architecture.md`: module contracts, orchestration, security and frontend patterns.
- `documentation/enterprise-administration.md`: Phase 7 behavior, identity setup, role semantics and remaining work.
- `documentation/cluster-integrations.md`: distribution/provider compatibility, prerequisites, lifecycle limits and preview status.
- `documentation/api.md`: API conventions/endpoints; runtime OpenAPI is under `/api/v1/docs` by default.
- `documentation/configuration.md`: configuration and encryption-key rotation.
- `automation/README.md`: installer automation decision.

Maintain this root file as a concise project handoff when status or major decisions change. Do not store credentials or rely on attachments outside the repository for essential project context.
