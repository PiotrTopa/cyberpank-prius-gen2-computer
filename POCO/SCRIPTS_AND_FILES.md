# Scripts & Files — POCO F1 (custom additions)

Every file **we** added or modified on the phone, so you never have to grep `systemd`
or reverse-engineer where the flags/scripts live. Stock pmOS files are not listed.

> Login: `ssh user@10.200.0.5` · user `user` · hostname `prius` · passwordless sudo.
>
> **Repo mirror:** the whole custom overlay is mirrored 1:1 under [rootfs/](rootfs/)
> (scripts, systemd units, NM drop-ins, templated connections) plus the VBUS patch in
> [dts/pmi8998-usb-vbus.dtso](dts/pmi8998-usb-vbus.dtso). Push it to the phone with
> [sync.sh](sync.sh) / [provision.sh](provision.sh) — see [PROVISIONING.md](PROVISIONING.md).
> So the device can be rebuilt from scratch.

## Power management

| Path | Type | Purpose |
|------|------|---------|
| `/usr/local/sbin/prius-power` | sh script (755) | low/full CPU power profiles. `prius-power {low\|full\|apply\|status}` |
| `/etc/prius/power-mode` | flag file (664) | single source of truth: `low` or `full`. Default `low` |
| `/etc/systemd/system/prius-power.service` | systemd oneshot (enabled) | runs `prius-power apply` on boot/trigger |
| `/etc/systemd/system/prius-power.path` | systemd path (enabled) | watches `/etc/prius/power-mode`, triggers the service on change |

Details in [POWER.md](POWER.md).

## USB per-port power (powerbox / gateway recovery)

| Path | Type | Purpose |
|------|------|---------|
| `/usr/local/sbin/prius-usb-power` | sh script (755) | per-port USB power control/recovery via `uhubctl -f`. `prius-usb-power {status\|cycle\|off\|on\|locate} [TARGET]`. Recovers a wedged RP2040 powerbox/gateway (silent USB-CDC) by cycling its **hub port** |
| `/usr/local/sbin/prius-flash-powerbox` | sh script (755) | reliable hands-off OTA flash of the powerbox firmware: `prius-flash-powerbox <main.py>`. Drops the auto-running firmware into its momentary REPL with a pyserial Ctrl-C nudge and copies via `mpremote resume` (skips the soft-reset that otherwise re-runs `main.py`). Verifies + resets. Stop `prius-backend` first; restart it within 60 s. See [POWER.md](POWER.md) |
| apk `uhubctl` | package | per-port USB power switching CLI (in [packages.txt](packages.txt)) |

- Car hub is Terminus/D-Link `1a40:0101`, falsely reports **ganged** → the script
  always passes **`-f`**.
- **Port map:** powerbox = hub `1-1` **port 1**, gateway = **port 2**. The backend
  discovers roles by this topology (works even when a board is silent/wedged).
- The powerbox is **USB-powered only**, so a port power-cycle (≥ 4 s off) is a true
  cold reset of the MCU. The firmware uses non-blocking USB-CDC writes (never wedges)
  with a watchdog backstop. Full detail + recovery procedure in [POWER.md](POWER.md).

## Networking

