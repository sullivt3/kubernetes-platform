# Install or update the kube-vip static pod. Idempotent: unchanged manifests are left alone
# (rewriting would restart the pod). The file is written next to the manifests directory
# and moved into place so kubelet never reads a partial manifest.
# Env: MANIFEST_B64, VIP (optional: once the API server runs, wait until it answers through
#      the virtual IP, so the next step does not race a kube-vip restart)

crictl_() { crictl --runtime-endpoint "$CRI_SOCKET" "$@" 2>/dev/null; }

mkdir -p /etc/kubernetes/manifests
tmp="$(mktemp /etc/kubernetes/.kube-vip.XXXXXX)"
trap 'rm -f "$tmp"' EXIT
printf '%s' "$MANIFEST_B64" | base64 -d > "$tmp"
chmod 600 "$tmp"
target=/etc/kubernetes/manifests/kube-vip.yaml
if [ -f "$target" ] && cmp -s "$tmp" "$target"; then
  log "kube-vip manifest unchanged"
else
  previous="$(crictl_ ps -q --name kube-vip --state running | head -n1 || true)"
  mv "$tmp" "$target"
  log "kube-vip manifest installed"
  if [ -n "${VIP:-}" ] && [ -n "$previous" ]; then
    # kubelet re-reads static pod manifests periodically (up to ~20s); while kube-vip restarts
    # the virtual IP briefly disappears.
    log "Waiting for kubelet to restart kube-vip with the new manifest"
    for _ in $(seq 1 60); do
      current="$(crictl_ ps -q --name kube-vip --state running | head -n1 || true)"
      [ -n "$current" ] && [ "$current" != "$previous" ] && break
      sleep 2
    done
  fi
fi

if [ -n "${VIP:-}" ]; then
  log "Waiting for the API server to answer on ${VIP}:6443"
  for _ in $(seq 1 90); do
    # Certificate-verified request: the VIP is in the API server certificate.
    if kubectl --kubeconfig /etc/kubernetes/admin.conf --server "https://${VIP}:6443" get --raw=/readyz >/dev/null 2>&1; then
      log "API server reachable through ${VIP}"
      exit 0
    fi
    sleep 2
  done
  fail "the API server is not reachable through the kube-vip address ${VIP}"
fi
