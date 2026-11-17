#!/usr/bin/env bash
# install.sh — install the head-unit ADB tools on the car computer (POCO), from the P52.
#
#   tools/headunit/install.sh                    PHONE=user@10.200.0.14 by default
#   dotinfra vault exec poco_user_password -- tools/headunit/install.sh --sudo-stdin
#
# Puts the tools in /usr/local/lib/prius-headunit/ with /usr/local/bin/hu-* symlinks,
# the udev rule POCO/rootfs/etc/udev/rules.d/51-prius-headunit.rules, and the apk
# packages android-tools curl jq. Does not touch prius-backend or the relays.
# The phone's ADB key (~user/.android/adbkey) is created by adb itself; its backup is
# vault key poco_adbkey_b64 (docs/HEADUNIT_INTEGRATION.md §7).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
PHONE="${PHONE:-user@10.200.0.14}"
PW=""
[ "${1:-}" = "--sudo-stdin" ] && { IFS= read -r PW; }
rsudo() { if [ -n "$PW" ]; then printf '%s\n' "$PW" | ssh "$PHONE" "sudo -S -p '' sh -c '$1'"; else ssh -n "$PHONE" "sudo -n sh -c '$1'"; fi; }

tmp=$(ssh -n "$PHONE" mktemp -d)
scp -q "$HERE"/hu-* "$HERE"/hu-common.sh "$REPO/POCO/rootfs/etc/udev/rules.d/51-prius-headunit.rules" "$PHONE:$tmp/"
rsudo "command -v adb >/dev/null || apk add android-tools; command -v curl >/dev/null || apk add curl; command -v jq >/dev/null || apk add jq
mkdir -p /usr/local/lib/prius-headunit
install -m 0755 $tmp/hu-[a-z]* /usr/local/lib/prius-headunit/
install -m 0644 $tmp/hu-common.sh /usr/local/lib/prius-headunit/
for f in /usr/local/lib/prius-headunit/hu-*; do [ \"\$(basename \$f)\" = hu-common.sh ] || ln -sf \$f /usr/local/bin/\$(basename \$f); done
install -m 0644 $tmp/51-prius-headunit.rules /etc/udev/rules.d/
udevadm control --reload-rules; udevadm trigger --subsystem-match=usb --attr-match=idVendor=0e8d || true
rm -rf $tmp"
ssh -n "$PHONE" 'mkdir -p ~/.android ~/headunit && chmod 700 ~/.android; [ -f ~/.android/adbkey ] || adb keygen ~/.android/adbkey; ls /usr/local/bin/hu-*'