| Path | Type | Purpose |
|------|------|---------|
| `/etc/wireguard/wg-homelab.conf` | WireGuard conf | VPN overlay, addr `10.200.0.5/32`, endpoint `<WG_SERVER_IP>:51820`, AllowedIPs `10.200.0.0/24,192.168.0.0/24`, keepalive 25 |
| NM conn `wg-homelab` | NetworkManager | brings up the WireGuard tunnel, autoconnect yes |
| NM conn `prius-wan` | NetworkManager | SIM WAN, APN `internet`, `gsm.pin` (value in secrets.env), metric 800, autoconnect yes |
| `/usr/local/sbin/prius-wifi` | sh script (755) | Wi-Fi mode toggle `prius-wifi {client\|ap\|off\|apply\|status}`. Never touches WAN/VPN |
| `/etc/prius/wifi-mode` | flag file (664) | single source of truth: `client`/`ap`/`off`. Default `off` |
| `/etc/systemd/system/prius-wifi.service` | systemd oneshot (enabled) | runs `prius-wifi apply` on boot/trigger |
| `/etc/systemd/system/prius-wifi.path` | systemd path (enabled) | watches `/etc/prius/wifi-mode`, triggers the service on change |
| NM conn `prius-home-wifi` | NetworkManager | Wi-Fi client (SSID `in_the_center_of_nowhere`), metric 600, **autoconnect no** (toggle) |
| NM conn `prius-ap` | NetworkManager | Wi-Fi AP (SSID `prius`, 2.4 GHz, `10.42.0.1/24`, dnsmasq DHCP, WPA2), **autoconnect no** (toggle) |
| NM conn `prius-dev-wifi` | NetworkManager | legacy dev Wi-Fi client (SSID `in_the_middle_of_nowhere`), **autoconnect no** (unused) |
| `/etc/NetworkManager/conf.d/99-unmanage-usb0.conf` | NM drop-in | leaves `usb0` unmanaged (USB lifeline) |
| `/etc/NetworkManager/conf.d/90-dns-none.conf` | NM drop-in | `dns=none`, keep manual `/etc/resolv.conf` (1.1.1.1 / 9.9.9.9) |
| `/usr/local/sbin/prius-netwatch` | sh script (755) | **VPN-over-SIM watchdog** `prius-netwatch {once\|status\|check}`. Pings WG server, escalates bounce-wg → bounce-wan → reboot on silent bearer loss |
| `/run/prius-netwatch.fail` | tmpfs flag | consecutive-failure counter (resets on boot / on recovery) |
| `/etc/systemd/system/prius-netwatch.service` | systemd oneshot | runs `prius-netwatch once` |
| `/etc/systemd/system/prius-netwatch.timer` | systemd timer (enabled) | fires every 60 s (`OnBootSec=120`, `OnUnitActiveSec=60`) |

Details in [NETWORKING.md](NETWORKING.md).

## USB role (host vs device)

| Path | Type | Purpose |
|------|------|---------|
| `/usr/local/sbin/prius-usb` | sh script (755) | switch USB role `prius-usb {host\|device\|status} [--reboot]`. Patches the DTB `dr_mode` + `mkinitfs` + reboot |
| `/usr/local/sbin/prius-vbus` | sh script (755) | add/remove the PMI8998 5 V VBUS regulator node so the phone **sources host power**. `prius-vbus {apply\|revert\|status} [--reboot]`. Idempotent; re-run after a kernel update |
| `/etc/prius/usb-mode` | flag file | current intent: `host` or `device`. Currently **`host`** |
| `/boot/dtbs/qcom/sdm845-xiaomi-beryllium-tianma.dtb` | DTB (patched) | `usb@a600000` `dr_mode` = `otg` (host) **+** `pmic@2/usb-vbus-regulator@1100` VBUS boost node. Source DTB consumed by boot-deploy |
| `/root/boot-backup/usb-otg-*/`, `/root/boot-backup/vbus-*/` | backup | pre-OTG and pre-VBUS `boot.img` + DTB (recovery) |

- `dwc3-qcom-legacy` on this kernel has **no runtime role switch** — the role is fixed at
  boot from the DTB `dr_mode`, so a switch needs `mkinitfs` (repack+flash `boot.img`) + reboot.
- **host** mode = OTG/xHCI to drive the RP2040 / CAN hardware; **kills** the USB-network
  lifeline (peripheral gadget gone) → VPN-over-SIM is the sole channel (hence `prius-netwatch`).
- **device** mode = restores the NCM USB-network lifeline (`172.16.42.1`) built by the
  initramfs at boot.
- **VBUS host power:** the phone sources its own 5 V via the `usb-vbus-regulator@1100`
  node under `pmic@2` (PMI8998), driven by the in-tree generic `qcom_usb_vbus-regulator`.
  Devices enumerate with no external power. Managed by `prius-vbus`; node mirrored in
  [dts/pmi8998-usb-vbus.dtso](dts/pmi8998-usb-vbus.dtso). Details in [ARCHITECTURE.md](ARCHITECTURE.md).
- ⚠️ After a **kernel/`linux-postmarketos-qcom-sdm845` update** the DTB is reinstalled
  pristine (`dr_mode=peripheral`, **no VBUS node**) → host mode + VBUS are lost → re-run
  `sudo prius-usb host` then `sudo prius-vbus apply --reboot`.

## Display / headless / GPU

