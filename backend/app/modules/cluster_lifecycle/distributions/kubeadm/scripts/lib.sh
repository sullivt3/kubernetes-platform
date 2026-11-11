# Shared prelude prepended to every kubeadm script (after the generated `export` lines).
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:${PATH:-}"
umask 022

log()  { printf '%s\n' "$*"; }
fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || fail "must run as root (sudo)"
[ -r /etc/os-release ] || fail "/etc/os-release not found"
# shellcheck disable=SC1091
. /etc/os-release
[ ! -e /run/ostree-booted ] || fail "immutable images are not supported; use a conventional DNF/APT-managed host"
case " ${ID:-} ${ID_LIKE:-} " in
  *" debian "*|*" ubuntu "*) OS_FAMILY=debian ;;
  *" rhel "*|*" fedora "*|*" centos "*) OS_FAMILY=rhel ;;
  *) fail "unsupported operating system: ${PRETTY_NAME:-unknown}" ;;
esac

apt_update()  { apt-get -o DPkg::Lock::Timeout=600 update -q; }
apt_install() { apt-get -o DPkg::Lock::Timeout=600 install -y -q --no-install-recommends "$@"; }
DNF=dnf
KUBERNETES_DNF_OPTIONS=(--disableexcludes=kubernetes)
if command -v dnf5 >/dev/null 2>&1; then
  DNF=dnf5
  KUBERNETES_DNF_OPTIONS=(--setopt=disable_excludes=kubernetes)
fi
dnf_install() { "$DNF" install -y -q "$@"; }
kubernetes_dnf() { "$DNF" "${KUBERNETES_DNF_OPTIONS[@]}" "$@"; }

# Use Docker's repository for the actual OS. Rocky/Alma retain the CentOS-compatible path.
docker_repo_os() {
  case "${ID:-}" in
    ubuntu|debian|fedora|rhel|centos) printf '%s\n' "$ID" ;;
    rocky|almalinux) printf '%s\n' centos ;;
    *) fail "no containerd repository configured for ${ID:-unknown}" ;;
  esac
}

CRI_SOCKET="unix:///run/containerd/containerd.sock"
KPLATFORM_DIR=/etc/kubernetes/kplatform
