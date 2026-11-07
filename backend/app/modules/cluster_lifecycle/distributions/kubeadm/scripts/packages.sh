# Configure the pkgs.k8s.io repository and resolve or install kubeadm/kubelet/kubectl.
# Env: K8S_MINOR (e.g. 1.34), K8S_VERSION (e.g. 1.34.2, empty = latest patch), MODE (resolve|install)

[ -n "${K8S_MINOR:-}" ] || fail "K8S_MINOR is required"
repo="https://pkgs.k8s.io/core:/stable:/v${K8S_MINOR}"

if [ "$OS_FAMILY" = debian ]; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL "${repo}/deb/Release.key" | gpg --dearmor --yes -o /etc/apt/keyrings/kubernetes-apt-keyring.gpg
  echo "deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] ${repo}/deb/ /" > /etc/apt/sources.list.d/kubernetes.list
  apt_update
  # apt-cache madison lists newest first: "kubeadm | 1.34.2-1.1 | https://..."
  pkg_version_for() {
    apt-cache madison kubeadm | awk -F'|' '{gsub(/ /,"",$2); print $2}' \
      | { if [ -n "${1:-}" ]; then grep -E "^${1//./\\.}-" ; else cat; fi; } | head -1
  }
else
  cat > /etc/yum.repos.d/kubernetes.repo <<EOF
[kubernetes]
name=Kubernetes
baseurl=${repo}/rpm/
enabled=1
gpgcheck=1
gpgkey=${repo}/rpm/repodata/repomd.xml.key
exclude=kubelet kubeadm kubectl cri-tools kubernetes-cni
EOF
  pkg_version_for() {
    dnf list --showduplicates --disableexcludes=kubernetes kubeadm 2>/dev/null \
      | awk '$1 ~ /^kubeadm/ {print $2}' \
      | { if [ -n "${1:-}" ]; then grep -E "^${1//./\\.}-" ; else cat; fi; } | sort -V | tail -1
  }
fi

pkg_version="$(pkg_version_for "${K8S_VERSION:-}")"
[ -n "$pkg_version" ] || fail "Kubernetes ${K8S_VERSION:-$K8S_MINOR} is not available from ${repo}"
upstream="${pkg_version%%-*}"

if [ "${MODE:-install}" = "resolve" ]; then
  echo "RESOLVED_VERSION=${upstream}"
  exit 0
fi

log "Installing kubeadm, kubelet and kubectl ${upstream}"
if [ "$OS_FAMILY" = debian ]; then
  apt-mark unhold kubelet kubeadm kubectl >/dev/null 2>&1 || true
  apt_install --allow-downgrades --allow-change-held-packages \
    "kubelet=${pkg_version}" "kubeadm=${pkg_version}" "kubectl=${pkg_version}" cri-tools
  apt-mark hold kubelet kubeadm kubectl >/dev/null
else
  dnf_install --disableexcludes=kubernetes \
    "kubelet-${pkg_version}" "kubeadm-${pkg_version}" "kubectl-${pkg_version}" cri-tools
fi
systemctl enable kubelet >/dev/null 2>&1

installed="$(kubeadm version -o short)"
[ "$installed" = "v${upstream}" ] || fail "expected kubeadm v${upstream}, found ${installed}"
log "Installed Kubernetes components ${installed}"
