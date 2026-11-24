# Custom workflows

Open **Operations → Custom workflows** and select a cluster. Organization
administrators can create, edit and delete reusable definitions. Cluster writers
can review and run them; readers can inspect the inventory and operation history.
Apply migration `0015` and restart API/worker when deploying this feature.

A definition is bounded YAML with 1–20 ordered steps. For example:

```yaml
steps:
  - action: cluster.check
  - action: resource.scale
    kind: Deployment
    namespace: default
    name: my-app
    replicas: 2
  - action: resource.restart
    kind: Deployment
    namespace: default
    name: my-app
  - action: resource.wait
    kind: Deployment
    namespace: default
    name: my-app
    timeout_seconds: 120
```

`cluster.check` requires a ready API and Ready nodes. Scaling supports Deployments
and StatefulSets, with 0–1,000 replicas. Restart supports Deployments. Readiness
waits support Deployments, StatefulSets, DaemonSets, Pods and Jobs, using observed
generation/replica readiness, Pod readiness/completion or Job completion as
appropriate. Waits accept 1–600 seconds and retain their original start time
across retry. Definition YAML is capped at 128 KiB. Shell commands, scripts,
credentials, arbitrary API paths, variable interpolation, branching, scheduling
and arbitrary manifest execution are outside this initial action catalog.

Definitions and immutable run payloads are encrypted with platform secret keys.
Full definition YAML requires explicit audited reveal; it stays outside shared
query caches. Inventory exposes name, generation and step count. Reviews expose
only the supported action parameters, never arbitrary workload YAML. Saving a
new generation or deleting a definition invalidates pending reviews and stops
further actions by an active run. Already accepted changes remain.

A run review dry-runs mutations and binds definition generation, target UID and
mutation content. Admission that changes fields beyond the bounded action is
refused; a changed admission outcome at execution stops later steps while retaining
the write already accepted. Status-only controller changes are tolerated; other changes
require a new review. Multiple steps against the same object are planned in
sequence. Confirmation requires typing the workflow name. Reviews expire after
15 minutes before starting; an approved started operation retains its approval
for recovery while permissions and target state are rechecked.

Runs use the existing operation engine with one checkpoint per step, cluster
exclusivity, cancellation, retry and live operation history. The worker resolves
the original user's current database memberships before each action and wait
iteration. Disabled users, permission loss, template edits and identity changes
stop further work. Mutations use resource versions and per-step markers to
recognize an interrupted accepted write when its reviewed content still matches.
Controller content changes can make recovery fail closed, requiring a fresh review.

This is a sequence, not a transaction: failed/cancelled runs retain completed
changes and never automatically undo earlier steps. Cancellation cannot retract
requests already accepted by Kubernetes. Workload acceptance is distinct from
readiness; include a wait step when that is required.

Local tests exercise the real operation executor with a simulated Kubernetes API.
The current project handoff records the separate Fedora/browser validation and
its limits. Third-party workloads and providers require their own validation.
