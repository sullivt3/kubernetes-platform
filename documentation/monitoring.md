# Prometheus monitoring

Open **Operations → Monitoring** and select a connected cluster. Choose
**Connect Prometheus** and enter the namespace, Kubernetes Service name, and
HTTP Service port. The port can be a name such as `http` or a number such as
`9090`. A viewer can read the selected cluster's history. An operator can
change or remove its connection.

Apply database migration `0007` before restarting the updated API, worker and
scheduler. The Prometheus Service must be reachable through the Kubernetes API
server's Service proxy. The platform uses the registered cluster credentials and
their TLS trust settings. Those credentials require `get` on `services/proxy`
for the chosen Service. Configure a Service in the same cluster that serves
the Prometheus HTTP API at its root. [Prometheus HTTP API](https://prometheus.io/docs/prometheus/latest/querying/api/)

## Data shown

| Chart | Required Prometheus data |
|---|---|
| Node CPU | node exporter `node_cpu_seconds_total` with idle CPU counters |
| Node memory | node exporter `node_memory_MemAvailable_bytes` and `node_memory_MemTotal_bytes` |
| Pod CPU | kubelet cAdvisor `container_cpu_usage_seconds_total` |
| Pod memory | kubelet cAdvisor `container_memory_working_set_bytes` |
| Node disk usage and I/O | root cgroup cAdvisor filesystem usage, capacity, read and write counters |
| Node network receive/transmit | root cgroup cAdvisor network byte counters, excluding loopback |

Set a `node` label on node exporter targets to show node names. The chart uses
the target's `instance` label otherwise. Pod charts group by `namespace` and
`pod` and support a namespace filter. Select a 1, 6, or 24 hour time range.
The platform runs fixed range queries with eight visible series per chart.
Hover over a plot to inspect the evaluation time. Missing samples produce
gaps. Empty charts indicate absent samples for the corresponding metric.

Use TLS for kubelet scraping and authorize the collector for `nodes/metrics`.
Prometheus owns the time-series storage and retention. The platform reads the
samples on demand. [Kubelet authorization](https://kubernetes.io/docs/reference/access-authn-authz/kubelet-authn-authz/)

The local Fedora validation stack has a dedicated `kplatform-monitoring`
namespace and an internal `prometheus:http` Service. It uses node exporter on
both nodes and TLS-verified kubelet scrapes. The lab retains up to 24 hours of history on its persistent volume.

## Grafana dashboards

The repository includes Kubernetes provisioning for **Node metrics** and
**Pod metrics** dashboards under `infrastructure/monitoring/grafana`. Both
use the same CPU and memory queries as the app. The pod dashboard has a
namespace selector.

1. Set the Prometheus URL in
   `infrastructure/monitoring/grafana/prometheus.yaml` if your Service differs
   from `prometheus:9090`. The provided manifests target the existing
   `kplatform-monitoring` namespace.
2. Create a Secret named `grafana-admin` with the key `password` in that
   namespace. Supply the initial password from a protected file or your
   deployment's secret manager. Apply the manifests with
   `kubectl apply -k infrastructure/monitoring/grafana`.
3. Set Grafana's `GF_SERVER_ROOT_URL` to its browser-facing base URL. Use your
   deployment's TLS endpoint and identity provider for shared access. For
   local testing, `kubectl -n kplatform-monitoring port-forward svc/grafana
   3300:3000` provides loopback access. The Fedora test Deployment uses
   `http://localhost:3300/` as its root URL.
4. In **Operations → Monitoring**, choose **Edit connection** and enter the
   Grafana base URL. The platform accepts HTTPS and loopback HTTP. The two
   links carry over the selected time range, and the pod link carries the
   applied namespace filter. Viewers can open the links using their Grafana
   sign-in. Grafana controls its own dashboard and data source permissions.

The supplied Grafana Deployment stores its local database on a PVC. Provisioned dashboards remain sourced from the mounted ConfigMaps.
[Grafana provisioning](https://grafana.com/docs/grafana/latest/administration/provisioning/),
[Prometheus data source](https://grafana.com/docs/grafana/latest/datasources/prometheus/)

## Disk, network and operational checks

Monitoring also shows host filesystem usage, combined disk read/write throughput,
and receive/transmit throughput. These use the root cgroup metrics already
collected from kubelet cAdvisor. The network totals sum non-loopback interface
counters, which can include bridges and virtual interfaces. They represent
interface traffic and can count a packet on multiple interfaces.

The provisioned node Grafana dashboard includes the same four charts.
The **Alerts** page runs current node readiness, node pressure, container startup and failed termination,
deployment progress, API readiness, and recent storage/network event checks.
It also reads Prometheus CPU, memory, filesystem and network-error measurements,
and verifies the API server serving certificate against the registered CA.
Certificates within 30 days of expiry produce a finding, with critical severity
within seven days. Each finding provides relevant investigation steps.

The page shows per-source coverage. Missing metrics or failed reads remain visible.
Prometheus rules in `infrastructure/monitoring/prometheus/alerts.yaml` provide
sustained thresholds for CPU over 85%, memory over 90%, filesystem usage over 85%,
network errors and unavailable scrape targets. Add the file to the Prometheus
configuration directory and list it under `rule_files`. Resource thresholds
require five minutes. Scrape availability requires two minutes. The lab has
these rules installed. The app displays their pending, firing and inactive states.
These in-app checks and rule states refresh while the page is open.
Prometheus evaluates its rules independently of the browser. External notification
delivery is not configured by this integration.

## Automated remediation

On **Operations → Alerts**, administrators can configure **Automated remediation**.
Detection is disabled until an organization or platform administrator selects
eligible namespaces and enables it. Operators and viewers can inspect observations
and operation links; they cannot configure, approve, dismiss or retry remediation.
Apply migration `0009` and restart the API, worker and scheduler.

The scheduler scans once per minute. Its first runbook identifies Deployment pods
with at least three restarts that are in CrashLoopBackOff or a failed termination
state. A continuous observation window (default five minutes, minimum one minute)
prevents immediate action on transient startup failures. A gap of over three
minutes restarts that observation window. Detection creates a durable proposal;
it never changes Kubernetes resources.

An administrator reviews the pod, Deployment, failure and disruption impact, then
types the cluster name to approve one attempt. Approval expires fifteen minutes
after proposal creation. The worker rechecks policy, failure, pod UID and workload
templates, evicts through the Kubernetes Eviction API, then waits up to three
minutes for a Ready replacement and available Deployment replicas. PodDisruptionBudgets
are enforced; no force deletion is used. An already recovered pod is skipped.
Workload drift blocks execution. A lost acknowledgement cannot evict a replacement
with a different UID. Each Deployment has a cooldown (default thirty minutes).
Failure leaves a failed tracked operation and investigation guidance; it does not
trigger repeated unattended replacements. Retrying a failed operation requires an
administrator, and an unexecuted eviction still requires an unexpired approval.

System/monitoring namespaces, standalone pods, Jobs, StatefulSets, static or critical
pods, host-access/privileged pods and pods with local or persistent data volumes are
excluded. This runbook can recover a transient pod problem; it does not repair an
invalid image, application configuration or underlying host failure. Other findings
continue to provide investigation guidance. The workflow retains normal audit,
target-exclusion, cancellation, checkpoint and recovery behavior.

Saving policy changes revokes outstanding observations/proposals and unexecuted
approvals. A scan can begin observing still-failing, never-approved pods again.
Dismissed pods are not automatically proposed again. The page shows the latest
100 observations; API source failures are shown in the scan status.

External notification delivery and host-loss recovery of the observability stack
are explicitly scheduled towards the end of Phase 8. The latter must include a
replacement-host/storage recovery drill; pod restart persistence is separate.

## Persistent storage

The Grafana, Loki and Alloy manifests now reference PVCs. Select a suitable
StorageClass or bind their claims to existing volumes before applying them.
The Prometheus claim and its deployment volume patch are in
`infrastructure/monitoring/prometheus`.

For an existing temporary deployment, preserve data before replacing its volume.
The Fedora lab was migrated at a quiescent point, retaining each application's
data directory, then switching its Deployment to its PVC. Verification replaced
the pods and compared historical metric samples, old log entries, and Grafana
preferences.

The lab uses retained local PVs on `tsnode1` under
`/var/lib/kplatform-observability`, with matching PVCs named
`kp-prometheus-data`, `kp-loki-data`, `kp-grafana-data`, and `kp-alloy-data`.
These survive pod replacement and remain tied to that node. Host loss requires
volume recovery from a separate backup or replacement storage. Their storage
requests describe expected capacity. Node-local directories rely on available
host disk space. Prometheus's configured retention size and Loki's retention
period still apply.
