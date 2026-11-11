#!/usr/bin/env bash
# Real package/repository smoke test inside a disposable, unprivileged container.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/dev-env.sh"
version="${1:-45}"
case "$version" in 43|44|45) ;; *) echo 'Use Fedora 43, 44 or 45' >&2; exit 1 ;; esac
exec docker run --rm --memory=768m --cpus=2 \
  -v "$KPLATFORM_ROOT/backend/app/modules/cluster_lifecycle/distributions/kubeadm/scripts:/installer/kubeadm:ro" \
  -v "$KPLATFORM_ROOT/backend/app/modules/cluster_lifecycle/distributions/scripts:/installer/shared:ro" \
  -v "$KPLATFORM_ROOT/scripts/fedora-package-check.sh:/check.sh:ro" \
  "fedora:$version" bash /check.sh
