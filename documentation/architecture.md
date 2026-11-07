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
│   │   │   ├── infrastructure/   machine inventory, provider connections, provisioning, network discovery, host access
│   │   │   ├── cluster_lifecycle/ cluster registration + Kubernetes distribution abstraction
│   │   │   ├── kubernetes/       API client, kubeconfig handling, live reads, server-side apply, health probe
│   │   │   └── workloads/        resource kinds, actions, YAML plan/apply, secrets, logs, terminal bridge
│   │   ├── orchestration/        operation engine: models, workflow API, executor, dispatch, recovery
│   │   │   └── workflows/        concrete workflows (system self-check in Phase 0)
│   │   ├── main.py               application factory
│   │   └── cli.py                operational CLI
│   ├── migrations/               Alembic
│   └── tests/
├── frontend/
│   ├── app/                      Next.js routes (thin; render feature components)
│   ├── features/                 dashboard, operations, infrastructure, clusters, kubernetes, workloads, security, administration
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
| Planned modules (networking, storage, monitoring…) not created as empty folders | They are added when implemented. Their navigation entries already exist and show an honest "planned" page. |

## Separation of infrastructure and Kubernetes

Three independent abstractions:

| Abstraction | Responsibility | Knows about |
|---|---|---|
| `InfrastructureProvider` (`modules/providers`) | Machine lifecycle: validate, discover, provision, delete, status, power, network | Its substrate API (Proxmox, EC2…) |
| `HostExecutor` (`modules/infrastructure/host_access.py`) | In-guest work: run commands, transfer files | SSH |
| `KubernetesDistribution` (`modules/cluster_lifecycle/distributions`) | Turn a `ClusterSpec` into an ordered workflow plan | Kubernetes installation, never a specific provider |

* `Machine` (infrastructure) has no reference to clusters. `ClusterMachine` (cluster_lifecycle) assigns a
  machine to a cluster role. A unique constraint guarantees that a machine belongs to at most one cluster.
* Distributions declare `supported_providers`. `validate_spec` rejects incompatible combinations, such as EKS on
  Proxmox, existing machines for Kind, an unsupported CNI, or an even-sized HA control plane.
* Provider-specific code lives only in `modules/providers/implementations/<provider>.py`. Everything else looks
  providers up through `provider_registry`.

### Providers in Phase 3

| Provider | Status | What it does |
|---|---|---|
| SSH | Available | Registers existing servers by address. No lifecycle control. |
| Local (Docker) | Available | Machines are privileged systemd containers (Ubuntu 24.04, OpenSSH, sudo user) on the Docker host the workers use, like Kind nodes. Provision, start/stop/reboot, destroy, discover. |
| Proxmox VE | Preview | Clones a cloud-init template (full clone), configures CPU/memory/user/SSH key/network, grows the boot disk, starts the VM and reads its IP through the QEMU guest agent. Power, delete, discover, network. |
| AWS, Azure, GCP | Preview (foundations) | Validate credentials and inventory instances read-only. Provisioning and managed Kubernetes are declared as `planned_capabilities` for Phase 5. |
| vSphere | Planned | Catalogued only. |

* **Provisioning workflow** (`machine.provision`): create (idempotent) → wait for an address → wait for SSH →
  connect and pin the host key → inspect. The machine row exists from the start (`requested` →
  `provisioning` → `ready`), so failures stay visible and can be retried. The provisioning SSH key is a stored
  credential; only its public key is sent to the provider (cloud-init or the container environment).
* **Ownership.** `Machine.provisioned` marks machines the platform created; only those can be destroyed
  (`machine.deprovision`, type-to-confirm). Providers also refuse to touch anything without their own marker:
  Proxmox VMs carry `managed-by: kubernetes-platform` in their description, local containers a label. A VM or
  container with the same name that the platform did not create is never adopted, changed or deleted.
* **Local nodes stay stable across restarts.** Docker re-randomizes ephemeral ports and reassigns container IPs
  on start, either of which would break SSH access or a Kubernetes node. The provider creates its own network
  with a free subnet (172.28–31.x.0/24 unless configured) and gives each machine a fixed IP and fixed published
  ports (SSH and 6443 on 127.0.0.1). The node image is built from `providers/implementations/local_node/` on
  first use and tagged by content hash.
* **Container machines and clusters.** `ClusterService.validate` applies `allow_swap`,
  `kube_proxy_conntrack_max_per_core: 0` and `kubelet_resolv_conf` for container machines unless the user set
  them, and reports this as a validation note. When the first control plane has a published API endpoint and no
  load-balancer endpoint is configured, the installer sets the cluster's API server override and TLS server name
  automatically, so the platform reaches the API through 127.0.0.1 while still verifying the node's certificate.
