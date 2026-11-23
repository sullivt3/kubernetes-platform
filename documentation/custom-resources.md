# Custom resources

Open **Configuration → Custom resources**, select a cluster, then select an
installed CustomResourceDefinition (CRD). The page discovers served API versions,
resource scope, establishment conditions, and schema field guidance. Namespaces
and definitions are searchable; lists support pagination and refresh. Unavailable
or forbidden APIs are distinguished from an empty inventory.

The page manages instances and the lifecycle of definitions installed through this
feature. Definition actions require organization administrator and cluster write
permissions. Operator installation, conversion webhooks and storage migration
remain separate workflows. Existing generic YAML apply is unchanged. Apply
migration `0014` and restart the API when deploying; definition reviews reuse the
existing encrypted review table and do not require a newer migration.

## Definition installation, updates and removal

Administrators can **Install CRD definition** from bounded YAML, review the server
dry run and type the full definition name to execute it. Only
`apiextensions.k8s.io/v1` is accepted, with one served storage version, at most 20
versions, and `None` conversion. The platform records cluster-bound ownership
labels. It refuses to adopt existing definitions or change definitions owned by
Helm, GitOps or an external controller. Externally managed definitions remain
available for discovery, schema inspection, instance actions and authorized reveal.

**Reveal definition YAML** is explicit, permission checked and audited. Edit-ready
YAML excludes server metadata, status, owners, finalizers and reserved ownership
markers. Updates bind the revealed content snapshot. They preserve existing
ownership/finalizers and recheck UID, content and stored versions at execution.
API group, names and scope remain fixed. Existing version entries must remain,
previously served versions must stay served, and changing storage version or
conversion configuration is refused. This feature does not perform storage
migration, validate third-party operator compatibility or install webhook servers.

Any specification change requires zero instances across all namespaces, checked
using the established served storage API. Metadata-only updates can proceed with
instances present. **Remove CRD definition** also requires zero instances. Both
review and execution repeat inventory checks; forbidden, unavailable, malformed
or incomplete inventory blocks the action. Delete previews put `dryRun: [All]`
in the DeleteOptions body and cannot start removal. Actual deletion uses UID and
resource-version preconditions and preserves finalizers.

Pause controllers and other writers before removing or changing a definition.
The inventory check and definition write are separate Kubernetes requests; an
external client can create an instance between them. Kubernetes definition
removal can delete such instances. The confirmation dialog explains this race;
this feature does not install an admission fence or claim an atomic emptiness
guarantee. A pending deletion remains visible until controller/finalizer cleanup
finishes. Installation/update acceptance is distinct from API establishment,
whose conditions are shown separately on the page.

Definition reviews use the same encrypted, author-bound 15-minute lifecycle as
instance reviews, with a separate discriminator and execution endpoint. An
instance execution endpoint cannot execute a definition review. Permission
changes are checked again on execution. No raw schema values enter public review
results, audit details or automatic query caches.

## Read and reveal

Cluster readers see resource identity, generation, deletion state, condition types
and statuses, and counts of owners/finalizers. Arbitrary spec/status values,
condition reasons/messages, schema defaults and examples are withheld because
custom resources and schemas can contain credentials. Schema guidance exposes
field paths, types and selected constraints, bounded to 500 fields and 12 levels.
Definition inventory is capped at 1,000 entries; resource pages contain 100 items.

Full resource/schema YAML requires the explicit **Reveal** action and
`secrets.reveal`. Reveals are audited, returned with `Cache-Control: no-store`,
and held only in component state rather than the shared query cache. Hide or
leave the selection to clear revealed content. Authorized users must still treat
revealed and edited YAML as sensitive.

## Review and execute

Cluster writers can create, update and delete instances for established, served
versions that advertise the required API resource. Creation starts with a skeletal
YAML template; fill required fields before reviewing. Updates additionally require
reveal access. Kubernetes performs server-side dry-run and strict field validation;
its schema/admission/conversion behavior is authoritative. Error responses expose
safe field paths rather than raw server messages or submitted values.

Reviews expire after 15 minutes and bind the user, organization/project, cluster,
definition UID/specification, API version, object identity and planned content.
Submitted manifests are encrypted using the platform secret store. Public reviews
contain changed field paths, not values. Retained review records are encrypted
data: database backups and encryption-key recovery policies apply to them.

Editing YAML invalidates the preview. Updates require the content snapshot from
the last reveal; edits made elsewhere require a reload and a fresh review. A
review becomes invalid if the definition or resource content changes before
execution. Status-only controller updates are preserved and do not invalidate the
content review. Execution requires typing the exact object name for every action.
Retries of a completed review return the recorded result; a review marker supports
recovery after an accepted write interrupted before its result was recorded.

Writes preserve status, finalizers, owner references and reserved Helm/platform
ownership metadata. They use the current resource version; deletion additionally
uses UID preconditions. Deletion requests foreground propagation and never strips
finalizers. **Deletion requested** means Kubernetes accepted the request; the object
can remain visible while controllers finish cleanup. Likewise, create/update
acceptance does not establish resource health or controller reconciliation success.
Delete previews put `dryRun: [All]` in the DeleteOptions body; live validation
checks the object remains unchanged before confirmation. Single-resource API writes run synchronously, following the existing workload
management pattern; this is not an orchestration or controller integration.

## Validation boundary

Simulator coverage includes scoped permissions, encrypted reviews, schema/UID
races, stale editors, controller status changes, pagination, cluster/namespaced
resources, version selection, retries and finalizer preservation. Local Keycloak browser validation passed on isolated Fedora CRDs for schema
rejection, viewer access/reveal denial, multi-version reads/updates, namespaced
and cluster-scoped CRUD, non-mutating delete review, and finalizer preservation.
The fixtures were removed and both nodes/all 17 active pods remained Ready.
The definition follow-up passed 35 final cases plus the broader 136-test backend
regression run. Live browser checks covered administrator/operator/viewer access,
install and schema update, blocked storage changes, nonempty/racing instance
guards, non-mutating removal review and finalizer-aware deletion. All isolated
definition fixtures and review rows were removed.
This does not validate third-party operators or conversion webhooks.

Kubernetes references: [CRD schemas and validation](https://kubernetes.io/docs/tasks/extend-kubernetes/custom-resources/custom-resource-definitions/),
[API concurrency and deletion](https://kubernetes.io/docs/reference/using-api/api-concepts/),
and [CRD versioning](https://kubernetes.io/docs/tasks/extend-kubernetes/custom-resources/custom-resource-definition-versioning/).
