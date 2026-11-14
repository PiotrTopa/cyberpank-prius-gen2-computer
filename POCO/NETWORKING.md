# Networking — POCO F1 car computer

WAN is a cellular SIM; remote access (SSH, API) uses a WireGuard overlay with stable
addresses, independent of the operator-assigned IP. Wi-Fi runs the car's AP.

```
                 WireGuard server <WG_SERVER_IP> (wg0 10.200.0.1/24, :51820)
                  ▲                       ▲                       ▲
                  │ 10.200.0.14           │ 10.200.0.7            │ 10.200.0.6
            ┌─────┴─────┐           ┌─────┴──────┐          ┌─────┴──────┐
            │ car POCO  │ SIM/LTE   │ ThinkPad   │          │ build box  │
            │           │ + WG      │ (Windows)  │          │ (nokia1)   │
            └───────────┘           └────────────┘          └────────────┘
```

## WireGuard

| Peer | VPN IP | Public key | Notes |
|---|---|---|---|
| car POCO | `10.200.0.14` | not recorded here | NM conn `wg-homelab`, address from `WG_ADDRESS` in `secrets.env` |
| build box (nokia1) | `10.200.0.6` | `4F3YI9h16PzSDw4oJF/RgycNtKN/+1iQ2+KRKRPBE14=` | systemd `wg-quick@wg-access` |
| ThinkPad (Windows) | `10.200.0.7` | `uGicop7VoUZGDsBw2uJ9TlopZm8k3I18LkxywNqw8ww=` | service `WireGuardTunnel$thinkpad-vpn` |
| previous car POCO | `10.200.0.5` | `bkOdzWb7mjS97FiMOuquR35eew0DZnqOZbzJkLnMqTU=` | retired |
| old bridge | `10.200.0.2` | `BdiuYOWO…` | dead |

- Server: `wg0 = 10.200.0.1/24`, ListenPort 51820, config `/etc/wireguard/wg0.conf`,
  `net.ipv4.ip_forward=1`. Server host, IP, public key and the jump host stay out of
  this repo (`secrets.env`: `WG_ENDPOINT`, `WG_PEER_PUBLIC_KEY`; private notes).
- Phone side (`wg-homelab`): AllowedIPs `10.200.0.0/24, 192.168.0.0/24`,
  PersistentKeepalive 25, route metric 900, autoconnect yes.
- Stale handshake after a link change: `nmcli con down wg-homelab; nmcli con up wg-homelab`.
- ThinkPad cannot ping 10.200.0.1: restart its tunnel (elevated PowerShell,
  `Restart-Service 'WireGuardTunnel$thinkpad-vpn'`).

## NetworkManager connections

| Connection | Device | Role | Autoconnect |
|---|---|---|---|
| `prius-wan` | `qmapmux0.0` (gsm/qrtr0) | SIM WAN, APN `internet` (ipv4v6), route metric 800 | yes |
| `wg-homelab` | wireguard | VPN overlay | yes |
| `prius-ap` | `wlan0` | AP: SSID `prius`, WPA2, 2.4 GHz (`band=bg`), `10.42.0.1/24`, `method=shared` (dnsmasq DHCP) | no (`prius-wifi`) |
| `prius-home-wifi` | `wlan0` | client, SSID `in_the_center_of_nowhere` (NM default metric 600) | no (`prius-wifi`) |
| `prius-dev-wifi` | `wlan0` | legacy dev client, SSID `in_the_middle_of_nowhere`, metric 700 | no (unused) |

Connections are rendered from `POCO/rootfs/etc/NetworkManager/system-connections/*.tmpl`
with `secrets.env` ([PROVISIONING.md](PROVISIONING.md)). DNS: `90-dns-none.conf`
(`dns=none`) + static `/etc/resolv.conf` (1.1.1.1, 9.9.9.9). `99-unmanage-usb0.conf`
leaves `usb0` (MFD gadget) to the backend.

## SIM and LTE

- Operator T-Mobile.pl, APN `internet`, LTE. `qmapmux0.0` gets a dynamic operator IP
  each session (irrelevant: access is via WireGuard).
- **SIM unlock: `prius-sim-unlock`** (`prius-sim-unlock.service`, oneshot, enabled).
  On this phone the SIM sits in physical slot 2 and ModemManager's unlock addresses
  slot 1 (QMI `Internal` error), so the script verifies PIN1 directly with `qmicli`
  on the provisioning session (`qrtr://0`) and then restarts ModemManager so
  `prius-wan` autoconnects. PIN from `/etc/prius/sim-pin` (root-only, rendered from
  `secrets.env`). **It tries at most once per boot**, only when the card is
  `enabled-not-verified` with all 3 retries left, and never again after a failure
  (stamp `/run/prius-sim-unlock.failed`): a wrong PIN burns one of 3 attempts.
  `prius-wan` also carries `gsm.pin`.
- Manual unlock targets the SIM object: `sudo mmcli -i <SIMidx> --pin=<SIM_PIN>`.
- Connectivity tests must bind to the interface (`ping -I qmapmux0.0 …`), not the
  source IP (reverse-path filtering drops `-I <srcIP>`).
- Harmless: NetworkManager logs `qrtr0 mtu: failure to set MTU` every ~15 s.

## VPN-over-SIM watchdog (`prius-netwatch`)

