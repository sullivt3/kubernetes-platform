# Initialize the first control-plane node. Idempotent: skipped when already initialized.
# Env: CONFIG_B64 (kubeadm config, base64), UPLOAD_CERTS (1|0)

export KUBECONFIG=/etc/kubernetes/admin.conf
mkdir -p "$KPLATFORM_DIR" && chmod 700 "$KPLATFORM_DIR"
config="$KPLATFORM_DIR/kubeadm-init.yaml"
trap 'rm -f "$config"' EXIT   # the config contains a bootstrap token

if [ -f /etc/kubernetes/admin.conf ]; then
  log "Control plane already initialized on this node; skipping kubeadm init"
else
  printf '%s' "$CONFIG_B64" | base64 -d > "$config"
  chmod 600 "$config"
  log "Pulling control-plane images"
  kubeadm config images pull --config "$config"
  log "Running kubeadm init"
  extra=()
  [ "${UPLOAD_CERTS:-0}" = "1" ] && extra+=(--upload-certs)
  kubeadm init --config "$config" "${extra[@]}"
fi

log "Waiting for the API server to become ready"
for _ in $(seq 1 90); do
  kubectl get --raw=/readyz >/dev/null 2>&1 && break
  sleep 2
done
kubectl get --raw=/readyz >/dev/null 2>&1 || fail "API server did not become ready"

# Discovery hash for joins (public information, not a secret).
hash="$(openssl x509 -pubkey -in /etc/kubernetes/pki/ca.crt \
  | openssl pkey -pubin -outform der 2>/dev/null \
  | openssl dgst -sha256 -hex | sed 's/^.* //')"
echo "CA_CERT_HASH=sha256:${hash}"
