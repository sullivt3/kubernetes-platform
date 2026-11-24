# Command Center

Operations → Command center provides reviewed, single-command cluster summaries.
This is the first stage of the [Phase 8 breakdown](../README.md#roadmap).

## Current scope

Select a cluster, enter a supported command, review its canonical form and target,
then type the cluster name to run it. Open the resulting operation for progress,
cancellation and retry, and use **Your recent commands → View output** to retrieve
results. Output is a timestamped snapshot; rerun a new review to refresh it.

Supported syntax:

```text
kubectl get RESOURCE [NAME] [-n NAMESPACE | -A]
helm list [-n NAMESPACE | -A]
helm history RELEASE [-n NAMESPACE]
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
cache and plugin directory. Only fixed list/history arguments are generated.
Helm output retains release/revision summary fields and omits values, manifests,
notes and descriptions. Credentials always come from the selected cluster's
platform connection, including managed token refresh; command text cannot supply
credentials, contexts, endpoints, impersonation, files or plugins.

## Permissions and review lifecycle

An operator or administrator needs `clusters.read`, `clusters.write` and
`terminal.exec`. Viewers cannot review, run, list history or retrieve output.
Each review belongs to its author and the selected tenant/project/cluster. It
expires after 15 minutes. Submission requires typing the cluster name and is
idempotent per review. Worker execution rechecks the original human's current
membership/permissions and expiry, including before saving output. Expired queued
commands fail and need a new review. Existing results remain accessible while the
author still has access; retrieving output is explicitly audited.

The existing asynchronous operation engine provides queueing, cluster exclusivity,
cancellation, retries and audit records. Kubernetes cancellation is cooperative
before/after its bounded network request; Helm cancellation polls while its child
process runs and terminates that process group. Cancellation never changes cluster
resources because these commands are read-only. A completed encrypted result is
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
- Kubernetes responses are read incrementally with a 2 MiB cap, a 10-second
  response deadline checked between reads and socket timeouts. Cancellation may
  wait for the current socket read to finish.
- Helm execution has a 20-second command timeout, the existing 10-second version
  check, process termination/cleanup and a 32 MiB raw-output limit. Release lists
  request 101 entries to detect the 100-row display boundary. History requests
  50 revisions; older revisions are outside the requested window.
- Display output is capped at 64 KiB, dropping whole rows and visibly marking
  truncation. Recent history shows the latest 20 reviews. There is no new automatic
  result-retention policy.

This first stage does not provide full kubectl/Helm CLI compatibility, arbitrary
commands, resource mutations, logs, streaming or an interactive shell. Use the
existing workload, YAML apply, logs and Helm management pages for those supported
operations.

## Follow-up milestones

1. Add selected mutations by reusing existing dry runs, UID/content drift checks,
   encrypted Helm release reviews and explicit confirmations. Do not expose
   arbitrary mutating flags as a shortcut.
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
list passed. Fedora had no existing Helm release, so live history retrieval
remains unverified. Both Fedora nodes and all 17 active pods were Ready afterwards;
platform readiness passed. These read-only checks created no cluster workloads.
