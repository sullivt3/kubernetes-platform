# Phase 8 remaining work

Updated 2026-11-24 with the interactive access/repository/transcript milestone.
This is the implementation backlog for
the remaining Phase 8 capabilities discussed with the user. Items below are
planned unless their status states otherwise. Implement one feature at a time; finish
its tests, live checks and handoff before starting another. Documenting an item
does not implement it or authorize an infrastructure change needed later.

## Completed baseline

Monitoring/logging, approval-controlled stateless pod replacement, Helm management,
native platform GitOps manual/automatic reconciliation and pruning, CRD discovery
and reviewed lifecycle/CRUD, bounded custom workflows, storage migration and
existing-Service conversion configuration/probes are implemented. Command Center
provides reviewed summaries, selected mutations, full administrator and scoped
read-only operator sessions, approved public repository access and optional
encrypted terminal-output recording.
Their detailed guides remain authoritative about supported scope and limits.

Runtime distribution and the initial outage-recovery follow-up are **complete**:
public digest-pinned amd64/arm64 GHCR image, direct pulls on both Fedora nodes,
five real recovery scenarios, 26 session/migration regressions and a final live
browser check. See [distribution and recovery](command-center-recovery.md).
Do not reopen these as unfinished preload-only work.

## Delivery order

The user intends to finish the remaining capabilities one feature at a time.
The three interactive features are implemented and validated. The next listed
extension is GitOps SSH authentication; wait for the user's instruction before
starting it. The user can steer the proposed sequence below.

| Order | Feature | Status |
|---|---|---|
| 1 | Namespace-scoped read-only operator sessions | Implemented; 34 scoped/session/migration tests, 29 frontend tests and live operator browser/token/revocation checks passed |
| 2 | Controlled external repository access in interactive sessions | Implemented; 25 repository/scoped tests and live HTTPS/OCI, default-deny and destination checks passed |
| 3 | Opt-in terminal transcript recording | Implemented; live encrypted recording, authorization, download/delete, real scheduled expiry and combined browser controls passed |
| 4 | GitOps SSH repository authentication | Planned |
| 5 | GitOps Helm rendering | Planned |
| 6 | GitOps Kustomize rendering | Planned |
| 7 | GitOps webhook triggers | Planned |
| 8 | Argo CD integration | Planned |
| 9 | Flux integration | Planned |
| 10 | Custom workflow typed variables | Planned |
| 11 | Custom workflow branching | Planned |
| 12 | Custom workflow scheduling | Planned |
| 13 | Additional workflow actions | Planned; choose actions individually |
| 14 | Additional remediation actions | Planned; choose runbooks individually |
| 15 | Generate/package/deploy CRD conversion services | Planned; conversion semantics required |
| 16 | External alert notifications | Planned; towards end of Phase 8 |
| 17 | Observability host-loss recovery | Planned; towards end of Phase 8 |

## 1. Scoped operator interactive sessions

The full-access shell deliberately grants cluster-admin to acknowledged platform
administrators. Do not enable it for operators by simply relaxing the platform
permission check. Kubernetes credentials must enforce the selected scope.

First slice: an administrator assigns permitted namespaces; an operator opens a
short-lived session for one permitted namespace with an explicit read-only resource
allowlist. Namespace assignment must be distinct from merely selecting a project
or having `clusters.write`. The exact assignment UI/model is a design decision.

Acceptance criteria:

- Authorize organization, project, cluster, namespace and session ownership on
  creation, preparation, attachment and heartbeat. Revoke the session when those
  grants change. Do not trust namespace names supplied only by the browser.
- Issue a pod-bound short-lived token with a namespace Role/RoleBinding, never
  cluster-admin or a copy of the management kubeconfig. Separate the runtime's
  disposable namespace from the allowed workload namespace where appropriate.
- Allow only chosen reads. Deny Secrets, workload mutations, RBAC changes,
  impersonation, TokenRequests, other namespaces, privileged session resources,
  pod exec/attach, port forwarding and API proxy escape paths. Logs require an
  explicit decision because application logs can contain sensitive values.
