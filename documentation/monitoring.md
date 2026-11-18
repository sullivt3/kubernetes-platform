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
both nodes and TLS-verified kubelet scrapes. The test Deployment keeps up to
24 hours of history in an `emptyDir`, which is discarded when its pod is
replaced. Use persistent Prometheus storage when retention must survive pod
replacement.

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

The supplied Grafana Deployment stores its local database in an `emptyDir`.
Replacing the pod recreates the provisioned dashboards and initial
administrator. Use persistent Grafana storage or an external database for
accounts, settings and sessions that need to survive replacement.
[Grafana provisioning](https://grafana.com/docs/grafana/latest/administration/provisioning/),
[Prometheus data source](https://grafana.com/docs/grafana/latest/datasources/prometheus/)
