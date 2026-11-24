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
- **Persistence:** Alembic migrations live in `backend/migrations/`. Current migration head is `0018`: `0016` added interactive session leases, `0017` explicit user/namespace grants and `0018` encrypted transcript chunks.
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

Context reviewed **2026-11-24**. The monitoring/logging milestone is implemented and live-validated. Phase 8 remains in progress. Check current HEAD and update this file when milestones change.

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
| 8 — Advanced operations | **In progress.** Monitoring/logging, approval-controlled stateless pod remediation and Helm release management implemented. Helm covers public/private HTTPS repositories and OCI registries, encrypted authentication, chart/version browsing, editable values reviews and install/upgrade/rollback/uninstall. GitOps HTTPS/SSH sources, manual sync, opt-in automatic reconciliation and guarded pruning are implemented; installed CRD discovery and reviewed instance CRUD are implemented; guarded lifecycle of platform-installed definitions is implemented; reviewed custom workflows, native/manual storage migration and Service-backed conversion configuration are implemented; a focused Command Center summary runner is implemented; selected reviewed command mutations are implemented; interactive administrator and namespace-scoped read-only operator sessions, opt-in approved public HTTPS/OCI access and encrypted output recording are implemented; write-capable scoped profiles, broader workflow actions and controller integrations remain planned. External alert notifications and observability host-loss recovery are scheduled towards the end of Phase 8. |

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

The Phase 7 permissions pass aligns viewer controls with the existing API
permissions. Resource list actions and mutation dialogs use the session's write
grants, page search follows the same permission mapping as the sidebar, and
viewing provider inventory cannot queue discovery without infrastructure write
access. On 2026-11-18, the local Keycloak browser checks passed for viewer and
operator controls, search, the deployment form and provider discovery. Live
Fedora node and DNS views passed for both roles. The Docker frontend build,
29 frontend tests and 36 backend identity tests passed.

Phase 7 now has read-only Kubernetes Roles & bindings and Service accounts pages.
They use the scoped cluster resource APIs and support built-in RBAC names such
as `system:kube-proxy`. Details show permission rules, grant scope, binding
subjects, token automount, and Secret references without retrieving Secret values.
These pages inspect Kubernetes RBAC declarations and do not calculate effective access.
The 67 targeted backend tests, 29 frontend tests and Docker build passed. Live
Keycloak viewer and operator browser sessions verified both pages against the
Fedora cluster.

The read-only Security policies page shows explicit Pod Security Admission
namespace labels and v1 ValidatingAdmissionPolicy objects and bindings. It
distinguishes unlabeled modes, empty policy inventory, and an unavailable API.
It uses cluster read access and does not evaluate CEL, resolve parameter objects,
or infer cluster admission defaults or exemptions. The 68 targeted backend tests,
29 frontend tests, and Docker build passed. Live viewer and operator sessions
verified Fedora namespace labels and admission inventory. Browser fixtures
verified policy details, binding actions, type warnings, and the unavailable API state.

The **Security → Security reporting** page generates a timestamped, read-only
Kubernetes posture report with severity filters, resource details, per-source
coverage, and JSON export. Checks examine declared RBAC grants, Pod Security
enforce labels, default service account token automount, and admission policy
type warnings. Sources are capped at 1,000 objects each. The API uses
`clusters.read` and the selected project scope, and reads no Secrets.
The 110 backend regression tests, 29 frontend tests and Docker build passed.
Live Fedora reports passed for Keycloak viewer and operator sessions, including
resource details, refresh and JSON download. Browser fixtures also covered
incomplete coverage, severity filters, pagination and a failed refresh.

Platform backup and restore now uses age-encrypted PostgreSQL archives with credential key recovery,
an offline recovery CLI, and optional administrator-triggered recovery into a fresh database.
The cluster workflow uses Velero v1 resources and reports actual controller phases and storage readiness.
Setup, coverage and cutover are documented in [Backup and restore](documentation/backup-restore.md).
The recovery drill uses PostgreSQL and age with disposable databases. It covers credential
key recovery, interrupted operation handling, encrypted downloads and a retried restore.
The local Keycloak browser check verified administrator backup and download on the real worker,
plus operator and viewer access boundaries. The Fedora test cluster currently has no Velero
controller. Automated tests exercise Velero request submission and status retrieval using the
simulated Kubernetes API.

Phase 8's first slice adds cluster-scoped Prometheus connections and historical
node and pod CPU and memory charts under **Operations → Monitoring**. Migration
`0007` stores the selected Service. The dashboard uses fixed range queries
through the Kubernetes API Service proxy. The local Fedora cluster has four
healthy scrape targets with TLS verification for both kubelets. Live Keycloak
operator and viewer browser checks passed. The 104 targeted backend tests,
29 frontend tests, and Docker frontend build passed.
[Monitoring setup](documentation/monitoring.md)

The Grafana integration stores an optional per-cluster dashboard base URL and
links from Monitoring with the selected time range and pod namespace. The
Kubernetes provisioning manifests ship node and pod dashboards backed by the
existing Prometheus Service. Live Fedora browser checks verified operator and
viewer links, Grafana sign-in, and matching samples across all four panels.
The 81 targeted backend tests, 29 frontend tests, and Docker frontend build passed.

Loki log search under **Operations → Logs** uses a cluster-scoped Service
connection stored by migration `0008`. The Fedora lab runs Loki and Alloy in
`kplatform-monitoring`. The shipped collector covers that namespace. The
initial live lab also included `default/loki-log-test` through a pod-scoped Role.
The Namespace dropdown queries the selected Loki Service for the selected
lookback and preselects a sole result. Live operator and viewer checks found logs from both UIDs of a
disposable, replaced pod. The 83 backend tests, 29 frontend tests and Docker
frontend build passed. [Logging setup](documentation/logging.md)

## Remaining work and priorities

The user intends to finish Phase 8 extensions **one feature at a time**. The
complete remaining-work plan is `documentation/phase-8-remaining.md`, including
scope, proposed sequence, security decisions, acceptance criteria and validation
gaps. Namespace-scoped read-only operator access, approved public repository access
and opt-in encrypted output recording are now implemented; see the current
interactive handoff below. Do not start the next GitOps/workflow extensions until
the user directs it. Preserve the full-access administrator guard.
Runtime GHCR distribution and the five-scenario recovery drill are already done.

