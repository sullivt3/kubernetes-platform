# API reference

The machine-readable contract is the OpenAPI document at `/api/v1/openapi.json`. Interactive docs are at
`/api/v1/docs`. This page summarizes the endpoints and their conventions.

## Conventions

* **Base path:** `/api/v1`. Resource endpoints require an authenticated principal and application permissions.
  Health probes and authentication entry/session endpoints are public; logout and context changes validate the
  current identity and session CSRF token. OIDC bearer clients use an API audience separate from the browser
  client ID. See [enterprise administration](enterprise-administration.md) for setup, roles and new endpoints.
* **Errors** always look like this:
  `{"error": {"code": "conflict", "message": "…", "details": {…}, "request_id": "…"}}`

  | Code | HTTP status |
  |---|---|
  | `validation_failed` | 422 |
  | `authentication_required` | 401 |
  | `permission_denied` | 403 |
  | `identity_provider_unavailable` | 503 |
  | `not_found` | 404 |
  | `conflict` | 409 |
  | `invalid_state` | 409 |
  | `confirmation_required` | 428 |
  | `capability_not_supported` | 400 |
  | `not_implemented` | 501 |
  | `internal_error` | 500 |
* **Request IDs:** send `X-Request-ID` to correlate. It is echoed on the response and recorded in audit events.
* **Pagination:** `limit`/`offset` query parameters; responses are `{items, total, limit, offset}`.
* **Long-running work** returns `202` with an operation. Follow it with `GET /operations/{id}` or the SSE stream.
* **Destructive actions** require `confirm=<resource name>`. Otherwise the response is 428, and
  `details.expected_confirmation` holds the required value.

## Endpoints

### Health (unauthenticated)

| Method | Path | Description |
|---|---|---|
| GET | `/health/live` | Process liveness. |
| GET | `/health/ready` | Status of the database (including migration revision), Redis, workers and secret store. 503 only when the database is unavailable. |

### System

| Method | Path | Description |
|---|---|---|
| GET | `/system/info` | Version, environment, auth mode, execution backend, secret backend and current user. No secrets. |
| POST | `/system/self-check` | Starts the `system.self_check` workflow (202). One at a time (409). Honours `Idempotency-Key`. |

### Catalog

| Method | Path | Description |
|---|---|---|
| GET | `/catalog/providers` | Infrastructure providers: capabilities, credential kinds, config JSON Schema, implementation status. |
| GET | `/catalog/distributions` | Kubernetes distributions: supported providers, runtimes and CNIs, capabilities, status. |
| GET | `/catalog/kubernetes-versions` | Supported upstream minor versions and their latest patches (from dl.k8s.io, cached for 1 h). `available: false` when offline. |
| GET | `/catalog/operation-types` | Registered workflows. |

### Operations

| Method | Path | Description |
|---|---|---|
| GET | `/operations` | List. Filters: `status` (repeatable), `active`, `type`, `target_type`, `target_id`. |
| GET | `/operations/{id}` | Detail, including steps, params, result and error. |
| GET | `/operations/{id}/logs?after=<log id>` | Log entries after a cursor. |
| POST | `/operations/{id}/cancel` | Cancels a queued operation immediately; a running one stops at the next safe point. 409 if already finished. |
| POST | `/operations/{id}/retry` | Re-runs a failed or cancelled operation, resuming at the first incomplete step. |
| GET | `/operations/{id}/events` | **SSE.** Event types: `log` (id = log id; resumable via `Last-Event-ID` or `?after=`), `operation` (full snapshot on change) and `end`. |
| GET | `/operations/stream/changes` | **SSE.** Organization-wide `change` events `{operation_id, kind, status}`. It sends `unavailable` and closes when Redis is down; clients should then poll. |

### Credentials

| Method | Path | Description |
|---|---|---|
| POST | `/credentials` | Create `{name, kind, secret_value, description?, public_metadata?}`. The secret is encrypted and never returned. |
| GET | `/credentials` | List metadata. Filter: `kind`. |
| GET | `/credentials/{id}` | Metadata plus `used_by` references. |
| PUT | `/credentials/{id}/secret` | Replace (rotate) the secret value. |
| DELETE | `/credentials/{id}?confirm=<name>` | Delete. 409 while the credential is referenced. |

### Machines

