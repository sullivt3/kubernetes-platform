# Install and configure containerd (from Docker's package repository). Idempotent.

if [ "$OS_FAMILY" = debian ]; then
  if [ "$ID" = ubuntu ] || [ "$ID" = debian ]; then
    repo_os="$ID"; codename="${VERSION_CODENAME:-}"
  elif [[ " ${ID_LIKE:-} " == *" ubuntu "* ]]; then
    repo_os=ubuntu; codename="${UBUNTU_CODENAME:-}"
  else
    repo_os=debian; codename="${VERSION_CODENAME:-}"
  fi
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
  log "Configuring Docker package repository (containerd.io)"
  curl -fsSL https://download.docker.com/linux/centos/docker-ce.repo -o /etc/yum.repos.d/docker-ce.repo
  dnf_install containerd.io
fi

log "Writing /etc/containerd/config.toml (systemd cgroup driver)"
mkdir -p /etc/containerd
if [ -f /etc/containerd/config.toml ] && ! grep -q "managed by kubernetes-platform" /etc/containerd/config.toml; then
  cp /etc/containerd/config.toml "/etc/containerd/config.toml.before-kplatform"
fi
{
  echo "# managed by kubernetes-platform"
  containerd config default | sed -e 's/SystemdCgroup = false/SystemdCgroup = true/'
} > /etc/containerd/config.toml.new
grep -q "SystemdCgroup = true" /etc/containerd/config.toml.new || fail "could not enable the systemd cgroup driver in containerd config"
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