The user has progressed into Phase 8. Monitoring/logging, the first bounded
remediation runbook, Helm release management and initial GitOps manual sync/drift
reviews, opt-in automatic reconciliation and guarded pruning are implemented.
Continue GitOps only within the user's requested scope; Argo CD/Flux integration
remains future work.
External alert notification delivery and observability host-loss recovery are
explicitly scheduled towards the end of Phase 8 (see the README roadmap).
The Phase 7 items below remain hardening and integration-validation boundaries.

### Phase 7

- Authentication/session tests: OIDC login/callback/logout, token validation, expiry/revocation, CSRF and browser behavior, plus live identity-provider validation.
- Expand authorization/isolation coverage for organization/project selection, memberships, read/write roles, detail lookups, linked resources, workers, streams, secret reveals and terminal access. The original suite predominantly uses the development administrator; the new identity suite uses real OIDC authentication with a simulated provider transport.
- Further platform audit log filtering/reporting, credential access review and any selected external secrets-store integration.
- Validate Velero recovery against the deployment’s storage provider and application volumes.
- General API rate limiting and production TLS/reverse-proxy deployment guidance. Login-start rate limiting already exists.

### Requirements not fully represented by “completed” earlier phases

- Global search currently covers pages and recent operations, not live clusters/nodes/pods/deployments/namespaces/services/infrastructure.
- CRD discovery/schema guidance, reviewed instance CRUD and guarded definition lifecycle are implemented. A generic explorer for all API resource types and richer graphical schema forms remain incomplete.
- Deployment creation has a dedicated form; other workload creation primarily uses YAML/templates. Advanced scheduling and broader graphical creation need requirement-by-requirement review.
- Monitoring/logging's CPU, memory, disk/network metrics, operational findings,
  investigation guidance and five sustained Prometheus rules are implemented.
  External notification delivery remains separate work; the initial remediation
  runbook covers approved replacement of repeatedly failing stateless pods.
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


Phase 8 monitoring/logging follow-up (2026-11-18): disk usage, disk I/O and
network throughput use existing root-cgroup kubelet cAdvisor metrics; no new
host filesystem mounts or node-exporter permissions are required. The node
Grafana dashboard includes matching panels. Operations → Alerts checks node
conditions, container waiting and failed termination states, deployment failures,
API readiness, storage/network events, metric thresholds, and verified API
serving certificate expiry. It reports incomplete coverage and investigation
steps. Five native Prometheus rules expose inactive/pending/firing state; external
notification delivery is not configured. The remediation follow-up below adds
an opt-in, approval-controlled runbook; it is disabled by default.

Logs → Collected namespaces queues `cluster.logs.collection`, requiring cluster
write permission and an explicitly registered Alloy Deployment. The manager uses
versioned configuration, ownership-checked namespace Roles/RoleBindings, rollout
verification and owned-grant revocation. The Fedora lab collects `default` and
`kplatform-monitoring`; removing a namespace preserves stored history.

Grafana/Loki/Alloy manifests use PVCs; Prometheus claim/patch and alert rules are
under `infrastructure/monitoring/prometheus`. The live lab migrated all four data
directories to retained local PVs on tsnode1 and verified history/preferences
survive replacing every pod. These volumes remain node-local and require separate
host-loss recovery. See the monitoring and logging guides for setup and limits.
Final verification passed 48 targeted backend tests, Ruff/format/Mypy,
29 frontend tests, frontend lint/type checking and the Docker production build.
Live operator/viewer checks covered collection authorization, disk/network charts,
certificate coverage and five healthy Prometheus rules. A temporary failing pod
and unreachable scrape target verified actionable findings and a rule reaching
firing after its two-minute hold. All fixtures were removed. Both Fedora nodes
and monitoring pods were Ready, all four claims Bound, four scrape targets up,
and all six Grafana node panels returned data. The local UI and API run this code.

On 2026-11-23 the original prompt's monitoring/operations requirements were
checked against the implementation. Live checks verified administrator/operator/
viewer login, the real worker pipeline, both Ready Fedora nodes, per-node pod DNS,
fresh Loki ingestion from temporary probes on each node, all eight metric panels,
five healthy rules, all alert-source coverage and four Bound observability claims.
The temporary probes were removed. The original default logging pod is no longer
present; do not treat its absent logs as collector failure. The UI's Docker port
mapping and loopback Grafana tunnel were restored. Worker1 has about 1 GiB free
disk (83% used), limiting additional test workload capacity. Monitoring/logging is
complete for the current milestone, with external notifications, broader PKI
expiry coverage and host-loss storage recovery outside that milestone.

Remediation follow-up (2026-11-23): migration `0009` adds cluster policy JSON and
`RemediationRequest` observations/approvals. `modules/kubernetes/remediation.py`
registers the PDB-aware `cluster.remediation.replace_pod` workflow; Celery beat
runs `remediation.scan` every minute. Only explicitly enabled namespaces are
observed. Repeated failure (three restarts plus a stability window) creates a
proposal, never an eviction. Organization/platform administrators approve each
attempt on Alerts by typing the cluster name. Approvals expire, configuration
edits revoke unexecuted approvals, and each Deployment has a cooldown. The worker
revalidates UID/owner templates/current failure and verifies recovery. Stateful,
standalone, protected, privileged, host-coupled and data-volume pods are excluded.
Operation retries are administrator-only. Other alert types remain guidance.

Validation passed 90 targeted backend regressions including schema consistency,
a final 19-case remediation suite (one additional observation-gap case), Ruff,
format/Mypy, 29 frontend tests and the Docker production build. A real isolated
Fedora Deployment verified policy configuration through the UI, sustained failure
detection, operator/viewer denial, typed administrator confirmation, PDB refusal,
and a retried eviction followed by a Ready replacement. The fixture's ConfigMap
was corrected before retry; the runbook itself does not repair configuration.
The validation namespace was removed, default detection restored to disabled,
and the updated local API/worker/scheduler/UI are running. The operation history
retains the actual failed-then-succeeded drill for review. Helm was added in the
subsequent implementation below.

