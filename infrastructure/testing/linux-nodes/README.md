# Disposable Linux test nodes

Two Ubuntu 24.04 containers with systemd, OpenSSH and a sudo user. They let you exercise machine onboarding and
real Kubernetes installation end to end on a workstation, without spare servers. The platform treats them exactly
like physical servers reached over SSH.

> These run **privileged** (like Kind nodes) and are for development and testing only.

## Start

```bash
# A throwaway key pair (keep it outside the repository)
ssh-keygen -t ed25519 -N "" -C kplatform-test -f ~/.ssh/kplatform_test

AUTHORIZED_KEY="$(cat ~/.ssh/kplatform_test.pub)" \
  docker compose -f infrastructure/testing/linux-nodes/docker-compose.yml up -d --build
```

| Node | SSH from the platform | Cluster-internal address |
|---|---|---|
| node-1 | `localhost:2221` | detected by inspection (docker network) |
| node-2 | `localhost:2222` | detected by inspection |

User: `platform` (passwordless sudo, key authentication only).

## Create a cluster on them

1. **Security → Credentials:** add an *SSH private key* with the contents of `~/.ssh/kplatform_test`. Add metadata
   `username = platform`.
2. **Infrastructure → Machines:** register `node-1` (host `localhost`, port `2221`) and `node-2` (port `2222`).
   Both should become **Ready**.
3. **Kubernetes → Create cluster:** SSH / kubeadm, pick a version, `node-1` as control plane and `node-2` as a
   worker. Under *Storage & advanced* set all three of these:
   * **Leave swap enabled.** Docker Desktop's VM has swap, which a container cannot turn off.
   * **Don't let kube-proxy change conntrack limits.** `/proc/sys` is read-only inside containers.
   * **Kubelet resolv.conf** = `/etc/kplatform-test/resolv.conf`. The container's own resolver is Docker's
     loopback DNS, which would make CoreDNS forward to itself. Inspection flags this as a DNS warning.

   Real servers normally need none of them.

## Reset

```bash
docker compose -f infrastructure/testing/linux-nodes/docker-compose.yml down -v   # -v wipes the nodes' /var
```

Remove the machines from the platform inventory afterwards, or delete the cluster first. Recreated containers get
new SSH host keys, so the platform refuses to connect until you choose *Accept new host key*. This is intentional.