| Method | Path | Description |
|---|---|---|
| GET | `/machines` | Inventory with inspection results, prerequisite checks and cluster assignment. |
| POST | `/machines` | Register `{name, host, port?, username?, credential_id}` (201). Returns the machine plus the started `machine.inspect` operation. |
| GET | `/machines/{id}` | One machine. |
| PATCH | `/machines/{id}/address` | `{host}` updates a registered SSH machine's address and returns `{machine, operation}` for a new inspection using its pinned host key. 409 for duplicate endpoints or active machine/cluster operations. |
| POST | `/machines/{id}/inspect` | Re-inspect (202). Body `{accept_new_host_key}`; accepting a new host key requires `?confirm=<name>`. |
| DELETE | `/machines/{id}?confirm=<name>` | Remove from the inventory. 409 while the machine belongs to a cluster. The server itself is not changed. |
| POST | `/machines/{id}/power` | `{action: start\|stop\|reboot}` → `machine.power` (202). Stopping or rebooting a cluster node requires `?confirm=<name>`. Only for providers with the `power` capability. |
| POST | `/machines/{id}/destroy?confirm=<name>` | Delete a platform-provisioned machine at its provider, then remove it from the inventory (`machine.deprovision`, 202). 409 for registered (not provisioned) machines and cluster nodes. |

Machines include `provider_type`, `connection_name`, `external_id`, `power_state`, `provisioned`,
`requested_spec` and `provider_metadata` (for local machines: the published Kubernetes API endpoint).

Machine responses include `connectivity_status` (`reachable`, `unreachable`, or
`unknown`), `connectivity_checked_at`, `connectivity_error`, and `last_seen_at`.
The scheduler checks saved SSH endpoints every 30 seconds with the pinned host
key and credentials, using an unprivileged `true` command. It does not reinstall,
restart or remove machines. No key is accepted automatically by the monitor;
first contact still requires inspection. Connection failures preserve lifecycle
and cluster membership. Results older than three probe intervals become unknown,
so a stopped scheduler cannot leave a stale green status. The Machines page polls
every 10 seconds; **Unassigned** describes cluster membership, not connectivity.
An SSH failure cannot establish whether a VM was deleted, powered off, moved or
blocked by a network problem; these all appear unavailable until access recovers.

Use **Infrastructure → Machines → actions → Update address** when a registered
SSH server moves to another IP or hostname. Its ID, SSH port/user/credential,
trusted host key and cluster assignment are preserved. The address is saved even
if the subsequent inspection fails; follow the returned operation for the result.
Changing this connection does not configure the server's network, update a
cluster's API endpoint, or regenerate Kubernetes certificates. Provider-provisioned
machines retain their provider-managed identity and do not expose this action.

### Provider connections and provisioning

| Method | Path | Description |
|---|---|---|
| GET | `/provider-connections` | Connections with status, capabilities and machine count. |
| POST | `/provider-connections` | `{name, provider_type, config, credential_id?}` (201) → the connection plus its `provider.validate` operation. Planned providers and `ssh` are rejected; the credential kind must match the provider. |
| GET | `/provider-connections/{id}` | One connection. |
| PUT | `/provider-connections/{id}` | `{config?, credential_id?, clear_credential?}` → re-validates. |
| DELETE | `/provider-connections/{id}?confirm=<name>` | 409 while machines belong to it (204). Nothing changes at the provider. |
| POST | `/provider-connections/{id}/validate` | Re-validate (202). |
| POST | `/provider-connections/{id}/discover` | `provider.discover` (202). `result.machines`: discovered machines with `inventory_machine`, `importable`, `ssh_endpoint`, `kube_api_endpoint`. Read-only. |
| POST | `/provider-connections/{id}/machines` | `{names[1..20], cpu_cores, memory_mb, disk_gb, credential_id, ssh_username?, network?, image?, labels?, provider_options?}` (202) → one `machine.provision` operation per machine. `credential_id` must be an SSH private key; its public key is installed. Proxmox: `provider_options.ipconfig` (cloud-init `ipconfig0`). |
| POST | `/provider-connections/{id}/import` | `{external_id, name, host, port, username?, credential_id, kube_api_host?, kube_api_port?}` (201): add an importable discovered machine to the inventory and inspect it. Discovered cloud instances are not importable; provisioned AWS machines are managed through the normal provisioning flow. |

| POST | `/machines/{id}/network?confirm=<machine-name>` | `{network, addresses?, options?}` → `machine.network` (202). Provider-specific network changes on platform-provisioned machines. |

### Network discovery

| Method | Path | Description |
|---|---|---|
| POST | `/network-discovery` | `{ranges[], ports=[22], timeout_seconds=1.5}` (202) → `network.discover`. `result.hosts`: `{address, port, banner, os_hint, hostname, registered_as}`. Public ranges are rejected unless allowed by configuration. |

### Clusters

`POST /clusters/{id}/upgrade` accepts `{target_version: "1.34.2", allow_emptydir_data?: false}` and returns a tracked operation. It currently supports platform-installed kubeadm clusters and allows at most one minor-version step. EmptyDir data loss must be explicitly enabled.

