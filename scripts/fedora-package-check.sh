#!/usr/bin/env bash
# Runs only in the disposable Fedora container created by check-fedora.sh.
set -euo pipefail
[ -f /.dockerenv ] && [ -d /installer/kubeadm ] || {
  echo 'Run bash scripts/check-fedora.sh instead; this script changes container packages.' >&2
  exit 1
}
dnf install -y ca-certificates /usr/bin/curl gnupg2 openssl conntrack-tools socat iptables-nft ethtool tar

# The image has no systemd PID 1. Only service control is replaced; DNF,
# repositories, RPM signatures, binaries, config generation and CRI are real.
install -d /usr/local/sbin
cat > /usr/local/sbin/systemctl <<'EOF'
#!/usr/bin/env bash
set -eu
case "$*" in
  'daemon-reload'|'enable containerd'|'enable kubelet') exit 0 ;;
  'restart containerd')
    containerd --config /etc/containerd/config.toml > /tmp/containerd.log 2>&1 &
    ;;
  *) echo "Unexpected service command: $*" >&2; exit 1 ;;
esac
EOF
chmod 0755 /usr/local/sbin/systemctl

run_installer() {
  cat /installer/kubeadm/lib.sh /installer/shared/swap.sh "/installer/kubeadm/$1" | bash
}

run_installer containerd.sh
grep -q 'download.docker.com/linux/fedora' /etc/yum.repos.d/docker-ce.repo
grep -q 'SystemdCgroup = true' /etc/containerd/config.toml
rpm -q containerd.io

export K8S_MINOR=1.36 K8S_VERSION='' MODE=resolve
resolved="$(run_installer packages.sh | sed -n 's/^RESOLVED_VERSION=//p')"
[ -n "$resolved" ]
export K8S_VERSION="$resolved" MODE=install
run_installer packages.sh
crictl info > /tmp/cri-info.json

# Exercise both package-upgrade paths against the next real minor repository.
export K8S_MINOR=1.37 K8S_VERSION='' MODE=resolve
resolved="$(run_installer packages.sh | sed -n 's/^RESOLVED_VERSION=//p')"
[ -n "$resolved" ]
export K8S_VERSION="$resolved" MODE=upgrade-kubeadm
run_installer packages.sh
export MODE=upgrade-kubelet
run_installer packages.sh

kubeadm version -o short
kubelet --version
kubectl version --client
containerd --version
echo 'Fedora package install, package upgrade, containerd configuration and live CRI checks passed.'
echo 'This does not exercise kernel preparation, SELinux, systemd boot, CNI or a running Kubernetes cluster.'
