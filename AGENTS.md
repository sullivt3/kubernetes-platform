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
- **Persistence:** Alembic migrations live in `backend/migrations/`. Current migration head is `0015`, adding encrypted reusable custom workflows after `0014` custom-resource reviews.
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

Context reviewed **2026-11-23**. The monitoring/logging milestone is implemented and live-validated. Phase 8 remains in progress. Check current HEAD and update this file when milestones change.

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
| 8 — Advanced operations | **In progress.** Monitoring/logging, approval-controlled stateless pod remediation and Helm release management implemented. Helm covers public/private HTTPS repositories and OCI registries, encrypted authentication, chart/version browsing, editable values reviews and install/upgrade/rollback/uninstall. GitOps manual sync, opt-in automatic reconciliation and guarded pruning are implemented; installed CRD discovery and reviewed instance CRUD are implemented; guarded lifecycle of platform-installed definitions is implemented; reviewed custom workflows, native/manual storage migration and Service-backed conversion configuration are implemented; broader workflow actions, controller integrations and command center remain planned. External alert notifications and observability host-loss recovery are scheduled towards the end of Phase 8. |

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
Automatic reconciliation, SSH transport and Argo CD/Flux integrations remain future
work. See `documentation/gitops.md`, `modules/gitops/` and `test_gitops.py`.


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
