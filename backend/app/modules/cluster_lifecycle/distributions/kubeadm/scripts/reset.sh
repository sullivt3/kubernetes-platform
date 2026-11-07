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
log "Node reset. Note: iptables/IPVS rules created by kube-proxy and the CNI were left in place."
