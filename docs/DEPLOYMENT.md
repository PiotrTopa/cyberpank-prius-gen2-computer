# Deployment

Two targets: the **backend** on the POCO F1 and the **frontend** on the MFD Raspberry Pi Zero v1.3.
The Pi is only the display driver; it reaches the backend over the USB Ethernet gadget link
(`usb0`, Pi 192.168.100.2, POCO 192.168.100.1).

| Target | Unit | Code location | How code gets there |
|---|---|---|---|
| POCO F1 | `prius-backend.service` (`/usr/local/sbin/prius-backend run`) | `/home/user/cyberpunk` (`BACKEND_REPO`) | `POCO/sync.sh --app` from the workstation |
| MFD Pi | `mfd.service` (`python -m cyberpunk_computer.frontend --host 192.168.100.1 --fullscreen --scale 1 --production`) | `/home/piotr/cyberpunk_computer`, venv `.venv` | `sudo prius-deploy-mfd` on the POCO |

## Backend on the POCO

`POCO/sync.sh --app` rsyncs `cyberpunk_computer/`, `requirements.txt`, the powerbox firmware files
(for `prius-flash-powerbox`) and `dashboard/dist/` (if built: `cd dashboard && npm ci && npm run build`)
to the phone, then runs `prius-backend ensure-venv` and restarts `prius-backend.service`. The rest of
the phone overlay (units, `/etc/prius/backend.env`, power and network scripts) is described in
[POCO/PROVISIONING.md](../POCO/PROVISIONING.md) and [POCO/SCRIPTS_AND_FILES.md](../POCO/SCRIPTS_AND_FILES.md).

- Configuration: `/etc/prius/backend.env` (`BACKEND_*` variables; seeded by `POCO/provision.sh`).
- Python deps: `cyberpunk_computer/backend/requirements-backend.txt`, venv with
  `--system-site-packages` (apk-provided pyserial).
- Logs: `journalctl -u prius-backend`.

## Frontend on the MFD Pi

`prius-deploy-mfd` (on the POCO, as root) rsyncs the POCO's staging copy
`$BACKEND_REPO/cyberpunk_computer/` to the Pi and restarts `mfd.service`; `--dry-run` shows the diff.
The Pi must be powered (backend MFD manager, `BACKEND_MFD_ENABLED=1`, or relay ch3 by hand) and its
gadget link up. New Python dependencies go into the Pi's `.venv` (apt-provided
`python3-pygame python3-zmq python3-serial`, `--system-site-packages`; no pip builds on ARMv6).

Proof of life: `journalctl -u mfd.service -n 20 --no-pager` on the Pi.

Wall clock: the Pi has no RTC and no internet. `prius-deploy-mfd` also installs
`/etc/systemd/timesyncd.conf.d/prius.conf` on the Pi (`NTP=192.168.100.1`, no fallback servers,
`PollIntervalMaxSec=64`, `SaveIntervalSec=60`) and enables `systemd-timesyncd` (Raspbian ships it disabled). The backend
answers on UDP 123 of the gadget link (`cyberpunk_computer/backend/ntp_server.py`,
`BACKEND_NTP_SERVE`, default on with the MFD manager) and only with a synchronized phone clock
(leap indicator 3 otherwise, which timesyncd ignores). Without the phone a cold boot starts at the
time saved last (`/var/lib/systemd/timesync/clock`). Check on the Pi: `timedatectl timesync-status`.
Other clients: `BACKEND_NTP_ALLOW` takes a comma-separated list. The car's `/etc/prius/backend.env` sets
`BACKEND_NTP_ALLOW=192.168.100.0/24,10.42.0.0/24`, so `poco2` (headless slave on the `prius` AP, no RTC) syncs from
`10.42.0.1`; `50_prius_backend.nft` accepts UDP 123 from that subnet on `wlan*`.
A freshly imaged card has the same file from the start (cyberpunk-prius-gateway
`raspberry_pi_config/zero13-image/timesyncd-prius.conf`, installed and enabled by `boot-tune.sh`);
the two copies are identical (`tests/test_pi_timesync_conf.py`).
The frontend ages its link on the monotonic clock, so the step at sync does not flap it.

The Pi image (config.txt with the DPI timings, cmdline.txt, gadget units, `mfd.service`,
framebuffer udev rule, zram) is maintained in `cyberpunk-prius-gateway/raspberry_pi_config/zero13-image/`;
its README is the reference for the board. Video timings: [MFD_VIDEO_TIMINGS.md](MFD_VIDEO_TIMINGS.md).

## Files in `deployment/`

| File | Purpose |
|---|---|
| `mfd.service` | frontend unit (copy; see Open gaps for the difference to the image's unit) |
| `cyberpunk-computer.service` + `run-on-console.sh` | alternative frontend unit for the `deploy.py --install` path: runs as root, venv `venv`, `--host 192.168.100.1 --port 8080 --production` |
| `99-framebuffer.rules` | udev: `/dev/fb*` group `video`, mode 0660 |
| `setup-rpi-display.sh` | adds the user to `video,tty`, installs the udev rule |
| `disable-getty-tty1.sh` | masks `getty@tty1` so nothing else draws on fb0 |
| `verify-vga666.sh` | prints fb0 device, driver name, mode |
| `rpi-helper.sh` | ssh shortcuts for the `cyberpunk-computer` unit: `status logs recent restart start stop deploy deploy-full setup shell test serial usb` |

`deploy.py` (repo root) is the workstation → Pi path that predates `prius-deploy-mfd`: it syncs the
repo to `/home/<user>/cyberpunk_computer` (rsync, scp fallback), creates the venv, installs
`requirements.txt` and, with `--install`, installs `cyberpunk-computer.service`. Options:
`--host` / `--user` (defaults from `.env`: `DEPLOY_HOST`, `DEPLOY_USER`; see `.env.example`),
`--setup-os`, `--install`, `--restart`, `--logs`, `--dry-run`.

## Production-mode behaviour

`--production` (frontend and standalone app): fullscreen, scale 1, INFO logging to the journal and to
`/var/log/cyberpunk_computer/app.log` (10 MB × 5 rotation). The standalone app's serial port
reconnects automatically every 2 s (`VirtualTwinConfig.serial_auto_reconnect`,
`serial_reconnect_delay`); the frontend's ZMQ link reconnects by itself.

## Checks

```bash
# Pi
systemctl status mfd.service
ls -l /dev/fb0; cat /sys/class/graphics/fb0/mode
# fb0 changes between two reads = the UI is rendering
python3 -c "import hashlib,time;r=lambda:hashlib.md5(open('/dev/fb0','rb').read(480*240*4)).hexdigest();a=r();time.sleep(1);print('rendering:',a!=r())"
# POCO
systemctl status prius-backend; journalctl -u prius-backend -f
```

## Open gaps

- Two frontend units exist (`mfd.service` and `cyberpunk-computer.service`); both draw on `/dev/fb0`.
  `prius-deploy-mfd` restarts `mfd.service`, and the image installs `mfd.service`; whether
  `cyberpunk-computer.service` is still installed on the Pi is not recorded here. Check with
  `systemctl list-unit-files 'mfd*' 'cyberpunk*'` on the Pi.
- `deployment/mfd.service` differs from the image's `zero13-image/mfd.service` (image: `After=systemd-remount-fs.service`,
  `Nice=-10`; this copy: `After=network-online.target`). The image copy is what runs on the card.
  `deploy/mfd.service` (another copy, `--host 10.42.0.1`, the Poco Wi-Fi AP used by the earlier Zero 2 W)
  is obsolete for the Zero v1.3, which has no Wi-Fi.
