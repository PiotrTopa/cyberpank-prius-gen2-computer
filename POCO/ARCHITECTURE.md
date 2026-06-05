# Architecture — POCO F1 Prius Board Computer

## Role

The POCO F1 is the **main board computer** of the Prius gen-2 build. It is a headless,
always-on Linux machine mounted in the car. It:

- listens permanently on **USB/OTG virtual COM** events (RP2040 IO + AVC-LAN/CAN gateways via a hub),
- listens permanently on the **network** (SSH login + API callers),
- runs over a **cellular SIM** WAN with a **WireGuard** overlay for remote access,
- scales power between a **low** (ignition off) and **full** (ignition on) profile,
  **without ever suspending** (suspend would drop modem/VPN/SSH/serial).

## Hardware

| Item | Value |
|------|-------|
| Device | Xiaomi POCO F1 / `beryllium` |
| SoC | Snapdragon 845 (SDM845), 8 cores, Adreno 630 GPU |
| CPU clusters | Silver `cpu0-3` (300 MHz–1.766 GHz), Gold `cpu4-7` (0.825–2.65 GHz) |
| GPU | Adreno 630 via **freedreno** (OpenCL/Vulkan; **no CUDA**) |
| Bootloader | unlocked, non-A/B, fastboot serial `6927a690` |
| Power | AUX battery; phone battery amputated, constant **4.1 V** on battery terminals |
| USB | **OTG host-only** target (never charge/power from USB) |

## Operating system

| Item | Value |
|------|-------|
| Distro | postmarketOS **edge** |
| Kernel | `7.1.0-rc1-sdm845` (mainline-based, enables Adreno) |
| Init | systemd |
| Hostname | `prius` |
| Login user | `user` (passwordless sudo via `/etc/sudoers.d/010-user-nopasswd`) |
| UI | none (headless) |
| Timezone | Europe/Warsaw |
| Built with | `pmbootstrap` on WSL2 x86_64, flashed via fastboot from the build box |

## Design constraints / decisions

- **Always-on, never suspend.** `/sys/power/state` supports `freeze`/`mem` but we do **not**
  use them — they would tear down the cellular modem, WireGuard, SSH session, and the
  OTG serial listener. Power saving is done by scaling CPU instead (see [POWER.md](POWER.md)).
- **No battery gauge / coulomb counting.** Battery is amputated; charge % is meaningless.
  We do not read or trust battery state.
- **WAN = cellular SIM.** Wi-Fi is `off` in production, but a `prius-wifi` toggle can put the
  single radio into **client** (join home Wi-Fi) or **AP** (serve SSID `prius`) mode on
  demand without disturbing the SIM WAN or VPN. Remote access normally rides the SIM + VPN.
- **USB = OTG host only** (future): RP2040 IO board + AVC-LAN/CAN gateways behind a hub.
  ⚠️ Enabling host mode will drop the current USB lifeline to the build box — only do it
  once VPN-over-SIM is proven as the sole channel.
  ✅ **VBUS host power (SOLVED 2026-06-05):** the phone **now sources its own 5 V VBUS** on
  the USB-C port. The PMI8998 has a USB-OTG boost converter at SPMI offset `0x1100` whose
  register layout is identical to the PM8150B, so the in-tree generic
  `qcom_usb_vbus-regulator` driver (which is chip-agnostic — it only reads `reg`/`compatible`
  from DT) drives it unchanged. We added a regulator node to the DTB; no kernel rebuild and
  no role-switch driver are needed (role is fixed `host` via `dr_mode`). Result: devices
  enumerate directly with **no external power / Y-cable** (pendrive verified high-speed,
  4 MB read @ 13.4 MB/s, no over-current). See **VBUS host-power reproduction** below.
  History: before this fix the phone could not source VBUS and a host enumerated nothing
  (floating D+/D- → phantom full-speed + `error -71`). Fallback if ever needed: **flip
  roles** — RP2040/RP2350 as USB host, phone as CDC-ACM gadget (`/dev/ttyGS0`).

### VBUS host-power reproduction (re-apply after a clean kernel/DTB update)

A `linux-postmarketos-qcom-sdm845` update reinstalls a pristine DTB, dropping **both** the
`dr_mode=host` patch and the VBUS regulator node. Re-apply both (`prius-usb host --reboot`
handles `dr_mode`; the steps below re-add VBUS). The reusable artifacts live in the repo:
[POCO/dts/pmi8998-usb-vbus.dtso](dts/pmi8998-usb-vbus.dtso) (the node) and
[POCO/rootfs/usr/local/sbin/prius-vbus](rootfs/usr/local/sbin/prius-vbus) (an idempotent apply script).

