# Join this node to the cluster. Idempotent: skipped when the node already joined.
# Env: CONFIG_B64 (kubeadm JoinConfiguration, base64)

if [ -f /etc/kubernetes/kubelet.conf ]; then
  log "Node already joined a cluster; skipping kubeadm join"
  exit 0
fi
mkdir -p "$KPLATFORM_DIR" && chmod 700 "$KPLATFORM_DIR"
config="$KPLATFORM_DIR/kubeadm-join.yaml"
trap 'rm -f "$config"' EXIT   # contains a bootstrap token
printf '%s' "$CONFIG_B64" | base64 -d > "$config"
chmod 600 "$config"
log "Running kubeadm join"
kubeadm join --config "$config"
log "Node joined"
