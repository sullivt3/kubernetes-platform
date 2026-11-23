# GitOps applications

Open **Workloads → GitOps** with a selected cluster. GitOps provides commit-pinned delivery
of plain Kubernetes YAML/JSON, reviewed manual syncs, opt-in automatic reconciliation,
guarded pruning and review/sync history. It uses the platform's
operation engine; no additional controller is installed on the managed cluster.

## Setup

Apply migrations through `0013` and restart API/worker/scheduler. API and worker hosts need
Git **2.46 or newer** on PATH; the backend image installs Git. The local testing
checkout installs it into ignored `.tools/git`, selected by `scripts/dev-env.sh`.
A portable installation can use an isolated conda-forge environment:

```sh
micromamba create -p .tools/git -c conda-forge 'git>=2.46'
```

Add an application with an HTTPS repository URL, branch, relative manifest directory
(`.` for the repository root), and one existing destination namespace. All YAML,
YML and JSON files beneath that directory are interpreted as Kubernetes manifests.
Use a directory containing only manifests. Each document needs `apiVersion`,
`kind`, and `metadata.name`. Namespaced resources can omit `metadata.namespace`;
explicit namespaces must match the destination. Cluster-scoped resources, List
wrappers, generated names, owner references and server metadata/status are rejected.
Namespace creation remains a separate Kubernetes operation.

The first milestone does not render Helm/Kustomize or execute repository code,
hooks, filters, plugins or submodules. Bundle rendered manifests in Git. SSH Git
URLs, Git write-back, webhooks and Argo CD/Flux controller integration remain future work.

## Authentication and network policy

Anonymous HTTPS, Basic username/password-token and direct Bearer tokens are
supported. Credentials are encrypted with the existing SecretStore and never
returned by list/history APIs. Editing leaves credential fields blank: preserve
them, re-enter credentials to rotate, or explicitly disable authentication to
remove them. Changing the repository URL requires re-entering or removing existing
credentials. Configuration/credential changes advance the application generation
and invalidate older reviews. Source/credential edits also pause automation; an
administrator must explicitly reapprove the resulting source configuration.

Organization/platform administrators control private RFC1918/ULA endpoints,
custom TLS ports and CA bundles. Operators can use existing profiles without
changing trust. TLS verification stays enabled. Loopback, metadata and link-local
addresses are rejected. Each fetch resolves and pins an approved IP; an isolated
single-destination CONNECT tunnel prevents alternate Git repository URLs from
bypassing that policy. Redirects are disabled. Git runs with an isolated home,
configuration and environment; credentials travel through runtime configuration
in the child environment, never process arguments or command output.

Fetches have a 120-second deadline and 128 MiB network/repository limit. Selected
manifest blobs are limited to 1 MiB each, 4 MiB total and 100 objects; each YAML
object additionally uses the bounded 128 KiB mapping validator. Large repositories
should use a dedicated deployment repository. Symlinks and submodules within the
selected directory are rejected. Private temporary repositories are removed after
fetch; no checkout is performed.

## Review, sync and drift

**Review latest commit** fetches the selected branch and pins the resolved commit.
It performs Kubernetes server-side dry runs and records resource identity and live
state. The browser receives only names, kinds, namespaces and create/update/
unchanged actions. Manifests, Secret values and raw API/Git output are withheld;
review payloads are encrypted in the platform database.

A review expires after fifteen minutes and belongs to its requesting user,
application generation and project. **Sync reviewed commit** requires typing the
application name. Branch movement after review cannot change the encrypted
manifest snapshot that is applied. Syncs run as tracked operations with cluster
mutual exclusion, cancellation, logs and recovery. Configuration changes/removal
are blocked during active sync operations.

The platform refuses to adopt existing resources without this application's
ownership annotation and refuses server-side apply field conflicts. It validates
all target identities before writing and uses UID/state guards plus Kubernetes
resource-version checks for updates. Creation uses POST so concurrent resource
creation is refused instead of adopted. Creation then establishes apply ownership
without forcing values and relinquishes only this application's POST manager entry,
using UID/resource-version tests and preserving every other manager. A sync can partially apply before a later
failure; it is not a multi-resource transaction. Owned inventory and review
markers let retries recognize successful earlier writes without recreating them.
External changes after a partial write require a fresh review.

A completed sync reports **Applied**: Kubernetes accepted the resources. It does
not claim deployment rollout, pod readiness or application health. Inspect the
existing workload pages for those checks. Run **Review latest commit** again to
observe drift. **InSync** means the last successful review found the selected
commit's desired resources unchanged; **OutOfSync** means resources would be
created or updated. **Conflict** means the dry run reported foreign field ownership;
those reviews cannot be synced until ownership is resolved and reviewed again.
Observations include their timestamp and commit. Manual applications are checked
only on review; approved automatic applications are polled at their configured interval. Review/sync history retains the newest twenty entries
in the UI, with operation links for actual execution outcomes.

