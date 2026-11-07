# Prepare the operating system for Kubernetes. Idempotent.
# Env: DISABLE_SWAP (1|0), NODE_ROLE (control_plane|worker), CNI (calico|flannel)

log "Loading kernel modules (overlay, br_netfilter)"
cat > /etc/modules-load.d/kplatform-k8s.conf <<'EOF'
overlay
br_netfilter
EOF
for module in overlay br_netfilter; do
  modprobe "$module" 2>/dev/null || log "note: could not modprobe $module (built into the kernel or already loaded)"
done
[ -e /proc/sys/net/bridge/bridge-nf-call-iptables ] || fail "br_netfilter is not available on this kernel"

log "Applying networking sysctls"
cat > /etc/sysctl.d/99-kplatform-k8s.conf <<'EOF'
net.bridge.bridge-nf-call-iptables  = 1
net.bridge.bridge-nf-call-ip6tables = 1
net.ipv4.ip_forward                 = 1
EOF
sysctl -q -p /etc/sysctl.d/99-kplatform-k8s.conf
[ "$(cat /proc/sys/net/ipv4/ip_forward)" = "1" ] || fail "could not enable IPv4 forwarding"

if [ "${DISABLE_SWAP:-1}" = "1" ]; then
  if [ -n "$(swapon --show --noheadings 2>/dev/null)" ]; then
    log "Disabling swap (required by kubelet)"
    swapoff -a
  fi
  sed -ri '/^[^#].*[[:space:]]swap[[:space:]]/ s/^/# disabled by kubernetes-platform: /' /etc/fstab
  for unit in $(systemctl list-units --type swap --plain --no-legend 2>/dev/null | awk '{print $1}'); do
    systemctl mask --now "$unit" >/dev/null 2>&1 || true
  done
else
  log "Leaving swap enabled (kubelet configured with failSwapOn=false)"
fi

log "Installing base packages"
if [ "$OS_FAMILY" = debian ]; then
  apt_update
  apt_install ca-certificates curl gnupg openssl conntrack socat iptables ethtool
else
  dnf_install ca-certificates curl gnupg2 openssl conntrack-tools socat iptables ethtool tar
fi

if command -v getenforce >/dev/null 2>&1 && [ "$(getenforce)" = "Enforcing" ]; then
  log "Setting SELinux to permissive (required by kubeadm)"
  setenforce 0
  sed -ri 's/^SELINUX=enforcing/SELINUX=permissive/' /etc/selinux/config
fi

# Open required ports when a host firewall is active.
ports="10250/tcp"
if [ "${NODE_ROLE:-worker}" = "control_plane" ]; then
  ports="$ports 6443/tcp 2379-2380/tcp 10257/tcp 10259/tcp"
else
  ports="$ports 30000-32767/tcp"
fi
case "${CNI:-}" in
  calico)  ports="$ports 179/tcp 4789/udp 5473/tcp" ;;
  flannel) ports="$ports 8472/udp" ;;
esac
if systemctl is-active --quiet firewalld 2>/dev/null; then
  log "Opening Kubernetes ports in firewalld: $ports"
  for p in $ports; do firewall-cmd -q --permanent --add-port="$p"; done
  firewall-cmd -q --reload
elif command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
  log "Opening Kubernetes ports in ufw: $ports"
  for p in $ports; do ufw allow "${p/-/:}" >/dev/null; done
fi

log "Operating system prepared"
