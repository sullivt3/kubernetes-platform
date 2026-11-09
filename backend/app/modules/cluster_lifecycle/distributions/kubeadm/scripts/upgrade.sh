# Upgrade the control-plane stack on the first control plane, or reconcile local kubeadm
# configuration on additional control-plane and worker nodes.
[ -n "${TARGET_VERSION:-}" ] || fail "TARGET_VERSION is required"
case "${APPLY_UPGRADE:-0}" in
  1)
    log "Applying Kubernetes v${TARGET_VERSION} control-plane upgrade"
    kubeadm upgrade apply --yes "v${TARGET_VERSION}"
    ;;
  0)
    log "Updating local kubeadm configuration for v${TARGET_VERSION}"
    kubeadm upgrade node
    ;;
  *) fail "APPLY_UPGRADE must be 0 or 1" ;;
esac
