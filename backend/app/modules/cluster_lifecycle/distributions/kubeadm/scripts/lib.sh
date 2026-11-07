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
case " ${ID:-} ${ID_LIKE:-} " in
  *" debian "*|*" ubuntu "*) OS_FAMILY=debian ;;
  *" rhel "*|*" fedora "*|*" centos "*) OS_FAMILY=rhel ;;
  *) fail "unsupported operating system: ${PRETTY_NAME:-unknown}" ;;
esac

apt_update()  { apt-get -o DPkg::Lock::Timeout=600 update -q; }
apt_install() { apt-get -o DPkg::Lock::Timeout=600 install -y -q --no-install-recommends "$@"; }
dnf_install() { dnf install -y -q "$@"; }

CRI_SOCKET="unix:///run/containerd/containerd.sock"
KPLATFORM_DIR=/etc/kubernetes/kplatform
