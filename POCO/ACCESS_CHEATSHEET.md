# Access Cheatsheet — POCO F1 Prius Board Computer

Copy-paste commands. Device user `user`, hostname `prius`, passwordless sudo.

## Log in

```bash
# Normal (over SIM + WireGuard) — from ThinkPad or the build box (both are WG peers)
ssh user@10.200.0.5

# NOTE: the USB lifeline (172.16.42.1) is RETIRED — USB is now in OTG host mode.
# VPN-over-SIM is the sole remote channel. To temporarily restore the lifeline:
#   sudo prius-usb device --reboot   (then on the box bring the enx... iface up on 172.16.42.2/24)
#   sudo prius-usb host   --reboot   (switch back to host mode)

# If the ThinkPad itself can't even ping the WG gateway (10.200.0.1), restart its
# local tunnel (Windows, needs UAC elevation):
#   powershell Start-Process powershell -Verb RunAs -ArgumentList '-Command',\
#     "Restart-Service 'WireGuardTunnel$thinkpad-vpn'"
```

## Power profile

```bash
sudo prius-power full      # key ON  -> all 8 cores, full clocks
sudo prius-power low       # key OFF -> 4 cores (cpu0-3), capped 1.2 GHz, rest
sudo prius-power status    # show current state, no change

# equivalent (flag-only; systemd path watcher applies in ~1-2 s):
echo full | sudo tee /etc/prius/power-mode
echo low  | sudo tee /etc/prius/power-mode
```

## Wi-Fi toggle

```bash
sudo prius-wifi client     # join home Wi-Fi (SSID in_the_center_of_nowhere)
sudo prius-wifi ap         # become access point (SSID 'prius', 2.4 GHz, 10.42.0.1/24)
sudo prius-wifi off        # radio off (production default)
sudo prius-wifi status     # show current mode + wlan0 state

# flag-only (systemd path watcher applies):
echo client | sudo tee /etc/prius/wifi-mode
# never disturbs prius-wan (SIM) or wg-homelab (VPN)
# AP password: nmcli -s -t -f 802-11-wireless-security.psk con show prius-ap
```

## GPU compute

```bash
# OpenCL (rusticl) — apps MUST export this env var to see the device:
export RUSTICL_ENABLE=freedreno
clinfo | grep -E 'Device Name|Max compute units'   # -> FD630, 2 CUs

# Vulkan (turnip):
vulkaninfo --summary | grep -E 'deviceName|driverName'   # -> Turnip Adreno (TM) 630
```

## Health checks

```bash
# everything at a glance
hostname; uptime
cat /etc/prius/power-mode; sudo prius-power status

# network
nmcli -t -f NAME,DEVICE,STATE c show --active
ip route | grep default
sudo wg show wg-homelab | grep -E 'endpoint|handshake'
mmcli -m any | grep -iE 'state:|signal'
```

## Reboot (and expect it back over VPN)

```bash
ssh user@10.200.0.5 'sudo systemctl reboot'
# comes back automatically in ~65 s: SIM auto-unlock -> modem WAN -> WireGuard -> 10.200.0.5
# poll:
until ssh -o ConnectTimeout=5 -o BatchMode=yes user@10.200.0.5 'echo UP'; do sleep 5; done
```

## SIM / modem

```bash
mmcli -m any                                  # modem status
mmcli -m any | grep -i 'state:'               # connected?
# PIN is stored in NM conn prius-wan (gsm.pin) -> auto-unlock on boot.
# The actual PIN lives only in POCO/secrets.env (gitignored), not in this repo.
# manual unlock if ever needed (target the SIM object, not the modem):
sudo mmcli -i <SIMidx> --pin=<SIM_PIN>
```

## WireGuard rebind (if handshake goes stale after a link change)

```bash
nmcli con down wg-homelab
nmcli con up   wg-homelab
ping -c2 10.200.0.1
```

## VPN-over-SIM watchdog (`prius-netwatch`)

```bash
sudo prius-netwatch status   # fail counter + live link reachability
sudo prius-netwatch check    # one-shot OK/FAIL
sudo journalctl -u prius-netwatch.service -n 20   # recent watchdog actions
# Auto-heals a SILENT SIM bearer drop (ModemManager stays 'connected' but data is dead):
# every 60 s pings 10.200.0.1, then bounce-wg -> bounce-wan -> reboot on sustained failure.
```

## USB role (host / device)

```bash
sudo prius-usb status            # live + DTB dr_mode, downstream USB devices
sudo prius-usb host   --reboot   # OTG host (drive RP2040/CAN) — kills USB lifeline
sudo prius-usb device --reboot   # peripheral — restores USB-network lifeline
# role is set in the DTB (no runtime switch on dwc3-qcom-legacy) -> needs reboot.
# after a kernel update the DTB resets to peripheral -> re-run `prius-usb host --reboot`.
# VBUS: phone DOES source its own 5V now (PMI8998 OTG boost enabled via a DTS regulator
#       node — see ARCHITECTURE.md "VBUS host power"). Devices enumerate with NO external
#       power / Y-cable. This survives reboots; it does NOT survive a fresh DTB from a
#       kernel update -> re-apply the vbus node (steps in ARCHITECTURE.md) if USB host
#       stops sourcing 5V (symptom: device unpowered, dmesg full-speed + error -71).
```

## Server-side (WireGuard admin)

```bash
ssh piotr@192.168.0.74        # jump host node1
ssh root@46.224.54.21         # WG server; config /etc/wireguard/wg0.conf
wg show wg0
```

## Do / Don't

- ✅ Switch power with `prius-power` or the flag.
- ✅ Switch Wi-Fi with `prius-wifi client|ap|off` — it never touches the SIM WAN or VPN.
- ✅ Switch USB role with `prius-usb host|device --reboot` (DTB + reboot).
- ✅ Trust `prius-netwatch` to auto-recover a silent SIM bearer drop; check it with
      `sudo prius-netwatch status` if the phone ever goes quiet.
- ❌ Don't suspend the device (`systemctl suspend` / `freeze`) — drops modem/VPN/SSH/serial.
- ❌ Don't move the SIM out of slot 1.
- ❌ Don't expect the USB lifeline — it's gone in host mode; use `prius-usb device --reboot`
      to bring it back for low-level recovery.