| Method | Path | Description |
|---|---|---|
| GET | `/clusters` | Clusters with state, version, node count and any active operation. |
| POST | `/clusters/validate` | Check `{spec}` against distribution rules and the live inventory. Returns `{valid, problems, steps, nodes, notes}`; no side effects. `notes` lists defaults applied automatically (e.g. for container machines). |
| POST | `/clusters` | Create `{spec, description?}` (202). Returns the cluster plus the `cluster.create` operation. 422 with `details.problems` when invalid. |
| GET | `/clusters/{id}` | Detail: spec, nodes, status detail (including the last failure), operation history. The kubeconfig is never included. |
| DELETE | `/clusters/{id}?confirm=<name>&force=<bool>&destroy_machines=<bool>` | Reset all nodes and release the machines (202). `force` continues past unreachable nodes; `destroy_machines` also destroys machines the platform provisioned. |
| POST | `/clusters/{id}/nodes` | `{pool, existing_machine_ids[], provision_count, machine_template?, provider_connection_id?, machine_credential_id?, labels?, taints?}` → `cluster.nodes.add` (202). `pool` may be an existing pool, the control-plane pool (clusters with a shared endpoint only) or a new worker pool (then `labels`/`taints` apply). |
| DELETE | `/clusters/{id}/nodes/{node}?confirm=<node>&destroy_machine=<bool>&force=<bool>` | Drain, reset and release the node (`cluster.nodes.remove`, 202). The last control plane cannot be removed. |

| POST | `/clusters/{id}/pools/{pool}/resize` | `{count}` → `cluster.pool.resize` (202). Managed clouds and k3d; scale-down requires `confirm=<cluster-name>`. GKE counts are per zone. |
| POST | `/clusters/{id}/upgrade` | `{target_version, target_release_revision?, allow_emptydir_data?}` → `cluster.upgrade` (202). kubeadm/K3s/RKE2 and managed clouds; EKS targets the next minor version. |

`ClusterSpec` (abridged):
`{name, distribution: "kubeadm", provider_type: "ssh"|"local"|"proxmox", provider_connection_id?, machine_credential_id?,
machine_username?, kubernetes_version: "1.37" | "1.37.2", network: {cni, pod_cidr, service_cidr, dns_domain},
control_plane: {count, existing_machine_ids, machine_template?, endpoint?, load_balancer: "none"|"kube-vip"|"external"|"provider"},
worker_pools: [{name, count, existing_machine_ids, machine_template?, labels, taints}], options: {allow_swap, control_plane_workloads,
kube_proxy_conntrack_max_per_core, ignore_preflight_errors, api_server_extra_sans, ready_timeout_seconds,
provider_lb_vpc_id, provider_lb_subnet_ids, provider_lb_scheme}}`. Provider-managed API load balancing currently creates an AWS Network Load Balancer and needs an AWS connection, VPC ID and subnet IDs.

For `eks`, `aks` and `gke`, set `control_plane.count` to `0`, provide a matching cloud `provider_connection_id`, and put desired node counts in `worker_pools` (with a `machine_template` for each nonzero pool). EKS options include `managed_cluster_role_arn`, `managed_node_role_arn` and `managed_subnet_ids`; AKS uses `managed_resource_group`, `managed_location` and `managed_subnet_id`; GKE uses `managed_location`, `managed_network` and `managed_subnetwork`. IAM roles, resource groups, VNets and VPC networks must already exist.

Kind/k3d require a Local Docker connection, exact patch versions and node counts rather than SSH inventory machines.
K3s/RKE2 accept `options.release_revision` for their release suffix, and use external API load balancing for HA.
Machine templates accept `image` and `provider_options`; managed templates honor disk and machine-type overrides.
Creation storage accepts `default_storage_class`, `csi_drivers: ["nfs.csi.k8s.io"]`, `nfs_server` and `nfs_share`.
See [Cluster integrations](cluster-integrations.md) for configuration, ownership safeguards and supported network operations.

### Cluster connectivity and live data

