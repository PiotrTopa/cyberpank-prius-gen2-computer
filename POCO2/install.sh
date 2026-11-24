#!/bin/sh
# Run on poco2 as root from the unpacked POCO2/ directory: sh install.sh
set -eu
cd "$(dirname "$0")"
install -m 755 rootfs/usr/local/sbin/poco2-prius-watch /usr/local/sbin/poco2-prius-watch
install -m 644 rootfs/etc/systemd/system/poco2-prius-watch.service /etc/systemd/system/poco2-prius-watch.service
install -m 644 rootfs/etc/modprobe.d/poco2-headless.conf /etc/modprobe.d/poco2-headless.conf
mkdir -p /etc/poco2
# Sensors are never used (their sensor flex is missing; slpi firmware already
# renamed): mask, never `apk del` (see ~/.infra/servers/poco2.md).
SENSORS="hexagonrpcd-adsp-sensorspd.service hexagonrpcd-sdsp.service iio-sensor-proxy.service"
systemctl stop $SENSORS 2>/dev/null || true
systemctl mask $SENSORS
systemctl daemon-reload
systemctl enable --now poco2-prius-watch.service
systemctl is-enabled $SENSORS poco2-prius-watch.service || true
