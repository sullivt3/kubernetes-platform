#!/bin/bash
# Install the authorized key from $AUTHORIZED_KEY, then hand PID 1 to systemd.
set -euo pipefail
if [[ -n "${AUTHORIZED_KEY:-}" ]]; then
  printf '%s\n' "$AUTHORIZED_KEY" > /home/platform/.ssh/authorized_keys
  chmod 600 /home/platform/.ssh/authorized_keys
  chown -R platform:platform /home/platform/.ssh
fi
# Fresh host keys per container (never shared between nodes).
rm -f /etc/ssh/ssh_host_*
ssh-keygen -A >/dev/null
# Pod mounts with Bidirectional propagation (e.g. Calico /sys/fs/bpf) need shared mounts,
# as on a real host. Kind nodes do the same.
mount --make-rshared /
# The container's resolv.conf points at Docker's embedded DNS (127.0.0.11), which pods
# cannot use (CoreDNS would loop). Provide upstream resolvers for kubelet's resolvConf.
mkdir -p /etc/kplatform-test
printf 'nameserver 1.1.1.1\nnameserver 8.8.8.8\n' > /etc/kplatform-test/resolv.conf
exec /lib/systemd/systemd