| Method | Path | Description |
|---|---|---|
| POST | `/clusters/import/inspect` | `{kubeconfig}` → contexts with auth type and whether each is usable. Nothing is stored. |
| POST | `/clusters/import` | `{name, kubeconfig, context?, server_override?, tls_server_name?, description?}` (202). Stores the minified context encrypted and starts `cluster.connect`. |
| PUT | `/clusters/{id}/connection` | `{server_override, tls_server_name}`, then re-verifies (202). |
| POST | `/clusters/{id}/connect` | Re-verify the connection and refresh health (202). |
| POST | `/clusters/{id}/unregister?confirm=<name>` | Imported clusters only (204). Forgets the cluster and its kubeconfig; the cluster is not changed. |
| POST | `/clusters/{id}/addons/metrics-server` | `{kubelet_insecure_tls}` → installs metrics-server v0.9.1 (202). |
| GET | `/clusters/{id}/health` | Live summary: version, readiness checks, nodes, pods, CPU/memory (allocatable, requested, used), alerts. |
| GET | `/clusters/{id}/monitoring/connection` | The selected cluster’s saved Prometheus Service and optional Grafana base URL, or null. |
| PUT | `/clusters/{id}/monitoring/connection` | Set or clear `{connection: {namespace, service, port, grafana_url?} \| null}`. Requires cluster write access. |
| GET | `/clusters/{id}/monitoring` | Historical node and pod CPU/memory plus node disk usage, disk I/O and network receive/transmit. Set `window=1h\|6h\|24h` and optionally `namespace` for pod charts. Returns evaluation timestamps, interval, and per-metric series with explicit panel status. |
| GET | `/clusters/{id}/logs/connection` | The selected cluster’s saved Loki Service or null. |
| PUT | `/clusters/{id}/logs/connection` | Set or clear `{connection: {namespace, service, port} \| null}`. Requires cluster write access. |
| GET | `/clusters/{id}/logs/namespaces` | Sorted namespace labels from the selected Loki Service. Accepts the same `window` as log search. |
| GET | `/clusters/{id}/logs/pods` | Loki pod labels filtered by required `namespace` and optional `window`. |
| GET | `/clusters/{id}/logs/containers` | Loki container labels filtered by required `namespace`, optional `pod`, and `window`. |
| GET | `/clusters/{id}/logs` | Historical pod log search. Requires `namespace`. Optional filters are `pod`, `container`, `contains`, and `window=15m\|1h\|6h`. Returns up to 200 entries, newest first. |
| GET | `/clusters/{id}/nodes` | Live nodes with roles, conditions, capacity, requested and used resources, pod counts. |
| GET | `/clusters/{id}/nodes/{name}` | One node plus its pods and events. |
| POST | `/clusters/{id}/nodes/{name}/cordon` · `/uncordon` | Mark the node (un)schedulable; returns the node. |
| PATCH | `/clusters/{id}/nodes/{name}/labels` | `{set: {k: v}, remove: [k]}`; Kubernetes-maintained labels are refused. |
| PUT | `/clusters/{id}/nodes/{name}/taints` | `{taints: [{key, value?, effect}]}` replaces the user-managed taints. |
| POST | `/clusters/{id}/nodes/{name}/drain` | `{force, delete_emptydir_data, grace_period_seconds?, timeout_seconds}` → `cluster.node.drain` (202). |
| GET | `/clusters/{id}/nodes/{name}/logs?unit=kubelet\|containerd&lines=` | Journal of the node as text (platform-installed clusters). |
| GET | `/clusters/{id}/namespaces` | Namespaces with pod counts and failing pods. |
| GET | `/clusters/{id}/security/pod-security` | Explicit namespace Pod Security Admission labels. Returns `{items, truncated}`. Each item has `namespace` and `modes.enforce\|audit\|warn` with nullable `level` and `version`. Requires `clusters.read`. |
| GET | `/clusters/{id}/security/report` | On-demand configuration report for the selected cluster. Returns `cluster_id`, `cluster_name`, `observed_at`, `coverage`, and severity-ordered `findings`. Requires `clusters.read`. |
| GET | `/clusters/{id}/pods?namespace=&node=&problems=` | Pods with kubectl-style status, readiness, restarts, owner. |
| GET | `/clusters/{id}/events?namespace=&warnings=&limit=` | Recent events, newest first. |
| GET | `/clusters/{id}/topology` | Nodes with their pods (and unscheduled pods). |

Live endpoints return **503 `cluster_unreachable`** or **502 `cluster_auth_failed`** when the cluster cannot be
used. `GET /clusters` and `GET /clusters/{id}` include the last observed `health`, `health_error` and
`observed_at`.

### DNS diagnostics and NFS CSI lifecycle

All paths below are under `/clusters/{id}`. Mutations return an asynchronous `OperationDetail` (202),
with logs, cancellation and retry available through `/operations/{operation_id}`. The cluster must be
connected and not deleting; tenancy and the cluster operation concurrency lock apply.

| Method | Path | Description |
|---|---|---|
| POST | `/dns/lookup` | `{namespace?: "default", hostname?: "kubernetes.default.svc", record_type?: "A"}`. Types: A, AAAA, CNAME, SRV, TXT, MX. Starts `cluster.dns.lookup`. |
| GET | `/storage/nfs` | Supported pinned version, registered/managed state, controller and node readiness, NFS StorageClasses. |
| POST | `/storage/nfs` | `{storage_class?: null, server?: null, share?: null, make_default?: false}`. Starts `cluster.storage.nfs.install` to install or reconcile NFS CSI v4.13.6. |
| DELETE | `/storage/nfs?confirm=<cluster-name>` | Starts `cluster.storage.nfs.remove`. Confirmation is mandatory (428 otherwise); references or external ownership fail the operation before deletion. |

