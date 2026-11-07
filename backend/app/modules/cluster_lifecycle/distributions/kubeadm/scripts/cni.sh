# Install the pod network (CNI) from a pinned upstream manifest. Idempotent (server-side apply).
# Env: CNI (calico|flannel), MANIFEST_URL, POD_CIDR

export KUBECONFIG=/etc/kubernetes/admin.conf
manifest="$(mktemp)"
trap 'rm -f "$manifest"' EXIT

log "Downloading ${CNI} manifest: ${MANIFEST_URL}"
curl -fsSL --retry 3 "$MANIFEST_URL" -o "$manifest"

case "$CNI" in
  calico)
    # Set the IP pool to the cluster's pod CIDR.
    sed -i \
      -e 's|^\([[:space:]]*\)# - name: CALICO_IPV4POOL_CIDR|\1- name: CALICO_IPV4POOL_CIDR|' \
      -e "s|^\([[:space:]]*\)#   value: \"192.168.0.0/16\"|\1  value: \"${POD_CIDR}\"|" \
      "$manifest"
    grep -q "value: \"${POD_CIDR}\"" "$manifest" || fail "could not set CALICO_IPV4POOL_CIDR in the manifest"
    workload="daemonset/calico-node"; namespace=kube-system
    ;;
  flannel)
    sed -i "s|\"Network\": \"10.244.0.0/16\"|\"Network\": \"${POD_CIDR}\"|" "$manifest"
    grep -q "\"Network\": \"${POD_CIDR}\"" "$manifest" || fail "could not set the flannel network in the manifest"
    workload="daemonset/kube-flannel-ds"; namespace=kube-flannel
    ;;
  *) fail "unsupported CNI: ${CNI}" ;;
esac

diagnose() {
  log "Diagnostics: pods that are not running"
  kubectl get pods -A --no-headers 2>/dev/null | awk '$4 != "Running" && $4 != "Completed"' | head -20 || true
  log "Diagnostics: recent warning events"
  kubectl get events -A --field-selector type=Warning --sort-by=.lastTimestamp 2>/dev/null | tail -10 || true
  for f in $(ls -t /var/log/pods/kube-system_kube-proxy*/kube-proxy/*.log 2>/dev/null | head -1); do
    log "Diagnostics: kube-proxy errors"
    grep -E " E[0-9]{4} " "$f" | tail -3 || true
  done
}

log "Applying ${CNI}"
kubectl apply --server-side --force-conflicts -f "$manifest" >/dev/null
log "Waiting for ${CNI} to roll out"
if ! kubectl -n "$namespace" rollout status "$workload" --timeout=600s; then
  diagnose
  fail "${CNI} did not become ready"
fi
log "${CNI} is running"