| Path | Type | Purpose |
|------|------|---------|
| `/etc/systemd/system/backlight-off.service` | systemd (enabled) | forces backlight to 0 at boot |
| `systemd-backlight@backlight:backlight.service` | masked | prevents stock backlight restore |
| `/lib/firmware/qcom/a630_sqe.fw` | firmware (apk `firmware-qcom-adreno-a630-sqe`) | Adreno 630 SQE microcode |
| `/lib/firmware/qcom/a630_gmu.bin` | firmware (apk `firmware-qcom-adreno-a630`) | GMU microcode (GPU power management) |
| `/etc/mkinitfs/files-extra/adreno-fw` | mkinitfs list | bundles `a630_sqe.fw` + `a630_gmu.bin` + `…/beryllium/a630_zap.mbn` into the **initramfs** |

> ✅ **GPU firmware loads cleanly** (verified post-reboot: `loaded qcom/a630_sqe.fw from new
> location`, no `-2`). Root cause was that `CONFIG_DRM_MSM=y` is built-in and the GPU probes
> at ~5.5 s **inside the initramfs**, before `/lib/firmware/qcom` exists. Fix: the firmware
> is now embedded in the initramfs via `/etc/mkinitfs/files-extra/adreno-fw`. **After any
> kernel/firmware update, re-run `sudo mkinitfs` and reboot.** Pre-fix initramfs backed up at
> `/root/boot-backup/`.
>
> ⚠️ **Never** live `unbind`/`bind` the `adreno` GPU driver while the `msm` display-controller
> is bound — it oopses `drm_self_refresh` and hangs a kernel worker (then reboot can't
> complete). To (re)load GPU firmware, just **reboot**: clean init loads it safely.
>
> ✅ GPU **compute userspace is installed and working** — OpenCL 3.0 (`rusticl`/`FD630`) and
> Vulkan 1.3 (`turnip`/Adreno 630). Full details, device caps, env vars and recommended math
> libraries in [GPU_COMPUTE.md](GPU_COMPUTE.md). Key gotcha: apps must
> `export RUSTICL_ENABLE=freedreno` to see the OpenCL device.

## Battery / power-supply

| Path | Type | Purpose |
|------|------|---------|
| `systemd-battery-check.service` | **masked** (→ `/dev/null`) | battery is amputated (phantom `qcom-battery` reads `Full`/99%); prevent early-boot battery gating |

## Access / auth

| Path | Purpose |
|------|---------|
| `/etc/sudoers.d/010-user-nopasswd` | passwordless sudo for `user` |
| `~user/.ssh/authorized_keys` | SSH keys: WSL `id_ed25519` (piotr@ThinkPad), box rsa (piotr@nokia1), Git Bash key |

## Quick "where is it?" index

- **Power flag** → `/etc/prius/power-mode`
- **Power script** → `/usr/local/sbin/prius-power`
- **Power units** → `/etc/systemd/system/prius-power.{service,path}`
- **Wi-Fi flag** → `/etc/prius/wifi-mode`
- **Wi-Fi script** → `/usr/local/sbin/prius-wifi`
- **Wi-Fi units** → `/etc/systemd/system/prius-wifi.{service,path}`
- **VPN watchdog** → `/usr/local/sbin/prius-netwatch` + `/etc/systemd/system/prius-netwatch.{service,timer}`
- **USB role flag** → `/etc/prius/usb-mode` (`host`/`device`)
- **USB role script** → `/usr/local/sbin/prius-usb`
- **VPN config** → `/etc/wireguard/wg-homelab.conf` (NM conn `wg-homelab`)
- **SIM/APN/PIN** → NM conn `prius-wan` (`nmcli con show prius-wan`)
- **Backlight off** → `/etc/systemd/system/backlight-off.service`
- **Sudo** → `/etc/sudoers.d/010-user-nopasswd`

### Handy inspection commands

```bash
# list our enabled units
systemctl list-unit-files | grep -E 'prius-power|prius-wifi|prius-netwatch|backlight-off'

# show the power flag + live state
cat /etc/prius/power-mode; sudo prius-power status

# show the wi-fi flag + live state
cat /etc/prius/wifi-mode; sudo prius-wifi status

# VPN-over-SIM watchdog + USB role
sudo prius-netwatch status
cat /etc/prius/usb-mode; sudo prius-usb status

# show network connections
nmcli -t -f NAME,DEVICE,STATE c show
sudo wg show wg-homelab
mmcli -m any

# GPU compute sanity check
RUSTICL_ENABLE=freedreno clinfo | grep -E 'Device Name|Max compute units'
vulkaninfo --summary | grep -E 'deviceName|driverName'
```
