# API reference (Phase 0)

The machine-readable contract is the OpenAPI document at `/api/v1/openapi.json`. Interactive docs are at
`/api/v1/docs`. This page summarizes the endpoints and their conventions.

## Conventions

* **Base path:** `/api/v1`. All endpoints except `/health/*` require an authenticated principal (`AUTH_MODE=dev`
  in Phase 0).
* **Errors** always look like this:
  `{"error": {"code": "conflict", "message": "…", "details": {…}, "request_id": "…"}}`

  | Code | HTTP status |
  |---|---|
  | `validation_failed` | 422 |
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
| POST | `/machines/{id}/inspect` | Re-inspect (202). Body `{accept_new_host_key}`; accepting a new host key requires `?confirm=<name>`. |
| DELETE | `/machines/{id}?confirm=<name>` | Remove from the inventory. 409 while the machine belongs to a cluster. The server itself is not changed. |
| POST | `/machines/{id}/power` | `{action: start\|stop\|reboot}` → `machine.power` (202). Stopping or rebooting a cluster node requires `?confirm=<name>`. Only for providers with the `power` capability. |
| POST | `/machines/{id}/destroy?confirm=<name>` | Delete a platform-provisioned machine at its provider, then remove it from the inventory (`machine.deprovision`, 202). 409 for registered (not provisioned) machines and cluster nodes. |

Machines include `provider_type`, `connection_name`, `external_id`, `power_state`, `provisioned`,
`requested_spec` and `provider_metadata` (for local machines: the published Kubernetes API endpoint).

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
| GET | `/clusters/{id}/nodes` | Live nodes with roles, conditions, capacity, requested and used resources, pod counts. |
| GET | `/clusters/{id}/nodes/{name}` | One node plus its pods and events. |
| POST | `/clusters/{id}/nodes/{name}/cordon` · `/uncordon` | Mark the node (un)schedulable; returns the node. |
| PATCH | `/clusters/{id}/nodes/{name}/labels` | `{set: {k: v}, remove: [k]}`; Kubernetes-maintained labels are refused. |
| PUT | `/clusters/{id}/nodes/{name}/taints` | `{taints: [{key, value?, effect}]}` replaces the user-managed taints. |
| POST | `/clusters/{id}/nodes/{name}/drain` | `{force, delete_emptydir_data, grace_period_seconds?, timeout_seconds}` → `cluster.node.drain` (202). |
| GET | `/clusters/{id}/nodes/{name}/logs?unit=kubelet\|containerd&lines=` | Journal of the node as text (platform-installed clusters). |
| GET | `/clusters/{id}/namespaces` | Namespaces with pod counts and failing pods. |
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
`referencegrants`, `resourcequotas`, `limitranges`, `configmaps`, `secrets`. All paths are under `/clusters/{id}`. For cluster-scoped kinds, pass
`cluster` in the `{namespace}` path segment; Kubernetes requests omit the namespace. Gateway API resources require
their CRDs and controller to be installed. Deleting controller-managed CSIDriver and VolumeAttachment objects through this API is disabled.

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
