# Architecture — POCO F1 car computer

## Role

Headless, always-on Linux computer in the car. It:

- talks to the RP2040 powerbox and gateway (CAN / AVC-LAN / RS485) and the comma.ai
  panda over USB-CDC behind a hub (USB OTG host);
- serves SSH and the backend API (REST/WebSocket/dashboard 8080, ZeroMQ 8081/8082 for
  the MFD frontend);
- reaches the outside over a cellular SIM with a WireGuard overlay;
- scales CPU between `low` (key off) and `full` (key on) and never suspends
  ([POWER.md](POWER.md));
- runs the roadsense perception stack (`roadsense.target`, separate repos) while ACC
  is on, started and stopped by the backend.

## Hardware

| Item | Value |
|---|---|
| Device | Xiaomi POCO F1 / `beryllium`, Tianma panel variant (DTB `sdm845-xiaomi-beryllium-tianma.dtb`) |
| SoC | Snapdragon 845, 8 cores, Adreno 630 |
| CPU clusters | Silver `cpu0-3` (300 MHz–1.766 GHz), Gold `cpu4-7` (0.825–2.65 GHz) |
| GPU | Adreno 630 via freedreno (OpenCL/Vulkan; no CUDA) |
| Bootloader | unlocked, non-A/B, fastboot serial `6927a690` |
| Power | from the car's 12 V via a buck; phone battery removed, constant **4.1 V** on the battery terminals; no screen in use |
| USB | OTG **host** only; the phone sources 5 V VBUS itself; never charged from USB |

The battery is gone, so charge % is meaningless and not read; the `qcom-battery`
thermal zone and power supply are phantoms (`systemd-battery-check.service` masked).

## Operating system

| Item | Value |
|---|---|
| Distro | postmarketOS edge, systemd |
| Kernel | `7.1.0-rc1-sdm845` (mainline-based) |
| Hostname / user | `prius` / `user` (sudoers drop-in `/etc/sudoers.d/010-user-nopasswd`) |
| UI | none |
| Timezone | Europe/Warsaw |
| Built with | `pmbootstrap`, flashed via fastboot; overlay from this repo ([PROVISIONING.md](PROVISIONING.md)) |

CPU layout: `prius.slice` (backend + small helpers) has `cpu3` to itself
(`AllowedCPUs=3`, CPUWeight 1000, MemoryMax 640M); `roadsense.slice`, `system.slice`
and `user.slice` get `AllowedCPUs=0-2,4-7` via `50-prius-cpus.conf` drop-ins. `cpu3` is
a Silver core, so it stays online in `low`.

## Design constraints

- **Never suspend** (modem, WireGuard, SSH, serial links would drop).
- **WAN = cellular SIM**, WireGuard overlay `10.200.0.0/24`. DNS sits on the LTE link,
  never on the tunnel: the RTC is not persisted (reads 1970 at boot) and the WG hub
  rejects handshakes stamped earlier than the last one, so the clock must be fixable
  (NTP, HTTPS fallback) while the tunnel is down ([NETWORKING.md](NETWORKING.md)).
- **Wi-Fi** runs the `prius` AP by default; `prius-wifi client|off` changes the single
  radio without touching WAN or VPN.
- **USB OTG host only.** Host mode removes the USB network lifeline; LTE + WireGuard is
  the sole remote channel (hence `prius-netwatch`).
- **GPU compute** via OpenCL/Vulkan on freedreno ([GPU_COMPUTE.md](GPU_COMPUTE.md)).

## USB host role and VBUS

- `dwc3-qcom-legacy` has no runtime role switch: the role comes from the DTB `dr_mode`
  (`usb@a600000`), so a change needs `mkinitfs` (repack + flash `boot.img`) + reboot.
  `prius-usb host|device --reboot` does it; flag `/etc/prius/usb-mode`.
- The phone sources its own 5 V VBUS: the PMI8998 USB-OTG boost at SPMI offset `0x1100`
  has the PM8150B register layout, so the in-tree generic `qcom_usb_vbus-regulator`
  drives it from a DT node; no kernel rebuild, no role-switch driver. Devices enumerate
  without external power (pendrive verified high-speed, 13.4 MB/s, no over-current).
  Without the node a host enumerates nothing (floating D+/D- → phantom full-speed +
  `error -71`).
- A `linux-postmarketos-qcom-sdm845` update reinstalls a pristine DTB
  (`dr_mode=peripheral`, no VBUS node): re-run `sudo prius-usb host` and
  `sudo prius-vbus apply --reboot` (or `./sync.sh --usb`).
