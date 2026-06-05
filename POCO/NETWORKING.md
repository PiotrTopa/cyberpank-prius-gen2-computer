# Networking — POCO F1 Prius Board Computer

The phone runs on a **cellular SIM** WAN with a **WireGuard** overlay. Remote access
(SSH, API) reaches it at a stable VPN address regardless of the operator-assigned IP.

## Target architecture (production)

```
                       WireGuard server <WG_SERVER_IP> (wg0 10.200.0.1/24, :51820)
                        ▲              ▲              ▲
        ┌───────────────┘              │              └───────────────┐
        │ 10.200.0.5                   │ 10.200.0.7                    │ 10.200.0.6
   ┌────┴─────┐                  ┌─────┴──────┐                  ┌─────┴──────┐
   │ Prius    │  SIM/LTE WAN     │ ThinkPad   │                  │ Build box  │
   │ (POCO F1)│ ───────────────► │ (Windows)  │                  │ (nokia1)   │
   │ prius    │   + WG overlay   └────────────┘                  └────────────┘
   └──────────┘
```

Everything talks over the `10.200.0.0/24` WireGuard overlay. The phone's WAN is the SIM
modem; Wi-Fi is off in production.

## WireGuard peers

| Peer | VPN IP | Public key | Notes |
|------|--------|------------|-------|
| **Prius / phone** | `10.200.0.5` | `bkOdzWb7mjS97FiMOuquR35eew0DZnqOZbzJkLnMqTU=` | NM conn `wg-homelab` |
| **Build box** (nokia1) | `10.200.0.6` | `4F3YI9h16PzSDw4oJF/RgycNtKN/+1iQ2+KRKRPBE14=` | systemd `wg-quick@wg-access` |
| **ThinkPad** (Windows) | `10.200.0.7` | `uGicop7VoUZGDsBw2uJ9TlopZm8k3I18LkxywNqw8ww=` | service `WireGuardTunnel$thinkpad-vpn` |
| _(dead)_ old bridge | `10.200.0.2` | `BdiuYOWO…` | inactive >60 d, ignore |

- **Server:** `root@<WG_SERVER_IP>` (hostname redacted), `wg0 = 10.200.0.1/24`,
  ListenPort `51820`, server pubkey redacted (kept in private `secrets.env` as
  `WG_PEER_PUBLIC_KEY`), config `/etc/wireguard/wg0.conf`, `net.ipv4.ip_forward=1`.
- **Server admin path:** `ssh piotr@<JUMP_HOST_IP>` (jump host `node1`) → `ssh root@<WG_SERVER_IP>`.

> Real server host/IP, jump-host IP and the server public key are intentionally kept
> out of this public repo. They live in the gitignored `secrets.env`
> (`WG_ENDPOINT`, `WG_PEER_PUBLIC_KEY`) or in the operator's private notes.

## Phone network configuration

All managed by **NetworkManager** (+ ModemManager for the modem).

| Connection | Device | Role | Autoconnect |
|-----------|--------|------|-------------|
| `prius-wan` | `qmapmux0.0` (gsm/qrtr0) | **SIM WAN**, APN `internet`, route-metric 800 | yes |
| `wg-homelab` | wireguard | **VPN overlay** to `<WG_SERVER_IP>:51820` | yes |
| `prius-home-wifi` | `wlan0` | Wi-Fi **client** (SSID `in_the_center_of_nowhere`), route-metric 600 | **no** (toggle) |
| `prius-ap` | `wlan0` | Wi-Fi **access point** (SSID `prius`, 2.4 GHz, `10.42.0.1/24` + DHCP) | **no** (toggle) |
| `prius-dev-wifi` | `wlan0` | legacy dev Wi-Fi client (SSID `in_the_middle_of_nowhere`) | **no** (unused) |

### SIM modem (`prius-wan`)
- APN `internet` (ipv4v6), T-Mobile.pl, registered LTE.
- **SIM PIN** stored in the connection (`gsm.pin`) for auto-unlock on boot. The actual
  PIN value lives only in `POCO/secrets.env` (gitignored), never in this repo.
  PIN must target the SIM object: `sudo mmcli -i <SIMidx> --pin=<SIM_PIN>`.
- Data iface `qmapmux0.0` gets a **dynamic operator IP** via DHCP (changes each session —
  irrelevant, access is via the VPN address).
- Route metric **800** so that if Wi-Fi is ever re-enabled (metric 700) it would win in dev;
  in production (no Wi-Fi) the modem is the default WAN.
- ⚠️ Connectivity tests must bind to the **interface** (`ping -I qmapmux0.0 …`), not the
  source IP (reverse-path filtering drops `-I <srcIP>`).
- SIM is in **slot 1** — do not move it.

### WireGuard overlay (`wg-homelab`)
- Config `/etc/wireguard/wg-homelab.conf`, address `10.200.0.5/32`.
- AllowedIPs `10.200.0.0/24, 192.168.0.0/24`, PersistentKeepalive 25.
- If a handshake goes stale after a link change, force a rebind:
  `nmcli con down wg-homelab; nmcli con up wg-homelab`.

## VPN-over-SIM watchdog (`prius-netwatch`) — critical-link self-heal