Resources removed from Git are retained by default. Explicit pruning is described below.
**Remove registration** deletes the application, stored credentials and reviews
while preserving Kubernetes resources and their ownership annotations. A new
application will refuse to adopt those resources. Resource cleanup or ownership
migration requires a separate explicit administrative action.

## Automatic reconciliation

Automation is disabled by default, including on existing applications upgraded from
`0012`. Expand **Automatic reconciliation**. Organization/platform administrators
can enable it, choose a 60–3,600 second interval (default 300), and separately enable
pruning. Saving requires typing the application name. The API binds approval to the
application's current generation, preventing a stale dialog from approving a changed
source. Configuration/credential edits pause automation and invalidate old reviews.

Celery beat runs `gitops.reconcile` every 30 seconds. It queues due applications as
tracked `cluster.gitops.reconcile` operations, preserving organization/project scope
and cluster mutual exclusion. Fetch/review and apply/prune are separate checkpointed
steps. Each execution pins and encrypts a specific commit before mutating resources.
Unchanged checks update the observation/history without writing Kubernetes objects.
The interval is a minimum polling cadence, not an exact execution deadline; busy
clusters defer checks. Both worker and scheduler must be running.

Cluster writers can **Reconcile now** under an active approved policy or **Pause
reconciliation**. Pause clears the schedule and advances the generation, invalidating
queued work, reviews and further mutations at the next checkpoint. Requests already
accepted by Kubernetes can finish. To stop a currently running operation immediately,
also request cancellation on its operation page; accepted side effects are not undone.
Administrator reapproval is required to resume. History distinguishes automatic and
manual reviews, links execution outcomes, and reports prune counts. A fetch failure
can occur before a review exists; use the latest-operation link or the Operations
page to inspect that execution. Source failures,
field conflicts, pause and cancellation never authorize force overrides.

## Pruning

For a single manual sync, select **Include pruning in manual review** and review its
**Prune plan** before typing the application name to execute. Automatic pruning is a
separate administrator-approved policy option. It is never enabled by ordinary
source configuration or by the default automatic-reconciliation setting.

Candidates come only from this application's recorded inventory and must be absent
from the pinned Git commit. Deletion requires the same namespace, canonical API
path, application ownership annotation and original UID. Replaced, unowned or
changed objects block the review/execution. Discovery must still serve the API;
an unavailable API is not treated as proof that a resource disappeared. Already
absent objects are removed from inventory without issuing a delete.

Direct pruning is limited to known built-in namespaced stateless resource kinds:
ConfigMaps, Secrets, Services, ServiceAccounts, Pods, ResourceQuotas, LimitRanges,
Deployments, ReplicaSets, DaemonSets, Jobs, CronJobs, Ingresses, NetworkPolicies,
Roles, RoleBindings and v2 HorizontalPodAutoscalers. PVCs, StatefulSets and custom
resources are retained. Add `kplatform.io/gitops-prune: "disabled"` (also accepts
`false` or `keep`) or `helm.sh/resource-policy: keep` to retain an eligible resource.
Retention annotations are rechecked just before deletion. The review lists retained
resources and reasons rather than claiming they were removed.

All desired objects are validated/applied before pruning. If deletion is needed,
the worker waits up to 120 seconds for supported desired workloads to be ready
(Deployments also need availability; Jobs need completion). Apply or readiness
failures stop before pruning starts. This is a limited workload gate, not a general
application health assessment. Each delete uses UID and resource-version preconditions
and foreground Kubernetes garbage collection, which follows owner references for
dependents. The worker waits up to 120 seconds per deletion and never strips
finalizers. Pending deletions remain in inventory; retries recognize already absent
objects and preserve recorded prune progress. Multi-resource syncs are not atomic,
and a failure or pause can leave earlier writes/deletions completed.

An empty or missing manifest set fails closed by default, even with pruning enabled.
To intentionally remove all eligible owned resources, separately opt in to **Allow
empty sources** (or the corresponding manual-review checkbox). The configured Git
directory must still exist in the commit; keep a non-manifest file such as `.gitkeep`
when removing its final YAML file. Missing directories, unavailable repositories,
invalid manifests and failed authentication never trigger mass deletion. Protected
and untracked resources remain untouched, including with this option enabled.

The platform's encrypted database backup includes application credentials and
review ciphertext. Retain the corresponding encryption keys for recovery.

Git transport configuration references:
[Git configuration](https://git-scm.com/docs/git-config) documents runtime
configuration, allowed protocols, TLS settings and pinned name resolution.

[Kubernetes server-side apply](https://kubernetes.io/docs/reference/using-api/server-side-apply/)
describes field ownership and conflict semantics.
