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

### Clusters

| Method | Path | Description |
|---|---|---|
| GET | `/clusters` | Clusters with state, version, node count and any active operation. |
| POST | `/clusters/validate` | Check `{spec}` against distribution rules and the live inventory. Returns `{valid, problems, steps, nodes}`; no side effects. |
| POST | `/clusters` | Create `{spec, description?}` (202). Returns the cluster plus the `cluster.create` operation. 422 with `details.problems` when invalid. |
| GET | `/clusters/{id}` | Detail: spec, nodes, status detail (including the last failure), operation history. The kubeconfig is never included. |
| DELETE | `/clusters/{id}?confirm=<name>&force=<bool>` | Reset all nodes and release the machines (202). `force` continues past unreachable nodes. |

`ClusterSpec` (abridged):
`{name, distribution: "kubeadm", provider_type: "ssh", kubernetes_version: "1.37" | "1.37.2", network: {cni, pod_cidr,
service_cidr, dns_domain}, control_plane: {count, existing_machine_ids, endpoint?, load_balancer: "none"|"external"},
worker_pools: [{name, count, existing_machine_ids, labels, taints}], options: {allow_swap, control_plane_workloads,
kube_proxy_conntrack_max_per_core, ignore_preflight_errors, api_server_extra_sans, ready_timeout_seconds}}`

### Audit

| Method | Path | Description |
|---|---|---|
| GET | `/audit-events` | Audit trail. Filters: `resource_type`, `resource_id`, `action`. |
