# Architecture

This document describes the foundations established in Phase 0 and the rules that later phases build on.

## System overview

```
 Browser ──► Next.js (frontend, :3000) ──/api/* rewrite──► FastAPI (backend API, :8000)
                                                              │        │
                                                              │        ├─► PostgreSQL  (state, logs, audit, encrypted credentials)
                                                              │        └─► Redis       (Celery broker + change notifications)
                                                              │
                                Celery worker(s) ◄────────────┘  execute operations (provisioning, installs, diagnostics…)
                                Celery beat      ─────────────►  periodic operation recovery
```

* **Modular monolith.** One backend codebase, three process roles (API, worker, scheduler) from one image.
  Modules communicate through Python interfaces, so any of them can later become a service.
* **The management plane is independent of managed clusters.** It never runs inside a cluster it manages and
  never stores live Kubernetes state in its database. A broken cluster cannot take the platform down, and
  administrators can always reach the UI to troubleshoot.
* **The browser only talks to the Next.js origin.** `/api/*` is proxied to FastAPI. This keeps API traffic
  same-origin, so future OIDC session cookies need no CORS. Credentials, kubeconfigs and private keys never
  reach client code.

## Directory structure

```
kubernetes-platform/
├── backend/
│   ├── app/
│   │   ├── core/                 config, logging, errors, secret store, principal
│   │   ├── db/                   declarative base, sessions, migration helpers, model registry
│   │   ├── api/                  FastAPI routes, auth seam, middleware, error mapping
│   │   ├── modules/
│   │   │   ├── organizations/    tenancy (organizations, projects, users)
│   │   │   ├── credentials/      encrypted credential storage
│   │   │   ├── auditing/         append-only audit trail
│   │   │   ├── providers/        infrastructure provider abstraction + implementations
│   │   │   ├── infrastructure/   machine inventory + host access (in-guest) interface
│   │   │   └── cluster_lifecycle/ cluster registration + Kubernetes distribution abstraction
│   │   ├── orchestration/        operation engine: models, workflow API, executor, dispatch, recovery
│   │   │   └── workflows/        concrete workflows (system self-check in Phase 0)
│   │   ├── main.py               application factory
│   │   └── cli.py                operational CLI
│   ├── migrations/               Alembic
│   └── tests/
├── frontend/
│   ├── app/                      Next.js routes (thin; render feature components)
│   ├── features/                 dashboard, operations, infrastructure, security, administration
│   ├── components/               ui (shadcn), layout (shell), common, providers
│   └── lib/                      typed API client + hooks, navigation model, formatting
├── automation/ansible/           playbooks/roles for OS preparation & Kubernetes install (Phase 3–4)
├── infrastructure/
│   ├── containers/               Dockerfiles
│   └── deployment/               docker-compose development stack
└── documentation/
```

**Deviations from the suggested layout, and why:**

| Change | Reason |
|---|---|
| Backend code under `backend/app/` | A top-level package named `platform` would shadow Python's standard-library module. A single importable package is also required for Celery, Alembic and packaging. |
| `orchestration/` inside `app/` (not a sibling of `backend/`) | It shares models, sessions and configuration with the API; workers import the same package. |
| `integrations/{ansible,terraform,helm,kubernetes}` not created yet | Empty folders add nothing. They are created with their first implementation. |
| Installer automation lives with the distribution (`distributions/kubeadm/scripts/*.sh`), not in `automation/ansible` | Ansible cannot use Windows as a control node, and the scripts must ship inside the backend image. See `automation/README.md`. |
| `frontend/layouts/` merged into `components/layout/` | The App Router owns layouts (`app/layout.tsx`); shell components live with other components. |
| Planned modules (workloads, networking, storage, monitoring…) not created as empty folders | They are added when implemented. Their navigation entries already exist and show an honest "planned" page. |

## Separation of infrastructure and Kubernetes

Three independent abstractions:

| Abstraction | Responsibility | Knows about |
|---|---|---|
| `InfrastructureProvider` (`modules/providers`) | Machine lifecycle: validate, discover, provision, delete, status, power, network | Its substrate API (Proxmox, EC2…) |
| `HostExecutor` (`modules/infrastructure/host_access.py`) | In-guest work: run commands, transfer files | SSH (Phase 3) |
| `KubernetesDistribution` (`modules/cluster_lifecycle/distributions`) | Turn a `ClusterSpec` into an ordered workflow plan | Kubernetes installation, never a specific provider |

* `Machine` (infrastructure) has no reference to clusters. `ClusterMachine` (cluster_lifecycle) assigns a
  machine to a cluster role. A unique constraint guarantees that a machine belongs to at most one cluster.
* Distributions declare `supported_providers`. `validate_spec` rejects incompatible combinations, such as EKS on
  Proxmox, existing machines for Kind, an unsupported CNI, or an even-sized HA control plane.