Helm follow-up (2026-11-23): **Workloads → Helm releases** implements public
HTTPS repository setup, chart/version browsing, values editing and expiring review
plans, release inventory/history, and tracked install/upgrade/rollback/uninstall.
`modules/helm/` contains bounded DNS-pinned catalog downloads, digest/archive
checks, isolated Helm subprocesses and workflow execution. Migration `0010`
stores per-cluster repositories and `HelmPlan` records; values are encrypted using
the existing SecretStore and never included in operation params/results/logs.
Helm 4.3.2 is checksum-pinned by `backend/tools/install_helm.py`, included in the
backend container, and installed locally under ignored `.tools/helm/bin`.

Mutations require cluster write access. Reviewed plans are user/project-bound,
expire after fifteen minutes and snapshot release revision/head Secret UID.
Upgrade, rollback and uninstall require typed release-name confirmation. The
worker checks the snapshot again after chart download; operation markers avoid
repeating an already committed successful action after recovery. Hooks are off
by default, Helm waits for readiness, and install/upgrade roll back on failure.
No ownership-taking, force replacement or automatic pending-release unlock is
exposed. Public history omits values/notes/descriptions; previews return resource
names/kinds only. See `documentation/helm.md` for offline-review/reused-values
limits, cancellation semantics, and repository trust/credential handling.

Validation: 82 targeted backend regressions (Helm, migrations, identity and
orchestration), plus the final 18-case Helm-specific suite and a queued-expiry check, frontend
checks (29 tests/type checking/lint/build) and both container builds. Real operator/viewer browser validation used Bedag's public
`raw` 2.0.2 chart in an isolated namespace: install created revision 1, upgrade
changed ConfigMap data at revision 2, rollback restored data at revision 3, and
uninstall removed the release and ConfigMap. Viewer mutation denial, encrypted
values and sanitized history/operation payloads passed. A delayed-response browser
check verified that editing values discards an in-flight older preview. All drill namespaces and
repository changes were cleaned up. Migration `0010` is applied locally and the
API/worker/scheduler/UI run this integration. GitOps remains unstarted.


Helm private-source follow-up (2026-11-23): migration `0011` adds encrypted,
revocable repository credentials. HTTPS indexes and full OCI chart URLs support
Basic/password-token and direct Bearer authentication. Read APIs expose only opaque
auth references. OCI token exchanges request chart-specific pull access, require
explicit trust before forwarding credentials to another token-service origin, and
pin manifest/layer digests. Credential rotation/removal and source configuration
changes invalidate affected new install/upgrade reviews. Public `0010` plans retain
their compatibility path. Rollback/uninstall do not require the chart source.

Organization/platform administrators control private RFC1918/ULA endpoints,
custom TLS ports, CA bundles and exact OCI token-service trust; operators can use
configured sources. TLS verification remains enabled; metadata/link-local/loopback
addresses are blocked and cross-origin redirects drop credentials/custom trust.
The GUI supports source editing/credential rotation and explicit OCI versions when
tag listing is unavailable. See `documentation/helm.md` and `test_helm_sources.py`.

Validation: 94 targeted backend tests, Ruff/format/Mypy, 29 frontend tests,
frontend lint/type checking and both production container builds passed. Real
TLS/Basic-protected HTTPS and OCI fixtures verified discovery, chart defaults,
write-only encrypted credentials and source editing in the browser. An operator
installed/removed the HTTPS chart, then installed/upgraded/rolled back/removed the
OCI chart on the Fedora cluster; rollback restored the original ConfigMap data.
The temporary namespace and registry were removed, repository settings restored,
and fixture credentials revoked. No fixture password was found in publishable
files or application logs. Migration `0011` is applied locally; API/worker/scheduler
and UI run this integration. GitOps remains unstarted.


GitOps follow-up (2026-11-23): **Workloads → GitOps** now connects an HTTPS Git
repository/branch/manifest directory to one existing namespace. Migration `0012`
stores scoped applications and encrypted, user-bound fifteen-minute commit reviews.
Review shows names/actions, records manual drift observations and flags foreign
field ownership as Conflict. Typed confirmation queues `cluster.gitops.sync` with
cluster exclusivity. The worker applies the encrypted snapshot, revalidates live
UID/state, records inventory after each write and recognizes its own partial writes
on retry. Resources outside the app's ownership are never adopted; foreign SSA
conflicts block sync. Creation uses POST to refuse concurrent adoption, then a
UID/resource-version-guarded handoff relinquishes only its own POST manager after
non-forced apply establishes shared ownership. Other managers are preserved.

Git 2.46+ is required on API/worker hosts and included in the backend image; local
Git is installed under ignored `.tools/git`. Fetches pin DNS and TLS through a
one-destination CONNECT tunnel, isolate Git configuration, disable redirects/local
protocols/hooks/submodules, and bound time/network/repository/manifest sizes.
Credentials are write-only/encrypted. Administrators control private endpoint/CA
trust; operators can use existing profiles. Plain YAML/JSON only: no render/build
execution. Sync completion means Applied, not workload readiness. Drift checks
are manual reviews, not background reconciliation. No pruning: removed manifests
and forgotten application registrations leave Kubernetes resources in place.
Automatic reconciliation and SSH transport were implemented in subsequent
milestones below; Argo CD/Flux integration remains future work. See `documentation/gitops.md`, `modules/gitops/` and `test_gitops.py`.


