#!/usr/bin/env bash
# Run all checks against the isolated local test services.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/dev-env.sh"
: "${TEST_DATABASE_URL:?Run python scripts/configure-local.py first}"
export DATABASE_URL="$TEST_DATABASE_URL"
export REDIS_URL="$TEST_REDIS_URL"
bash "$KPLATFORM_ROOT/scripts/test-services.sh" up -d --wait
cd "$KPLATFORM_ROOT/backend"
python -m pytest
python -m ruff check app tests
python -m ruff format --check app tests
python -m mypy app
case "${KPLATFORM_FRONTEND_CHECKS:-native}" in
  docker)
    cd "$KPLATFORM_ROOT"
    docker build -f infrastructure/containers/frontend-checks.Dockerfile -t kplatform-frontend-checks frontend
    ;;
  native)
    cd "$KPLATFORM_ROOT/frontend"
    npm run typecheck
    npm run lint
    npm test
    npm run build
    ;;
  *)
    echo 'KPLATFORM_FRONTEND_CHECKS must be docker or native' >&2
    exit 1
    ;;
esac
