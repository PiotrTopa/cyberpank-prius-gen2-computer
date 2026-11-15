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
from HTTPS `Date` headers (`htpdate`, ~1 s accuracy) and touches `/run/prius-clock-synced`.

At boot the clock starts at the time timesyncd saved last (2026-11-15: booted at
2026-11-14 03:09, NTP stepped it ~23 h forward 102 s after boot). Everything that ages
data uses the monotonic clock; wall-clock consumers wait for a synchronized clock: the
backend's SNTP server for the MFD Pi (UDP 123 on `usb0`, `backend/ntp_server.py`) answers
"not synchronized" and the VFD satellite's RTC is not set until
`/run/systemd/timesync/synchronized` or `/run/prius-clock-synced` exists.

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

## Wi-Fi AP lifeline (`prius-lifeline`)

Rule: **uplink down ⇒ car AP up**, in every power profile and regardless of the key-off
Wi-Fi suspend, so the phone is always reachable over the `prius` Wi-Fi at `10.42.0.1`
(ssh 22; 8080 with a pairing link for `--host 10.42.0.1`, [docs/AUTH.md](../docs/AUTH.md)).
Added after 2026-11-15: the profile was stuck at `full` while parked (netwatch off), LTE
internet dropped at 15:05 CET, Wi-Fi was suspended, and the phone was unreachable until a
manual reboot at 16:42.

`prius-lifeline.service` (enabled, `Type=simple`, `Restart=always`, `prius.slice`) runs
a loop every 30 s, independent of `prius-power` and of the netwatch ladder:

| Step | Condition | Action |
|---|---|---|
| check | `ping -c 2 -W 3 10.200.0.1` (hub through the tunnel) | — |
| ON | `/etc/prius/lifeline-fails` (default `3`) consecutive misses, i.e. 60–90 s after the loss | marker `/run/prius-lifeline/active`; `prius-wifi lifeline` (radio on, `prius-ap` up, `wifi-mode` flag untouched); journal line `lifeline ON` |
| while ON | every check | AP re-asserted if anything took it down; `prius-wifi suspend` is a no-op (journal `suspend skipped`), `prius-wifi apply` with flag `off` keeps the AP |
| hold | hub answers again | journal `uplink back … holding`; one miss restarts the hold |
| OFF | hub answered without a miss for `/etc/prius/lifeline-hold` (default `10min`) | marker removed; `low`: Wi-Fi suspended now if the key-off grace already ran out (a still-pending grace timer is left to fire), `full`: `prius-wifi apply` (flag mode); journal `lifeline OFF` |

- It never bounces links or reboots; recovery stays with `prius-netwatch` (unchanged). The
  lifeline's ON comes before netwatch's first WireGuard bounce (2 misses at 60 s).
- Client mode (`wifi-mode` = `client`): an associated `prius-home-wifi` that holds a
  default route on `wlan0` is left alone (bench use unchanged); a client without a route,
  or a radio that is off, is switched to the AP for the lifeline. The flag is never
  rewritten, so OFF in `full` returns to client mode. Flag `off` is overridden while ON.
- Reboot (e.g. netwatch's last resort): state is in `/run` (tmpfs), the AP comes up at boot
  through `prius-wifi.service` (flag `ap`), and with the uplink still down the lifeline turns
  ON again 60–90 s after the service starts, before the key-off grace (20 min) can
  suspend Wi-Fi.
- A hub outage (home internet down) with LTE fine also turns the AP on: harmless, costs
  the AP's draw ([POWER.md](POWER.md#key-off-timers-owned-by-prius-power)).
- `prius-lifeline status` (state, fail count, hold progress, uplink, Wi-Fi);
  `prius-lifeline state` prints `on|off`; `prius-wifi status` shows the lifeline line too.
- Test hook (real links untouched): `sudo prius-lifeline simulate-down <1..1800 s>` makes
  the check fail until a deadline kept in `/run/prius-lifeline/simulate-down`;
  `simulate-clear` ends it.
- Installed by `provision.sh` with the car network layer (not with `--core`); `provision.sh`
  seeds `lifeline-hold` / `lifeline-fails` and restarts the service.
- Verified 2026-11-15 on the car phone: simulated loss (hook) → ON 80 s after the start,
  AP `10.42.0.1/24`, OFF 2 min after the end (test hold) with Wi-Fi suspended again; real
  WireGuard drop (`nmcli con down wg-homelab`, time-boxed) → ON after 83 s (wpa_supplicant
  `AP-ENABLED`), netwatch bounced WireGuard at its 2nd miss as before, OFF 10 min after
  the uplink returned. The AP's beacons were received by a laptop; no client has joined
  it yet (open gaps).

## Firewall

pmOS loads nftables with input policy drop. `/etc/nftables.d/50_prius_backend.nft`
accepts TCP 8080 (backend REST/WebSocket/dashboard) and 8081/8082 (ZeroMQ for the MFD
frontend), and on `wlan*` only UDP 8787 / TCP 8788 (Wi-Fi satellites,
[docs/SATELLITES.md](../docs/SATELLITES.md)). Its `prius_guard` table (input hook,
priority -10) drops 8080/8081/8082/8090 on every interface except `lo`, `wg*`, `usb*`
and, for 8080/8090 only, the car AP address `10.42.0.1` (from `10.42.0.0/24`); so Wi-Fi
client mode and LTE never reach them, and roadsense's own `50_roadsense.nft` (8090 accept
everywhere, from the roadsense repo) is confined too. Details and the 8080 token/session
auth: [docs/AUTH.md](../docs/AUTH.md). Traffic from the MFD Pi arrives on `usb*`,
accepted by the base ruleset. `provision.sh` reloads nftables only after `nft -c` accepts
the full ruleset.

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
| A wedged bearer with the key on is not auto-healed (netwatch is ACC-off only); the AP lifeline makes the phone reachable over the `prius` Wi-Fi meanwhile. | Decide on an ACC-on recovery tier that cannot reboot mid-drive. |
| No client has joined the AP during a lifeline yet. 2026-11-15 (real WireGuard drop, car parked outside): the P52's USB card saw the AP beaconing (BSSID = the phone's `wlan0` MAC, channel 1, signal 34–60) but every WPA2 attempt ended in a 4-way-handshake timeout (reason 15, PSK verified identical). | Join `prius` from closer range (in or next to the car) during `prius-lifeline simulate-down 300`; check ping/ssh/8080 over `10.42.0.1`. |