**Why it exists:** the QMI/rmnet mobile-data bearer can drop **silently**. ModemManager
keeps reporting the modem `connected` (registration `home`, packet `attached`) while the
actual data path is dead, so NetworkManager sees **no state change** and `autoconnect`
never fires — the phone stays stuck with a stale, dead IP/route on `qmapmux0.0`. Observed
**2026-06-05: an 18 h outage** after a 3GPP packet-service detach/attach; the OS was up the
whole time, only the SIM data path was gone, and only a manual reboot recovered it.
In USB **host** mode this VPN-over-SIM tunnel is the **only** remote channel, so a silent
bearer loss must self-heal.

**What it does:** every 60 s it pings the WG server (`10.200.0.1`) *through* the tunnel and
escalates on consecutive failures:

| Consecutive fails | Action |
|---|---|
| 2 | bounce `wg-homelab` (cheap re-handshake) |
| 4 and 7 | bounce `prius-wan` → re-establish the data bearer (new IP/route), then bring `wg-homelab` back up |
| 11 | reboot (last resort; **skipped if uptime < 600 s** to avoid a boot-loop — cycles the WAN instead) |

- Script `/usr/local/sbin/prius-netwatch` — `once` (timer entrypoint), `status`, `check`.
- Fail counter in `/run/prius-netwatch.fail` (tmpfs — resets on boot, resets to 0 on any
  successful check).
- Units `prius-netwatch.service` (oneshot) + `prius-netwatch.timer`
  (`OnBootSec=120`, `OnUnitActiveSec=60`), both **enabled**.
- **Validated:** a `prius-wan` bounce re-establishes the bearer and re-handshakes WireGuard
  in ~3 s (`autoconnect=yes`, `autoconnect-retries=-1` are the safety net if an explicit
  `up` fails).
```sh
sudo prius-netwatch status   # fail counter + live link reachability
sudo prius-netwatch check    # one-shot OK/FAIL
```
- Harmless noise: NetworkManager logs `qrtr0 mtu: failure to set MTU` every ~15 s
  (≈240 lines/h) — longstanding, **not** a fault.

## Wi-Fi toggle (`prius-wifi`)

`wlan0` can be switched between three mutually-exclusive modes **without ever disturbing
the SIM WAN or the WireGuard overlay** (that invariant is the whole point of the script).

| Mode | Effect |
|------|--------|
| `client` | Radio on, AP down, connect `prius-home-wifi` → joins home LAN, gets `192.168.0.x`, default route metric **600** (beats SIM's 800, so LAN/internet prefers Wi-Fi when present). |
| `ap` | Radio on, client down, bring up `prius-ap` → `wlan0 = 10.42.0.1/24`, dnsmasq DHCP, SSID **`prius`** (WPA2, 2.4 GHz). Other devices can join the car's network. |
| `off` | Both connections down, radio off. Production default — modem-only. |

### Usage
```sh
sudo prius-wifi client     # join home Wi-Fi
sudo prius-wifi ap         # become the 'prius' access point
sudo prius-wifi off        # radio off (production)
sudo prius-wifi status     # show current mode + wlan0 state
# or write the flag directly (a systemd path unit re-applies it):
echo client | sudo tee /etc/prius/wifi-mode
```

- Flag file: `/etc/prius/wifi-mode` (`client|ap|off`, default `off`).
- `prius-wifi.service` (oneshot) applies the flag at boot; `prius-wifi.path` watches the
  flag file and re-applies on change. Both enabled.
- **AP PSK** was generated on-device (random 16 chars); read it with
  `nmcli -s -t -f 802-11-wireless-security.psk con show prius-ap`.
- ⚠️ Don't hammer the flag with rapid successive writes — the oneshot can't restart while a
  `con up` is still blocking. Single switches are reliable.
- **Invariant:** the script never touches `prius-wan` or `wg-homelab`. Verified in all three
  modes — WAN + VPN stay up across switches.

## Access paths

| From | Command | Notes |
|------|---------|-------|
| ThinkPad | `ssh user@10.200.0.5` | direct over VPN, ~100 ms |
| Build box (nokia1) | `ssh user@10.200.0.5` | direct over VPN |
| ~~USB lifeline~~ | ~~from box: `ssh user@172.16.42.1`~~ | **RETIRED** — USB is now in OTG host mode (see below) |

### USB lifeline — RETIRED (USB now in OTG host mode)
The USB-gadget recovery network (box `172.16.42.2/24` ↔ phone `172.16.42.1`) is **gone**:
USB has been switched to **OTG host** mode to drive the RP2040 / CAN hardware, which removes
the peripheral gadget. **VPN-over-SIM is now the sole remote channel** — that is exactly why
the `prius-netwatch` watchdog (above) was added.
- To temporarily restore the lifeline for low-level recovery: `sudo prius-usb device --reboot`
  (rebuilds the NCM gadget at boot), then on the box bring the `enx…` iface up with
  `172.16.42.2/24` and `ssh user@172.16.42.1`. Switch back with `sudo prius-usb host --reboot`.
- See **USB host mode** in `SCRIPTS_AND_FILES.md` for the `prius-usb` toggle details.

## Reboot persistence (verified)

After a cold reboot the phone comes back **automatically** in ~65 s, modem-only, VPN up:
SIM auto-unlocks (stored PIN) → modem WAN → WireGuard over cellular → reachable at
`10.200.0.5`. No Wi-Fi. The operator IP differs each session but the VPN address is stable.