GitOps validation: the final 27-case GitOps/migration suite passed, after the wider
backend run passed 107 cases and exposed one missing test import that was corrected
and rerun in the final suite. Private Helm-source regressions also passed in the
preceding combined run. Ruff/format, full Mypy (156 files), an additional check of
untyped GitOps function bodies, 29 frontend tests, frontend lint/type checks and
both production builds passed. A real public anonymous Git fetch and TLS/Basic
private Git fetch passed, including the packaged backend. Browser/operator/worker
validation on Fedora verified source setup, blank credential readback, immutable
review execution after branch movement, next-commit updates, InSync observations,
blocked foreign field conflicts, missing-resource recreation, per-review typed
confirmation and registration removal retaining its resources. Viewer read/write
boundaries and encrypted credential/history checks passed. Only the owned fixture
namespaces/repository were removed; fixture credentials and registrations were
deleted, with no password found in publishable files or application logs. Both
Fedora nodes and all active pods were Ready afterwards. Migration `0012` is applied
locally and API/worker/UI run the implementation.


Automatic GitOps/pruning follow-up (2026-11-23): migration `0013` adds the
per-application automation JSON and persisted next due time. Both automation and
pruning default off. Administrators approve a generation-bound policy by typing
the app name (60–3,600 second interval, default 300); a stale generation cannot be
approved. Operators may pause or request a check under an active policy. Policy
changes/pause advance generation and revoke pending reviews/further mutations;
source/credential edits pause automation and require administrator reapproval.

Celery beat dispatches `gitops.reconcile` every 30 seconds. A due app queues
`cluster.gitops.reconcile` with fetch/review and apply/prune checkpoints, project
scope and cluster exclusivity. Source failures and conflicts do not prune. No-op
checks do not rewrite objects. The UI exposes policy, next due time, last operation,
automatic/manual history and explicit manual prune/allow-empty review options.

Pruning uses only recorded inventory absent from the pinned commit. Ownership,
UID, namespace, canonical API path, live state and API discovery are revalidated;
DELETE includes UID/resource-version preconditions. Only explicit built-in stateless
kinds/versions are eligible. PVCs, StatefulSets, custom resources and live retention
annotations are retained. Desired supported workloads must be ready before deletion;
Jobs need completion, with a 120-second gate. Deletion waits up to 120 seconds per
resource, retains pending inventory, records retry progress and never strips
finalizers. Empty sources fail closed unless separately approved, and the Git
directory must still exist (for example with `.gitkeep`). Forgetting registration
still retains resources. Pausing cannot undo requests already accepted by Kubernetes.
See `documentation/gitops.md`, `automation.py`, `pruning.py` and
`tests/test_gitops_automation.py`. Argo CD/Flux integration remains unstarted.


Automatic reconciliation/pruning validation: 146 targeted backend regressions
passed sequentially, including policies, scheduler, pruning, legacy reviews,
Helm, identity, orchestration and model/migration consistency. Ruff/format,
full Mypy (159 files) plus checking untyped GitOps bodies, 29 frontend tests,
frontend type checking/lint and both production builds passed. The real TLS/Basic
Git fixture and actual Celery beat/worker/Fedora cluster verified scheduled initial
sync, branch updates, retention with pruning off, owned pruning with it on, no-write
unchanged checks, protected/untracked retention, operator pause across a scheduler
tick, administrator-only approval, default empty-source refusal, separately approved
empty-source pruning, manual prune review/typed confirmation and viewer boundaries.
Source failures appear in actual operation history without raw output. All fixture
registrations/namespaces were removed; private Git credentials/certificates were
cleaned up and secret checks found no matched local secrets in publishable files
or fixture passwords in application logs. Final Fedora nodes and active pods were
Ready. Migration `0013` is applied locally; API, worker, scheduler and UI run the
new integration. Protected data kinds, readiness/finalizer failure paths and UID/
ownership races have simulator coverage; live pruning used isolated ConfigMaps.

Custom resources initial integration (2026-11-23): **Configuration → Custom
resources** discovers installed definitions, versions/scopes and safe schema
field guidance, then lists metadata/conditions and offers reviewed instance
create/update/delete. Migration `0014` stores author-bound encrypted reviews with
15-minute expiry. Full schema/resource YAML requires an explicit audited
`secrets.reveal` action; ordinary queries omit spec/status values, defaults,
examples and condition messages. Revealed YAML stays outside the shared query
cache. Server dry runs validate manifests; delete dry runs set `dryRun: [All]`
in the DeleteOptions body (the live custom-resource endpoint did not honor the
query alone when a body was supplied). Every execution requires the object name.
Definition UID/spec and resource identity/content are rechecked; edits require
an unchanged revealed snapshot while status-only updates remain permitted.
Writes preserve finalizers, owners and reserved ownership markers; deletion
requests foreground cleanup and never strips finalizers. API acceptance is not
resource health or completed deletion. Definition installation/upgrades/removal,
conversion-webhook management and operator integrations remain separate work.
See `documentation/custom-resources.md`, `custom_resources.py` and
`tests/test_custom_resources.py`.

CRD validation: 140 backend regressions passed, followed by 32 final CRD cases
including two live-discovered corrections (edit snapshot copy isolation and
body-bound delete dry runs). The frontend's 29 tests, type checking, lint and
production build passed. Full Mypy passed across 162 files, and the final CRD service/routes passed
with untyped function bodies checked; Ruff/format passed. Both production images
built successfully.
Live Keycloak operator/viewer browser checks on isolated Fedora CRDs covered
schema rejection, encrypted reviews, stale in-flight previews, metadata-only
viewer access/reveal denial, served/unserved versions, namespaced/cluster CRUD,
status/ownership preservation and finalizers. Delete reviews were explicitly
checked to leave both resources unchanged before confirmation. Fixtures/reviews
were removed with UID checks; both nodes and all 17 active pods were Ready.
Publishable-file and application-log secret checks passed. Migration `0014` is
applied locally; API, worker, scheduler and UI run the integration. Third-party
operators and conversion webhooks still require relevant live validation.