- Preserve admin sessions, image pinning, lease limits, single attachment,
  revocation, quotas, identity guards, finalizer preservation and cleanup recovery.
- Tests must attempt forbidden actions through the delegated Kubernetes credential,
  not just check hidden UI controls. Live operator/admin/viewer checks must prove
  allowed reads, denied mutations/Secret access, cross-namespace denial and cleanup.

Later write-capable scoped profiles require their own resource/verb design and
review; they are not part of this first read-only slice.

## 2–3. Interactive repository access and transcripts

Implemented choices and verification are in [the interactive guide](interactive-command-center.md).
Repository rules approve pinned public IPs over HTTPS, not HTTP hostname isolation.
Recording captures output (including any echoed input), defaults off, requires
consent, encrypts before display and expires after 24 hours. The combined milestone
passed 359 shared backend regressions plus two additional cleanup safety cases,
29 frontend tests, full Mypy for 180 files with strict changed-module checks,
lint/format/type checks, both production builds and live Keycloak/Fedora UI tests.
Remaining operational boundaries are tracked at the bottom of this document.

**External repositories:** add opt-in administrator-managed destinations for chart
downloads. Decide how DNS, destination pinning, redirects and registry layer hosts
are handled before widening egress. Preserve default API-only networking. Never
expose management credentials or put registry passwords in public operation data.
Test approved HTTPS/OCI downloads and denial of metadata, loopback, unauthorized
private endpoints and redirects. Verify actual network enforcement on the Fedora
CNI and clean up temporary access/credentials on close and failure.

**Transcripts:** current audit records lifecycle metadata only. Decide input/output
capture, consent, authorization, encrypted storage, retention, deletion and audited
download before implementation. Terminal output can include arbitrary Secrets;
redaction cannot guarantee safe content. Keep recording off by default, surface its
state, define behavior if recording fails, and test access isolation, bounds,
retention and absence from shared caches/application logs. Kubernetes API auditing
is separate and must not be presented as a terminal transcript.

## 4–9. GitOps extensions

Build on existing pinned reviews, encrypted credentials/plans, target identity
guards, ownership, pruning safeguards, drift detection and operation recovery.

- **SSH:** encrypted key handling and rotation, explicit host-key verification,
  bounded isolated Git execution and network policy equivalent to HTTPS. Test
  private repository fetch, wrong/changed host-key refusal, auth failure and
  credential redaction. Never accept an unknown host key silently.
- **Helm rendering:** pin chart/source/dependency versions and reviewed values;
  render within time/output limits without arbitrary plugins or repository code.
  Feed rendered objects through existing scope validation and dry-run/review rules.
  Test source/dependency drift, Secret handling and unsafe rendered inventories.
- **Kustomize rendering:** use a pinned tool and bounded input/output. Define remote
  bases and dependency pinning; reject exec plugins and unrestricted filesystem or
  network access. Test overlays, source drift and attempted path/remote escapes.
- **Webhooks:** verify provider signatures, source/application binding, replay and
  delivery deduplication. Trigger the approved reconciliation policy rather than
  bypassing it. Test forged/duplicate events, paused apps and queue failures.
- **Argo CD, then Flux:** each needs controller discovery/version support,
  authentication, tenant/project scoping, actual application/source status and
  documented create/import/sync/suspend ownership. Decide which controller owns
  each application's writes/pruning; never reconcile the same objects with both
  the platform loop and an external controller. Live-test each integration with
  its actual controller, including drift, failures, suspension and uninstall or
  disconnect behavior. Choose supported APIs before coding; installation itself
  is a separately reviewed infrastructure operation.

Git write-back is also a documented future extension in [GitOps](gitops.md), but
was not in the user's selected remaining-feature table. Keep it deferred unless
the user adds it; it needs a separate reviewed write/branch/PR design.

## 10–13. Custom workflow extensions

- **Variables:** typed schemas, defaults, required/range/enum validation and bounded
  substitution into supported action parameters. Define secret inputs separately;
  never introduce shell interpolation. Bind resolved values to the encrypted run
  review so a retry cannot silently execute changed inputs.
