# POCO F1 — car computer (postmarketOS)

The Xiaomi POCO F1 (`beryllium`, SD845) is the car's only computer: headless,
always on, phone battery removed and 4.1 V fed to the battery terminals. Remote
access is LTE + WireGuard. It runs the backend (`prius-backend.service`, API on
8080) and, while ACC is on, the roadsense perception stack (`roadsense.target`,
repos `roadsense` (MIT) and `roadsense-signs` (AGPL), with chris-integrator on top since
lane L44: consolidated API on 8090, roadsense debug face on 8091), which the backend
starts and stops (`backend/roadsense_power.py`). There is no second POCO.

| File | Covers |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | hardware, OS, design constraints, USB host + VBUS, GPU firmware |
| [NETWORKING.md](NETWORKING.md) | SIM WAN, SIM unlock, WireGuard, Wi-Fi modes, netwatch, clock fallback, firewall |
| [POWER.md](POWER.md) | CPU profiles, display blanking, freeze recovery, key-off/key-on sequence, roadsense + camera preview power, USB port power, MFD power |
| [GPU_COMPUTE.md](GPU_COMPUTE.md) | Adreno 630 OpenCL/Vulkan, device caps, libraries |
| [SCRIPTS_AND_FILES.md](SCRIPTS_AND_FILES.md) | every custom script, unit, flag and config on the phone |
| [PROVISIONING.md](PROVISIONING.md) | `rootfs/` overlay, `sync.sh` / `provision.sh` |

Powerbox firmware, relays and flashing: [powerbox/README.md](../powerbox/README.md).

## Operating commands

Login `user` on the WireGuard address (see [NETWORKING.md](NETWORKING.md)):

```bash
ssh user@10.200.0.14
```

```bash
# power profile (flag /etc/prius/power-mode; the backend normally writes it)
sudo prius-power full | low | status

# Wi-Fi (flag /etc/prius/wifi-mode); never touches the SIM WAN or WireGuard
sudo prius-wifi ap | client | off | status
nmcli -s -t -f 802-11-wireless-security.psk con show prius-ap   # AP password

# USB role (DTB + reboot); host is the production role
sudo prius-usb status
sudo prius-usb host --reboot | device --reboot

# health
cat /etc/prius/power-mode; sudo prius-power status
nmcli -t -f NAME,DEVICE,STATE c show --active
ip route | grep default
sudo wg show wg-homelab | grep -E 'endpoint|handshake'
mmcli -m any | grep -iE 'state:|signal'
sudo prius-netwatch status

# reboot and wait for it over the VPN (~65 s: SIM unlock -> LTE -> WireGuard)
ssh user@10.200.0.14 'sudo systemctl --no-block reboot'
until ssh -o ConnectTimeout=5 -o BatchMode=yes user@10.200.0.14 'echo UP'; do sleep 5; done

# WireGuard rebind after a link change
nmcli con down wg-homelab; nmcli con up wg-homelab; ping -c2 10.200.0.1

# GPU compute
RUSTICL_ENABLE=freedreno clinfo | grep -E 'Device Name|Max compute units'   # FD630, 2 CUs
vulkaninfo --summary | grep -E 'deviceName|driverName'                       # Turnip Adreno (TM) 630
```

`reboot &` over SSH gets SIGHUP and does not reboot; use
`systemctl --no-block reboot`.

## Rules

- Never suspend the device (`systemctl suspend`, `freeze`): it drops modem, VPN, SSH
  and the USB serial links.
- Switch power, Wi-Fi and USB role only with `prius-power`, `prius-wifi`,
  `prius-usb` (or their flag files).
- Do not move the SIM (`prius-sim-unlock` addresses the slot it is in).
- In USB host mode there is no USB network lifeline; LTE + WireGuard is the only
  remote channel. `prius-usb device --reboot` restores the lifeline for low-level
  recovery.
- GPU apps must `export RUSTICL_ENABLE=freedreno` or OpenCL sees no device.
- WireGuard server admin path: jump host → WG server (`/etc/wireguard/wg0.conf`,
  `wg show wg0`); real addresses are kept out of this repo.
