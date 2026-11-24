# poco2 (headless slave POCO F1) - local configuration

Facts about the unit (access, boot quirks, power management by the car computer) live in the
CMDB (`~/.infra/servers/poco2.md`) and in [../POCO/POWER.md](../POCO/POWER.md#secondary-poco-poco2-power-management).

Files here, installed by `sh install.sh` as root on poco2 (poco2 is reachable only over the car
computer's AP, so copy the directory through `poco` with `/etc/prius/poco2_ssh_key`):

| File | What |
|---|---|
| `rootfs/usr/local/sbin/poco2-prius-watch` + `poco2-prius-watch.service` | Powers poco2 off after 30 min (boot-time uptime) without the Wi-Fi profile `prius`, i.e. away from the car or with the car AP down. The car computer starts it again with the power button. Bench override: `/etc/poco2/stay-up`. Env: `POCO2_WATCH_LIMIT_S`, `POCO2_WATCH_CHECK_S`, `POCO2_WATCH_PROFILE`. |
| `rootfs/etc/modprobe.d/poco2-headless.conf` | Built-in MIPI cameras (CAMSS, CCI, sensor drivers) never load. |
| `install.sh` | Installs the above and **masks** the sensor stack: `hexagonrpcd-adsp-sensorspd` (restart-looped on every boot, 2800 times in a long one), `hexagonrpcd-sdsp`, `iio-sensor-proxy`. Mask, never `apk del`. Audio DSP, modem, `rmtfs`, `tqftpserv` stay: the Wi-Fi firmware needs them. |

Installed 2026-11-24; 3 clean restarts afterwards came back in 47-98 s with no sensor service running.
