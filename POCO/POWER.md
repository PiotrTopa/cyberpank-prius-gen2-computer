# Power — POCO F1 board computer

Host-side power: CPU profiles, display blanking, freeze recovery, the ACC-driven
key-off/key-on sequence, roadsense and camera-preview power, USB port power and the
powerbox link. The powerbox itself (pins, relay board, wake ladder, heartbeat
protocol, firmware flashing) is documented in
[powerbox/README.md](../powerbox/README.md).

## Never suspend

The phone is always on. System suspend (`freeze`/`mem`) is not used: it tears down
the modem, WireGuard, SSH and the USB serial links. Invariants in every profile:
reachable over SSH, modem on, USB on, WireGuard up. Power saving = CPU scaling only.

## CPU profiles

| Profile | When | What it does | `cpus online` |
|---|---|---|---|
| `low` | key off | Gold cluster (`cpu4-7`) offline; Silver `schedutil`, `policy0` max `1228800` (1.2 GHz); arms the Wi-Fi suspend and netwatch timers (below) | `0-3` |
| `full` | key on | all cores online, `schedutil`, each policy max = `cpuinfo_max_freq`; cancels both timers, `prius-wifi resume` | `0-7` |

SD845 facts: `policy0` = `cpu0-3` Silver (300 MHz – 1.766 GHz), `policy4` = `cpu4-7`
Gold (0.825 – 2.65 GHz). Governors: `ondemand userspace performance schedutil` (no
`powersave`), hence offlining + capping. The GPU (`5000000.gpu`, freedreno) idles to
`cur=0` by itself (`simple_ondemand`). `cpu3` is reserved for `prius.slice` (backend)
and stays online in both profiles. Savings of `low` were not measured; `low` limits
work on the phone (compiling) to 4 × 1.2 GHz, so use `full` for that.

| Item | Value |
|---|---|
| Flag | `/etc/prius/power-mode` = `low` \| `full`; single source of truth; seeded `full` by `provision.sh` if missing |
| Script | `/usr/local/sbin/prius-power {low\|full\|apply\|status}` (`low`/`full` write the flag and apply) |
| Units | `prius-power.service` (oneshot, `prius-power apply`, on boot) + `prius-power.path` (re-applies within ~1–2 s of a flag change); both enabled |
| Writer | the backend (`PowerModeRule`) at key-on, and at key-off only once the roadsense stack is down |

```bash
sudo prius-power full                     # or: echo full | sudo tee /etc/prius/power-mode
sudo prius-power low
sudo prius-power status                   # cores, governors, clocks, pending Wi-Fi suspend, display
```

Verified: in `low` the modem stays connected, WireGuard up, default route via
`qmapmux0.0`, SSH responsive; the path watcher switches `0-3` ↔ `0-7`; the flag
survives reboot.

### Key-off timers (owned by `prius-power`)

| Timer | Grace file (systemd time span) | Default | Effect |
|---|---|---|---|
| `prius-wifi-suspend` (transient, `systemd-run`) | `/etc/prius/wifi-grace` | `20min` (`0` = immediate) | runs `prius-wifi suspend` (radio off, `wifi-mode` flag kept); the AP stays usable for 20 min after key-off |
| `prius-netwatch-arm` (transient) | `/etc/prius/netwatch-grace` | `20min` | starts `prius-netwatch.timer`; netwatch runs **only with the car off** |

Every `low` re-arms the full period; `full` cancels both and stops netwatch. A boot
with the flag at `low` also gets the grace. Netwatch is never enabled at boot: an
always-on netwatch rebooted the phone mid-drive. See [NETWORKING.md](NETWORKING.md).

## Display blanking (opt-in)

The phone is headless; the backlight is held at 0 (`backlight-off.service`,
`systemd-backlight@backlight:backlight.service` masked). On the mainline sdm845 kernel
the DSI pipeline never power-collapses while fbcon holds the CRTC: the DSI PLL
(~1.9 GHz VCO) and MDSS clocks keep running. `dpms = off` is `EPERM` under atomic KMS
and unbinding fbcon does not stop the clocks.

`prius-blankd` (`prius-blank.service`, not enabled) does a real modeset-off: becomes
DRM master on `/dev/dri/card0`, disables every CRTC (`DRM_IOCTL_MODE_SETCRTC`,
`fb_id=0`), which powers down the DSI PHY/PLL and the panel bias regulators
(`labibb`), unbinds the fbcon vtconsole and holds master. Raw DRM ioctls via
`ctypes`, no libdrm. Saving: **~0.19 W (−8 %, 2.29 → 2.11 W resting)**, INA219 clean
idle A/B, corroborated by `poco_power` (−14 %). Wi-Fi off saved only ~37 mW. Blanking
does not touch the Adreno GPU (`/dev/dri/renderD128` stays usable) unless labibb is
unbound (below).

