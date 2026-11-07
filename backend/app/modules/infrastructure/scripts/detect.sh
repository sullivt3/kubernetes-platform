#!/usr/bin/env bash
# Machine inspection. Read-only; runs WITHOUT sudo.
# Output: one KEY=VALUE per line (values never contain newlines). Parsed by facts.py.
set -u
kv() { printf '%s=%s\n' "$1" "$(printf '%s' "${2:-}" | tr '\n' ' ' | sed 's/[[:space:]]*$//')"; }

if [ -r /etc/os-release ]; then
  # shellcheck disable=SC1091
  . /etc/os-release
fi
kv os_id "${ID:-unknown}"
kv os_id_like "${ID_LIKE:-}"
kv os_version_id "${VERSION_ID:-}"
kv os_pretty_name "${PRETTY_NAME:-}"
kv hostname "$(hostname 2>/dev/null)"
kv kernel "$(uname -r)"
kv arch "$(uname -m)"
kv cpu_cores "$(nproc 2>/dev/null || getconf _NPROCESSORS_ONLN)"
kv memory_mb "$(awk '/^MemTotal:/ {printf "%d", $2/1024}' /proc/meminfo)"
kv swap_mb "$(awk '/^SwapTotal:/ {printf "%d", $2/1024}' /proc/meminfo)"

primary_ip=""
if command -v ip >/dev/null 2>&1; then
  primary_ip="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i=1;i<=NF;i++) if ($i=="src") {print $(i+1); exit}}')"
  kv all_ipv4 "$(ip -4 -o addr show scope global 2>/dev/null | awk '{split($4,a,"/"); printf "%s ", a[1]}')"
fi
kv primary_ip "$primary_ip"

if [ -d /run/systemd/system ]; then kv systemd yes; else kv systemd no; fi
if command -v apt-get >/dev/null 2>&1; then pm=apt
elif command -v dnf >/dev/null 2>&1; then pm=dnf
elif command -v yum >/dev/null 2>&1; then pm=yum
else pm=none; fi
kv package_manager "$pm"

kv root_disk_gb "$(df -BG --output=size / 2>/dev/null | tail -1 | tr -dc '0-9')"
kv root_disk_free_gb "$(df -BG --output=avail / 2>/dev/null | tail -1 | tr -dc '0-9')"
if [ -f /sys/fs/cgroup/cgroup.controllers ]; then kv cgroup_version 2; else kv cgroup_version 1; fi
kv virtualization "$(systemd-detect-virt 2>/dev/null || echo unknown)"
kv selinux "$(getenforce 2>/dev/null || echo absent)"
kv nameservers "$(awk '/^nameserver/ {printf "%s ", $2}' /etc/resolv.conf 2>/dev/null)"
kv systemd_resolved "$(systemctl is-active systemd-resolved 2>/dev/null || echo inactive)"

kv kubeadm_version "$(kubeadm version -o short 2>/dev/null || true)"
kv kubelet_version "$(kubelet --version 2>/dev/null | awk '{print $2}' || true)"
kv kubelet_active "$(systemctl is-active kubelet 2>/dev/null || true)"
kv containerd_version "$(containerd --version 2>/dev/null | awk '{print $3}' || true)"
[ -e /etc/kubernetes/admin.conf ] && kv has_admin_conf yes || kv has_admin_conf no
[ -e /etc/kubernetes/kubelet.conf ] && kv has_kubelet_conf yes || kv has_kubelet_conf no
kv internet "$(curl -fsS -o /dev/null -m 5 -w '%{http_code}' https://pkgs.k8s.io/ 2>/dev/null || echo unreachable)"