```bash
ssh user@10.200.0.5            # all steps sudo; active DTB is the Tianma panel variant
DTB=/boot/dtbs/qcom/sdm845-xiaomi-beryllium-tianma.dtb
sudo install -d /root/boot-backup/vbus-$(date +%Y%m%d-%H%M%S)
sudo cp -a /boot/boot.img "$DTB" /root/boot-backup/vbus-*/   # backup first
sudo dtc -I dtb -O dts -o /tmp/t.dts "$DTB"                  # decompile
# Insert this node INSIDE pmic@2 (it has #address-cells=1 #size-cells=0), before charger@1000:
#   usb_vbus: usb-vbus-regulator@1100 {
#       compatible = "qcom,pm8150b-vbus-reg";
#       reg = <0x1100>;
#       regulator-min-microamp = <500000>;
#       regulator-max-microamp = <3000000>;
#       regulator-always-on;
#       regulator-boot-on;
#   };
sudo dtc -I dts -O dtb -o "$DTB" /tmp/t.new.dts             # recompile in place
sudo mkinitfs                                              # repack + flash boot.img
sudo systemctl --no-block reboot   # NOTE: `reboot &` over ssh gets SIGHUP and won't reboot
```

Verify after reboot (`~90 s` to rejoin VPN):
```bash
ls /proc/device-tree/soc@0/spmi@c440000/pmic@2/ | grep vbus        # node present
grep -l usb_vbus /sys/class/regulator/*/name                       # regulator exists
cat /sys/class/regulator/<that>/state                              # -> enabled
lsusb && ls /dev/sd*                                              # plugged device enumerates
```
Why it works: `regulator-always-on` makes the regulator core write `OTG_EN` (`CMD_OTG` at
base `+0x40`) at boot, so PMI8998 boosts 5 V onto USBIN; with `dr_mode=host` the dwc3 xHCI
host then enumerates whatever is plugged in. Revert by restoring
`/root/boot-backup/vbus-*/boot.img.pre-vbus` (or removing the node + `mkinitfs` + reboot).
- **GPU compute** (point clouds, matrices) uses OpenCL/Vulkan via freedreno. **Never CUDA**
  (Adreno is not NVIDIA). The stack is installed and working — see
  [GPU_COMPUTE.md](GPU_COMPUTE.md).

## Display

Headless. Backlight is forced to 0 and held there:
- `/etc/systemd/system/backlight-off.service` (enabled),
- `systemd-backlight@backlight:backlight.service` masked.

## GPU

Adreno 630 via `msm`/freedreno. The microcode (`a630_sqe.fw` + `a630_gmu.bin`) is installed
(apk `firmware-qcom-adreno-a630-sqe` + `firmware-qcom-adreno-a630`) under `/lib/firmware/qcom/`.

✅ **Firmware loads cleanly at boot.** Root cause of the earlier `-2` failure: `CONFIG_DRM_MSM=y`
is built-in, so the GPU probes at ~5.5 s **inside the initramfs**, before `/lib/firmware/qcom`
exists. Fix: the firmware is bundled into the initramfs via
`/etc/mkinitfs/files-extra/adreno-fw` (re-run `sudo mkinitfs` + reboot after any kernel/fw
update). Verified: dmesg shows `loaded qcom/a630_sqe.fw from new location`, no `-2`.

✅ **Compute userspace installed & working:** OpenCL 3.0 (`rusticl`/`FD630`) and Vulkan 1.3
(`turnip`/Adreno 630), Mesa 26.1.1 + LLVM 22. Render node `/dev/dri/renderD128`. Full device
caps, env vars (`RUSTICL_ENABLE=freedreno`) and recommended math libraries are documented in
[GPU_COMPUTE.md](GPU_COMPUTE.md).

⚠️ Never live `unbind`/`bind` the `adreno` driver while the display-controller is bound
(it hangs a DRM kernel worker and blocks reboot). Reload firmware by rebooting instead.

## System layers

```
┌──────────────────────────────────────────────────────────────┐
│ Application: cyberpunk_computer (board computer app)          │
├──────────────────────────────────────────────────────────────┤
│ IO listeners:  OTG/USB virtual COM (serial)   +   SSH / API   │
├──────────────────────────────────────────────────────────────┤
│ Power: prius-power  (low | full)  ← /etc/prius/power-mode flag │
├──────────────────────────────────────────────────────────────┤
│ Net: ModemManager (SIM WAN) + NetworkManager + WireGuard      │
├──────────────────────────────────────────────────────────────┤
│ OS: postmarketOS edge, systemd, kernel 7.1.0-rc1-sdm845       │
├──────────────────────────────────────────────────────────────┤
│ HW: SD845, 8 cores, Adreno 630, SIM, USB-OTG, AUX 4.1 V       │
└──────────────────────────────────────────────────────────────┘
```

See [NETWORKING.md](NETWORKING.md), [POWER.md](POWER.md), and
[SCRIPTS_AND_FILES.md](SCRIPTS_AND_FILES.md) for the concrete moving parts.