CRD definition lifecycle follow-up (2026-11-23): `definition_lifecycle.py`
adds administrator-only reviewed install/update/remove using the encrypted `0014`
review table, separated by category and execution endpoint from instance reviews.
No new migration is needed. UI actions live on Configuration → Custom resources.
Definitions installed here carry cluster-bound ownership labels; existing,
Helm/GitOps/controller-owned definitions are never adopted. Full definition YAML
is an explicit audited reveal outside shared query caches. Updates bind that
snapshot, preserve ownership/finalizers/status and recheck stored versions.
Only None conversion is supported. Group/names/scope, storage version and old
version entries are retained; previously served versions stay served. Any spec
change requires zero instances, while metadata-only changes can retain instances.
Removal repeats all-namespace inventory checks at review and execution, fails
closed on incomplete/unavailable inventory, uses body-bound dry run and UID/RV
preconditions, and preserves finalizers. Inventory and deletion are separate API
requests: concurrent writers are not fenced, and Kubernetes can delete instances
created in the gap. The UI prompts users to pause writers first. API
establishment and deletion cleanup remain separate observed conditions.
Storage migration, webhook/operator installation and custom workflows remain
separate milestones. See `documentation/custom-resources.md` and
`tests/test_definition_lifecycle.py`.

Definition lifecycle validation: 136 backend regressions passed, followed by all
35 definition cases against final source. Full Mypy passed for 163 files with
untyped bodies checked for the definition service and routes. Ruff/format,
frontend type checking/lint, 29 frontend tests and both production images passed.
Live Keycloak/Fedora browser validation covered administrator/operator/viewer
boundaries, encrypted non-mutating install review, typed confirmation, explicit
reveal, storage-version refusal, empty schema update with an additional served
version, nonempty removal refusal, instances created between review/execution,
non-mutating delete previews, pending deletion and finalizer preservation.
Only isolated platform-owned fixtures were changed; their UID-matched resources
and review rows were removed. Both Fedora nodes and all 17 active pods were Ready.
Publishable-file/application-log secret checks passed; private fixture state was
removed. API, worker, scheduler and production UI run the integration locally.
Custom workflows and advanced CRD operations were subsequently implemented below;
external notifications and observability host-loss recovery remain scheduled towards the end.

Custom workflows and advanced CRD operations (2026-11-23): migration `0015`
adds encrypted tenant/cluster-scoped `CustomWorkflow` definitions; immutable runs
reuse `CustomResourceReview` with separate categories and execution routes. See
`modules/custom_workflows`, `kubernetes/reviewed_operations.py`, `crd_operations.py`,
`documentation/custom-workflows.md` and `documentation/crd-operations.md`.

Operations → Custom workflows supports reusable YAML sequences (1–20 steps):
cluster readiness check, Deployment/StatefulSet scale, Deployment restart, and
bounded readiness waits for Pods/Jobs/Deployments/StatefulSets/DaemonSets. Admins
manage definitions; cluster writers review/run. Reviews pin generation, UID and
content, dry-run mutations, encrypt full plans and require typed confirmation.
Each step is an operation checkpoint with cluster exclusivity, cancellation,
retry and history. Worker guards re-resolve the requesting user's current database
permissions before each action/wait. Template edits/deletion revoke further work.
No shell/variable interpolation, arbitrary manifests, branching or schedules.
Completed actions remain on later failure/cancellation; drift can require a fresh
review. Wait start times survive retry. Full definitions require audited reveal.

CRD migration certification requires native storagemigration.k8s.io/v1 resources,
with UID/spec/condition checks and retained diagnostics. Other active migrations
for the resource block approval. Cancellation requests deletion of only the
owned native job; accepted controller writes remain. Explicit rewrite-only
maintenance is separately named, requires one observable mirrored API server and
control-plane node, uses the already selected storage version, and always retains
old storedVersions with RewrittenUncertified output. It never certifies encoding
from a manual pass/discovery hash. Inventory caps are 5,000 objects/100 pages.
None conversion requires identical schemas. Native partial failures retain the
selected storage and writes; only native success permits history cleanup.
Status PUT must preserve full metadata: live testing found a minimal metadata
body erased ownership labels; this is fixed and covered by a realistic status fake.

Conversion operations configure an existing Service-backed TLS ConversionReview
v1 server, requiring current certificate-only PEM CA input, Service UID, selected
TCP port and Ready EndpointSlices. No arbitrary URL/ExternalName, private keys or
traversal paths. Configuration waits for a unique temporary short-name discovery alias in the
same spec update before probing, then removes that alias with observation. The
barrier observes API discovery before probing; it does not attest all HA backends. Existing
instances (up to 200) are probed via every served API and unchanged dry-run writes. Failure/cancellation attempts conditional restoration
of the prior spec; concurrent changes can block rollback. Zero-instance results
explicitly lack protocol proof. These features do not generate/deploy business
conversion logic or retire API versions. Encrypted plans/config stay out of public
operation parameters, logs, histories and shared query caches; worker API clients
are reused/closed per step. See `tests/test_advanced_automation.py`.

Automation validation: all 171 final backend regressions passed (workflow/CRD,
identity, orchestration and migration suites), along with Ruff/format and Mypy
for 169 files with untyped bodies checked in the new services/routes. The 29
frontend tests, type checking/lint and both production image builds passed.
Migration `0015` is applied locally; API, worker, scheduler and UI run the feature.
Real Keycloak browser/Celery/Fedora validation completed a four-step workflow on
a zero-replica fixture Deployment (no workload image pulls), operator/viewer
boundaries, CA/SAN-verified TLS ConversionReview v1 via a temporary scoped Service,
native storage migration/certification from v1 to v2 with UID/content retained,
a deliberately failing conversion server causing conditional restoration, and a
successful post-restoration protocol probe. The HTTP test server must parse the
request path separately from Kubernetes' timeout query parameter; earlier exact
URI matching falsely accepted the intended failure route. This was a fixture
issue, not evidence of stale converter caching. All live fixtures/review rows were
removed by UID. API-level/native-controller proof is distinct from reading etcd;
HA/multi-provider validation and arbitrary business conversion semantics remain
separate. Final publishable-file/application-log secret checks passed; private TLS
material and fixture state were removed. Both Fedora nodes and all 17 active pods
were Ready at final verification. Inspect the Git history for the recorded
automation milestone and any subsequent changes.


