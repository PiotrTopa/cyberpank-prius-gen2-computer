# POCO F1 — Prius Board Computer (postmarketOS)

This directory documents the **Xiaomi POCO F1 (`beryllium`, SD845)** that runs the
Prius gen-2 board computer on **postmarketOS**. It exists so we never have to reverse
engineer `systemd`, hunt for flag files, or rediscover the networking layout by hand.

> The phone is the actual on-car computer. It is headless, always-on, powered from the
> AUX battery (the phone battery is amputated; a constant 4.1 V is fed to the battery
> terminals). Access is over a cellular SIM + WireGuard overlay.

## Documents

| File | What it covers |
|------|----------------|
| [ARCHITECTURE.md](ARCHITECTURE.md) | System overview, hardware, OS, design constraints |
| [NETWORKING.md](NETWORKING.md) | SIM WAN, WireGuard peers, access paths, USB lifeline |
| [POWER.md](POWER.md) | `prius-power` low/full profiles, the mode flag, systemd units |
| [GPU_COMPUTE.md](GPU_COMPUTE.md) | Adreno 630 compute (OpenCL/Vulkan), device caps, math libraries |
| [SCRIPTS_AND_FILES.md](SCRIPTS_AND_FILES.md) | Exact paths of every custom script, unit, and config we added |
| [PROVISIONING.md](PROVISIONING.md) | Infra-as-code: `rootfs/` mirror + `sync.sh`/`provision.sh` to push the repo to the phone |
| [ACCESS_CHEATSHEET.md](ACCESS_CHEATSHEET.md) | Copy-paste commands to log in and operate the device |

## TL;DR

- **SSH in (normal):** `ssh user@10.200.0.5` (works from ThinkPad or the build box — both
  are WireGuard peers). The phone is reachable over the SIM + VPN, no Wi-Fi needed.
- **Power:** `sudo prius-power low` (key off / resting) or `sudo prius-power full`
  (key on / full performance). Or just write `low`/`full` to `/etc/prius/power-mode`.
- **Wi-Fi:** `sudo prius-wifi client` (join home Wi-Fi), `ap` (become SSID `prius`), or
  `off` (modem-only, production). Never disturbs the SIM WAN or VPN.
- **GPU compute:** OpenCL 3.0 (`rusticl`) + Vulkan 1.3 (`turnip`) work on the Adreno 630;
  apps must `export RUSTICL_ENABLE=freedreno`. See [GPU_COMPUTE.md](GPU_COMPUTE.md).
- **Login user:** `user`, hostname `prius`, passwordless sudo.
- **Never suspend** the device — it would drop modem, WireGuard, SSH, and OTG serial.
