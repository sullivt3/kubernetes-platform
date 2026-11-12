# Opt-in administrator access for an existing local user.
# Env: TARGET_USER, CHECK_ONLY (1|0), CONFIG_B64, CONTEXT_NAME.
# Credentials arrive with the script over stdin, never in process arguments.
[[ "$TARGET_USER" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || fail "invalid kubectl username"
command -v runuser >/dev/null || fail "runuser is required to configure kubectl safely"
entry="$(getent passwd "$TARGET_USER")" || fail "kubectl user '$TARGET_USER' does not exist"
IFS=: read -r login _ uid gid _ target_home _ <<< "$entry"
[ "$login" = "$TARGET_USER" ] || fail "could not resolve kubectl user"
[[ "$target_home" = /* && "$target_home" != / ]] || fail "kubectl user must have an absolute home directory other than /"
[ -d "$target_home" ] || fail "kubectl user's home directory does not exist"
runuser -u "$TARGET_USER" -- test -w "$target_home" || fail "kubectl user's home directory is not writable by that user"
if [ "${CHECK_ONLY:-0}" = 1 ]; then
  log "Kubectl account verified: $TARGET_USER"
  exit 0
fi

stage="$(mktemp -d)"
trap 'rm -rf -- "$stage"' EXIT
chmod 700 "$stage"
printf '%s' "$CONFIG_B64" | base64 -d > "$stage/generated"
chmod 600 "$stage/generated"
chown -R "$uid:$gid" "$stage"

# All home/config operations run as the target user. Flattening an existing
# kubeconfig cannot read files that only root can access.
runuser -u "$TARGET_USER" -- bash -s -- "$target_home" "$stage" <<'KPLATFORM_USER_SCRIPT' || fail "could not configure kubectl for '$TARGET_USER'; existing configuration preserved"
set -euo pipefail
umask 077
target_home="$1"
stage="$2"
directory="$target_home/.kube"
config="$directory/config"
[ ! -L "$directory" ] || { echo "ERROR: refusing a symlinked .kube directory" >&2; exit 1; }
mkdir -p -- "$directory"
chmod 700 "$directory"
[ ! -L "$config" ] || { echo "ERROR: refusing a symlinked kubeconfig" >&2; exit 1; }
[ ! -e "$config" ] || [ -f "$config" ] || { echo "ERROR: kubeconfig is not a regular file" >&2; exit 1; }
temporary="$(mktemp "$directory/.config.kplatform.XXXXXX")"
trap 'rm -f -- "$temporary"' EXIT

if ! KUBECONFIG="$stage/generated:$config" kubectl config view --raw --flatten > "$temporary" 2> "$stage/error"; then
  echo "ERROR: could not merge kubeconfig; the existing file was preserved" >&2
  exit 1
fi
if [ -f "$config" ]; then
  current="$(kubectl --kubeconfig "$config" config current-context 2>/dev/null || true)"
  if [ -n "$current" ]; then
    kubectl --kubeconfig "$temporary" config use-context "$current" >/dev/null 2> "$stage/error" || {
      echo "ERROR: could not preserve the current context; the existing file was preserved" >&2
      exit 1
    }
  fi
  if cmp -s "$config" "$temporary"; then
    chmod 600 "$config"
    exit 0
  fi
  backup="$(mktemp "$directory/config.backup.XXXXXX")"
  cp -- "$config" "$backup"
  chmod 600 "$backup"
fi
chmod 600 "$temporary"
mv -f -- "$temporary" "$config"
KPLATFORM_USER_SCRIPT
log "Kubectl configured for $TARGET_USER at $target_home/.kube/config"
log "Context: $CONTEXT_NAME (existing current context preserved)"