Command Center first stage (2026-11-23): Operations → Command center provides
reviewed, read-only summary commands: `kubectl get` for an explicit built-in
resource allowlist and `helm list`/`helm history`. Kubernetes syntax uses the
existing REST client (no kubectl executable); Helm uses the pinned private runner.
See `documentation/command-center.md`, `modules/command_center/service.py` and
`tests/test_command_center.py`. Reviews/results reuse the encrypted `0014` table;
no migration. The worker rechecks the original human's current permissions and
expiry; output/history are author-bound. Ordinary operation data contains only
execution metadata. Output is explicitly fetched outside the shared query cache.
The parser rejects arbitrary flags, Secrets/configuration, endpoints, kubeconfigs,
impersonation, files, shell syntax and mutations. Lists/output/network reads and
Helm execution are bounded; cancellation uses the existing operation engine.
The staged roadmap is in README: focused summaries first, then selected reviewed
mutations, then separately designed isolated interactive sessions. Broader CLI
compatibility, streaming and interactive shell access are not implemented here.
Validation: 112 final Command Center/Kubernetes/Helm backend tests passed; the
preceding shared regression run also passed identity, orchestration and advanced
automation cases (its two Command Center fixture failures were corrected and
rerun). Ruff/format and full Mypy for 172 files passed, with untyped bodies checked
in the new service/routes. Frontend type checks/lint, 29 tests and both production
image builds passed. Actual Keycloak operator browser/Celery/Fedora validation
passed review, typed confirmation, node-summary retrieval, edited-preview
invalidation, viewer denial and cross-author output isolation. Real pinned Helm
list passed. The user subsequently confirmed successful live Helm history
retrieval through Command Center on the Fedora test cluster; this closes the
initial history-validation gap (user-reported verification). Both Fedora nodes and all 17 active pods were
Ready afterwards; local platform readiness passed. API, worker, scheduler and
production UI run this slice. Publishable-file local-secret checks passed.
Read-only command history from the live drill is retained; no cluster workloads
were created or changed. Inspect Git history/status for this milestone and later
changes.


Command Center reviewed mutations (2026-11-24): supports Deployment/StatefulSet
scale (0–1,000 replicas), Deployment rollout restart and Helm rollback to an
explicit older successful revision. Parser remains a strict allowlist with no
shell, credentials/context flags, arbitrary files, hooks, force or deletion.
Reads retain cluster-name confirmation; mutations require target workload/release
name confirmation and display cluster, namespace, identity and intended changes.
Mode is selected from the stored review and uses `cluster.command.mutate`.

Kubernetes commands share `prepare_mutation`/`apply_mutation` with custom workflows:
review server dry-run, UID/content binding, repeat dry-run before mutation,
resourceVersion CAS and exact recovery marker/content recognition. Version 2
mutation fingerprints additionally bind UID and last-applied annotations omitted
from display diffs. Older pending mutation reviews require a fresh review; reads
and completed results remain usable. Admission drift
now prevents a custom-workflow write rather than only reporting a changed accepted
write; the regression assertion was updated accordingly. Results report Accepted,
not rollout readiness. Whole reviewed objects remain encrypted, never public logs.

Helm rollback reuses `HelmPlan` and the existing executor via `execute_plan` with a
final command guard. Current/target revision Secret contents and plan spec are
bound. Review displays target inventory and current resources absent from it.
Review/execution use Helm server simulation with hooks disabled. This does not
prove admission on every resource or all deletion effects; external Helm writers
must be paused, because revision checks do not fence them. Current human access,
review expiry and revision content are rechecked before mutation. Existing owned
revision recovery avoids repeating a completed rollback. Accepted changes remain
on failure/cancellation. Install/upgrade/uninstall remain on Helm management UI;
initial interactive sessions were subsequently implemented below. See `documentation/command-center.md` and
`tests/test_command_mutations.py`. No new migration is required.
Validation: 80 final mutation/workflow tests passed, including all new parser,
confirmation, encryption, UID/spec/admission drift, resourceVersion race,
StatefulSet, retry-after-acceptance, role revocation (queued and after dry run),
Helm history-content and review-error redaction cases. The earlier broad run
passed 209 backend cases; its obsolete admission-after-write expectation was
updated and passed in the final run. Ruff/format, Mypy for 173 files (untyped
bodies checked in command services/routes and shared workflow helpers), frontend
type checks/lint, 29 frontend tests and both production images passed. Built
backend and running frontend hashes match final source.

Actual Keycloak GUI/worker/Fedora validation passed typed target confirmation,
non-mutating Kubernetes review, scale/restart at zero replicas, stale-content
refusal, Helm server simulation without revision writes, and rollback of a new
isolated zero-replica chart from revision 2 to revision 1 creating deployed
revision 3. Viewer mutation review was denied. No fixture pods were started.
The UID-owned temporary namespace/release and private fixture reviews/plans were
removed; operation/audit history remains. Both Fedora nodes and all 17 active
pods were Ready after cleanup; platform readiness and publishable-file/log secret
checks passed. API, worker, scheduler and production UI run this milestone.
Inspect Git status/history for this milestone and subsequent work.


Interactive Command Center (2026-11-24): `sessions.py`, `session_models.py`,
`api/routes/command_sessions.py` and migration `0016` add author/tenant/project-bound
leases and tracked `cluster.command.session.prepare` operations. Platform-admin
access plus terminal/write/reveal permissions, explicit full-cluster acknowledgement
and typed cluster name are required. Operators retain reviewed commands only.
Ordinary management API paths hide/reject UUID-shaped session namespaces and
grants (`kubernetes/reserved.py`); generic pod exec and rendered Helm inventories
are also guarded. Session lifecycle handlers explicitly opt out after admin/author
authorization. The reviewed command runner retains bounded-response reads through
the guard. The interactive shell intentionally has cluster-admin access and bypasses per-change
reviews; it is for trusted administrators, not a hostile-code sandbox. User-created
resources/grants are not rolled back on session close.

