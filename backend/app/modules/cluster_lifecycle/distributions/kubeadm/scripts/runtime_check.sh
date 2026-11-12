# Verify a real CRI sandbox before kubeadm uses the runtime. No CNI is needed:
# network=NODE is host networking, as used by control-plane and CNI system pods.
probe_directory="$(mktemp -d)"
sandbox_id=""
cleanup_probe() {
  if [[ "$sandbox_id" =~ ^[a-f0-9]{64}$ ]]; then
    crictl --runtime-endpoint "$CRI_SOCKET" --timeout=10s stopp "$sandbox_id" >/dev/null 2>&1 || true
    crictl --runtime-endpoint "$CRI_SOCKET" --timeout=10s rmp "$sandbox_id" >/dev/null 2>&1 || true
  fi
  rm -rf -- "$probe_directory"
}
trap cleanup_probe EXIT
cat > "$probe_directory/sandbox.json" <<EOF
{
  "metadata": {
    "name": "kplatform-runtime-check-$$",
    "namespace": "kplatform-diagnostics",
    "uid": "kplatform-$$-${RANDOM}",
    "attempt": 0
  },
  "log_directory": "$probe_directory",
  "linux": {
    "cgroup_parent": "system.slice",
    "security_context": {"namespace_options": {"network": 2}}
  }
}
EOF
log "Verifying containerd can start and keep a real pod sandbox running"
if ! sandbox_id="$(crictl --runtime-endpoint "$CRI_SOCKET" --timeout=90s runp --cancel-timeout=90s "$probe_directory/sandbox.json" 2> "$probe_directory/error")"; then
  tail -5 "$probe_directory/error" >&2
  fail "container runtime could not create a pod sandbox; check the pause image and containerd logs before retrying"
fi
[[ "$sandbox_id" =~ ^[a-f0-9]{64}$ ]] || fail "runtime returned an invalid sandbox ID"
for _ in 1 2 3; do
  sleep 1
  if ! crictl --runtime-endpoint "$CRI_SOCKET" --timeout=10s inspectp "$sandbox_id" > "$probe_directory/status" 2> "$probe_directory/error"; then
    fail "container runtime could not inspect its test sandbox"
  fi
  if ! grep -Eq '"state"[[:space:]]*:[[:space:]]*"SANDBOX_READY"' "$probe_directory/status"; then
    fail "pod sandbox exited immediately; the cached pause image or runtime may be damaged (including empty executables); repair the runtime image cache before retrying"
  fi
done
log "Container runtime sandbox verified (startup, liveness and host networking)"