- Fallback if host mode ever fails: swap roles (RP2040/RP2350 as USB host, phone as
  CDC-ACM gadget `/dev/ttyGS0`).

### VBUS host-power reproduction (re-apply after a clean kernel/DTB update)

`prius-vbus {apply|revert|status} [--reboot]` is the idempotent script; the node is in
[dts/pmi8998-usb-vbus.dtso](dts/pmi8998-usb-vbus.dtso). Manual equivalent:

```bash
DTB=/boot/dtbs/qcom/sdm845-xiaomi-beryllium-tianma.dtb
sudo install -d /root/boot-backup/vbus-$(date +%Y%m%d-%H%M%S)
sudo cp -a /boot/boot.img "$DTB" /root/boot-backup/vbus-*/   # backup first
sudo dtc -I dtb -O dts -o /tmp/t.dts "$DTB"
# Insert INSIDE pmic@2 (#address-cells=1 #size-cells=0), before charger@1000:
#   usb_vbus: usb-vbus-regulator@1100 {
#       compatible = "qcom,pm8150b-vbus-reg";
#       reg = <0x1100>;
#       regulator-min-microamp = <500000>;
#       regulator-max-microamp = <3000000>;
#       regulator-always-on;
#       regulator-boot-on;
#   };
sudo dtc -I dts -O dtb -o "$DTB" /tmp/t.new.dts
sudo mkinitfs                                  # repack + flash boot.img
sudo systemctl --no-block reboot               # `reboot &` over ssh gets SIGHUP
```

Verify (~90 s to rejoin the VPN):

```bash
ls /proc/device-tree/soc@0/spmi@c440000/pmic@2/ | grep vbus
grep -l usb_vbus /sys/class/regulator/*/name
cat /sys/class/regulator/<that>/state           # enabled
lsusb && ls /dev/sd*
```

`regulator-always-on` makes the regulator core set `OTG_EN` (`CMD_OTG`, base `+0x40`)
at boot, so the PMI8998 boosts 5 V onto USBIN; with `dr_mode=host` the dwc3 xHCI host
enumerates. Revert: restore `/root/boot-backup/vbus-*/boot.img.pre-vbus` (or remove the
node, `mkinitfs`, reboot). Symptom of a lost node: device unpowered, dmesg full-speed +
`error -71`.

## Display

Headless. Backlight forced to 0 (`backlight-off.service` enabled,
`systemd-backlight@backlight:backlight.service` masked). DSI blanking is opt-in and has
hard rules (LAB/IBB IRQ storm): [POWER.md](POWER.md#display-blanking-opt-in).

## GPU firmware

Microcode `a630_sqe.fw` + `a630_gmu.bin` (apk `firmware-qcom-adreno-a630-sqe`,
`firmware-qcom-adreno-a630`) in `/lib/firmware/qcom/`, plus `…/beryllium/a630_zap.mbn`.
`CONFIG_DRM_MSM=y` is built in, so the GPU probes at ~5.5 s inside the initramfs,
before the rootfs firmware exists; the files are therefore bundled into the initramfs
via `/etc/mkinitfs/files-extra/adreno-fw`. After any kernel or firmware update run
`sudo mkinitfs` and reboot. Good boot: dmesg `loaded qcom/a630_sqe.fw from new
location`, no `-2`.

Never live `unbind`/`bind` the `adreno` driver while the display controller is bound
(oopses `drm_self_refresh`, hangs a kernel worker, blocks reboot). Reboot to reload
firmware.

## System layers

```
┌──────────────────────────────────────────────────────────────┐
│ Application: cyberpunk_computer backend (prius.slice, cpu3)   │
│   + roadsense.target (perception stack, ACC only)             │
├──────────────────────────────────────────────────────────────┤
│ IO: USB-CDC (powerbox, gateway, panda), BLE (prius-bled), API │
├──────────────────────────────────────────────────────────────┤
│ Power: prius-power (low | full) ← /etc/prius/power-mode       │
├──────────────────────────────────────────────────────────────┤
│ Net: ModemManager (SIM) + NetworkManager + WireGuard          │
├──────────────────────────────────────────────────────────────┤
│ OS: postmarketOS edge, systemd, kernel 7.1.0-rc1-sdm845       │
├──────────────────────────────────────────────────────────────┤
│ HW: SD845, Adreno 630, SIM, USB OTG host + VBUS, 4.1 V feed   │
└──────────────────────────────────────────────────────────────┘
```
