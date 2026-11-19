# Loki log search

Open **Operations → Logs** and select a connected cluster. Choose
**Connect Loki** and enter the namespace, Kubernetes Service name, and HTTP
Service port. Viewers can search logs for clusters in their project.
Operators can change or remove a connection. A Loki connection can be
configured independently of Prometheus.

Apply database migration `0008` before restarting the API, worker and scheduler.
The cluster credential requires `get` on `services/proxy` for the Loki Service.
The platform queries a fixed Loki API path through that Service and uses the
existing Kubernetes connection and TLS trust. [Loki HTTP API](https://grafana.com/docs/loki/latest/reference/loki-http-api/)

## Search

Select a lookback of 15 minutes, 1 hour, or 6 hours. The **Namespace** menu
shows the namespace labels returned by Loki for the selected cluster and
lookback. If Loki returns a single namespace, it is selected automatically.
Namespace, Pod, and Container each provide a search box above the available
options. Type part of a name to filter the list, then select a match. Pod
options follow the selected namespace, and container options follow the
namespace and pod. Choose All pods or All containers to clear those filters. The text filter matches
a literal, case-sensitive substring. Results are ordered newest first and
limited to 200 entries. Lines longer than 4,096 characters are shortened
in the viewer. Logs from a replaced pod can remain searchable for the
configured Loki retention period.

Loki streams need a `namespace` label. Supply `pod` and `container` labels
to enable those filters and identify the source of each entry. The
historical viewer reads logs already collected by Loki. For a live stream
from a running pod, use its **Logs** tab under **Workloads → Pods**.

## Local Fedora deployment

The manifests under `infrastructure/monitoring/loki` provision Loki and
Alloy in the existing `kplatform-monitoring` namespace. Apply them with
`kubectl apply -k infrastructure/monitoring/loki`. Connect the platform to
`kplatform-monitoring/loki:http`. The lab pins both deployments to the
control-plane node.

Alloy discovers pods and reads their logs through the Kubernetes API. The
example grants it permissions only in `kplatform-monitoring`. To collect
from additional namespaces, extend the Alloy namespace discovery list and
grant its ServiceAccount pod discovery and `pods/log` access in each
selected namespace. [Alloy Kubernetes log source](https://grafana.com/docs/alloy/latest/reference/components/loki/loki.source.kubernetes/)

The example uses a single-tenant, internal Loki Service with local filesystem
storage and a 24-hour retention policy. The supplied deployment stores this data on its PVC. For a shared deployment, use authenticated Loki access
behind an internal Service and durable storage suited to its retention
requirements. [Loki retention](https://grafana.com/docs/loki/latest/operations/storage/retention/)

## Configure collected namespaces

Operators can expand **Collected namespaces** on the Logs page and select the
namespaces Alloy should collect. The app submits a tracked operation and shows
its operation link. Refresh status after completion.

The manager targets a dedicated Alloy Deployment named `alloy` in the Loki
Service's namespace, running as the `alloy` ServiceAccount and writing to
`loki:3100` there. Register the installation with the Deployment annotation
`kubernetes-platform.io/log-collector=<platform-cluster-UUID>`. Registration
should follow a review of the existing installation. The Fedora lab is registered.

The operation creates owned namespace Roles and RoleBindings, deploys a
versioned Alloy ConfigMap, verifies rollout, then revokes its own grants for
deselected namespaces. Existing grants managed by another installation retain
their ownership. Deselecting a namespace stops its collection after rollout,
while its stored logs follow Loki retention. At least one namespace is required,
and a collection can include up to 30 namespaces.

The shipped deployments now use PVCs for Loki and Alloy. The Fedora lab's
persistent storage migration and restart verification are described in the
[monitoring guide](monitoring.md). Retention and durable storage should be
configured together.
