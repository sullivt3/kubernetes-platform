# Install and configure containerd (from Docker's package repository). Idempotent.

repo_os="$(docker_repo_os)"
if [ "$OS_FAMILY" = debian ]; then
  codename="${VERSION_CODENAME:-}"
  [ -n "$codename" ] || fail "cannot determine distribution codename"
  install -m 0755 -d /etc/apt/keyrings
  log "Configuring Docker package repository (containerd.io) for $repo_os $codename"
  curl -fsSL "https://download.docker.com/linux/${repo_os}/gpg" -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/${repo_os} ${codename} stable" \
    > /etc/apt/sources.list.d/docker.list
  apt_update
  apt_install containerd.io
else
  log "Configuring Docker package repository (containerd.io) for $repo_os"
  curl -fsSL "https://download.docker.com/linux/${repo_os}/docker-ce.repo" -o /etc/yum.repos.d/docker-ce.repo
  dnf_install containerd.io
fi

log "Writing /etc/containerd/config.toml (systemd cgroup driver)"
mkdir -p /etc/containerd
if [ -f /etc/containerd/config.toml ] && ! grep -q "managed by kubernetes-platform" /etc/containerd/config.toml; then
  cp /etc/containerd/config.toml "/etc/containerd/config.toml.before-kplatform"
fi
{
  echo "# managed by kubernetes-platform"
  containerd config default | sed \
    -e 's/SystemdCgroup = false/SystemdCgroup = true/' \
    -e 's/image_pull_with_sync_fs = false/image_pull_with_sync_fs = true/' \
    -e 's/use_local_image_pull = false/use_local_image_pull = true/'
} > /etc/containerd/config.toml.new
grep -q "SystemdCgroup = true" /etc/containerd/config.toml.new || fail "could not enable the systemd cgroup driver in containerd config"
# Sync extracted image files before publishing them as usable. In containerd 2.1+
# this requires local pull mode; older versions already use the local pull path.
grep -q "image_pull_with_sync_fs = true" /etc/containerd/config.toml.new || fail "containerd does not expose synchronized image unpacking"
mv /etc/containerd/config.toml.new /etc/containerd/config.toml

cat > /etc/crictl.yaml <<EOF
runtime-endpoint: ${CRI_SOCKET}
image-endpoint: ${CRI_SOCKET}
timeout: 10
EOF

systemctl daemon-reload
systemctl enable containerd >/dev/null 2>&1
systemctl restart containerd
for _ in $(seq 1 30); do
  ctr --address /run/containerd/containerd.sock version >/dev/null 2>&1 && break
  sleep 1
done
ctr --address /run/containerd/containerd.sock version >/dev/null 2>&1 || fail "containerd did not become ready"
log "containerd $(containerd --version | awk '{print $3}') is running"
