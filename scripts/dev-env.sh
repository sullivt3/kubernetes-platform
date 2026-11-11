# Source this file from Bash: source scripts/dev-env.sh
# Tooling stays local to the checkout; macOS system tools are left intact.
KPLATFORM_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export KPLATFORM_ROOT
export PATH="$KPLATFORM_ROOT/backend/.venv/bin:$KPLATFORM_ROOT/.tools/node/bin:$KPLATFORM_ROOT/.tools/runtime/bin:/Applications/Docker.app/Contents/Resources/bin:$PATH"
if [ "$(uname -s)" = Darwin ]; then
  export LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 LC_CTYPE=en_US.UTF-8
  export KPLATFORM_FRONTEND_CHECKS="${KPLATFORM_FRONTEND_CHECKS:-docker}"
fi
export NEXT_TELEMETRY_DISABLED=1
export PLAYWRIGHT_BROWSERS_PATH="$KPLATFORM_ROOT/.tools/playwright-browsers"

# This file is generated locally, ignored by Git, and contains shell-safe values.
if [ -f "$KPLATFORM_ROOT/.env.test-services" ]; then
  set -a
  source "$KPLATFORM_ROOT/.env.test-services"
  set +a
  export TEST_DATABASE_URL="postgresql+psycopg://kplatform:$KPLATFORM_TEST_DB_PASSWORD@127.0.0.1:55432/kplatform_test"
  export TEST_REDIS_URL="redis://127.0.0.1:56379/15"
fi
