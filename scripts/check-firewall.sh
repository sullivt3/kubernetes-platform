#!/usr/bin/env bash
# Real firewalld D-Bus/policy/reload test in its own disposable network namespace.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/dev-env.sh"
docker build -f "$KPLATFORM_ROOT/infrastructure/containers/firewall-check.Dockerfile" \
  -t kplatform-firewall-check "$KPLATFORM_ROOT"
exec docker run --rm --memory=512m --cpus=2 --cap-add=NET_ADMIN --cap-add=NET_RAW \
  -v "$KPLATFORM_ROOT/backend/app/modules/cluster_lifecycle/distributions/kubeadm/scripts:/installer:ro" \
  -v "$KPLATFORM_ROOT/scripts/firewall-integration-check.sh:/check.sh:ro" kplatform-firewall-check