Each session gets a unique restricted namespace, non-root read-only tools pod,
service account, owned grant, API-only NetworkPolicy and ephemeral credential
Secret. Only a short-lived TokenRequest bound to the pod UID is mounted, never the
parent client key/token; responses exceeding the requested expiry bound fail closed.
The supplied runtime image contains checksum-pinned kubectl 1.37.2/Helm 4.3.2;
`COMMAND_CENTER_RUNTIME_IMAGE` is required and must pin a trusted image digest.
`COMMAND_CENTER_KUBECTL_MINOR` must match that image; observed server skew >1 minor
or unknown versions are refused. Runtime CPU/memory/storage and input/output are
bounded. API-only egress requires a NetworkPolicy-capable CNI and limits external
Helm repository operations. No backend Docker socket or host-shell access is used.

Sessions are single-attach, at most 10 minutes, with a 60-second Ready attachment
window and 2-minute input-idle limit. Current auth/context/lease is checked every
5 seconds. Disconnect/cancellation writes durable Closing state under cancellation
shielding. Cross-replica advisory locks serialize provisioning/cleanup; row locks
preserve newly recorded UIDs and recheck reaper eligibility. Cleanup binds the
cluster's kube-system namespace UID, revokes owned grant subjects, and removes only
UID-owned grant/namespace objects, preserving finalizers. Closed requires observed
absence of both. Beat reaps every 10 seconds, including heartbeat abandonment after
20 seconds; unavailable targets retain cleanup-pending state for retry. Token expiry
and pod active deadline are independent backstops. Deletion, unregister and connection
changes are blocked until sessions are closed. Closed history cascades with cluster
unregistration. The original slice audited lifecycle metadata only; the later
opt-in encrypted output recording below supersedes its no-transcript limitation.
Provisioning never copies the parent key/token, and terminal content stays out of
ordinary logs.

See `documentation/interactive-command-center.md`, the reviewed runner guide and
`tests/test_command_sessions.py`. Local testing builds the public tools image and
preloads its digest on both pinned Fedora nodes; it is not published to a registry.
The ignored backend `.env` now enables that exact cached image. Other/new nodes need
a trusted registry image or the same preload. Migration `0016` is applied locally;
API, worker, scheduler and production UI run the session implementation. The real
Keycloak GUI/Pod exec drill passed kubectl nodes, Helm version/list, operator denial,
full target confirmation, grant/namespace cleanup and delegated-token revocation.
Closed validation leases/audit history are retained; no active validation grants or
runtimes remain.

Final validation: 189 targeted backend regressions passed (session leases, reserved
resource paths, reviewed commands/mutations, workloads/terminals, Helm, GitOps and
migration consistency). Earlier identity/orchestration/lifecycle regressions also
passed; the local-image setting required correcting the disabled-feature fixture
and the corrected session/migration suite passed. Ruff/format, full Mypy for 177
files with untyped new services/routes/reserved API bodies checked, frontend type
checks/lint, 29 frontend tests and all three production/runtime image builds passed.
Built backend and running frontend source hashes match final implementation.

Live Keycloak/Fedora checks additionally denied ordinary Secret/pod-detail/pod-exec
access to an active privileged runtime, verified actual browser-navigation cleanup,
confirmed signed-token expiry/Pod UID claims, and exercised the real beat/worker
cleanup task with an accelerated unattached lease. Final checks found no active
leases, runtime namespaces or grants, both Fedora nodes and all 17 active pods Ready,
healthy platform readiness, and no matched local secrets/JWTs in publishable files
or application logs. This is local Fedora validation, not HA/cloud/CNI certification.
Inspect Git status/history for this milestone and subsequent work.

Command Center distribution/recovery follow-up (2026-11-24): the manually
dispatched `.github/workflows/command-runtime.yml` publishes checksum-verified
amd64/arm64 tools to GHCR with a short-lived Actions package token and pinned
action commits. Publishing commit `b12f378`, run `45423664082`, produced public
`ghcr.io/sullivt3/kubernetes-platform/command-runtime@sha256:7393b4a2cf2247d624278ab19ddee1e3100069a023e9b4e043266da389614a20`.
Anonymous manifest access and direct CRI pulls on both pinned Fedora nodes passed;
Docker executed kubectl/Helm for amd64 and emulated arm64. The private backend
configuration and API/worker/scheduler now use this digest, superseding the earlier
preload-only limitation. Future nodes need registry access; no private pull token
is needed for this public tools image.

`scripts/check-command-recovery.py` passed five live scenarios with real timers:
SIGKILL API during browser attachment, worker/beat loss, real Redis pause during
close, interruption of a temporary registration's TLS pass-through connection,
and a binding finalizer. Durable Closing state survived outages; cleanup preserved
finalizers, revoked subjects before deletion and completed after recovery. The
delegated token lost authorization. Normal cluster connectivity and Fedora hosts
were not disrupted. The fixture registration/runtimes/grants were removed, native
processes restored, platform readiness passed and both nodes/all 17 active pods
were Ready. The 26 session/migration regressions passed. New local process logs
are in ignored `.tools/command-recovery/`; they use `python -m celery` rather than
the older CLI executable form. Do not run tests while this drill pauses Redis.
See `documentation/command-center-recovery.md` for the repeatable harness and
limits: single local replica/Fedora only, no HA/cloud/host-loss certification.
The feature extensions and two end-of-Phase-8 items in the roadmap remain planned.

Interactive extensions (2026-11-24): `command_center/access.py` and migration
`0017` add platform-admin-managed user/namespace assignments bound to cluster,
project and namespace UID. Operators receive a separate disposable runtime SA and
namespaced read-only Role/RoleBinding, with an explicit resource allowlist. Secrets,
logs, writes, RBAC, nodes, exec/attach, port forwarding, proxy and TokenRequests
are excluded. Grant/current membership is checked at start, preparation, attach
and heartbeat. Revocation commits Closing leases with assignment deletion and
queues owned grant/Role/runtime cleanup. Ordinary resource routing also reserves
UUID-shaped session Role/RoleBinding names. Full administrator sessions remain
acknowledged cluster-admin; broader platform operator permissions outside the shell
are unchanged. Existing cluster grants to authenticated SA groups can augment RBAC.