**Blanking is OFF by default.** `prius-power` starts `prius-blank` in either profile
only when `/etc/prius/blank-display` exists. On kernel 7.1 with roadsense, blanking
leaves the LAB/IBB over-current IRQ storming (~4600/s), unbinding labibb takes the GPU
down, and the blanked panel is the suspect for `systemctl reboot` hangs.

### LAB over-current IRQ storm

With the panel unprepared, the pmi8998 labibb driver leaves its level-triggered
`lab-over-current` IRQ (IRQ 87, `pmic_arb`) enabled while LAB is off; it storms
(~13 kHz on the old kernel, ~4600/s on 7.1) and floods the SPMI/PMIC arbiter. It once
caused a 6-hour system-wide hang (journald/timesyncd/`sync` in D-state).

Rules:

- **Never rebind labibb at runtime.** The panel driver keeps stale regulator handles
  and the next panel prepare hard-hangs the kernel (reproduced; recovered only by a
  powerbox force press).
- **No runtime display restore.** `prius-power` refuses it; to use the panel, reboot
  with `prius-blank` not running (masked, or without `/etc/prius/blank-display`).
- `prius-blankd` unbinds labibb (frees the IRQ) per `PRIUS_BLANK_LABIBB`:
  `never` (default; it logs the LAB/IBB IRQ rate after blanking), `auto` (only above
  `PRIUS_BLANK_STORM_HZ`, default 1000), `always` (headless box without GPU work).
  Unbinding labibb cascades into `msm_drm_unbind` + `adreno_unbind`: no GPU, no
  roadsense.
- Never live `unbind`/`bind` the `adreno` driver while the display controller is
  bound (hangs a DRM worker, blocks reboot). Reload GPU firmware by rebooting.

## Freeze recovery

The SoC has hard-frozen with no panic and no trace (kernel dead, no network, journal
stops). Layers, fastest first:

1. **APSS hardware watchdog** (`qcom_wdt`, `/dev/watchdog0`), petted by systemd:
   `/etc/systemd/system.conf.d/10-watchdog.conf` `RuntimeWatchdogSec=30`,
   `RebootWatchdogSec=30`. `qcom_wdt` rejects timeouts above ~37 s (60 s → EINVAL).
2. **Panic on oops:** `/etc/sysctl.d/10-lockup-panic.conf` `kernel.panic=10`,
   `kernel.panic_on_oops=1` (softlockup / hung-task detectors are not built into this
   kernel).
