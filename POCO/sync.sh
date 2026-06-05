#!/usr/bin/env bash
# sync.sh — push the repo's POCO/rootfs overlay to the phone and provision it.
#
# Workflow (the "docker build && run" of this project):
#   1. render *.nmconnection.tmpl with the secrets from secrets.env,
#   2. rsync the staged rootfs to a temp dir on the phone (no secrets in repo),
#   3. run provision.sh on the phone to install + activate everything.
#
# Run this from a POSIX shell with rsync + ssh + envsubst available — i.e. WSL2 or
# Linux. (git-bash usually lacks rsync/envsubst; see PROVISIONING.md.)
#
#   ./sync.sh                 # sync files + activate units (no apk, no reboot)
#   ./sync.sh --packages      # also apk add the package list
#   ./sync.sh --usb           # also apply USB host + VBUS (REBOOTS the phone)
#   PHONE=user@10.200.0.5 ./sync.sh --dry-run   # show what rsync would change
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PHONE="${PHONE:-user@10.200.0.5}"
SSH_OPTS="${SSH_OPTS:--o ConnectTimeout=15 -o StrictHostKeyChecking=accept-new}"

DRY=0
PROV_ARGS=()
for a in "$@"; do
    case "$a" in
        --dry-run)  DRY=1 ;;
        --packages) PROV_ARGS+=(--packages) ;;
        --usb)      PROV_ARGS+=(--usb) ;;
        *) echo "unknown arg: $a" >&2; exit 2 ;;
    esac
done

# --- preconditions -----------------------------------------------------------
for t in rsync ssh scp envsubst; do
    command -v "$t" >/dev/null 2>&1 || { echo "ERROR: '$t' not found (use WSL2 — see PROVISIONING.md)" >&2; exit 1; }
done
SECRETS="$HERE/secrets.env"
[ -f "$SECRETS" ] || { echo "ERROR: $SECRETS missing — copy secrets.env.example and fill it in" >&2; exit 1; }
# Source secrets, tolerating CRLF (the file is gitignored so not LF-normalized).
SECRETS_CLEAN="$(mktemp)"
sed 's/\r$//' "$SECRETS" > "$SECRETS_CLEAN"
# shellcheck disable=SC1090
set -a; . "$SECRETS_CLEAN"; set +a

# --- 1. stage + render -------------------------------------------------------
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE" "$SECRETS_CLEAN"' EXIT
cp -a "$HERE/rootfs/." "$STAGE/"

# Render every *.tmpl -> same name without .tmpl, then drop the template.
find "$STAGE" -name '*.tmpl' | while IFS= read -r tmpl; do
    out="${tmpl%.tmpl}"
    envsubst < "$tmpl" > "$out"
    rm -f "$tmpl"
done
echo "sync: staged + rendered overlay in $STAGE"

# --- 2. ensure rsync on the phone, then push ---------------------------------
# shellcheck disable=SC2086
ssh $SSH_OPTS "$PHONE" 'command -v rsync >/dev/null 2>&1 || sudo apk add rsync'
# shellcheck disable=SC2086
ssh $SSH_OPTS "$PHONE" 'rm -rf /tmp/prius-rootfs && mkdir -p /tmp/prius-rootfs'

RSYNC_FLAGS=(-rlpt --delete -e "ssh $SSH_OPTS")
[ "$DRY" = 1 ] && RSYNC_FLAGS+=(--dry-run --itemize-changes)
rsync "${RSYNC_FLAGS[@]}" "$STAGE/" "$PHONE:/tmp/prius-rootfs/"

if [ "$DRY" = 1 ]; then
    echo "sync: --dry-run, not provisioning."
    exit 0
fi

# --- 3. provision on the phone ----------------------------------------------
# shellcheck disable=SC2086
scp $SSH_OPTS "$HERE/provision.sh" "$PHONE:/tmp/provision.sh"
# shellcheck disable=SC2086
scp $SSH_OPTS "$HERE/packages.txt" "$PHONE:/tmp/packages.txt"
# shellcheck disable=SC2086
ssh $SSH_OPTS "$PHONE" "sudo sh /tmp/provision.sh /tmp/prius-rootfs ${PROV_ARGS[*]:-}"
echo "sync: done."
