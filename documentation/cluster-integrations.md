# Cluster creation and lifecycle integrations

The integrations below execute real operations. New distribution and vSphere paths
are **preview**: automated coverage uses explicit Docker, SSH, Kubernetes and vendor
API simulators; live cloud accounts, vCenter and complete native installations must
be validated in the target environment before production use.

## Supported paths

SSH machine installation also supports conventional Fedora 43/44/45 hosts.
See [Fedora hosts](fedora.md) for the DNF5, runtime repository, swap and SELinux
behavior and the boundary between package checks and live cluster validation.

| Distribution | Infrastructure | Creation and deletion | Scale workers | Upgrade |
|---|---|---|---|---|
| kubeadm | SSH, Local Docker, Proxmox, vSphere, AWS, Azure, GCP | Yes | Machine add/remove | Serial control planes, then workers |
| K3s / RKE2 | SSH, Proxmox, vSphere, AWS, Azure, GCP | Yes | Machine add/remove | Serial servers, then agents |
| Kind | Local Docker | Yes, including multiple control planes | Fixed at creation | Recreate a development cluster |
| k3d | Local Docker | Yes, including multiple servers | Resize existing worker pools | Recreate a development cluster |
| EKS / AKS / GKE | Matching cloud connection | Yes | Resize existing worker pools | Native control-plane and pool updates |

Kind/k3d upgrades are not advertised as supported. Recreating a cluster is a separate,
destructive operation, not an in-place upgrade.

## Kubeadm installation verification

Installation checks behavior as well as installed packages:

- The package step verifies kubeadm, kubelet and kubectl match the selected version.
  Before initializing or joining, every node must start a real CRI pod sandbox and
  keep it Ready across repeated observations. This uses host networking so it can
  run before a CNI exists, and removes only its own test sandbox.
- Containerd synchronizes extracted image files before publishing them as usable.
  Where required, the installer selects local image pulls to enable this setting.
  This can increase image pull time and does not repair already damaged caches.
- The initial Calico/Flannel step waits for the initial control-plane rollout.
  Workers join afterward, so that step passing does not certify their networking.
- After joining, the readiness step requires both Node Ready and a Ready networking
  pod on every selected node. It reports node names and failing init/container
  reasons. Persistent fatal CNI failures stop the operation after a 120-second
  startup grace; other startup delays remain bounded by `ready_timeout_seconds`.
- Before creation succeeds, a temporary ordinary pod on **each selected node**
  must pull the test image and resolve the Kubernetes API service through cluster
  DNS to its exact expected service address. This catches tunnel, forwarding and
  DNS failures even when all system pods report Ready. Test pods are cleaned up
  after success, failure or cancellation.

A failed runtime check requires examining containerd and the pause image; a failed
DNS check requires examining the affected node's CNI, routes and host firewall.
The installer does not purge image caches or disable host firewalls automatically.
For Calico IP-in-IP, opening TCP/UDP ports alone is insufficient: peers must be
allowed numeric IP protocol `4` (the `ipip` name can resolve to protocol 94), and
forwarded pod-to-pod traffic must be permitted within
its configured pod CIDR. A firewalld zone's input rich rule does not establish a
forwarding policy. Preserve existing host/hypervisor firewall configuration when
correcting it, and verify real pod DNS before treating networking as healthy.

## Automatic firewalld policies (kubeadm)

When firewalld is active, OS preparation reconciles persistent policies before
initialization or joining. No GUI setting is needed. Each cluster owns two policies
identified by its UUID and an ownership description:

- A host-input policy opens the existing Kubernetes/CNI ports appropriate to the
  node's role. Calico additionally accepts **numeric IP protocol 4** only from the
  other nodes' inspected IPv4 addresses.
- A forwarding policy accepts traffic only when **both source and destination**
  belong to the configured pod CIDR. Its default target is `CONTINUE`, so other
  traffic continues through the existing firewall configuration.

Adding a node updates peer allowances on all existing joined hosts as well as the
new host, without repeating OS or runtime installation on existing nodes. Removing
a node revokes its peer allowances on surviving hosts. Successful reset removes
only this cluster's owned policies; unreachable hosts skipped with force require
cleanup when they return. Existing zone rules and older manually applied repair
rules are not adopted or deleted.

The manager verifies policy ownership, serializes its own reloads, saves runtime
zone/policy settings, activates the permanent changes with a normal reload, then
restores existing runtime settings and VM interface bindings. Unrelated temporary
settings are not made permanent. Failed updates restore previous owned policies.
Correct settings are verified without another reload on retry.

