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
