# Command Center

Operations → Command center provides reviewed summaries and selected single-command changes.
These are the first two stages of the [Phase 8 breakdown](../README.md#roadmap).

## Current scope

Select a cluster, enter a supported command, review its canonical form and target,
then type the cluster name for reads or the target name for changes to run it. Open the resulting operation for progress,
cancellation and retry, and use **Your recent commands → View output** to retrieve
results. Output is a timestamped snapshot; rerun a new review to refresh it.

Supported syntax:

```text
kubectl get RESOURCE [NAME] [-n NAMESPACE | -A]
helm list [-n NAMESPACE | -A]
helm history RELEASE [-n NAMESPACE]
kubectl scale deployment|statefulset NAME --replicas=N [-n NAMESPACE]
kubectl rollout restart deployment NAME [-n NAMESPACE]
helm rollback RELEASE REVISION [-n NAMESPACE]
```

`--namespace NAME`, `--namespace=NAME` and `--all-namespaces` are also accepted.
The namespace defaults to `default`. `-A` supports lists only. Cluster-scoped
Kubernetes resources reject namespace flags.

Kubernetes resources: nodes, namespaces, pods, services, deployments, replicasets,
statefulsets, daemonsets, jobs, cronjobs, persistentvolumeclaims, persistentvolumes
and storageclasses. Common aliases `no`, `ns`, `po`, `svc`, `deploy`, `rs`, `sts`,
`ds`, `pvc`, `pv` and `sc` are accepted. Output contains name, namespace, kind, UID
and recognized phase/Ready states where available. It omits specs, annotations,
labels, condition messages, Secret values and arbitrary YAML/JSON.

Kubernetes syntax is interpreted through the existing TLS-verified REST client;
no kubectl executable is required and no shell is invoked. Helm uses the existing
pinned 4.3.2 runtime, a private temporary kubeconfig and an isolated configuration,
cache and plugin directory. Only fixed inventory/history, manifest-inspection and rollback arguments are generated.
Helm output retains release/revision summary fields and omits values, manifests,
notes and descriptions. Credentials always come from the selected cluster's
platform connection, including managed token refresh; command text cannot supply
credentials, contexts, endpoints, impersonation, files or plugins.

## Permissions and review lifecycle

An operator or administrator needs `clusters.read`, `clusters.write` and
`terminal.exec`. Viewers cannot review, run, list history or retrieve output.
Each review belongs to its author and the selected tenant/project/cluster. It
expires after 15 minutes. Submission requires typing the cluster name for reads or target name for changes and is
idempotent per review. Worker execution rechecks the original human's current
membership/permissions and expiry, including before saving output. Expired queued
commands fail and need a new review. Existing results remain accessible while the
author still has access; retrieving output is explicitly audited.

The existing asynchronous operation engine provides queueing, cluster exclusivity,
cancellation, retries and audit records. Kubernetes cancellation is cooperative
before/after its bounded network request; Helm cancellation polls while its child
process runs and terminates that process group. Accepted mutations are retained
on failure/cancellation; cancelling a Helm client cannot undo controller changes. A completed encrypted result is
reused on recovery rather than silently observing a new state.

Full plans and output are encrypted in the existing review table; no migration is
needed. Ordinary operation results/logs contain execution metadata, row count,
truncation and observation time. Command history retains canonical command text
(resource and namespace names); it is restricted to the author. Output is fetched
explicitly, returned with `Cache-Control: no-store`, and held outside the shared
frontend query cache. No raw tool/API errors are returned.

## Limits

- Kubernetes lists request one page of 100 objects and report a continuation token
  as truncation. No unbounded pagination or watch is supported.
- Kubernetes summary-read responses are read incrementally with a 2 MiB cap, a 10-second
  response deadline checked between reads and socket timeouts. Cancellation may
  wait for the current socket read to finish.
- Helm inventory/history execution has a 20-second command timeout, the existing 10-second version
  check, process termination/cleanup and a 32 MiB raw-output limit. Release lists
  request 101 entries to detect the 100-row display boundary. History requests
  50 revisions; older revisions are outside the requested window.
- Display output is capped at 64 KiB, dropping whole rows and visibly marking
  truncation. Recent history shows the latest 20 reviews. There is no new automatic
  result-retention policy.

This integration does not provide full kubectl/Helm CLI compatibility, arbitrary
commands, other mutations, logs, streaming or an interactive shell. Use the
existing workload, YAML apply, logs and Helm management pages for those supported
operations.


## Reviewed mutations

`kubectl scale` supports Deployments and StatefulSets, 0–1,000 replicas. The
`--replicas N` spelling and aliases `deploy`/`sts` are also accepted. Restart
supports Deployments only. Both bind UID and resource content, encrypt the full
reviewed object and change only replicas or the pod-template restart timestamp,
plus a per-review recovery marker. All other fields, ownership and finalizers
are retained. Review runs a Kubernetes server dry-run PUT. Execution rechecks
UID/content/deletion state, repeats the server dry run to detect admission drift,
then uses resourceVersion CAS for the actual PUT. The fingerprint includes last-applied
annotations and UID, which ordinary display diffs omit. Older pending mutation
reviews without the stronger fingerprint must be reviewed again. Status-only updates do not
invalidate the review. Accepted writes report `rollout_verified: false`; this is
not a workload readiness check. Controllers can subsequently revert replicas or
specs. A retry recognizes only the exact marker and accepted content, preventing
a repeated restart after an interrupted result save.

`helm rollback` requires an explicit positive successful revision older than the
current one. It reuses the encrypted Helm release plan and existing executor,
checks current revision/UID and binds both current and target Secret content.
The review shows target manifest resource names/kinds/namespaces and resources
absent from the target. Values and raw manifests remain withheld. Review and
execution run pinned Helm `--dry-run=server --no-hooks` simulation, bounded to 30
seconds per simulation, and the mutation waits up to 120 seconds using the normal
runner timeout/cleanup. Hooks are always disabled. Helm simulation is not proof
of Kubernetes admission on every object, nor of all deletion/retention effects.
Pause external Helm writers while reviewing/executing: the app's cluster lock
does not fence them, and a revision check is not a Kubernetes-wide transaction.
Accepted changes remain if the rollback fails or is cancelled; inspect release
status before reviewing another action. The executor recognizes its own deployed
revision on recovery and refuses to repeat an uncertain failed/pending revision.

Mutations use `cluster.command.mutate`, separate from read operation steps. The
backend selects the mode from the stored review; the client cannot turn a read
into a write. Worker permission/expiry checks run before the mutation, including
after dry runs/simulation. Typed confirmation is the workload/release name; the
review and dialog display the cluster and namespace. Editing the command or
switching cluster discards the frontend review.

## Follow-up milestones

1. Extend the explicitly supported mutations only through reviewed domain actions.
   Install/upgrade/uninstall remain on the existing Helm release management page.
2. Design interactive sessions separately: isolated per-user execution, short-lived
   credentials, streaming, disconnect cleanup, cancellation and session audit.
3. Validate each expansion against role/tenant boundaries and a real cluster.

## Validation

`backend/tests/test_command_center.py` covers parsing/injection rejection,
non-mutating review, private encrypted output, selected namespace/cluster binding,
expiry, typed confirmation, current worker permissions, cancellation, pagination,
response/output limits and projected Helm output through the real operation engine.
Simulator coverage and local live validation must be reported separately.

Local validation on 2026-11-23 passed all 112 final Command Center/Kubernetes/Helm
backend tests, Ruff/format, Mypy across 172 files (including untyped bodies in the
new module/routes), frontend type checks/lint, 29 frontend tests and both production
image builds. The real Keycloak operator browser and Celery worker read Fedora
node summaries, enforced typed confirmation and invalidated an edited preview;
viewer access and cross-author output retrieval were denied. Real pinned Helm
list passed. The user subsequently confirmed successful live Helm history
retrieval through Command Center on the Fedora test cluster, closing the initial
validation gap (user-reported verification). Both Fedora nodes and all 17 active pods were Ready afterwards;
platform readiness passed. These read-only checks created no cluster workloads.

Reviewed-mutation validation on 2026-11-24 passed 80 final mutation/workflow tests
and the shared Command Center/Helm/identity/orchestration regression checks.
Ruff/format, Mypy across 173 files, frontend type checking/lint, 29 frontend tests
and both production builds passed. Live Keycloak GUI and Celery/Fedora checks
verified target confirmation, non-mutating review, zero-replica scale/restart,
stale-content refusal, Helm simulation without revision writes and a real rollback
from revision 2 to revision 1 creating deployed revision 3. The Helm release was
an isolated fixture; the user's existing release was not changed. Viewer mutation
review was denied. Temporary namespace/release/review fixtures were removed by
identity; no fixture pods were started. Both Fedora nodes and all 17 active pods
were Ready afterwards, and local platform readiness passed.