`command_center/repositories.py` adds admin-managed per-cluster public hostnames,
explicit per-session opt-in, public-only DNS checks, pinned host aliases and TCP443
NetworkPolicy rules. No general DNS is opened. Mixed/private/metadata/mapped answers
fail closed. Saving policy closes opted-in leases; worker/attach/heartbeat recheck
policy. This is an IP allowlist, not an HTTP hostname firewall: shared/CDN IPs may
serve other hosts. Private network targets/custom CA and stored credential copying
are excluded; full cluster administrators can deliberately alter network policies.
Real Fedora checks passed default internet denial, approved Bedag HTTPS index and
GHCR podinfo OCI chart downloads, metadata/unlisted destination denial and cleanup.
The original repository policy was restored.

`command_center/transcripts.py` and migration `0018` add explicit-consent terminal
output recording, off by default. Raw input events are not hooked, but TTY output
can echo commands/passwords and any printed Secret. Encrypt each frame before
browser delivery; recording failure closes the session. Bounds: 2MiB/4096 frames,
24h from session creation. Author/current terminal+reveal permissions or scoped
platform admin access govern audited no-store JSON export; metadata is paginated,
admin can include other authors, closed recordings can be deleted. Expiry denies
reads immediately; the ten-second session reaper purges expired ciphertext when
services return. Keep encryption keys for retained data/backups. Downloads/backups
have independent retention, and abrupt API loss can truncate output. All session
history/operation/audit payloads retain metadata only, except the separate encrypted
chunk table. Native live checks passed real recording, ciphertext persistence,
operator/viewer denial, export/deletion and real beat purge with an accelerated
owned expired recording. Validation recordings were removed; closed audit retained.

Scoped access passed 34 scoped/session/migration tests, 29 frontend tests and real
Keycloak/Fedora operator browser, delegated-token denial and admin revocation checks.
Repository/scoped tests passed 25 cases. The initial combined suite passed 56 tests;
the final shared regression suite passed 359 tests, followed by two additional
namespaced binding-finalizer/Role-UID cleanup tests. Full Mypy passed 180 files with
strict changed-module checks; Ruff/format, frontend type/lint/29 tests and both
production builds passed. Final real UI checks covered assignment/revocation,
operator confirmation/shell, repository policy/opt-in and Helm HTTPS download,
recording consent/status and JSON download/delete. Native dependencies and both
Fedora nodes/all 17 pods were healthy, with no active validation resources or
recording chunks; source hashes matched built backend/running UI. Private-secret
and JWT scans found no matches in publishable files or native/build/live logs.
See the interactive guide for exact validation boundaries. Migration `0018` is applied locally, native API/worker/beat
run the new backend; logs remain in ignored `.tools/command-recovery/`. The runtime
image digest is unchanged. The recovery harness now skips unrelated process lines
that cannot be parsed as shell arguments. Local Fedora evidence does not certify
HA, managed clouds, other CNIs or arbitrary cluster RBAC policies.

GitOps SSH source milestone (2026-11-24): `gitops/ssh_source.py` adds in-memory
Ed25519/ECDSA/RSA-2048+ key authentication with optional encrypted-key passphrases,
and an exact server public-key pin checked before client-key authentication.
`source.py` accepts explicit ssh:// URLs and common user@host:path URLs, preserving
SCP-relative versus explicit absolute paths (SCP URLs are retained rather than
normalized into absolute URLs). SSH username comes from the URL.
Administrators control new/changed host trust, private targets and custom ports;
operators may reuse existing trusted sources and rotate encrypted credentials.
Blank editing fields preserve the key/passphrase; URL changes require re-entry.
SSH credentials cannot be removed without switching to HTTPS. No schema migration;
GitOps existing encrypted credential JSON and generation/automation invalidation
are reused. Existing HTTPS Basic/Bearer behavior is preserved.

The bridge connects only to the DNS-pinned approved address and offers the key
only after host verification. Private key/passphrase never reach disk or child
environment/argv. Git's helper is a capability-authenticated binary loopback relay;
its destination/user/upload-pack command are fixed by the parent, without system
SSH, agents, SSH config, forwarding, password fallback or remote code execution
beyond upload-pack. Existing 120s/128MiB/output/manifest limits and cancellation
apply. Production loopback/metadata policy stays intact; unit loopback exceptions
exist only in explicitly patched fixture tests. Git 2.46+ and the existing Paramiko
dependency suffice. Host certificates/agents/Git write-back remain out of scope.

Live Keycloak/browser/private-address SSH fixture/Fedora validation passed encrypted
key/passphrase form submission, commit-pinned review, manual worker sync, credential
preservation and blank readback, wrong server-key refusal before auth, and actual
automatic reconciliation of a second commit. Test repository/server/key/application,
reviews and UID-owned Fedora namespace were removed; operation/audit metadata stays.
One interrupted attempt left a temporary namespace after a connection reset; it
was identified and removed with its exact UID. A clock jump expired a test JWT;
the identity regression passed on rerun, with no authentication behavior changes.
Final source hashes matched the built backend/running frontend. Platform readiness,
both Fedora nodes and all 17 active pods were healthy; no SSH fixtures remained.
Private credential/JWT/key scans passed, excluding only the unchanged nonparseable
fake-key marker in `test_cluster_lifecycle.py` from the key-header check.
Validation: 74 GitOps/SSH/automation/schema cases, 94 overlapping shared
SSH/identity/orchestration/HTTPS/schema cases, two focused policy/cancellation
cases and nine URL-validation cases passed. Mypy checked 181 files with GitOps
untyped bodies included; Ruff/format, frontend type/lint/29 tests and production
backend/frontend builds passed. RSA and Ed25519 encrypted/plain keys were exercised
through actual Git/SSH transfers, and a capability-less local relay connection was
refused before key use. See `documentation/gitops.md` for live evidence and limits. Stop after SSH: Helm rendering is the next listed feature,
and must wait for the user's instruction.

## Local development and checks

Database credentials have no shared default. Set `DATABASE_URL` explicitly and
supply `POSTGRES_PASSWORD` to the development Compose stack from private local
configuration. GitHub secret scanning and push protection are enabled. A redacted
history audit found only dummy bootstrap-token fixtures, with no matches for the
generated local secrets. The former development database default was removed
from the current tree; historical commits were not rewritten.

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