* **HTTP and TLS for API providers.** `providers/http.py` is a small urllib3 client with one test seam
  (`set_transport`). TLS is always verified; Proxmox connections can supply a CA bundle or pin the certificate's
  SHA-256 fingerprint (how self-signed installations are reached securely). Vendor SDKs are not used: AWS requests
  are signed with SigV4 (checked against AWS's published test vectors), Azure uses the client-credentials flow and
  GCP a self-signed service-account JWT.
* **Testing without the real systems.** Proxmox is exercised against an API simulator (`tests/fake_proxmox.py`),
  the local provider against a fake Docker engine, the clouds against recorded-style responses. The local provider
  was additionally verified live: it provisioned a container machine and the kubeadm installer built a working
  single-node cluster on it.

### Network discovery

`network.discover` probes CIDRs, last-octet ranges or single addresses for SSH: a TCP connect plus reading the
SSH identification banner, which often names the distribution. No credentials are sent. Only private, loopback
and link-local ranges are accepted unless `DISCOVERY_ALLOW_PUBLIC_RANGES` is set, and a scan is capped at 8192
probes. Results are the operation's `result.hosts`, marked with any matching inventory machine; registering a host
runs the normal inspection.

### Adding a provider

1. Create `implementations/<name>.py` with a subclass of `InfrastructureProvider`.
2. Declare its metadata, a pydantic `config_model` for non-secret config, `credential_kinds` and `capabilities`.
3. Override the method behind each declared capability. A test fails if a capability is declared without an
   implementation.
4. Make every mutating method idempotent.
5. Add the class to `ALL_PROVIDERS`, and add the type to `ProviderType` if it is new. Enums are stored as strings,
   so no migration is needed.
6. Set `implementation_status = AVAILABLE` only when the provider really works; use `PREVIEW` while it has only
   been tested against simulators. `ProviderRegistry.create` refuses to instantiate `PLANNED` providers.
   Capabilities that are on the roadmap but not built go in `planned_capabilities` (shown in the catalog only).

Distributions follow the same pattern: subclass `KubernetesDistribution`, implement `plan_*`, and register.

## Cluster creation (kubeadm on Linux machines)

```
Machines                              Cluster wizard / API                   Orchestration worker
--------                              --------------------                   --------------------
register --> machine.inspect          spec --> POST /clusters/validate       cluster.create plan (kubeadm):
provision -> machine.provision                 - distribution rules          [wait-machines] -> validate -> prepare
             - pin SSH host key                - inventory + provisioning    -> containerd -> resolve-version
             - detect OS/hardware                checks, plan preview        -> packages -> init (+kube-vip)
             - prerequisite checks    POST /clusters --> machines provisioned -> cni -> join-cp:* (serial, +kube-vip)
             -> ready / error         + Cluster/ClusterMachine rows + op     -> join-workers (parallel) -> configure
                                                                             -> wait-ready -> smoke-test -> register
```

**Machines.**
* Registered by address with an SSH credential, or provisioned through a provider connection (pools whose `count`
  exceeds their `existing_machine_ids` get new machines from `provider_connection_id`, with the SSH key
  `machine_credential_id`). Provisioning runs as separate `machine.provision` operations; the cluster operation
  starts with `wait-machines`.
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
* **Smoke test.** After all nodes are Ready, a pod (tolerating every taint) resolves
  `kubernetes.default.svc.<domain>` and must get the first service IP: it proves scheduling, image pulls, the pod
  network and cluster DNS before the cluster is registered.

**High availability (kube-vip).**
* `load_balancer: "kube-vip"` with an unused IPv4 `endpoint` on the control-plane subnet. kube-vip v1.3.1 runs as
  a static pod on every control plane, elects a leader through a Kubernetes lease and announces the VIP with ARP;
  `controlPlaneEndpoint` is the VIP, so it is in the API server certificate.
* On the first control plane the manifest is written before `kubeadm init` and mounts `super-admin.conf` (1.29+:
  `admin.conf` has no RBAC binding until init finishes); afterwards it is switched to `admin.conf`. kubelet then
  restarts kube-vip and the VIP disappears for a few seconds, so the step waits for the restart and for a
  certificate-verified `/readyz` through the VIP before continuing.
* Joining control planes get the manifest after `kubeadm join` (its preflight requires an empty manifests
  directory). `kubeadm reset` on a leaving control plane releases the VIP from its interface.
* The validate step rejects a VIP that is a node address or outside the nodes' subnet. Clusters created with
  `load_balancer: "none"` have no shared endpoint, so control planes cannot be added to them later.
* Verified live with three local control planes: stopping the VIP holder moved the VIP to another node, the API
  kept answering through it and etcd kept quorum; restarting the node restored all three members.
* The local provider assigns machines `.10`–`.199` of its network, leaving `.200`–`.254` for virtual IPs.

**Adding and removing nodes** (`cluster.nodes.add`, `cluster.nodes.remove`).
* Add: registered and/or provisioned machines join an existing pool, the control-plane pool (only with a shared
  endpoint) or a new worker pool. An existing control plane (the *anchor*) issues a fresh bootstrap token and, for
  control planes, uploads certificates with a new certificate key; the new nodes get the cluster's exact version.
  Control-plane workload policy is copied from the anchor's taints. The cluster keeps running throughout.
* Remove: drain (via the anchor) → `kubeadm reset` on the node (a control plane leaves etcd here) → make sure no
  etcd member with the node's name remains (covers unreachable nodes, `force`) → delete the Node object → release
  the machine, optionally destroy it. The last control plane cannot be removed.
* The spec stored on the cluster is updated only when the operation succeeds; a failed node keeps a `failed`
  membership and can be retried or removed.

**Secrets.**
* Bootstrap tokens and certificate keys are generated per step, passed on stdin, deleted from disk after use,
  and redacted from logs.
* The admin kubeconfig is stored as an encrypted `kubeconfig` credential and never returned by the API.

**Failure handling.**
* A failed step leaves the cluster in `error` with the failure recorded.
* *Retry* resumes at the failed step; completed steps such as `kubeadm init` never repeat. A failed machine
  provisioning is retried on its own `machine.provision` operation, then the cluster operation is retried.
* *Delete* runs `kubeadm reset` on every node (with an optional force mode for unreachable nodes), deletes the
  kubeconfig, releases the machines and, with `destroy_machines`, destroys the ones the platform provisioned.

**Scope today.**
* kubeadm with Calico or Flannel on Linux machines (registered, local containers, Proxmox VMs in preview).
* Cluster upgrades, K3s/RKE2 and managed services are planned (Phase 5).

## Node management

Works on every connected cluster (imported ones too), through the Kubernetes API:
* **Cordon / uncordon:** `spec.unschedulable`.
* **Labels:** set/remove; labels Kubernetes maintains (hostname, os, arch, topology, instance type) are refused.
* **Taints:** the user-managed taints are replaced as a list; taints managed by Kubernetes (`node.kubernetes.io/*`)
  are kept. The merge patch carries `resourceVersion`, so concurrent edits are not silently overwritten.
* **Drain** (`cluster.node.drain`): follows `kubectl drain` — DaemonSet and static pods stay; pods without a
  controller or with emptyDir data need explicit `force` / `delete_emptydir_data` (checked before anything changes);
  every other pod is *evicted* through the Eviction API, so PodDisruptionBudgets are respected (429 responses are
  retried until the timeout).
* **System logs** (platform-installed clusters): `journalctl -u kubelet|containerd` read over SSH with the pinned
  host key.

All node changes are audited (`node.cordon`, `node.labels`, `node.taints`, `node.drain`, …).

## Kubernetes connectivity (live cluster data)

`app/modules/kubernetes/` reaches each cluster's API server over HTTPS:

* **Transport.** A small REST client built on urllib3 (`client.py`), not the official Python client.
  * The official client's recent releases conflict with Celery's pinned `python-dateutil`.
  * It loads client keys from file paths, whereas this client holds TLS material in memory.
  * The Kubernetes REST API is the stable contract, and discovery-based server-side apply (`apply.py`) covers any
    resource, including CRDs.
* **TLS.**
  * The server certificate is always verified against the cluster CA.
  * `insecure-skip-tls-verify` kubeconfigs are rejected.
  * A per-cluster **API server override** plus **TLS server name** reaches API servers through NAT, proxies or load
    balancers while verifying their real identity.
  * Client keys are loaded into an in-memory SSL context; the temporary files `ssl` requires exist only while
    loading.
* **Credentials.**
  * Kubeconfigs are minified to the selected context and stored as encrypted `kubeconfig` credentials.
  * Pooled clients are cached per cluster for 5 minutes, so normal page loads don't decrypt anything.
  * `exec`/`auth-provider` plugins are refused: the platform never executes external programs.
* **Reads happen at request time**, with timeouts (5 s connect, 20 s read). An unreachable cluster returns 503
  `cluster_unreachable` and never blocks the rest of the platform.
* **Health observation.**
  * Celery beat runs `clusters.probe` every 60 s. It stores a summary in `clusters.status_detail.health` with
    `observed_at`, and moves clusters between `active`, `degraded` and `unreachable`.
  * States owned by operations (`provisioning`, `deleting`, `error`) are never overwritten.
  * The dashboard shows these stored observations with their timestamp; detail pages read live data.
* **Utilization.**
  * Live usage comes from `metrics.k8s.io` when metrics-server is installed.
  * Otherwise the UI shows *requested* resources, labelled as such.
  * Installing metrics-server is a tracked operation (`cluster.addon.metrics_server`): a pinned manifest applied
    through the API with server-side apply.
* **Imported clusters.**
  * `cluster.connect` verifies access and detects the distribution (EKS/GKE/AKS/K3s/RKE2/Kind/kubeadm, otherwise
    `other`).
  * *Unregister* forgets the cluster and its kubeconfig without touching the cluster itself.

## Workload management

`app/modules/workloads/` builds on the Kubernetes client to read and change resources inside a cluster.

* **Generic kinds.** `kinds.py` describes each supported kind (API group/version, plural, namespaced, which actions
  apply). One set of routes (`/clusters/{id}/resources/{kind}`) serves pods, deployments, ReplicaSets,
  StatefulSets, DaemonSets, Jobs, CronJobs, Services, ConfigMaps and Secrets. Adding a kind is a table entry plus
  a summary function and a frontend column definition.
* **Mutations are direct, audited API calls, not operations.** Scale, restart, rollback, CronJob trigger/suspend,
  delete and apply complete in one request against the API server, so queuing them through Celery would only add
  latency. Each one writes an audit event with outcome. Long-running, multi-step work (installing clusters or
  add-ons) stays in the orchestration engine.
  * Scale uses the `scale` subresource; restart patches `kubectl.kubernetes.io/restartedAt` on the pod template
    (as `kubectl rollout restart` does); rollback copies the pod template of the chosen revision's ReplicaSet back
    onto the Deployment with a JSON patch; CronJob trigger creates a Job from `jobTemplate` with an owner reference.
  * Delete requires `confirm=<name>` (428 otherwise), as everywhere else.
* **YAML apply is plan-then-apply.**
  * `POST /apply` with `dry_run: true` parses every document, resolves each kind by discovery (so CRDs work), and
    runs a **server-side apply with `dryRun=All`**. The response shows per object whether it would be created,
    changed or left unchanged, plus a unified diff between the live object and the dry-run result. Admission
    webhooks and schema validation therefore run exactly as they would for real.
  * Objects in a namespace that the same manifest creates cannot be dry-run (the namespace does not exist yet);
    they are reported with a note and validated when applied.
  * `dry_run: false` applies with server-side apply (field manager `kubernetes-platform`, `force=true`). The UI
    only enables *Apply* after a successful dry run of the current text.
* **Secrets.** Secret values are replaced by `<redacted>` in every list, detail and YAML view. Apply refuses a
  manifest that still contains the redaction marker, so a redacted copy can never overwrite real data. Revealing
  values is a separate POST that is audited.
* **Logs.** A snapshot endpoint returns the last N lines (current or previous container). The follow endpoint is
  SSE: the API streams the Kubernetes `follow=true` log response line by line with heartbeats, and stops when the
  browser disconnects or after 15 minutes.
* **Terminal.** A WebSocket (`/pods/{ns}/{name}/exec`) bridges the browser to the Kubernetes exec API
  (`v5.channel.k8s.io`, falling back to `v4`).
  * The browser speaks a small JSON protocol (`input`, `resize` in; `output`, `exit`, `error` out); the server
    translates it to the channel protocol, so cluster credentials never reach the browser.
  * The Origin header must match the host or an allowed CORS origin (cross-site WebSocket hijacking).
  * The shell tries `bash`, then `sh`. Sessions end after one hour. Start and end are audited.
  * Next.js rewrites proxy the WebSocket in development and production, so the browser only talks to the UI origin.
* **Frontend.** `features/workloads/` holds the kind-driven list and detail dialog (overview, pods, history, YAML,
  events, logs, terminal tabs), the shared `YamlApplyPanel` (CodeMirror with YAML linting, dry-run results and
  diffs), templates, and the new-deployment form, which generates a manifest client-side and sends it through the
  same dry-run/apply path.

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
* **Cluster access from the browser:** none. Kubeconfigs stay encrypted on the server; logs, apply and the terminal
  are proxied by the API with the platform's credentials. Secret values are redacted unless explicitly revealed
  (audited). The terminal WebSocket checks the Origin header.
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
