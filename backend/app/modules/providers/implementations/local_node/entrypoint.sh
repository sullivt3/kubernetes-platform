#!/bin/bash
# Create the access user with the injected public key, then hand PID 1 to systemd.
set -euo pipefail
NODE_USER="${NODE_USER:-platform}"
if ! id "$NODE_USER" >/dev/null 2>&1; then
  useradd --create-home --shell /bin/bash "$NODE_USER"
fi
echo "$NODE_USER ALL=(ALL) NOPASSWD:ALL" > "/etc/sudoers.d/$NODE_USER"
chmod 440 "/etc/sudoers.d/$NODE_USER"
home="$(getent passwd "$NODE_USER" | cut -d: -f6)"
mkdir -p "$home/.ssh"
if [[ -n "${AUTHORIZED_KEY:-}" ]]; then
  printf '%s\n' "$AUTHORIZED_KEY" > "$home/.ssh/authorized_keys"
fi
chmod 700 "$home/.ssh"; chmod 600 "$home/.ssh/authorized_keys" 2>/dev/null || true
chown -R "$NODE_USER:$NODE_USER" "$home/.ssh"
# Host keys are generated once per node and kept across restarts (/etc is in the
# container layer), so the platform's pinned host key stays valid.
if ! ls /etc/ssh/ssh_host_*_key >/dev/null 2>&1; then
  ssh-keygen -A >/dev/null
fi
# Pod mounts with Bidirectional propagation (e.g. Calico) need shared mounts, as on a real host.
mount --make-rshared /
# Docker's embedded DNS (127.0.0.11) is unusable from pods (CoreDNS would loop), so
# kubelet gets upstream resolvers from this file (kubelet_resolv_conf).
mkdir -p /etc/kplatform-node
printf 'nameserver 1.1.1.1\nnameserver 8.8.8.8\n' > /etc/kplatform-node/resolv.conf
exec /lib/systemd/systemd
