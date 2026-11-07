# Remove Kubernetes state from this node (kubeadm reset). Packages stay installed.
# Firewall/iptables rules are NOT flushed: the host may have its own rules.

if command -v kubeadm >/dev/null 2>&1; then
  log "Running kubeadm reset"
  kubeadm reset -f --cri-socket "$CRI_SOCKET" 2>&1 | grep -v '^W' || true
else
  log "kubeadm is not installed; nothing to reset"
fi
rm -rf /etc/cni/net.d /var/lib/etcd /var/lib/cni "$KPLATFORM_DIR" /root/.kube
if command -v ip >/dev/null 2>&1; then
  for link in cni0 flannel.1 vxlan.calico tunl0; do
    ip link show "$link" >/dev/null 2>&1 && ip link delete "$link" 2>/dev/null || true
  done
fi
if systemctl is-enabled --quiet kubelet 2>/dev/null; then systemctl stop kubelet || true; fi
# A control plane that held the kube-vip address keeps it on its interface after kube-vip
# stops; release it so the address can move to a remaining control plane.
if [ -n "${VIP:-}" ] && command -v ip >/dev/null 2>&1; then
  ip -o -4 addr show | awk -v want="${VIP}/32" '$4 == want {print $2}' | while read -r dev; do
    ip addr del "${VIP}/32" dev "$dev" 2>/dev/null && log "Released kube-vip address ${VIP} from ${dev}"
  done
fi
log "Node reset. Note: iptables/IPVS rules created by kube-proxy and the CNI were left in place."
