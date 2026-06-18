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
#   ./sync.sh --app           # also rsync the app code to the phone repo dir
#   ./sync.sh --usb           # also apply USB host + VBUS (REBOOTS the phone)
#   PHONE=user@10.200.0.5 ./sync.sh --dry-run   # show what rsync would change
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"
PHONE="${PHONE:-user@10.200.0.5}"
APP_DEST="${APP_DEST:-/home/user/cyberpunk}"
SSH_OPTS="${SSH_OPTS:--o ConnectTimeout=15 -o StrictHostKeyChecking=accept-new}"

DRY=0
DO_APP=0
PROV_ARGS=()
for a in "$@"; do
    case "$a" in
        --dry-run)  DRY=1 ;;
        --packages) PROV_ARGS+=(--packages) ;;
        --app)      DO_APP=1 ;;
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
# Normalize line endings: a Windows working tree can carry CRLF even with
# .gitattributes. A stray CR breaks shebangs (203/EXEC) and config parsers, so
# strip it from every staged text file before it reaches the phone.
find "$STAGE" -type f ! -name '*.png' ! -name '*.jpg' ! -name '*.gz' \
    -exec sed -i 's/\r$//' {} +
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
# Ship LF-clean copies of the helper + package list (CRLF would break `sh`).
PROV_CLEAN="$(mktemp)"; PKGS_CLEAN="$(mktemp)"
trap 'rm -rf "$STAGE" "$SECRETS_CLEAN" "$PROV_CLEAN" "$PKGS_CLEAN"' EXIT
sed 's/\r$//' "$HERE/provision.sh" > "$PROV_CLEAN"
sed 's/\r$//' "$HERE/packages.txt" > "$PKGS_CLEAN"
# shellcheck disable=SC2086
scp $SSH_OPTS "$PROV_CLEAN" "$PHONE:/tmp/provision.sh"
# shellcheck disable=SC2086
scp $SSH_OPTS "$PKGS_CLEAN" "$PHONE:/tmp/packages.txt"
# shellcheck disable=SC2086
ssh $SSH_OPTS "$PHONE" "sudo sh /tmp/provision.sh /tmp/prius-rootfs ${PROV_ARGS[*]:-}"

# --- 4. app code (optional; after provision installs prius-backend) ----------
# Push the backend application source to the phone so prius-backend can run it.
# Only the runtime package + its deps file are needed (no tests/assets/logs).
if [ "$DO_APP" = 1 ]; then
    echo "sync: pushing app code to $PHONE:$APP_DEST"
    # shellcheck disable=SC2086
    ssh $SSH_OPTS "$PHONE" "mkdir -p $APP_DEST/cyberpunk_computer $APP_DEST/data"
    rsync -rlpt --delete \
        --exclude '__pycache__' --exclude '*.pyc' \
        -e "ssh $SSH_OPTS" \
        "$REPO_ROOT/cyberpunk_computer/" "$PHONE:$APP_DEST/cyberpunk_computer/"
    # shellcheck disable=SC2086
    scp $SSH_OPTS "$REPO_ROOT/requirements.txt" "$PHONE:$APP_DEST/requirements.txt"
    # shellcheck disable=SC2086
    ssh $SSH_OPTS "$PHONE" "sudo /usr/local/sbin/prius-backend ensure-venv && sudo systemctl restart prius-backend.service"
fi
echo "sync: done."
