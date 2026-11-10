# Cluster creation and lifecycle integrations

The integrations below execute real operations. New distribution and vSphere paths
are **preview**: automated coverage uses explicit Docker, SSH, Kubernetes and vendor
API simulators; live cloud accounts, vCenter and complete native installations must
be validated in the target environment before production use.

## Supported paths

| Distribution | Infrastructure | Creation and deletion | Scale workers | Upgrade |
|---|---|---|---|---|
| kubeadm | SSH, Local Docker, Proxmox, vSphere, AWS, Azure, GCP | Yes | Machine add/remove | Serial control planes, then workers |
| K3s / RKE2 | SSH, Proxmox, vSphere, AWS, Azure, GCP | Yes | Machine add/remove | Serial servers, then agents |
| Kind | Local Docker | Yes, including multiple control planes | Fixed at creation | Recreate a development cluster |
| k3d | Local Docker | Yes, including multiple servers | Resize existing worker pools | Recreate a development cluster |
| EKS / AKS / GKE | Matching cloud connection | Yes | Resize existing worker pools | Native control-plane and pool updates |

Kind/k3d upgrades are not advertised as supported. Recreating a cluster is a separate,
destructive operation, not an in-place upgrade.

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