- **Branching:** bounded, validated conditions over explicit step results; reject
  cycles/unbounded loops. Journal branch decisions for retry and define skip,
  cancellation and failure behavior. Preserve permission/state checks per action.
- **Scheduling:** administrator-managed timezone-aware schedules, pause/resume,
  concurrency limits and defined missed-run behavior. Scheduled mutation authority
  must be explicit and rechecked at execution; an expired human review must not
  become permanent authority. Test duplicate scheduler delivery, permission loss,
  template edits and restart recovery.
- **Actions:** choose one action at a time with supported targets, permissions,
  preconditions, dry-run/confirmation behavior, postconditions, timeout, retry and
  cancellation semantics. Preserve accepted-change versus readiness distinctions.
  Do not turn the bounded catalog into arbitrary shell or arbitrary API execution.

For each slice, exercise real operation checkpoints, encrypted snapshots, stale
reviews, access changes, retry-after-acceptance and live UI/worker execution.

## 14. Additional remediation

The existing supported runbook replaces approved stateless pods. Choose the next
runbook before implementation; Deployment restart is a possible candidate, not an
already authorized policy. Define eligible resources, approval/automation authority,
cooldowns, attempt budgets, concurrency, health gates and escalation on failure.
Exclude stateful deletion, node drain/reboot and storage actions unless separately
designed. Test false positives, repeated detections, policy changes, role loss,
partial acceptance, cancellation and cooldown/retry exhaustion. Live validation
must prove both recovery of an owned fixture and refusal of ineligible targets.

## 15. Conversion service generation and deployment

Existing CRD tooling configures and probes an already running TLS ConversionReview
v1 service; it does not implement an application's conversion semantics. Select a
real multi-version schema pair and explicit field mappings/defaults before building
a generator/template. Define unsupported or lossy conversions and error behavior.

Acceptance requires reviewed source/image provenance, bounded ConversionReview
handling, Service deployment, least-privilege runtime, certificate/CA lifecycle,
readiness, scaling and rollback. Keep service installation separate from enabling
the CRD's conversion configuration. Test round trips, unknown fields, malformed
requests, certificate rotation, conversion failures and actual served-version
reads/writes. Zero-instance probes do not prove business conversion correctness.
Storage migration certification and API-version retirement remain separate steps.

## 16–17. End-of-phase observability items

**External notifications:** implement reviewed email/webhook destinations,
encrypted credentials, transport verification and administrator-controlled network
trust. Define routing, resolved alerts, retries/backoff, deduplication, rate limits,
delivery history and failed-delivery handling. Test real delivery and recovery from
destination outages; never expose credentials or sensitive alert payloads in logs.
Use controlled test destinations before enabling ongoing delivery.

**Host-loss recovery:** inventory Prometheus, Loki, Grafana and Alloy data/config,
credentials, volume layout and retention. Define backup consistency, encryption,
off-host storage, retention and measurable recovery-point/recovery-time targets.
Restore onto replacement storage/host, reconnect collection, and verify historical
metrics/logs, dashboards, queries and alerts. Record the actual drill and gaps.
Pod restart with an intact volume and platform database restore do not prove this.
Prepare a concrete reviewed replacement-host plan before disruptive live testing.

## Validation and operational boundaries still open

- HA replicas, managed clouds and other CNI implementations need their own live
  session/cleanup/egress verification; Fedora simulator/local checks do not certify
  those environments. Record tested combinations rather than a global ready flag.
- Total management-plane loss backstops have claim/configuration checks; the live
  drill has not waited out real token expiry and pod deadlines during total loss.
- Active session cleanup blocks cluster connection changes. If an endpoint changes
  permanently during cleanup, design a same-cluster-identity recovery procedure;
  do not bypass identity checks or mark unreachable resources as removed.
- Operator/controller integrations and generated conversion business logic need
  their own live fixtures; do not reuse a prior milestone's successful tests as
  proof for a newly supported capability.

Keep earlier-phase deployment/provider hardening in its existing backlog in
[AGENTS.md](../AGENTS.md); this document does not claim those items are complete.
After each feature, update this checklist, the relevant guide, README and AGENTS
with implemented behavior, automated checks, live results and any remaining limits.