Runtime-only zone/policy objects or direct rules that cannot be safely restored
block the step with an explanation before any changes. Review those settings
before retrying. This automation uses the system Python/firewalld client supplied
by the firewalld package. An inactive firewall is not enabled. UFW retains the
existing port preparation; these policy objects apply specifically to firewalld.

Regression tests cover ownership, retry, rollback, VM bindings and node lifecycle.
`bash scripts/check-firewall.sh` also exercises a real Fedora firewalld daemon in
a disposable Docker network namespace with `NET_ADMIN`/`NET_RAW`, including runtime
preservation, idempotence and removal. It does not use host networking or validate
a full VM cluster's packet routing.

## Optional kubectl access for a Linux user (kubeadm)

In **Create cluster → Storage & advanced**, enable **Configure kubectl for a Linux
user** and enter an existing account on the first control-plane machine, including
`root` if desired. The API equivalent is `spec.options.kubectl_username: "alice"`;
omit it to leave user kubeconfigs untouched. This option is currently for kubeadm.

Preflight checks the account and home before installing Kubernetes. After node,
CoreDNS and smoke checks pass, the installer merges the cluster administrator
configuration into that user's `~/.kube/config`. It gives the directory mode 700
and file mode 600 under that user's ownership. Existing configuration is backed
up privately before replacement; retries reuse the same uniquely named context.
Merge/file operations run as the target user, reject symlinked config paths, and
leave an invalid existing config unchanged.

A new config selects this cluster automatically. An existing current context is
preserved; the operation logs show `kubectl config use-context <context>` to select
the new cluster. A failure in this step can be retried without reinstalling nodes.
For SSH provisioned as one account, selecting another account configures only the
selected account; no Linux account is created and no workstation file is changed.

This grants the selected account **cluster administrator** access using the shared
bootstrap administrator identity. It does not issue individual identities or
configure OIDC. Cluster deletion leaves user configs/backups in place; old entries
can be removed with normal kubectl config commands.

## Local clusters

Select Local Docker, a validated connection and Kind or k3d. No SSH credential or
inventory machine is needed. Nodes are created by the distribution and read through
the live Kubernetes API. Connections must use `published_ports`; workers must run
on the Docker host so the loopback API endpoint is reachable. Docker socket access
and privileged containers are required. Resources come from Docker Desktop's host
allocation; the wizard does not pretend to apply VM CPU/disk sizing to these nodes.

Workers automatically cache Kind v0.33.0 and k3d v5.8.3 in
`~/.cache/kubernetes-platform/tools`. Published SHA256 sums are verified before use.
Cluster names include the platform cluster UUID to isolate organizations. CLI
commands receive argument arrays, never shell strings; cancellation terminates the
active CLI process. Partial clusters remain available for retry or explicit deletion.
The default workstation kubeconfig is not modified.

Use an exact patch version with a published Kind node image or K3s image. For k3d,
`options.release_revision` selects the `-k3sN` image suffix. Scaling down k3d deletes
local node containers and their data after confirmation; it does not promise a
production workload migration.

## Native K3s and RKE2

Machines must be SSH reachable, have systemd, passwordless sudo and outbound HTTPS
access to the vendor artifact hosts. Exact Kubernetes patches and release revisions
are required: `1.34.2` with revision `1` resolves to `v1.34.2+k3s1` or
`v1.34.2+rke2r1`; availability is distribution-specific.

OS preparation installs dependencies, configures forwarding and disables swap.
On an enforcing SELinux host, install the distribution's signed SELinux policy first;
the platform does not silently disable enforcement. Native installers verify their
downloaded distribution artifacts. Config files and ownership markers live under
`/etc/rancher/{k3s,rke2}` with restricted permissions. Join tokens travel through SSH
stdin, are redacted and are never written into operation outputs or cluster specs.
Existing unowned installations are refused.

HA uses embedded etcd and an odd control-plane count. Provide an existing external
TCP load balancer: port 6443 for both distributions and port 9345 for RKE2 agent/server
registration. Kube-vip and AWS-managed API load balancers remain kubeadm-specific.
Deleting a native node removes its Kubernetes node membership before uninstalling,
allowing the distribution's etcd controller to remove the member. Destruction of
platform-provisioned VMs is optional and separately confirmed by the cluster action.