3. **Powerbox wake ladder:** heartbeat lost → 6 s presses (≤ 1 per 5 min), after two
   unanswered presses a 12 s force press. A frozen SoC ignores short presses. Details
   in [powerbox/README.md](../powerbox/README.md#poco-power-button-and-wake-ladder).

## roadsense stack (ACC only)

`roadsense.target` (camerad-dvr, modeld, envd, api + the roadsense-signs `signd`)
runs on this phone only while ACC is on; it is not enabled at boot. The backend's
`RoadsenseManager` (`backend/roadsense_power.py`, env `BACKEND_ROADSENSE_*`, commands
`roadsense_mode auto|on|off` and `roadsense_restart`, state `roadsense`):

1. key on → relay ch4 (camera hub) on → 8 s settle →
   `systemctl start --no-block roadsense.target` → `/v1/health` on `127.0.0.1:8090`
   polled from a worker thread;
2. key off → 30 s grace → `systemctl stop --no-block roadsense.target` → wait until
   every unit is inactive (60 s cap) → relay ch4 off;
3. watchdog while running: `systemctl is-active` per unit + the health probe; an API
   silent for 180 s restarts the target (≤ 1 restart per 300 s);
4. CPU coupling: the stack pins cpu4-7 (modeld/signd/envd), which `low` takes
   offline. While the manager is active the backend holds `full`; at key-off
   `PowerModeRule` hands the `low` switch to the manager (`hold_low`), applied once
   the stack is off. Mode `on` (bench) keeps `full` with the key off;
5. resources: `roadsense.slice` (roadsense repo: CPUWeight 100, MemoryHigh 3G,
   MemoryMax 3584M, AllowedCPUs 0-2,4-7); `prius-backend.service` in `prius.slice`
   (cpu3, CPUWeight 1000, Nice −10, MemoryMax 512M);
6. thermals: every tick samples all `/sys/class/thermal` zones into `roadsense.temps`
   (+ `hottest_zone` / `hottest_c`) whether or not the stack runs.

The sign provider is AGPL and reached only over its sockets; this repo only drives
`systemctl` and reads JSON over HTTP.

### Dashboard camera preview (on demand, `backend/camera_preview.py`)

Overview tab **Camera** panel: START takes a *preview lease*, the page shows a
downscaled still about once per second, and when the last lease ends the camera
returns to what it would be without the preview (after a 3-min grace unless STOP was
pressed). REST (`api/camera_preview_routes.py`, same auth as `/api/v1`):

| Method + path | What |
|---|---|
| `POST /api/v1/camera/preview/lease` `{}` / `{"lease_id": id}` / `{"resume": true}` | start (explicit START) / renew / resume the current viewing → `{lease_id, ttl_s, expires_in_s, max_session_s, session_left_s, phase, reason, source, leases, resumed}` |
| `DELETE /api/v1/camera/preview/lease/{id}[?stop=1]` | release: soft (grace keeps the camera) or, with `stop=1`, the user's STOP (camera off now; also ends a running grace, any id). `POST …/lease/{id}/release[?stop=1]` is the `navigator.sendBeacon` form |
| `GET /api/v1/camera/preview/frame.jpg?w=640&q=70[&lease=id]` | latest frame; `ETag` (304 on `If-None-Match`), `X-Frame-Age`, `X-Frame-Time`, `X-Frame-Seq`, `X-Frame-Source` (`stack`/`direct`), `X-Preview-Scaled`; 503 + `Retry-After` + `{detail:{phase,reason}}` while the camera comes up; 409 without a lease |
| `GET /api/v1/camera/preview` or `GET /api/v1/camera/preview/` | status (same dict as the state field) |

Refusals: 409 `undervoltage`, 409 `not_resumable` (resume with no viewing to
continue: never a cold start), 410 `expired` / `max_session` (press again; no
cooldown), 429 `too_many_leases` (8). State `roadsense.camera_preview` =
`{enabled, active, phase, reason, camera_phase, source, leases, started_at, grace,
grace_left_s, session_left_s, ttl_s, max_session_s, grace_s, scaling}`; phases
`off → powering → enumerating → waiting_for_frame → live`, `error` (with `reason`;
the reader keeps trying), and `grace` (no lease, camera kept on; `camera_phase` = what
the camera path is doing, `grace_left_s` = how long relay 4 stays on).

* **Grace (`BACKEND_CAMERA_PREVIEW_GRACE_S`, default 180, 0 = off).** When the last
  lease ends softly (page hidden, `pagehide`/unload beacon, unmount, TTL expiry, the
  session cap) nothing is switched: the hold stays, relay 4 stays on and the reader
  keeps reading, so a lease taken within the grace is `live` at once. When the grace
  runs out the hold is dropped and the manager cuts relay 4. Ended at once (no grace):
  STOP (`?stop=1`), 12 V under-voltage, backend shutdown, and stack-has-the-camera
  (nothing to keep; the stack is never touched). ACC on during a grace hands the
  camera to the stack and ends the grace.
* **Session cap = lease time per viewing.** A viewing runs from an explicit START until
  the camera is released. Every lease of the viewing (renewals and resumes: tab visible
  again, reload, re-opened page, lapsed lease) ends at START + `max_session_s`
  (10 min); a resume after the cap is refused (`max_session`). At most one grace
  follows, so the camera is on ≤ 10 + 3 min per viewing without a button press.
  START again (also during the grace: instant) starts a new viewing.
* **Client:** stops fetching at once when hidden and keeps a "was previewing" marker in
  `localStorage`. On becoming visible, reload or re-open it resumes by itself only with
  that marker (< 15 min old) and only if `GET /api/v1/camera/preview` reports `grace`
  or an active preview. While paused it shows "Paused — camera kept on for m:ss" with
  Resume / Off now. A frame is greyed out ("camera re-enumerating… — frame N s old"
  or similar) when the phase is not `live` or the frame is older than 5 s.
* **Lease rules:** TTL 15 s without renewal (page renews every 5 s), cap as above
  (`BACKEND_CAMERA_PREVIEW_TTL_S`, `…_MAX_SESSION_S`, `…_GRACE_S`, `…_CAPTURE_S`,
  `…_ENABLED=0` disables). The page sends a DELETE (keepalive; `?stop=1` only for STOP)
  on stop/unmount and a `sendBeacon` release on `pagehide`; the server TTL is the
  guarantee if all of that is lost on LTE.
* **Stack running (ACC on):** nothing is powered or started. Frames are the stack's own
  `/v1/frame.jpg` (camerad-dvr's copy, ~10/s), fetched at most every 0.25 s and shared
  between viewers. No second block-device reader: camerad-dvr opens the disk `O_EXCL`,
  and a second reader would steal USB bandwidth.
* **Stack down (car off):** the lease sets the manager's *preview hold*: `off → preview`
  = relay ch4 on, **no** roadsense unit, **no** `full` profile (PREVIEW counts as off
  for the deferred `low` switch; a manual `set_relay` ch4 is still refused). The backend
  reads the camera itself, read-only, with the camerad-dvr protocol (1902:3241 found
  via sysfs, raw disk `O_RDONLY|O_DIRECT|O_EXCL`, MBR → layout, info sector at LBA
  0x380/0x300, frame at 0x391/0x311, check word, EOI trim) at 2 frames/s. Zero info
  sectors right after power-on → `waiting_for_frame`. The device can vanish while live
  (seen on the car: live at +7 s, `info read failed … short read 0/512` at ~+29 s;
  the node may come back under another name, sdg → sdh, and read-fail for a few seconds
  first): the reader closes, goes `enumerating`, rescans sysfs once per second trying
  every 1902:3241 disk (a stale node next to the new one is skipped), logs every 10 s
  (`open attempt N: …`) and "camera back on /dev/sdX after N attempt(s)". ACC on during
  a preview: the manager calls the preview's `release_device` right before
  `systemctl start` (no settle) and the preview switches to the stack's frames. ACC off
  during a preview: the normal grace + stop runs, then the manager stays in `preview`
  instead of cutting ch4. Last lease gone: `preview → off`, ch4 cut.
* **Safety:** no preview-driven power-up while `powerbox.undervoltage` (or a shutdown
  request) is set; a direct preview already running is ended and ch4 released (viewing
  a running stack stays allowed). Backend shutdown drops every lease, closes the device
  and cuts a preview-only ch4 (a running stack is left alone).
* **Downscaling:** Pillow (`py3-pillow`, `POCO/packages.txt`) in JPEG draft mode (DCT
  decode at 1/2 then a small resize; 6–8 ms per 1280×720 frame on the laptop, not
  measured on the phone); without Pillow frames are served full size (~50–65 KB,
  `X-Preview-Scaled: 0`). The page asks for 640 px and drops to 480/320 px and up to
  one frame per 5 s when frames arrive slowly.
* **Battery (car off):** relay ch4 draws +84 mA at 12 V (≈ 1 W) for the VIA hub +
  dashcam, so a full 10-min session costs ≈ 14 mAh, on top of the POCO's idle draw
  (the preview keeps `low`; reading two ~60 KB frames/s is not measured on the phone).
  The dashcam may record to its TF card while powered.

## Key-off / key-on sequence

Measured on the bench with the full stack. Everything follows `powerbox.acc_on`; ACC
off is debounced `BACKEND_ACC_OFF_DEBOUNCE_S` (default 3 s), ACC on is immediate.

| t (key-off) | what | owner |
|---|---|---|
| 0 s | ACC off seen; `low` held back while roadsense runs | `PowerModeRule` + `_roadsense_holds_low` |
| +10 s | satellite rail (relay ch5) off after linger; gateway relay ch3 off | `SatellitePowerRule`, gateway ACC follower |
| +30 s | roadsense grace over → `roadsense.target` stopped (~0.3 s) | `RoadsenseManager` |
| +35 s | camera relay ch4 off → `low` applied (cpu4-7 offline, Wi-Fi suspend and netwatch timers armed) | `RoadsenseManager` → deferred switch |
| +20 min | Wi-Fi suspended; netwatch armed | `prius-power` timers |
| +1200 s | MFD Pi (dark standby since key-off: UI frozen, screen blanked): `poweroff` over SSH, 45 s later relay ch2 off | `MfdPowerManager` |

| t (key-on) | what |
|---|---|
| 0 s | `full` written (cores online within 0.3 s, Wi-Fi back ~4 s); satellite + gateway relays on |
| +2 s | MFD relay on (boot ≈ 55 s); camera relay on |
| +13 s | `roadsense.target` started (8 s camera settle) → running at ≈ +25 s |

Rules encoded in `backend/port_power.py` and the backend:

- **Relay port OFF = VBUS first, data-off after the disconnect; ON = data-enable first,
  then the relay.** Disabling the hub port first hides the disconnect from the kernel:
  ~10 s of `clear tt` errors, then the kernel re-enables the port. A device left
  data-connected on an unpowered socket back-feeds through its ESD diodes and
  destabilises the whole hub, hence data-off whenever VBUS is off.
- **Relay commands are lossy; state is verified.** Controllers record the desired
  state; `enforce()` compares it with the powerbox `rly` read-back (~15 s) and
  re-issues until reality matches; plus a re-assert ~6 s after `POWERBOX_READY`.
- **Gateway auto-recovery counts from the VBUS-on edge**, not from the last heartbeat,
  so key-on does not cold-cycle a gateway that was silent only because its relay was
  off.
- **A backend (re)started at key-off adopts the gateway at key-on:** its serial port
  starts on a pending placeholder and the reader thread connects when the hotplug
  monitor retargets it.

## USB hub and port power

Topology as seeded into `/etc/prius/backend.env` by `provision.sh` (see *Open gaps*):

| Hub `1-1` port | Device | Power | Discovery |
|---|---|---|---|
| 1 | powerbox (by-id `usb-MicroPython_Board_in_FS_mode_503359277a7c699f-if00`) | hub VBUS (12 V via relay 1 NC upstream) | by port (`BACKEND_USB_PORT_ROLES=1:powerbox`) |
| 2 | MFD Pi Zero v1.3 (USB gadget `usb0`, 192.168.100.2) | relay ch2 | `BACKEND_MFD_PORT=2` |
| 3 | CAN hub `1a40:0101`: gateway + panda | relay ch3 | gateway one hub level down → `whoami` probing (`BACKEND_USB_PROBE_FALLBACK=1`) |
| 4 | VIA camera hub `2109:2817` (dashcam) | relay ch4 | — |

`BACKEND_PP_{POWERBOX,MFD,GATEWAY,CAMERA}_PORT` map the port-power controllers to
these ports. Changing ports = edit `/etc/prius/backend.env` + restart the backend.
Port-based discovery binds the right board even while it is silent and is immune to
ACM renumbering; `whoami` probing is the fallback for unmapped ports.

`prius-usb-power` (POSIX sh, wraps `uhubctl -f`; apk `uhubctl`): the hub descriptor
claims "ganged", so per-port commands need `-f`.

```bash
sudo prius-usb-power status              # hubs, ports, attached devices
sudo prius-usb-power locate [TARGET]     # resolved "HUB PORT", e.g. "1-1 1"
sudo prius-usb-power cycle  [TARGET]     # off DELAY s -> on (PRIUS_USB_CYCLE_DELAY)
sudo prius-usb-power off|on [TARGET]
```

`TARGET` = tty path, `/dev/serial/by-id/...` (preferred), `"LOCATION PORT"`, or
omitted (powerbox by-id). Resolution via sysfs (`/sys/class/tty/<n>/device` →
`…/1-1/1-1.1/…` → hub `1-1`, port `1`). On a socket without switchable VBUS, `off` /
`cycle` is a data-only disconnect.

### USB link to the powerbox

- **Staleness watchdog.** A silent USB-CDC stall raises nothing in the serial reader
  (`io/serial_io.py` only flags `OSError`/`SerialException`; `readline()` returns empty
  bytes forever), so the last frame would stay on screen as if live.
  `BackendService._powerbox_watchdog_tick()` dispatches
  `SetPowerboxConnectionAction(connected=False)` when no frame arrives for
  `powerbox_stale_s`; edge-triggered, auto-clears when frames resume.
- **Auto-recovery.** Forces the serial port to close + reopen
  (`SerialPort.force_reconnect()`); the DTR toggle resets the RP2040, which
  re-enumerates and resumes. Electrically safe (no rails since fw 2.0), but **every NO
  relay drops** for the reset + 3 s safe-boot until the backend re-asserts them.

| `BackendConfig` field | Default | Meaning |
|---|---|---|
| `powerbox_stale_s` | `15.0` | no-frame timeout (0 disables) |
| `powerbox_auto_recover` | `False` in code; **`1` on the car** (`BACKEND_POWERBOX_AUTO_RECOVER` default in `prius-backend`) | master switch |
| `powerbox_recover_cooldown_s` | `20.0` | spacing between attempts, doubling per consecutive failure |
| `powerbox_recover_max_cooldown_s` | `600.0` | ceiling of the doubling (a flat 20 s retry caused a hub reset storm: 137 enumerations in 14 min) |

**Manual recovery** (only meaningful on a socket with real switchable VBUS; else use
`{"a":"hard_restart"}` or the backend's reconnect):

```bash
sudo systemctl stop prius-backend                   # release the port
sudo PRIUS_USB_CYCLE_DELAY=4 prius-usb-power cycle  # ≥ 4 s off
sudo systemctl start prius-backend
python3 -c 'import urllib.request,json,time; pb=json.load(urllib.request.urlopen("http://127.0.0.1:8080/api/v1/state"))["state"]["powerbox"]; print("mah",pb["energy_mah"],"age",round(time.time()-pb["last_update_time"],1))'
```

After a power cycle the backend needs a few seconds to re-bind; until then
`/api/v1/state` shows the value from before. Check `last_update` age < 2 s first;
`energy_mah` resets to ~0 on a true cold boot of the powerbox.

### MFD video board (`backend/mfd_power.py`)

Pi Zero v1.3 on relay ch2, enumerating as a `cdc_ether` gadget (`usb0`, renamed by
`/etc/udev/rules.d/70-prius-mfd-usb0.rules` from MAC `02:1e:4d:00:00:01`). The host
address (192.168.100.1/24) is re-applied after every enumeration. `MfdPowerManager` is
an ACC follower:

    OFF ──ACC on──▶ BOOTING ──iface+ping──▶ ON ──ACC off──▶ GRACE (20 min)
     ▲                                       ▲                │
     │                                       └────ACC on──────┘
     └── relay off ◀── POWERING_OFF ◀── SHUTTING_DOWN (ssh `sudo poweroff`) ◀──┘

Config: `BACKEND_MFD_*` in `/etc/prius/backend.env` (`ENABLED`, `HUB`, `PORT`,
`IFACE`, `HOST_CIDR`, `IP`, `SSH_USER`, `GRACE_S` 1200, `BOOT_TIMEOUT_S` 180,
`SHUTDOWN_WAIT_S` 45). Clean shutdown uses root's key (`/root/.ssh/id_ed25519`,
authorized on the Pi). State: `connection.mfd_state` / `mfd_usb_power` /
`mfd_reachable`. Code deploy to the Pi: `sudo prius-deploy-mfd`.

## Thermal testing rules

For heat-management work (fan policy for `low` / `full`, report in
[docs/THERMAL_MODEL.md](../docs/THERMAL_MODEL.md)):

- The car POCO has **no screen and no battery** (battery removed, 4.1 V fed to the
  terminals, see [ARCHITECTURE.md](ARCHITECTURE.md)). The `qcom-battery` thermal zone
  is meaningless and **no battery-temperature check is required**; the only thermal
  limit is the SoC die.
- On the bench the stack may be **overridden completely** for a measurement: stop the
  backend, stop roadsense, set the power mode by hand, pin the fan (`set_fan` /
  `fan_auto` API). No risk; return everything to automatic afterwards.
- Thermal load must drive **CPU and GPU together** to reproduce the roadsense load
  (camera + road model on the Adreno + the CPU side). A CPU-only burner is not
  representative. Record the powerbox `v`/`ma` draw with every run so loads are
  comparable.

## Open gaps

| Gap | What would resolve it |
|---|---|
| Hub model / switchable sockets: `provision.sh` says Terminus `1a40:0101` with the powerbox on port 1; `port_power.py`, `io/discovery.py` and `prius-usb-power` describe the ActionStar `2101:8500` (VBUS switching on internal port 5 only, `DEFAULT_PORT_ROLES = {5: powerbox, 2: gateway}`). | `lsusb -t` / `uhubctl` on the car; cycle a port and check that the device loses power. |
| Root cause of SoC freezes unknown; pstore/ramoops not wired, so a freeze leaves nothing. | Wire up ramoops; if freezes persist, try disabling the deepest cpuidle states (CX/AOSS collapse suspected). |
| Draw saved by `low` never measured. | INA219 A/B, `low` vs `full`, idle. |
| Whether blanking (opt-in) still saves ~0.19 W on kernel 7.1 without the labibb unbind, and the reboot-hang link. | A/B on the car POCO with `/etc/prius/blank-display`; repeated `systemctl reboot`. |
| Camera preview downscale time and frame-read cost on the phone. | Measure on the POCO. |
