# Helm release management

Open **Workloads → Helm releases** with a selected cluster. Cluster readers can
view releases and history and browse configured charts. Cluster writers can configure
repositories and run release actions. The API enforces organization/project scope,
browser CSRF checks and application permissions.

## Setup and repositories

Apply migrations through `0011`, then restart the API, worker and scheduler. API and worker
hosts require Helm **4.3.2**. The backend container includes the checksum-pinned
binary. For the configured local development checkout:

```sh
source scripts/dev-env.sh
python backend/tools/install_helm.py --destination .tools/helm/bin/helm
```

The installer supports Linux amd64/arm64 and Intel macOS. Other hosts must install
the same official Helm version on PATH. The integration uses Helm's Secret storage
driver and the cluster's registered CA, server-name verification and client
credentials. No kubeconfig or private key is returned to the browser.

Expand **Chart repositories** to add a named HTTPS Helm index or full OCI chart
URL, such as `oci://registry.example.com/team/chart`. Each OCI source represents
one chart; registries do not provide a portable chart catalog. Repositories belong
to the selected cluster and project. Choose searchable chart/version options, or
enter a **Known OCI version** if the registry does not allow tag listing.

Sources support anonymous access, username/password (including registry tokens),
and direct bearer tokens. Authentication is write-only: credentials are encrypted
with the existing SecretStore, never returned by repository reads, and never passed
to Helm's process arguments or registry configuration. Editing leaves credential
fields blank; retain them unchanged or re-enter credentials to rotate them.
Disabling authentication explicitly removes credentials. Rotation/removal revokes
the prior credential reference and requires affected install/upgrade plans to be
reviewed again. Removing a repository preserves installed releases but invalidates
new reviewed plans that reference that source. Rollback and uninstall do not need
the original chart repository.

URLs cannot contain credentials or query tokens. HTTPS requests pin resolved IPs
and verify TLS on every hop. Public endpoints use port 443 by default. Organization
or platform administrators may configure **Endpoint trust settings**: private
RFC1918/ULA addresses, custom TLS ports, and a PEM CA bundle. Operators can use
these profiles without changing their trust settings. Loopback, link-local and
metadata addresses remain blocked. Credentials and custom trust do not follow
cross-origin redirects. An authenticated OCI registry with a separate token service
requires its exact HTTPS token endpoint to be explicitly trusted; requests ask only
for pull access to the configured chart path. TLS verification stays enabled.

The HTTPS index is bounded to 8 MiB; archives to 16 MiB compressed/64 MiB expanded.
Indexed SHA256 digests must match downloaded archives. OCI discovery reads up to
ten bounded tag pages and displays the newest 100 discovered semantic versions. OCI plans pin both the
manifest and chart-layer SHA256 digests, so changing a tag after review cannot
change the artifact executed. No mutable `latest` selection is used. Dependencies
must be bundled; dependency downloads and Helm plugins are not exposed.

## Review and execution

Choose an existing release namespace. Namespace creation remains an explicit
Kubernetes resource action. Install and upgrade allow values YAML editing and
loading the selected chart's defaults. Upgrade reuses installed values by
default and applies the new overrides; uncheck that option to reset values.
Installed values are never loaded into the browser. Values must be a YAML mapping
of at most 128 KiB with bounded nesting and no cyclic aliases.

**Review Helm plan** validates the selection, snapshots release revision/identity,
and renders an offline resource inventory for install/upgrade. The inventory
contains kinds/names/namespaces, not manifests, values or Secret contents. Existing
upgrade values and cluster-dependent template lookups can change the actual
rendering; offline review does not claim to be an exact server-side diff.

The review expires after fifteen minutes and binds the exact submitted values,
action, chart archive, namespace and release snapshot to the requesting user.
Editing the form clears the review and discards older in-flight preview/defaults responses. Upgrade, rollback and uninstall require typing
the release name, independently checked on the server. Each execution is audited
and runs as a persistent operation with logs, checkpoints, cancellation and retry.
Cluster-target mutual exclusion prevents overlapping platform cluster changes.

Submitted values are encrypted with the platform SecretStore in `helm_plans`.
Operations contain only the plan ID and non-secret action/target fields. Credentials
and values exist in private temporary files while Helm runs, with isolated config,
cache and plugin directories. Raw Helm output, errors, release notes and history
descriptions are withheld because they can contain sensitive values. Keep old
credential encryption keys while retained plans need decryption; platform database
backups include these encrypted plans under the existing recovery process.

Helm waits for resources within the selected timeout (30–600 seconds). Install
and upgrade use rollback-on-failure. Chart hooks are disabled by default and can
be explicitly enabled; hooks may execute Kubernetes Jobs. Ownership-taking and
force-replacement flags are not exposed. Charts may contain cluster-scoped
resources, so review kinds and names and select trusted repositories.

## History, rollback and removal

History exposes revision, chart, application version, status and update time.
Rollback can select an available successful older revision and creates a new
revision. At most fifty Helm history revisions are retained by platform upgrades
and rollbacks. Rollback changes Kubernetes resources and values; it does not
restore application/database data.

Uninstall removes the release and its chart-managed resources and waits for Helm's
completion. CRDs, retained volumes and resources marked `helm.sh/resource-policy:
keep` may remain according to Helm/chart semantics. Their cleanup is separate.

Before execution the worker rejects changed release revisions or head-Secret UIDs.
An operation marker on created revisions lets recovery recognize an already
committed successful install/upgrade/rollback without repeating it. An already
absent release completes uninstall without another mutation. A failed or pending
revision is not force-unlocked. Inspect its status and Kubernetes events, then
create a fresh review when needed. External Helm users can also change releases;
avoid concurrent external changes during a platform operation.

Cancellation terminates the Helm process at a safe polling point but cannot undo
Kubernetes requests already accepted. Timeouts, interruption or failed Helm
rollback can leave a pending/failed release. The operation reports failure; it
does not manufacture recovery or bypass Helm's ownership/locking checks.

Helm command semantics and download guidance:
[install](https://helm.sh/docs/helm/helm_install/),
[upgrade](https://helm.sh/docs/helm/helm_upgrade/),
[rollback](https://helm.sh/docs/helm/helm_rollback/),
[binary verification](https://helm.sh/docs/intro/install/).