DNS results contain `{hostname, record_type, namespace, resolved, exit_code, output}`. A completed query
that returns NXDOMAIN reports `resolved: false`; scheduling, admission and timeout failures fail the operation.
The Job runs for at most 60 seconds, output is capped at 32 KiB, and cleanup uses the Job UID. A TTL provides
cleanup after worker failure. The utility image is `registry.k8s.io/e2e-test-images/dnsutils:1.3`.

For NFS, providing `storage_class` requires both `server` and an absolute `share` path. The existing NFS 4.1
export must be reachable and writable from Linux nodes using `/var/lib/kubelet`. The installer downloads four
upstream manifests from a pinned release and checks SHA-256 hashes before applying. It installs privileged
controller/node plugins and their RBAC in `kube-system`, then waits up to five minutes for readiness.
Existing resources without the add-on ownership labels are refused; another default StorageClass is never
silently replaced. Configured classes use `Retain`, `onDelete: retain`, and NFS 4.1. Existing class parameters
are subject to Kubernetes immutability rules; create a new class to use a different export. Driver readiness
does not validate the export; provisioning is verified when a claim binds. Snapshot controller installation
and dedicated installers for other vendors are outside this integration.

Removal refuses incomplete inventories, Pending claims, retained/released volumes, attachments, and foreign
resources. It deletes only owned driver/RBAC/class resources with UID preconditions; it never deletes PVs,
PVCs or NFS files. It does not require an upstream download and can clean up a partial installation.

### Workloads and resources

`{kind}` is one of `pods`, `deployments`, `replicasets`, `statefulsets`, `daemonsets`, `jobs`, `cronjobs`,
`services`, `ingresses`, `networkpolicies`, `endpointslices`, `persistentvolumes`, `persistentvolumeclaims`,
`storageclasses`, `csidrivers`, `volumeattachments`, `gatewayclasses`, `gateways`, `httproutes`,
`referencegrants`, `resourcequotas`, `limitranges`, `configmaps`, `secrets`, `roles`, `clusterroles`,
`rolebindings`, `clusterrolebindings`, `serviceaccounts`, `validatingadmissionpolicies`, and
`validatingadmissionpolicybindings`. All paths are under `/clusters/{id}`. For cluster-scoped kinds, pass
`cluster` in the `{namespace}` path segment. Gateway API resources require their CRDs and controller.
The direct delete endpoint is disabled for CSIDrivers, VolumeAttachments, RBAC objects, ServiceAccounts,
and validating admission policies and bindings. These security resource details omit event lookups and
use `clusters.read`. ServiceAccount Secret references are names only. Encode resource names as path segments,
including names such as `system:kube-proxy`. Admission inventory uses `admissionregistration.k8s.io/v1`.
A missing endpoint returns `404 not_found_in_cluster`; an available endpoint with no objects returns an empty list.

| Method | Path | Description |
|---|---|---|
| GET | `/resources/{kind}?namespace=` | Summaries (status, readiness, images, age…). `truncated` is set when the list was capped. |
| GET | `/resources/{kind}/{namespace}/{name}` | `{kind, summary, object, yaml, events, capabilities}`, plus kind-specific fields such as deployment history and storage/network summaries. Secret values are `<redacted>`. |
| DELETE | `/resources/{kind}/{namespace}/{name}?confirm=<name>` | Delete (204). |
| POST | `/resources/{kind}/{namespace}/{name}/scale` | `{replicas}` (deployments, statefulsets, replicasets). |
| PUT | `/deployments/{namespace}/{name}/scaling-policy` | `{minimum, maximum, metric: "cpu"|"memory", target_utilization, scale_down_stabilization_seconds?}` creates or updates an `autoscaling/v2` HPA; requests must exist on workload containers. |
| DELETE | `/deployments/{namespace}/{name}/scaling-policy` | Removes the platform-managed HPA for that Deployment. |
| POST | `/resources/{kind}/{namespace}/{name}/restart` | Rolling restart (deployments, statefulsets, daemonsets). |
| POST | `/deployments/{namespace}/{name}/rollback` | `{revision}` → restore that revision's pod template. |
| POST | `/cronjobs/{namespace}/{name}/trigger` | Create a Job from the CronJob now; returns the Job name. |
| POST | `/cronjobs/{namespace}/{name}/suspend` | `{suspend: bool}`. |
| POST | `/apply` | `{manifest, namespace?, dry_run}`. Multi-document YAML. Returns per-object `{kind, namespace, name, action: create\|update\|unchanged, diff, note?}`. Refuses manifests containing `<redacted>`. |
| POST | `/secrets/{namespace}/{name}/reveal` | Decoded secret values (audited). |
| GET | `/pods/{namespace}/{name}/logs?container=&tail=&previous=&timestamps=` | Log snapshot as `text/plain`. |
| GET | `/pods/{namespace}/{name}/logs/stream?container=&tail=` | **SSE** `line` events while following (`error` on failure, `end` when the stream closes; capped at 15 minutes). |
| WS | `/pods/{namespace}/{name}/exec?container=` | Interactive shell. Client sends `{"type":"input","data"}` and `{"type":"resize","cols","rows"}`; server sends `output`, `exit` and `error`. Close codes 4403 (origin) and 4400 (invalid reference). |