The QMI/rmnet data bearer can drop silently: ModemManager keeps reporting `connected`,
NetworkManager sees no change, autoconnect never fires, and the phone stays offline
until a reboot (seen: 18 h and ~8 h outages; once under full roadsense load). Only an
OS reboot reliably clears a wedged bearer; a modem reset (`mmcli --reset` + RF toggle)
does not, so there is no modem-reset tier.

Every 60 s it pings `10.200.0.1` through the tunnel (2 pings, 4 s timeout) and
escalates on consecutive failures:

| Fails | Action |
|---|---|
| 2 | bounce `wg-homelab` |
| 4, 6 | bounce the active gsm profile (detected at run time), then bring `wg-homelab` up |
| 8 | reboot; deferred while uptime < 600 s (anti boot-loop) |

- **Runs only with the car off**: `prius-power low` arms it 20 min after key-off
  (`/etc/prius/netwatch-grace`), `full` stops it. `prius-netwatch.timer` has no
  `[Install]` and is never enabled at boot (an always-on netwatch rebooted the phone
  mid-drive). With the key on, a wedged bearer is not auto-healed.
- Every external command is wrapped in `timeout` (25 s) and the service has
  `TimeoutStartSec`, so a hung `nmcli` cannot stall the watchdog.
- Script `/usr/local/sbin/prius-netwatch {once|status|check}`; fail counter
  `/run/prius-netwatch.fail` (reset on boot and on success); a WAN bounce
  re-establishes the bearer and the WireGuard handshake in ~3 s.

## Clock fallback

The PMIC RTC is not persisted (1970 at boot). `systemd-timesyncd` restores an
approximate time and steps via NTP; some APNs block UDP 123, which would leave the
clock stale and WireGuard down. `prius-clock-fallback` (`.timer`: 2 min after boot,
then every 5 min) is a no-op once NTP is synchronized; otherwise it steps the clock
from HTTPS `Date` headers (`htpdate`, ~1 s accuracy).

## Wi-Fi (`prius-wifi`)

| Mode | Effect |
|---|---|
| `ap` (default) | client down, `prius-ap` up: `wlan0 = 10.42.0.1/24`, SSID `prius` |
| `client` | AP down, `prius-home-wifi` up: `192.168.0.x`, default route metric 600 (beats LTE's 800) |
| `off` | both down, radio off |

- Flag `/etc/prius/wifi-mode` (`client|ap|off`, seeded `ap`; unreadable flag → `off`).
  It is the operator's choice only: key-off `low` runs `prius-wifi suspend` after the
  Wi-Fi grace (radio off, flag untouched) and `full` runs `prius-wifi resume`, so the
  choice survives a key cycle ([POWER.md](POWER.md#key-off-timers-owned-by-prius-power)).
- `prius-wifi.service` applies the flag at boot; `prius-wifi.path` re-applies on change.
  Verbs are idempotent and serialised with `flock`. Do not write the flag in rapid
  succession: the oneshot cannot restart while a `con up` blocks.
- Invariant: `prius-wifi` never touches `prius-wan` or `wg-homelab` (verified in all
  three modes).
- AP PSK: generated on the device; `nmcli -s -t -f 802-11-wireless-security.psk con show prius-ap`.

## Firewall

pmOS loads nftables with input policy drop. `/etc/nftables.d/50_prius_backend.nft`
accepts TCP 8080 (backend REST/WebSocket/dashboard) and 8081/8082 (ZeroMQ for the MFD
frontend), and on `wlan*` only UDP 8787 / TCP 8788 (Wi-Fi satellites,
[docs/SATELLITES.md](../docs/SATELLITES.md)). The roadsense API has its own
`50_roadsense.nft` from the roadsense repo. Traffic from the MFD Pi arrives on `usb*`,
accepted by the base ruleset.

## USB network lifeline (retired in host mode)

The USB gadget network (box `172.16.42.2/24` ↔ phone `172.16.42.1`) exists only in USB
device mode. For low-level recovery: `sudo prius-usb device --reboot` (NCM gadget built
by the initramfs), bring the box's `enx…` interface up on `172.16.42.2/24`,
`ssh user@172.16.42.1`; switch back with `sudo prius-usb host --reboot`.

## Boot

After a cold reboot the phone is reachable again in ~65 s: SIM unlock → LTE →
WireGuard over LTE.

## Open gaps

| Gap | What would resolve it |
|---|---|
| Identity of the car POCO. `secrets.env.example`, `ARCHITECTURE` and `scripts/diag_cli.py` use `10.200.0.14` (and `sync.sh` calls `10.200.0.5` "the old car POCO"), but `sync.sh` still defaults `PHONE=user@10.200.0.5` and `WG_ADDRESS=10.200.0.5/32`; `PROVISIONING.md` calls the `192.168.0.86` phone (sudo with password) the bench POCO while `sync.sh` calls it the car POCO. Hostname and sudo mode of the current car phone are therefore unconfirmed. | `ssh` to 10.200.0.14: `hostname`, `sudo -n true`, `wg show`; then fix the `sync.sh` defaults. |
| Public key of the current car POCO's tunnel. | `sudo wg show wg-homelab public-key`. |
| A wedged bearer with the key on is not auto-healed (netwatch is ACC-off only). | Decide on an ACC-on recovery tier that cannot reboot mid-drive. |