* Provider-specific code lives only in `modules/providers/implementations/<provider>.py`. Everything else looks
  providers up through `provider_registry`.

### Adding a provider

1. Create `implementations/<name>.py` with a subclass of `InfrastructureProvider`.
2. Declare its metadata, a pydantic `config_model` for non-secret config, `credential_kinds` and `capabilities`.
3. Override the method behind each declared capability. A test fails if a capability is declared without an
   implementation.
4. Make every mutating method idempotent.
5. Add the class to `ALL_PROVIDERS`, and add the type to `ProviderType` if it is new. Enums are stored as strings,
   so no migration is needed.
6. Set `implementation_status = AVAILABLE` only when the provider really works. `ProviderRegistry.create` refuses
   to instantiate `PLANNED` providers.

Distributions follow the same pattern: subclass `KubernetesDistribution`, implement `plan_*`, and register.

## Cluster creation (kubeadm on existing Linux machines)

```
Machines (SSH provider)               Cluster wizard / API                   Orchestration worker
-----------------------               --------------------                   --------------------
register --> machine.inspect          spec --> POST /clusters/validate       cluster.create plan (kubeadm):
             - pin SSH host key                - distribution rules          validate -> prepare -> containerd
             - detect OS/hardware              - inventory checks            -> resolve-version -> packages
             - prerequisite checks             - plan preview                -> init -> cni -> join-cp:* (serial)
             -> ready / error         POST /clusters --> Cluster +           -> join-workers (parallel)
                                      ClusterMachine rows + operation        -> configure-nodes -> wait-ready
                                                                             -> register (kubeconfig credential)
```

**Machines.**
* Registered by address with an SSH credential.
* The first connection records the server's host key (trust on first use). Every later connection requires that
  exact key; accepting a changed key is an explicit, confirmed action.
* Inspection (`detect.sh`) reads OS, hardware and Kubernetes state without sudo, and evaluates prerequisites:
  supported OS, architecture, systemd, sudo, CPU, memory, swap, disk, network and repository reachability.
* Only machines with no failing checks are `ready`.

**Installer.**
* Steps run idempotent scripts on nodes, in parallel where safe. Control-plane joins are serialized for etcd safety.
* Version handling: the minor version chosen by the user is resolved to the newest patch available from
  pkgs.k8s.io on the first control plane, then pinned on every node. Packages are held.
* containerd comes from Docker's repository, configured with the systemd cgroup driver.
* kubeadm configuration is generated (v1beta3/v1beta4), including the pod/service CIDRs, DNS domain, node IPs,
  an HA `controlPlaneEndpoint`, and kubelet/kube-proxy options.

**Secrets.**
* Bootstrap tokens and certificate keys are generated per step, passed on stdin, deleted from disk after use,
  and redacted from logs.
* The admin kubeconfig is stored as an encrypted `kubeconfig` credential and never returned by the API.

**Failure handling.**
* A failed step leaves the cluster in `error` with the failure recorded.
* *Retry* resumes at the failed step; completed steps such as `kubeadm init` never repeat.
* *Delete* runs `kubeadm reset` on every node (with an optional force mode for unreachable nodes), deletes the
  kubeconfig and releases the machines.

**Scope today.**
* kubeadm with Calico or Flannel, on existing Linux machines.
* HA control planes require an existing external load balancer or DNS name; kube-vip is not yet supported.
* VM provisioning (Proxmox/cloud), K3s/RKE2 and managed services remain catalogued as planned.
* The installer was verified end to end on real systemd hosts: see `infrastructure/testing/linux-nodes`.

## Orchestration engine

Every long-running action is an **operation**.

```
submit ─► PENDING ─dispatch─► QUEUED ─claim─► RUNNING ─► SUCCEEDED
                                         │           ├─► FAILED ──retry──► PENDING (resumes at failed step)
                                         │           └─► CANCELLING ─► CANCELLED
                                         └─ cancel (before start) ─► CANCELLED
```

* **Workflows** (`orchestration/workflow.py`): `WorkflowDefinition(type, params_model, plan(params) -> [StepDefinition])`.
  The plan is computed and persisted as `OperationStep` rows at submission, so users can review every step
  before and during execution.
* **Step contract:**
  * Steps are idempotent.
  * They return checkpoint output that later steps can read.
  * They raise `RetryableStepError` for transient failures (automatic exponential backoff, up to `max_attempts`)
    and `StepFailedError` for explained failures.
  * They call `ctx.check_cancelled()` at safe points.
  * They call `ctx.redact(secret)` before logging anything derived from a secret.
* **Exactly one executor per operation.** Workers claim with an atomic conditional `UPDATE`, so duplicate broker
  deliveries are no-ops. Celery runs with `acks_late`/`reject_on_worker_lost`.