### Audit

| Method | Path | Description |
|---|---|---|
| GET | `/audit-events` | Audit trail. Filters: `resource_type`, `resource_id`, `action`. |

### Backup and restore

Platform administrators can list archive metadata (`GET /backups`), start an encrypted
backup (`POST /backups`), download ciphertext (`GET /backups/{backup_id}/archive`), and
stage a recovered database (`POST /backups/{backup_id}/restore` with `confirm` set to
the archive UUID). Both writes return a tracked platform operation.

`GET /clusters/{id}/backups` returns Velero backups, restores, storage locations and
list truncation indicators. `POST /clusters/{id}/backups` accepts `name` and `location`.
`POST /clusters/{id}/restores` accepts `name`, `backup` and the exact cluster name
as `confirm`. A 202 response means Velero accepted a request. Read its phase to
determine the outcome. [Recovery guide](backup-restore.md)

### Helm releases

All endpoints below are relative to `/clusters/{id}/helm`. Read requests require
cluster read permission; mutations require cluster write permission. Replies omit
installed values, manifests, release notes and potentially sensitive descriptions.

| Method | Path | Behavior |
|---|---|---|
| GET / PUT | `/repositories` | List or replace up to twenty named HTTPS indexes or full OCI chart references (`{repositories: [{name, url, ...}]}`). |
| GET | `/charts?repository=...&search=...` | Browse up to 500 matching chart names and up to 100 indexed versions per chart. |
| GET | `/charts/values?repository=...&chart=...&version=...` | Fetch digest-verified chart defaults; does not read installed values. |
| GET | `/releases?namespace=...` | Inventory up to 256 releases, optionally restricted to a namespace; reports truncation. |
| GET | `/releases/{namespace}/{release}/history` | Sanitized history, up to fifty revisions. |
| POST | `/preview` | Review `action`, `release`, `namespace`, optional chart repository/name/version, `values_yaml`, `reuse_values`, `hooks`, rollback `revision`, `timeout_seconds`; returns expiring plan ID and resource inventory. Values are encrypted at rest. |
| POST | `/execute` | Submit `{plan_id, confirm?}`; non-install actions require the release name as confirmation. Returns a tracked operation (202). |

Apply migrations through `0011` and install the pinned Helm runtime on API/worker hosts.
Repository entries accept write-only `authentication: {mode: "basic" | "bearer",
username?, password}`; `password` contains the password/token. Reads return only
an opaque `auth_id`. Preserve that reference or omit authentication to retain it;
`clear_credentials: true` explicitly removes it. Rotation/removal revokes previous
credentials and invalidates affected source reviews. Credentials cannot be borrowed
from another cluster or repository URL.

`allow_private_network`, `ca_pem` and `token_service_url` configure endpoint trust;
new or changed trust requires `organization.manage` in addition to cluster write
access. The token service is an exact HTTPS endpoint, without query parameters.
Private access permits RFC1918/ULA and custom TLS ports, never loopback/link-local
or metadata addresses. TLS verification and origin-bound credential handling remain
mandatory.
See [Helm management](helm.md) for review, recovery and repository limitations.

### GitOps applications

All endpoints are relative to `/clusters/{id}/gitops/applications`, tenant/project
scoped. GET requires `clusters.read`; mutations require `clusters.write` and browser
CSRF protection. Creating/changing private endpoint or CA trust additionally
requires `organization.manage`.

| Method | Path | Behavior |
|---|---|---|
| GET / POST | `/` | List up to 100 application configurations or create one. |
| PUT | `/{application_id}` | Replace configuration, advancing generation and invalidating reviews. |
| POST | `/{application_id}/review` | Optional `{prune: false, allow_empty: false}`; fetch branch, encrypt commit-pinned manifests, validate resources and record drift/prune candidates. Returns review ID, SHA, expiry and sanitized resource actions. |
| POST | `/{application_id}/sync` | `{review_id, confirm: application_name}` queues the reviewed commit as a tracked operation (202). |
| GET | `/{application_id}/history` | Latest twenty sanitized review entries, commit and operation links. |
| POST | `/{application_id}/forget` | `{confirm: application_name}` removes registration and saved credentials; retains Kubernetes resources. |

