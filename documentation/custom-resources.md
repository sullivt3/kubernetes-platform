# Custom resources

Open **Configuration → Custom resources**, select a cluster, then select an
installed CustomResourceDefinition (CRD). The page discovers served API versions,
resource scope, establishment conditions, and schema field guidance. Namespaces
and definitions are searchable; lists support pagination and refresh. Unavailable
or forbidden APIs are distinguished from an empty inventory.

This initial integration manages instances of installed CRDs. Installing,
upgrading or removing definitions, editing conversion webhooks and managing
operators are outside this page's scope. Existing generic YAML apply is unchanged.
Apply database migration `0014` and restart the API when deploying this feature.

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
This does not validate third-party operators or conversion webhooks.

Kubernetes references: [CRD schemas and validation](https://kubernetes.io/docs/tasks/extend-kubernetes/custom-resources/custom-resource-definitions/),
[API concurrency and deletion](https://kubernetes.io/docs/reference/using-api/api-concepts/),
and [CRD versioning](https://kubernetes.io/docs/tasks/extend-kubernetes/custom-resources/custom-resource-definition-versioning/).
