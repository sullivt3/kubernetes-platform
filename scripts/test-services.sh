#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/dev-env.sh"
if [ ! -f "$KPLATFORM_ROOT/.env.test-services" ]; then
  echo 'Run python scripts/configure-local.py first.' >&2
  exit 1
fi
exec docker compose --env-file "$KPLATFORM_ROOT/.env.test-services" \
  -f "$KPLATFORM_ROOT/infrastructure/deployment/docker-compose.test.yml" "$@"
