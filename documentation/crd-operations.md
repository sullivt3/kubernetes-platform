# CRD storage migration and conversion webhooks

In **Configuration → Custom resources**, select a platform-installed definition
and explicitly **Reveal definition YAML**. Administrators can then open
**Storage migration & conversion webhooks**. Every operation binds this revealed
snapshot and requires a separate expiring review and full-definition-name
confirmation. Progress, cancellation, retry and results are under Operations.
Externally managed definitions are not adopted. API/worker must run the new code;
reviews use the `0014` encrypted table, alongside `0015` custom workflows.

## Storage migration

The target must already exist as a served version. Certified migration changes storage selection, retains existing version schemas/served
APIs, delegates rewriting to the native controller and clears old
`status.storedVersions` only after native success and guarded completion. None conversion requires identical schemas across versions; differing
schemas require a working conversion webhook. Back up objects and pause external
writers to the definition first. Instance writes after storage selection use the
selected storage version; the operation does not freeze external writers.

The review checks existing instances using strict, non-persistent update requests
and rejects observable admission/conversion changes. Review inventory is capped
at 5,000 instances and 100 pages. Review can invoke existing admission/conversion services in
dry-run mode; it does not persist resource changes.

Certification requires **storagemigration.k8s.io/v1 StorageVersionMigration**,
served and authorized, with its controller running. It creates one review-labelled resource, monitors its UID,
specification and Succeeded/Failed conditions, and keeps it for diagnosis. Cancellation requests removal of only the UID-bound owned migration resource;
already accepted controller writes can still complete. A controller can also keep
working after monitoring fails, so inspect retained resources before starting
another migration. Waiting
is bounded to ten minutes across retry. The operation never adopts a colliding
migration object. Its absence is an unavailable certification capability, not a successful no-op.
Older alpha migration APIs are not used.

**Rewrite current storage only (uncertified)** is a separate opt-in maintenance
mode. It requires one observable control-plane node and its mirrored API server,
uses the already-selected storage version, never changes storage selection, and
never clears stored-version history. Their UIDs are bound and checked between
phases. Unobservable, incomplete or changed topology blocks this mode. A complete
pass reports `RewrittenUncertified` with `stored_versions_retained: true` under a
separately named rewrite operation; it never reports a certified migration.

The rewrite-only path lists a consistent paginated snapshot, re-reads each
instance, checks UID, dry-runs an unchanged PUT, then writes with the current
resource version. Conflicts get fresh reads; replacements and content-changing
admission fail closed. Deleted objects are counted. Interrupted passes restart
scanning on retry; this preserves content rather than promising exactly-once HTTP
requests. Pagination is capped at 100 pages and 5,000 instances, and incomplete
snapshots fail closed. Definition RV/spec/UID and topology must remain stable.
Current API reads or discovery hashes cannot establish physical storage convergence;
that is why this mode retains legacy history even after successful rewrites.

A partial native migration failure/cancellation leaves the selected storage
version and accepted writes in place. Controller work already accepted can finish.
Inspect the retained native resource and operation history. Only certified native
success permits old stored-version cleanup by this feature. Its status PUT retains
full ownership labels, annotations, finalizers and metadata; interrupted status
acceptance is recognized by persisted certification intent. Do not edit history
manually to make a failed migration appear successful. Existing served versions
remain; API version retirement is a separate compatibility decision.

This follows Kubernetes' [stored-version migration model](https://kubernetes.io/docs/tasks/extend-kubernetes/custom-resources/custom-resource-definition-versioning/)
and uses the [native migration API](https://kubernetes.io/docs/tasks/manage-kubernetes-objects/storage-version-migration/)
where available. API-level rewrite validation is distinct from independently
inspecting etcd encoding.

## Conversion webhooks

This feature configures an existing TLS server; it does not generate business
conversion logic or deploy that server. Configure its namespace, Service, TCP
port, conversion path and certificate-only PEM CA bundle. Only Service-backed
ConversionReview `v1` is supported. Arbitrary webhook URLs, ExternalName Services,
private keys in CA input, expired certificates and traversal paths are rejected.
The encrypted review binds Service UID as well as definition UID/content. The
Service must advertise the selected TCP port and have a Ready EndpointSlice
backend; incomplete endpoint inventory blocks approval/execution.

Kubernetes performs the TLS connection and conversion calls using the supplied
CA. Trust the server: it receives custom-resource contents. The platform's service
readiness check alone does not verify its certificate, protocol or conversion
logic. Configuration first adds a unique temporary short name in the same CRD update
as the converter and waits for discovery to expose that alias. This observes the
serving cache update before testing; immediate requests can otherwise exercise
the previous converter. It then reads instances through every served version and
performs unchanged dry-run updates. After success the operation removes only its
reviewed temporary alias and waits for discovery to observe removal. Interrupted
operations retain recovery state for this stage. The barrier/probes observe the
registered API endpoint; they do not attest every backend behind an external HA
load balancer. Validate HA routing and converter rollout separately.
It checks identity, requested API version and stable observable content. Probing
is capped at 200 instances. Choose **Review conversion probe** to run those
checks again without changing configuration.

A failed/cancelled configuration probe conditionally restores the prior
specification, preserving metadata and using the current resource version. It
refuses to overwrite concurrent specification/ownership changes. An interrupted
configuration can be recognized by its review marker; a rolled-back review needs
a fresh approval. Concurrent external writes can prevent restoration, so inspect
operation history and current configuration after failure. These probes sample
current objects, not all possible future inputs or business semantics.

With zero instances, the result explicitly reports
`ConfiguredWithoutInstanceProbe`; this is not protocol verification. A successful
probe reports `Verified` and the instance count. None conversion can be selected
only with identical version schemas. Normal definition edits retain an already
approved webhook configuration; changing it uses this separate workflow.

All configuration/payloads are encrypted. Public histories show safe summaries,
counts, operation IDs and recovery state. Full schemas and webhook configuration
remain behind explicit reveal. Database backups require the corresponding secret
keys. See [Custom resources](custom-resources.md) for ownership and deletion limits.
