#!/usr/bin/env bash
# Show the colour chart on the MFD glass (raw to /dev/fb0, UI stopped, NO gains).
#   tools/calib/show_on_mfd.sh          show chart
#   tools/calib/show_on_mfd.sh restore  restart the UI
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
PI=(ssh -o ConnectTimeout=10 -o ProxyCommand="ssh -o ConnectTimeout=10 -J root@46.224.54.21 user@10.200.0.14 nc 192.168.100.2 22" piotr@192.168.100.2)
if [[ "${1:-}" == restore ]]; then
  "${PI[@]}" 'sudo systemctl start mfd.service && systemctl is-active mfd.service'
  exit
fi
"${PI[@]}" 'sudo systemctl stop mfd.service; cat /etc/systemd/system/mfd.service.d/equalizer.conf 2>/dev/null; cat > /dev/fb0' < "$here/chart.bgra"
echo "chart on the glass (UI stopped). restore: $0 restore"