Configuration fields: `name`, HTTPS `url`, `branch` (default `main`), `path`
(default `.`), existing `namespace`, `allow_private_network` (default false),
optional `ca_pem`, optional write-only `authentication: {mode, username?, password}`
and `clear_credentials`. Authentication mode is `basic` or `bearer`; `password`
holds the password/token. Omitting authentication preserves credentials; changing
repository URLs requires replacement credentials or explicit removal. Reads expose
only `authenticated`, never credential material. Manifest snapshots and Secret
contents are absent from all public responses. Apply migration `0012` and install
Git 2.46+ on API/worker hosts. Apply migrations through `0013` for automation.

| Method | Path relative to the application | Behavior |
|---|---|---|
| PUT | `/automation` | Organization administrators configure `{enabled, prune, allow_empty, interval_seconds, generation, confirm}`. Exact application-name confirmation and current generation are mandatory. |
| POST | `/automation/pause` | Cluster writers pause scheduling and revoke queued/further mutations by advancing generation. |
| POST | `/automation/reconcile` | Cluster writers request the next scheduler check under an already approved active policy. Returns `{scheduled: true}`; no approval bypass. |

Automation defaults disabled; pruning defaults false. `interval_seconds` is 60–3,600
(default 300). `allow_empty` requires `prune`. Source/credential edits pause automation.
Reads include public policy, next due time, latest operation and observations;
review/history includes explicit prune and protected-retention plans. Auto execution
uses the persistent administrator approval; manual pruning still requires review and
confirmation. Replaced/unowned resources block pruning, while PVCs, StatefulSets,
custom resources and retention annotations are protected. Deletes use UID/resource-
version preconditions and preserve finalizers. [GitOps guide](gitops.md)

### Custom resources

All paths below are relative to `/clusters/{id}/custom-resources` and respect
organization/project scope. Reads require `clusters.read`, mutations require
`clusters.write`, and explicit YAML reveals additionally require `secrets.reveal`.
Browser writes require CSRF protection. Apply migration `0014`.

| Method | Path | Behavior |
|---|---|---|
| GET | `/definitions` | Bounded installed CRD inventory; distinguishes unavailable/forbidden APIs. |
| GET | `/definitions/{name}?version=...` | Metadata, safe schema guidance and skeleton template; optional namespace. |
| GET | `/definitions/{name}/resources?version=...` | Metadata-only resource page; optional namespace and continue_token. |
| GET | `/resource` | Metadata/conditions for definition, version, name and optional namespace/expected_uid. |
| POST | `/definitions/{name}/reveal` | Audited full schema YAML; body version and optional definition_uid. |
| POST | `/reveal` | Audited full resource/edit YAML and content_hash; body identifies target and optional expected UIDs. |
| POST | `/review` | Dry-run create/update/delete; encrypted, author-bound 15-minute review with field paths only. |
| POST | `/execute` | `{review_id, confirm: object_name}` executes the reviewed action and returns metadata/status. |

Review bodies require `definition`, `definition_uid`, `version`, `name`, `action`,
optional namespaced `namespace`, and `manifest` YAML for create/update. Update/delete
require `expected_uid`; update additionally requires `expected_hash` from reveal's
`content_hash`. Definition/content changes invalidate reviews. Accepted delete
requests preserve finalizers and can remain pending. Status-subresource writes are not provided. Definition lifecycle uses the
separate administrator endpoints below. [Custom-resource guide](custom-resources.md)

Definition lifecycle uses the same `/clusters/{id}/custom-resources` prefix:

| Method | Path | Behavior |
|---|---|---|
| POST | `/definitions/{name}/manifest/reveal` | Explicit audited full/edit-ready definition YAML and content_hash; body `{name, expected_uid?}`. Requires secrets.reveal. |
| POST | `/definition-reviews` | `{name, action: install/update/remove, manifest?, expected_uid?, expected_hash?}`. Organization administrators review a platform-owned definition action. |
| POST | `/definition-execute` | `{review_id, confirm: full_definition_name}` executes a current, author-bound definition review; organization administrator and cluster write grants required. |

Definition inventory includes a `managed` boolean scoped to the selected cluster.
Updates/removal require expected_uid; updates also require expected_hash from
manifest reveal. New installs never adopt an existing definition. API identity,
storage version, version retention, conversion, empty-instance inventory and
ownership guards apply. Specification changes and removal require empty inventory
at review and execution; concurrent external writers are not fenced. Kubernetes
can delete instances created in the gap before definition removal. Pause writers
before using removal. Accepted results report establishment/deletion separately.
Definition review payloads share migration `0014` encrypted storage; instance and
definition execution routes cannot consume one another's reviews.