K3s supports Flannel, Calico and Cilium; RKE2 uses its bundled Canal, Calico or Cilium
installation. Kind supports its default CNI, Calico and Cilium. The platform's pinned
Cilium 1.20.4 chart path supports Kubernetes 1.33–1.36 and uses a checksum-verified
Helm v3.19.0 executable for local rendering. Existing unowned CNI objects are not
overwritten. Rendering a chart does not add general Helm release management.

## Cloud VMs and managed Kubernetes

The creation wizard sends each pool's OS image and provider options through to the
machine provisioning operation:

| Provider | Required VM inputs |
|---|---|
| AWS | AMI, instance type, subnet and security group IDs |
| Azure | `publisher:offer:sku:version` image, location, resource group, VM size and subnet; one owned NIC is created per VM |
| GCP | Image resource URL, zone, machine type, network and optional subnetwork |
| vSphere | Linux template, compute cluster, datastore and network port group (connection defaults are supported) |

Cloud machine types determine CPU and memory; disk sizes are sent to the provider.
SSH routing, firewalls, usable images, IAM roles, existing networks, quota and vendor
version support are prerequisites. The platform does not automatically create an
entire VPC or grant itself IAM privileges.

Managed worker pools honor machine type overrides, disk size, labels and taints.
EKS Cilium uses AWS VPC CNI chaining; AKS uses Azure CNI with the Cilium dataplane;
GKE uses Dataplane V2. EKS pod addresses come from VPC subnets rather than the wizard
pod CIDR. Managed DNS uses `cluster.local`. GKE regional node counts are **per zone**,
including resize counts. Cloud API permissions for pool updates and upgrades are
required in addition to creation/deletion permissions.

EKS upgrades accept the next minor version (for example `1.35`), since the service
controls control-plane patch releases. AKS/GKE use an exact supported target version.
Both control plane and worker pools are updated. Provider disruption-budget and
version-support rules can reject an operation; the error remains visible for recovery.
Manually resizing an autoscaled AKS pool is refused. No downgrade or minor-version
skipping is supported.

## vSphere

Create a `vsphere_password` credential containing the password. Configure `server`,
`username`, `datacenter` and optional provisioning defaults. TLS verification is on
by default. The SDK dependency is `pyvmomi` (8.0.3 through 9.x).

Templates must have VMware Tools and cloud-init's VMware datasource enabled and
cleaned for cloning. This first implementation supports one disk and one network
adapter; requested disks may grow but cannot shrink. Guestinfo carries base64
metadata and cloud-init user data to create the selected SSH user and authorized key.
IP discovery relies on VMware Tools. Both standard and distributed port groups are
supported. Power/delete/network mutations require the platform ownership marker.
Port-group changes use guest DHCP; static guest network customization is rejected.

## Machine networking and power

The Machines menu exposes provider-supported actions. Network changes require the
machine name as confirmation and create a `machine.network` operation:

- AWS: change security groups within the existing subnet; subnet/IP migration is rejected.
- Azure: change the single NIC's subnet/private IP; a blank IP selects DHCP.
- GCP: update alias CIDR ranges on an existing interface; primary IP/VPC migration is not supported.
- vSphere: move the single adapter to a port group.
- Proxmox: update net0's bridge and cloud-init address/gateway; a guest reboot may be required.

Cloud network/power mutations require ownership tags or labels. Provider APIs can
accept changes asynchronously; logs distinguish acceptance from the observed power
state. Inspect the machine after the provider completes a network change.

## Creation-time storage

The wizard can install the existing pinned NFS CSI add-on as the final creation step,
optionally create a class backed by an existing NFS export, and select a default
StorageClass. Driver installation runs after encrypted kubeconfig registration in
the same tracked cluster operation. Other CSI names are rejected instead of ignored.
No NFS server is provisioned. An existing default is not silently replaced; clear it
explicitly before selecting another class. K3s/k3d omit their bundled local-path
provisioner when an NFS default is selected during creation.

## API additions

- `POST /api/v1/clusters/{id}/pools/{pool}/resize` — `{ "count": 3 }`; `confirm=cluster-name` for scale-down.
- `POST /api/v1/clusters/{id}/upgrade` — `target_version`; managed and native paths use the same endpoint.
- `POST /api/v1/machines/{id}/network` — `network`, `addresses`, provider `options`; `confirm=machine-name` is required.
- Provisioning requests and cluster machine templates accept `image`; templates retain `provider_options`.
- `ClusterSpec.storage` accepts `default_storage_class`, `csi_drivers` (`nfs.csi.k8s.io`), `nfs_server`, and `nfs_share`.

All new mutations keep organization scoping, operation exclusivity and audit events.