* **Mutual exclusion per target** is enforced by a PostgreSQL partial unique index on `concurrency_key` for
  active operations. For example, two upgrades of the same cluster cannot overlap, even under a race; the API
  returns 409.
* **Idempotent submission:** an `Idempotency-Key` header returns the existing operation.
* **Destructive workflows** require a confirmation token matching the target name, enforced server-side.
* **Recovery** (`recover_operations`, every 30 s via Celery beat):
  * running operations with a stale heartbeat resume from the interrupted step (bounded by
    `OPERATION_MAX_RECOVERIES`), then fail with `worker_lost`;
  * operations stuck in `pending` (broker unreachable at submission) or `queued` (lost message) are
    re-dispatched;
  * operations stuck in `cancelling` become `cancelled`.
* **Observability:**
  * Every transition and log line is persisted (`operation_logs`, ordered by a global identity column).
  * Redis pub/sub is only a doorbell: stream endpoints re-read the database from their cursor. A lost
    notification can delay an update but never lose one, and without Redis the streams degrade to polling.
  * Notifications are per organization (no cross-tenant leakage).
* **Live UI:** `GET /operations/{id}/events` (SSE, resumable via `Last-Event-ID`) and `GET /operations/stream/changes`.
  SSE responses carry `Cache-Control: no-transform` so compressing proxies, including Next.js, don't buffer them.

`OperationDispatcher` is the seam for a future durable workflow engine such as Temporal. Workflows and steps
would not need to change.

## Data model (Phase 0)

| Table | Purpose |
|---|---|
| `organizations`, `projects`, `users` | Tenancy and identity. Every tenant-owned row carries `organization_id` (+ optional `project_id`). |
| `credentials` | Encrypted secrets (`key_id`, `ciphertext`) plus non-secret `public_metadata`. |
| `audit_events` | Append-only audit trail, written in the same transaction as the change it describes. |
| `provider_connections`, `machines` | Infrastructure inventory (populated from Phase 3). |
| `clusters`, `cluster_machines` | Cluster registration and machine role assignment (populated from Phase 1/4). |
| `operations`, `operation_steps`, `operation_logs` | Orchestration state, checkpoints and logs. |

Enums are stored as strings without DB-level constraints, so new providers, distributions and states don't need
migrations. Validation happens in the application.

## Security model

* **Authentication seam:** every protected route resolves a `Principal` through `get_principal`.
  * Phase 0 ships `AUTH_MODE=dev`, a fixed local administrator. Configuration validation makes it impossible to
    enable in staging or production.
  * OIDC (Keycloak or compatible) plugs into the same seam in Phase 7.
* **Credentials:**
  * Fernet (AES-128-CBC + HMAC-SHA256) encryption with key IDs. Rotation works by prepending a new key; old
    ciphertexts stay readable.
  * The API is write-only for secret material. `CredentialService.reveal` is for workers only and is never routed.
  * Payload validation errors never echo the secret. Metadata keys that look sensitive are rejected.
  * Credentials still referenced by providers, machines or clusters cannot be deleted.
  * `SecretStore` is an interface; a Vault backend can replace Fernet.
* **Destructive actions:** the UI asks for type-to-confirm, and the backend separately requires `confirm=<name>`
  (HTTP 428 otherwise).
* **Audit:**
  * Records actor, action, resource, outcome, request ID and client IP.
  * Detail values whose keys look secret are scrubbed.
* **HTTP:**
  * Request IDs on every response.
  * Security headers (`nosniff`, `DENY` framing, `no-referrer`, COOP), `Cache-Control: no-store` on API responses.
  * Restricted CORS.
  * Structured errors that never leak internals.
* **Containers:** the backend runs as a non-root user.
* **Not yet implemented** (tracked for Phase 7): OIDC, application RBAC enforcement, rate limiting, TLS
  termination guidance, backup/restore.

## Frontend architecture

* **Rendering:** Next.js 16 App Router with `cacheComponents` and partial prefetching. Route files are thin; UI
  lives in `features/`.
* **Data:**
  * Fetched client-side with TanStack Query through a typed client (`lib/api`). Errors are mapped to `ApiError`
    with code, message and request ID.
  * Every data view implements loading, error (with retry), empty and success states.
* **Live updates:** `LiveUpdatesProvider` subscribes to the organization change feed and invalidates queries. If
  the feed is unavailable, queries fall back to polling, and the top bar shows which mode is active.
* **Navigation:** `lib/navigation.ts` is the single source of truth for the sidebar, search and the planned-feature
  route.
  * Items not yet implemented carry a `phase` and are served by `app/[...slug]`, which shows an explicit
    "planned for Phase N" page. Nothing simulates functionality.
  * Tests guarantee that every implemented item has a real page.