### Reviewed custom workflows and CRD operations

`/clusters/{id}/custom-workflows` supports GET inventory and POST create.
PUT `/{workflow_id}` replaces an encrypted definition using its current generation.
Bodies are `{name, document: YAML, generation?}`. Supported steps are cluster.check,
resource.scale, resource.restart and resource.wait. Definition changes/deletion
require organization.manage; review/run require clusters.write. Reads are scoped.

| Method | Path relative to custom-workflows | Behavior |
|---|---|---|
| POST | `/{workflow_id}/reveal` | Explicit audited YAML reveal; secrets.reveal. |
| POST | `/{workflow_id}/review` | Dry-run ordered steps; encrypted immutable target review. |
| POST | `/{workflow_id}/run` | `{review_id, confirm: workflow_name}` returns tracked operation (202). |
| GET | `/{workflow_id}/history` | Latest twenty safe reviews and operation links. |
| POST | `/{workflow_id}/forget` | `{confirm: workflow_name}` removes the definition; accepted changes remain. |

Under `/clusters/{id}/custom-resources`, administrator endpoints are:

| Method | Path | Behavior |
|---|---|---|
| POST | `/migration-reviews` | `{name, expected_uid, expected_hash, target_version, mode?: certified/rewrite_only}`; preview storage migration and select native/manual capability. |
| POST | `/conversion-reviews` | The same revealed definition identity plus `{action: configure/probe, strategy: Webhook/None, service_namespace?, service_name?, port?, path?, ca_pem?}`. |
| POST | `/automation-run` | `{category: storage_migration/conversion_webhook, review_id, confirm: definition_name}` returns tracked operation (202). |
| GET | `/definition-operations?name=...` | Latest twenty safe migration/conversion reviews, progress and operation links. |

Certified migration requires storagemigration.k8s.io/v1. Explicit rewrite_only
requires an observable single control plane, uses the current storage version and
retains all stored-version history with RewrittenUncertified output. Conversion supports Service-backed TLS
ConversionReview v1. Empty-instance configuration reports lack of protocol proof.
Incomplete migration never authorizes clearing old stored-version metadata.
Reviews expire after fifteen minutes before starting and cannot be consumed by
other execution categories. Workers reauthorize the original user's current
memberships before every action, retaining cluster exclusivity and cancellation.
Apply migration `0015` for custom workflow definitions. See
[Custom workflows](custom-workflows.md) and [CRD operations](crd-operations.md).

### Operational monitoring

`GET /clusters/{id}/monitoring/remediation` returns the opt-in policy, last scan
status and latest 100 observations/proposals with operation links. Cluster readers
can inspect it. Organization/platform administrators can `PUT` the policy
(`enabled`, `namespaces`, `grace_seconds`, `cooldown_minutes`), `POST` to
`/remediation/scan`, and `POST` to `/remediation/{request_id}/approve` with
`{confirm: <cluster name>}` or `/remediation/{request_id}/dismiss`.
Approval returns a tracked operation (202). Every eviction requires a fresh,
administrator-approved proposal and revalidation in the worker. These endpoints
use the normal project scope, CSRF policy and audit trail. Apply migration `0009`.

`GET /clusters/{id}/monitoring/alerts` returns current findings with investigation
guidance, per-source coverage and the connected Prometheus alert rule states.
It requires cluster read access. The eight historical monitoring panels now include
disk usage, disk I/O, network receive and network transmit.

`GET /clusters/{id}/logs/collection` returns collector registration, selected
namespaces and available namespaces. `POST /clusters/{id}/logs/collection`
accepts `{namespaces: [...]}`, requires cluster write access, and returns a
tracked operation for namespace grant/configuration reconciliation.

### Command Center

All endpoints are cluster-scoped under `/clusters/{cluster_id}/commands` and
require operator/administrator command access. See [Command Center](command-center.md)
for the supported summary/mutation command subset and limits.

- `POST /reviews`: `{command}` → canonical command, target name, review ID and expiry;
  creates an author-bound encrypted review. Reads do not contact the cluster; mutations
  inspect targets and run Kubernetes dry runs or Helm server simulation without applying the change.
- `POST /run`: `{review_id, confirm}` → tracked operation (`202`); confirmation is
  the cluster name for reads or the target workload/release name for mutations.
  A review can be submitted once, idempotently. Mutations use `cluster.command.mutate`.
- `GET /history`: current author's latest 20 command reviews and operation links.
- `GET /{review_id}/output`: explicitly audited private output retrieval. `result`
  remains null until a result is available; completed output includes timestamp,
  selected summary rows and truncation. Returns `Cache-Control: no-store`.

Operation metadata never includes raw command output. Use the existing operation
cancellation endpoint to request cancellation; a pending Kubernetes read may finish
its bounded socket request before observing it.
