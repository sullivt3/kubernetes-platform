# Shared swap preparation for kubeadm and native distributions. Define, do not run.
disable_swap() {
  # Capture units BEFORE swapoff makes them inactive, so every former swap stays masked.
  local units
  units="$(systemctl list-units --type swap --all --plain --no-legend | awk '{print $1}')"
  swapoff -a
  sed -ri '/^[^#].*[[:space:]]swap[[:space:]]/ s/^/# disabled by kubernetes-platform: /' /etc/fstab
  # Fedora enables swap via zram-generator even when fstab has no swap entries.
  # Mask the generator in /etc; keep vendor and user zram configuration untouched.
  if [ -x /usr/lib/systemd/system-generators/zram-generator ]; then
    install -d /etc/systemd/system-generators
    ln -sfn /dev/null /etc/systemd/system-generators/zram-generator
  fi
  for unit in $units; do
    systemctl mask --now "$unit"
  done
  systemctl daemon-reload
  [ -z "$(swapon --show --noheadings)" ] || { echo "ERROR: swap is still active" >&2; return 1; }
}
