# Power Management — POCO F1 Prius Board Computer

## Principle: never suspend

The device is **always-on**. We do **not** use system suspend (`freeze`/`mem`), because it
would tear down the cellular modem, WireGuard, the SSH session, and the OTG serial listener.
Instead we keep the system awake and scale the **CPU** between two profiles:

| Profile | Meaning | What it does | `cpus online` |
|---------|---------|--------------|----------------|
| `low`  | ignition OFF / resting | offline Gold cluster (`cpu4-7`); Silver cluster `schedutil`, capped to 1.2 GHz; DSI display kept blanked; Wi-Fi kept up for the grace period, then suspended | `0-3` |
| `full` | ignition ON / active   | all cores online; `schedutil`, max clocks on both clusters; DSI display kept blanked | `0-7` |

Across **both** profiles the modem, WireGuard, SSH, and OTG serial stay fully alive.
The GPU (`freedreno`) auto-idles to `cur=0` on its own (`simple_ondemand`) — we don't touch it.

## Hardware facts (SD845)

- 8 cores, 2 cpufreq policies:
  - `policy0` = `cpu0-3` **Silver** (300 MHz – 1.766 GHz)
  - `policy4` = `cpu4-7` **Gold** (0.825 – 2.65 GHz)
- Governors available: `ondemand userspace performance schedutil` — **no `powersave`**,
  so low mode is achieved by **offlining the Gold cluster + capping Silver**, not a governor.
- GPU `5000000.gpu` (freedreno) idles to `cur=0` via `simple_ondemand`.

## Wi-Fi grace at key-off (since 2026-11-06)

`prius-power low` no longer suspends Wi-Fi at once. It arms a transient systemd timer
(`prius-wifi-suspend.timer`, via `systemd-run --on-active`) that runs `prius-wifi suspend`
after the grace period in `/etc/prius/wifi-grace` (a systemd time span; default `20min`,
`0` = immediate as before). So the AP stays usable for 20 min after the car is switched off,
even though the CPU profile is already `low`. `prius-power full` cancels the pending timer and
resumes Wi-Fi; every new `low` re-arms the full period. A boot with the flag at `low` also
gives 20 min of Wi-Fi. `prius-power status` shows the pending suspend and its wall-clock time.

## The mode flag

```
/etc/prius/power-mode      # contains exactly  "low"  or  "full"
```

- This is the **single source of truth** for the power profile.
- Default after install: `low` (resting / key-off).
- A systemd **path watcher** re-applies the profile within ~1–2 s of the file changing.
- A future "ignition" message only has to **write this flag** — nothing else.

## The script

```
/usr/local/sbin/prius-power      # POSIX sh, chmod 755
```

Usage:

```bash
sudo prius-power low       # write flag=low  + apply now
sudo prius-power full      # write flag=full + apply now
sudo prius-power apply     # apply whatever the flag currently says (used on boot)
sudo prius-power status    # print current cores/governors/freqs (no change)
```

- `low`  → `schedutil` on all policies, `policy0` max = `1228800`, offline `cpu4-7`.
- `full` → online `cpu4-7`, `schedutil`, each policy max = its `cpuinfo_max_freq`.

## systemd integration

| Unit | Type | Purpose |
|------|------|---------|
| `prius-power.service` | oneshot | runs `prius-power apply` (on boot + when triggered) |
| `prius-power.path` | path | watches `/etc/prius/power-mode`, triggers the service on change |

Both are **enabled**. So:
- on **boot**, the service applies whatever the flag says (survives reboot);
- on **flag change**, the path watcher re-applies automatically.

## How to switch power (the two equivalent ways)

```bash
# A) via the CLI (writes flag + applies immediately)
sudo prius-power full          # going to full performance (key on)
sudo prius-power low           # going to rest (key off)

# B) by writing the flag only (path watcher applies it in ~1-2 s)
echo full | sudo tee /etc/prius/power-mode
echo low  | sudo tee /etc/prius/power-mode
```

Use **B** from the future ignition/battery message handler — it just needs to write the file.

## Verified behaviour

- LOW: cores drop to `0-3`, `policy0` capped at 1.2 GHz, Gold cluster gone — while modem
  stayed `connected`, WireGuard alive, default route via `qmapmux0.0`, SSH responsive.
- Path watcher: writing `full` → cores auto-online to `0-7`; writing `low` → back to `0-3`.
- Survives reboot (service applies the flag on boot).

## Display pipeline blanking (DSI power-down)

The box is **headless**: the local panel's backlight is always off
(`backlight-off.service`) and the dashboard is served over the network. But on
the mainline sdm845 kernel the display pipeline never power-collapses on its own
— as long as the in-kernel **fbcon** client holds the CRTC enabled, the **DSI PLL
(~1.9 GHz VCO)** plus the MDSS byte/pixel/MDP clocks keep running at full rate
even though nothing is on screen. Legacy sysfs blanking can't stop it: writing
`.../dpms = off` is `EPERM` under atomic KMS, and unbinding fbcon doesn't drop
the clocks.

`prius-blankd` fixes this by doing a real modeset-off from userspace:

- opens `/dev/dri/card0`, becomes **DRM master**, and disables every active CRTC
  via `DRM_IOCTL_MODE_SETCRTC` (`fb_id=0`, no connectors). This powers down the
  DSI PHY/PLL and the panel bias regulators (`labibb`).
- **unbinds the DRM fbcon vtconsole** so nothing can re-modeset the panel, and
  **unbinds the `qcom-lab-ibb-regulator` driver** (see the IRQ-storm section
  below).
- **holds** master and idles. There is **no runtime restore**: on
  `SIGTERM`/`SIGINT` it exits leaving the panel off — **reboot to get the
  display back**.

It's a self-contained Python script using raw DRM ioctls via `ctypes` — no
`libdrm` and no compiler required.

### LAB over-current IRQ storm / system-wide hang (root cause, 2026-07-11)

With the panel unprepared, the pmi8998 **labibb** kernel driver leaves its
*level-triggered* `lab-over-current` IRQ (IRQ 87, `pmic_arb`) enabled while the
LAB regulator is **off**. The line never clears, so the IRQ **storms at
~13 kHz** — observed 99M+ interrupts and 43 min of `irq/87-lab-over` thread CPU
in 2 h, all on CPU0. The storm floods the SPMI/PMIC arbiter and can starve
PMIC transactions system-wide; it caused a **6-hour hang** (2026-07-10 23:02 →
04:51: journald/timesyncd/`sync` unkillable in D-state, `/dev/kmsg` overrun,
47 service-restart timeouts) that needed a manual reboot.

Verified live: stopping `prius-blankd` (panel re-prepared) stops the storm
instantly; re-blanking restarts it; unbinding the labibb driver frees the IRQ
for good. **Never rebind labibb at runtime** — the panel driver keeps stale
regulator handles and the next panel prepare hard-hangs the kernel (reproduced;
recovered only by the powerbox freeze-recovery long-press). Hence blanking is
now a one-way trip per boot.

**Measured saving (INA219, clean idle A/B):** blanking drops the total draw by
**~0.19 W (−8.1%)** — from ~2.29 W to ~2.11 W resting — corroborated by
`poco_power` (−14%) with bus voltage flat. This is the single largest idle lever
found (WiFi-off was only ~37 mW).

Lifecycle is a systemd service driven by the power profile, **not** enabled
standalone:

| Unit | Type | Purpose |
|------|------|---------|
| `prius-blank.service` | simple | runs `prius-blankd` (blank + fbcon/labibb unbind + hold master; no runtime restore) |

`prius-power` starts it in **both** profiles (`apply_low` and `apply_full` both
run `systemctl start prius-blank`) — the DSI display/graphics output is never
used on this box, so it stays blanked at all times for the ~0.19 W saving. This
does **not** touch the Adreno GPU: blanking is a DRM modeset-off of the display
controller only, so GPU compute (point clouds, radar, etc. via `/dev/dri/renderD128`)
remains fully available. To use the physical panel for on-site debugging,
**reboot with `prius-blank` masked** — runtime restore was removed (it hangs
the kernel, see the IRQ-storm section above).
`prius-power status` prints `display : blanked|on`.

## Freeze recovery (hardware watchdog + powerbox escalation)

The sdm845 SoC has been observed to **hard-freeze**: kernel dead, zero network
traffic (not even WG handshake attempts), no panic, journal just stops
mid-stream (2x on 2026-07-10, boots ending 04:11:14 and once ~70 s into early
boot). No software watchdog can recover this — the whole OS is gone. Three
layers of defence, from fastest to last-resort:

1. **APSS hardware watchdog** (`qcom_wdt` @ 17980000, `/dev/watchdog0`) —
   systemd PID 1 pets it (`/etc/systemd/system.conf.d/10-watchdog.conf`,
   `RuntimeWatchdogSec=30`). A frozen kernel gets a SoC self-reset within 30 s.
   Note: qcom_wdt rejects timeouts > ~37 s, so 30 s (60 s fails EINVAL).
2. **Panic-on-oops** (`/etc/sysctl.d/10-lockup-panic.conf`): `kernel.panic=10`,
   `kernel.panic_on_oops=1`. (softlockup/hung_task detectors not built into
   this pmOS kernel.)
3. **Powerbox wake escalation** (firmware ≥ 1.6.0): when the POCO heartbeat
   dies, the powerbox presses the power button. A short 3 s press only powers
   ON an *off* POCO — a *frozen* one ignores it (verified 2026-07-10: powerbox
   press at freeze+16 s rebooted it, but the rescue boot froze again and 3 s
   presses did nothing for 4.5 h). After `POCO_WAKE_SHORT_TRIES` (2) failed
   short presses, the powerbox escalates to a 12 s forced hard power-cycle
   hold, repeating each 60 s cooldown until the heartbeat returns.

Freeze forensics are poor: pstore/ramoops is not wired up on this kernel, so a
freeze leaves nothing behind. If freezes continue despite the watchdog,
suspicion falls on deep platform idle (CX/AOSS collapse); next lever would be
disabling the deepest cpuidle states or wiring up ramoops.

## Status / decisions

- **Current scope is final for now: CPU-only scaling.** Going further on `low` (I/O throttle,
  disabling subsystems, modem low-power, etc.) is **deferred** — we keep `low` as it is rather
  than risk destabilising a working system. Revisit only if real current draw turns out too high.
- An **intermediate** profile could be added later if needed, but is not implemented yet.
- Invariants that must hold in **every** profile: system **loggable over SSH**, **modem ON**,
  **USB ON**, OTG serial + WireGuard alive. Only "everything else" gets trimmed when we optimise.

## roadsense stack (same POCO, ACC-only)

The road-perception stack (`roadsense.target`: camerad-dvr, modeld, envd, api +
the roadsense-signs `signd`) runs on this phone only while ACC is on. The
backend's `RoadsenseManager` (`backend/roadsense_power.py`, env
`BACKEND_ROADSENSE_*`, API commands `roadsense_mode auto|on|off` and
`roadsense_restart`, state `roadsense` in `/api/v1/state`):

1. key on → powerbox relay ch4 (camera hub) on → 8 s settle →
   `systemctl start --no-block roadsense.target` → `/v1/health` on
   `127.0.0.1:8090` polled (worker thread, never blocks the engine loop);
2. key off → 30 s grace → `systemctl stop --no-block roadsense.target` → wait
   until every unit is inactive (60 s cap) → relay ch4 off;
3. RUNNING watchdog: `systemctl is-active` per unit + the health probe; an API
   silent for 180 s restarts the target (one restart per 300 s);
4. **CPU profile coupling:** the stack pins cpu4-7 (modeld/signd/envd), which
   the `low` profile takes offline. While the manager is active the backend
   holds `full`; at key-off the `PowerModeRule` hands the `low` switch to the
   manager (`hold_low`), which applies it only once the stack is OFF. With mode
   `on` (bench) the profile stays `full` with the key off.
5. Resources: the stack runs in `roadsense.slice` (roadsense repo: CPUWeight
   100, MemoryHigh 3G / MemoryMax 3584M), `prius-backend.service` at
   CPUWeight 1000 / Nice -10, so the backend stays responsive at full load.
6. Thermals: every tick samples all `/sys/class/thermal` zones into
   `roadsense.temps` (plus `hottest_zone`/`hottest_c`) whether or not the stack
   runs — the raw input for the heat model.

`roadsense.target` is **not enabled at boot**; the backend owns it. The sign
provider is AGPL and only ever talks to the stack over its sockets; this repo
only drives `systemctl` and reads JSON over HTTP.

### Dashboard camera preview (on demand, `backend/camera_preview.py`)

The dashboard's Overview tab has a **Camera** panel: START takes a *preview
lease*, the page shows a downscaled still about once per second, and the
camera goes back to what it would have been without the preview when the
last lease ends (after a 3-min grace unless STOP was pressed, below). REST (`api/camera_preview_routes.py`, same auth as the rest
of `/api/v1`):

| Method + path | What |
|---|---|
| `POST /api/v1/camera/preview/lease` `{}` / `{"lease_id": id}` / `{"resume": true}` | start (explicit START) / renew / resume the current viewing → `{lease_id, ttl_s, expires_in_s, max_session_s, session_left_s, phase, reason, source, leases, resumed}` |
| `DELETE /api/v1/camera/preview/lease/{id}[?stop=1]` | release: soft (grace keeps the camera) or, with `stop=1`, the user's STOP (camera off now; also ends a running grace, any id). `POST …/lease/{id}/release[?stop=1]` is the `navigator.sendBeacon` form |
| `GET /api/v1/camera/preview/frame.jpg?w=640&q=70[&lease=id]` | latest frame; `ETag` (304 on `If-None-Match`), `X-Frame-Age`, `X-Frame-Time`, `X-Frame-Seq`, `X-Frame-Source` (`stack`/`direct`), `X-Preview-Scaled`; 503 + `Retry-After` + `{detail:{phase,reason}}` while the camera comes up; 409 without a lease |
| `GET /api/v1/camera/preview` or `GET /api/v1/camera/preview/` | status (same dict as the state field) |

Refusals: 409 `undervoltage`, 409 `not_resumable` (resume with no viewing to
continue: never a cold start), 410 `expired` / `max_session` (press again; no
cooldown), 429 `too_many_leases` (8). State: `roadsense.camera_preview` =
`{enabled, active, phase, reason, camera_phase, source, leases, started_at,
grace, grace_left_s, session_left_s, ttl_s, max_session_s, grace_s, scaling}`;
phases `off → powering → enumerating → waiting_for_frame → live`, `error`
(with `reason`; the reader keeps trying), and `grace` (no lease, camera kept
on; `camera_phase` says what the camera path is doing, `grace_left_s` how long
relay 4 stays on).

* **Grace (`BACKEND_CAMERA_PREVIEW_GRACE_S`, default 180, 0 = off).** When
  the last lease ends *softly* — page hidden, `pagehide`/unload beacon,
  unmount on navigation, TTL expiry, or the session cap — nothing is switched:
  the preview hold stays, relay 4 stays on and the reader keeps reading, so a
  lease taken within the grace is `live` at once with no power-up. When the
  grace runs out the hold is dropped and the manager cuts relay 4. Ended at
  once (no grace): the user's STOP (`?stop=1`), 12 V under-voltage, backend
  shutdown, and situation a (the stack has the camera: nothing to keep; the
  hold is dropped immediately, the stack is never touched). ACC on during a
  grace hands the camera to the stack and ends the grace.
* **Session cap = lease time per viewing.** A *viewing* runs from an explicit
  START until the camera is released. Every lease of the viewing — renewals
  and resumes (`{"resume": true}`: tab visible again, reload, re-opened page,
  a lapsed lease) — ends at the viewing's cap, START + `max_session_s`
  (10 min); a resume after the cap is refused (`max_session`). Whatever ended
  the leases, at most one grace follows, so the camera is on for at most
  10 min + 3 min per viewing without a button press. Pressing START again
  (also during that grace: instant, no power-up) starts a new viewing with a
  fresh 10 min.
* **Client:** the page stops fetching at once when hidden and keeps a
  "was previewing" marker in `localStorage`. On becoming visible again, on
  reload or when re-opened it resumes by itself — only with that marker
  (< 15 min old) and only if `GET /api/v1/camera/preview` reports `grace` or an
  active preview. While paused it shows "Paused — camera kept on for m:ss"
  with Resume / Off now. A frame is greyed out with "camera re-enumerating… —
  frame N s old" (or the matching text) whenever the phase is not `live` or
  the frame is older than 5 s.

* **Lease rules:** TTL 15 s without renewal (the page renews every 5 s), cap
  as above (`BACKEND_CAMERA_PREVIEW_TTL_S`, `…_MAX_SESSION_S`, `…_GRACE_S`,
  `…_CAPTURE_S`, `…_ENABLED=0` disables). The page stops renewing at once when
  it is hidden (Page Visibility API), sends a DELETE (keepalive; `?stop=1`
  only for STOP) on stop/unmount and a `sendBeacon` release on `pagehide`;
  the server TTL is the guarantee if all of that is lost on the LTE link.
* **Stack running (ACC on):** nothing is powered or started. Frames are the
  stack's own `/v1/frame.jpg` (camerad-dvr's copy of the camera JPEG, ~10/s)
  fetched over the HTTP boundary at most every 0.25 s and shared between
  viewers. No second block-device reader: camerad-dvr opens the disk
  `O_EXCL`, and a second reader would steal USB bandwidth from it.
* **Stack down (car off):** the lease sets the manager's *preview hold*:
  `off → preview` = relay ch4 on, **no** roadsense unit, **no** `full` CPU
  profile (PREVIEW counts as off for the deferred `low` switch; a manual
  `set_relay` ch4 is still refused). The backend then reads the camera itself,
  read-only, with the camerad-dvr protocol (1902:3241 found via sysfs, raw disk
  `O_RDONLY|O_DIRECT|O_EXCL`, MBR → layout, info sector at LBA 0x380/0x300,
  frame at 0x391/0x311, check word, EOI trim) at 2 frames/s. Zero info
  sectors right after power-on → `waiting_for_frame`; a vanished device (the
  first car run, 2026-11-13: live at +7 s, `info read failed … short read
  0/512` at ~+29 s; the node may come back under another name, sdg → sdh, and
  read-fail for a few seconds first) → close, `enumerating`, then once per
  second a fresh sysfs scan trying *every* 1902:3241 disk (a stale node next
  to the new one is skipped), logged every 10 s (`open attempt N: …`) and
  "camera back on /dev/sdX after N attempt(s)" on recovery.
  ACC on during a preview: the manager calls the preview's `release_device`
  right before `systemctl start` (no settle, the hub is already up) and the
  preview switches to the stack's frames. ACC off during a preview: the normal
  grace + stop runs, then the manager stays in `preview` (camera kept) instead
  of cutting ch4. Last lease gone: `preview → off`, ch4 cut.
* **Safety:** no preview-driven power-up while `powerbox.undervoltage` (or a
  shutdown request) is set — the lease is refused, and a direct preview
  already running is ended and ch4 released (viewing an already-running stack
  stays allowed: it powers nothing). Backend shutdown drops every lease,
  closes the device and cuts a preview-only ch4 (a running stack is left
  alone, as on every backend restart).
* **Downscaling:** Pillow (`py3-pillow`, POCO/packages.txt) in JPEG draft mode
  (DCT-domain decode at 1/2 then a small resize; 6-8 ms per 1280×720 frame on the laptop, not yet measured on the phone);
  without Pillow frames are served full size (~50-65 KB, `X-Preview-Scaled: 0`).
  The page asks for 640 px and drops to 480/320 px and up to one frame per
  5 s when frames arrive slowly.
* **Battery (car off):** relay ch4 on draws about **+84 mA at 12 V (≈1 W)**
  for the VIA hub + dashcam (powerbox/README.md relay table), so a full 10-min
  session costs ≈ 14 mAh of the 12 V battery, on top of the POCO's own idle
  draw (the preview keeps the `low` profile; reading two ~60 KB frames per second is light, not yet measured on the phone).
  The dashcam may record to its TF card while it is powered.

## Key-off / key-on sequence (as measured 2026-11-04, bench)

Everything follows `powerbox.acc_on` in the store (ACC-off is debounced
`BACKEND_ACC_OFF_DEBOUNCE_S`, default 3 s, before it reaches the store;
ACC-on is immediate).

| t (key-off) | what | owner |
|---|---|---|
| 0 s | ACC off seen; `low` profile **held back** while roadsense runs | `PowerModeRule` + `_roadsense_holds_low` |
| +10 s | satellite rail (relay ch5) off (linger); gateway relay ch3 off | `SatellitePowerRule`, gateway ACC follower |
| +30 s | roadsense grace over → `roadsense.target` stopped (~0.3 s) | `RoadsenseManager` |
| +35 s | camera relay ch4 off → `low` profile applied (cpu4-7 offline, Wi-Fi suspended) | `RoadsenseManager` → deferred switch |
| +600 s | MFD Pi: clean `poweroff` over SSH, 45 s later relay ch2 off | `MfdPowerManager` |

| t (key-on) | what |
|---|---|
| 0 s | `full` profile written (cores online within 0.3 s, Wi-Fi back ~4 s); satellite + gateway relays on |
| +2 s | MFD relay on (boot ≈ 55 s); camera relay on |
| +13 s | `roadsense.target` started (8 s camera settle) → running at ≈ +25 s |

Rules that came out of that measurement:

- **Relay port OFF = VBUS first, data-off after the disconnect** (`backend/port_power.py`).
  Disabling the hub port first hid the disconnect from the kernel: ~10 s of `clear tt`
  errors (321 in one key-off), then the kernel re-enabled the port and the data-off had to
  be applied again. ON = data-enable first, then the relay.
- **Gateway auto-recovery counts from the VBUS-on edge**, not from the last heartbeat, so
  key-on does not cold-cycle a gateway that was silent only because its relay was off.
- **A backend (re)started at key-off adopts the gateway at key-on.** Its serial port starts on
  a pending placeholder; the reader thread now runs regardless and connects when the hotplug
  monitor retargets it (before 2026-11-04 18:43 the thread was never started, the gateway
  stayed deaf and the 30 s stale-link recovery cold-cycled it in vain).
- **PCF8574 expander reset at the first key-off, seen once** (`rly_fix … pcf_reset=1`, want
  0xE1 got 0xFF, 16:16): every relay dropped for ~1 s while the MCU kept running → MFD Pi
  brown-out reboot, all hubs re-enumerated, "MFD board UNREACHABLE" for 50 s. Not an
  ACC-line effect (the ACC input is only a 2N7000 gate into GP11, no load); it did not recur
  on the 17:19 and 18:01 key-offs. Firmware ≥ 2.2.2 re-checks the expander every 100 ms (was
  ~1 s, STATUS-paced), which bounds and time-stamps any repeat. Cause open — see
  powerbox/RELAY-CHANGE-PLAN.md.

## Powerbox power management (relay board, POCO button, heartbeat)

The powerbox RP2040-Zero (firmware 2.1) starts/wakes the main POCO through its
power button, switches the loads on an 8-channel relay board and coordinates
a clean shutdown with the POCO. There are **no MOSFET rails any more**: the
powerbox, its hub and the POCO bucks sit on the permanent 12 V feed. Full
pinout and wire protocol live in [`powerbox/README.md`](../powerbox/README.md);
the behaviour the POCO side depends on:

| Relay | Contact | Load | After any powerbox restart | Backend owner |
|-------|---------|------|----------------------------|---------------|
| 1 | **NC** | the powerbox's **own** supply — `{"a":"hard_restart"}` only, refused by `relay` | n/a (it *is* the restart) | `powerbox_hard_restart` API command |
| 2 | NO | MFD Raspberry Pi Zero | OFF | `backend/mfd_power.py` (ssh poweroff + wait, then cut) |
| 3 | NO | USB hub: CAN gateway **and** comma.ai panda | OFF | gateway power path (`SetGatewayUsbPowerAction`, satellite wake-locks) |
| 4 | NO | USB hub (VIA): fvision dashcam / aux | OFF | `backend/roadsense_power.py` while the roadsense stack runs (on with ACC, off after the stop) or a dashboard camera preview holds it (state `preview`); `set_relay` ch4 otherwise |
| 5 | NO | RS485 satellite power | OFF | `state/rules/satellite_power.py` wake-locks |
| 6-8 | NO | unassigned | OFF | `set_relay` |

| Pin | Function | Idle | Behaviour |
|-----|----------|------|-----------|
| GP27 | main POCO power button (short-to-GND) | input, pulls off | LOW pulse = press (6 s powers on, 35 s forces a reset). Never driven high (phone line ~1.8 V). |
| — | "poco 2" slot | not connected | no second POCO exists (single-computer layout); `button`/`wake` for poco 2 → `ERROR POCO2_NC`, no press. |
| GP29 / GP28 | chassis fan PWM (25 kHz, not inverted) / tach | 0 % at boot | fan runs flat out while the firmware is not driving GP29 (bootloader, 3 s safe-boot). |

**Restart scope.** Relay opto VCC is the RP2040's 5 V, so whenever the RP2040
is unpowered or restarting (spontaneous reset, `hard_restart`, WDT, reflash
VBUS cut) **every NO relay releases**: the MFD Pi gets a hard cut, the CAN hub
(gateway + panda) and the camera hub drop. The PCF8574 wakes at 0xFF, so all
loads come back **OFF**; the backend re-asserts its wanted states ~6 s after
the `POWERBOX_READY` banner (`BackendService._pp_reassert_at`) and the
periodic `rly` read-back/enforce sweep converges the rest. `hard_restart`
measured 2026-11-03: USB gone after 0.4 s, re-enumerated after 1.0 s, first
STATUS after 4.9 s.

**Reset cause.** The firmware reports `machine.reset_cause()` as `rc` in the
boot banner, in every STATUS and in the `boot` event of `getlog` (1 = power-on
/ brown-out / hard_restart, 3 = watchdog or `machine.reset()`), plus `up`
(uptime s). The backend mirrors them as `powerbox.reset_cause`,
`powerbox.uptime_s` and counts banners in `powerbox.boot_count`, so the
spontaneous resets seen on the bench (2026-11-03) can be attributed.

**RS485 satellite rail (relay ch5, backend wake-locks).** The firmware never
manages it; policy lives in the backend
(`state/rules/satellite_power.py` + `backend/satellites.py`). The rail is ON iff
at least one named wake-lock is held: `acc` (ignition on → satellites always
ready), `queue` (the satellite job queue has pending/running work — scheduled
sensor reads, event commands, config pushes) or `manual:*` (API holds). A
~10 s linger before the final OFF absorbs back-to-back jobs so the rail never
flaps, and sustained 12 V under-voltage cancels queued key-off work outright.
Jobs are serialized through one priority queue (FIFO within priority), which
also matches the half-duplex RS485 bus. See `docs/VIRTUAL_TWIN_ARCHITECTURE.md`.

**Shutdown and sleep (no suicide path).** The box cannot cut the POCO's power.
When a shutdown is requested — the backend's `UndervoltageProtectionRule`
(sends `{"a":"off"}` and runs `systemctl poweroff` when
`local_poweroff_on_undervoltage` is set) or the firmware's last-resort backstop
(< 10.0 V for 20 s, only if the backend is dead, which sends `SHUTDOWN` to the
host) — the firmware enters `shutdown`, waits for the heartbeat to stop (≥ 25 s)
or the grace, then `sleep`: no wake presses until ACC rises or `{"a":"wake"}`.
An off phone draws next to nothing, which is the point.

**Bidirectional heartbeat.** The powerbox emits a `STATUS` message (~2 s) with a
rolling counter, POCO liveness, PM state, reset cause/uptime, fan duty/RPM and
the relay read-back; the backend sends `{"a":"hb","n":N}` (~2 s,
`powerbox_heartbeat_s`). If the POCO's counter stops advancing for 15 s the
powerbox considers it dead and — past the 20 s boot grace and a 5 min cooldown —
holds GP27 LOW for 6 s to **wake** it (two unanswered presses, then a 35 s
forced reset). Waking is disabled during a shutdown so we never fight our own
poweroff. Firmware TX is gated on these heartbeats: without them the port is
silent (bench tools must send them, see `powerbox/relay_bringup_test.py`).

These mirror into `state.powerbox` (`relays`, `rs485_power`, `poco_alive`,
`pm_state`, `powerbox_hb`, `reset_cause`, `uptime_s`, `boot_count`,
`relay_fix_count`, `relay_err_count`) and onto the dashboard.

## USB per-port power (powerbox / gateway recovery)

The RP2040 **powerbox** and **gateway** talk to the backend over USB-CDC behind
the car's USB hub.

**Power model (fw 2.x).** The powerbox MCU is **bus-powered** — it runs off USB
VBUS, with no direct 12 V line to the MCU. The hub's 5 V buck sits on the
permanent 12 V feed, through the powerbox's own **NC relay 1**: the chain is

```
12 V (permanent)  →  relay 1 (NC, released = closed)  →  hub 5 V buck  →  VBUS  →  powerbox MCU
```

so the box is always on; energising relay 1 (`{"a":"hard_restart"}`) is the
only thing that cuts it, and it self-restores because the PCF8574 wakes at
0xFF (coil released). Any MCU reset is electrically harmless — but it releases
every NO relay (relay opto VCC is the RP2040's 5 V), see the table above.

* **Cold reset via VBUS — one switchable socket only.** The replacement hub
  (labeled **D-Link DUB-H4 rev D1**) is an **ActionStar `2101:8500`** inside
  (5 internal ports + built-in HID `2101:8501`, which is just the LED
  controller). That's the same silicon as the *unsupported* rev C1 (uhubctl
  issue #88); the genuinely per-port DUB-H4 rev D enumerates as Genesys
  `05E3:0608`. **Bench-tested 2026-07-21 (nokia1):** only the socket wired to
  **internal port 5** has real switchable VBUS — every other socket keeps
  power even with *all* ports commanded off (their 5 V is hardwired, no
  FETs). Consequences:
  - **Put the gateway on the port-5 socket** — it's the board whose USB PHY
    hard-wedges (`error -71`, connect-but-no-enable) and needs a real VBUS
    cut; a `prius-usb-power cycle` there is a true cold reset.
  - The powerbox on any other socket gets **data-only** off/cycle; recover it
    via DTR-pulse serial reset / `mpremote` / its firmware WDT, or ultimately
    with `{"a":"hard_restart"}` (relay 1 cuts the box's own supply; fw 2.1).
  - `hubcycle` (all-ports gang off) does **not** cut the non-switchable
    sockets on this hub — it only worked on the old rev F1.
  *(History: the previous hub was a DUB-H4 rev F1 — Terminus FE1.1s
  `1a40:0101`, fully ganged — per-port off was data-only there too, but
  `hubcycle` really gang-cut all VBUS. If hub hardware changes again, re-test
  which sockets actually switch before trusting the docs.)*
* **MCU resets are harmless to the box, not to its loads.** Reflashing, a
  DTR-pulse serial reset, the WDT and a soft reboot never take the box down
  (there is no latch to lose), but every one of them releases the NO relays
  for the duration of the reset plus the 3 s safe-boot: the MFD Pi gets a hard
  cut, the CAN hub (gateway + panda) and the camera hub drop, and the backend
  must re-assert them afterwards (it does so on the `POWERBOX_READY` banner).
  Prefer a clean MFD shutdown before deliberate resets. (Historically — fw 1.x
  — the box latched its own OUT1 rail and a reset with ACC off was fatal; see
  commit `783e80a`, which removed the now-obsolete
  `powerbox_recover_requires_acc` guard.)

> When checking whether a reset happened, read a **fresh** state sample: after a
> power-cycle the backend needs a few seconds to re-bind the re-enumerated board, and
> until then `/api/v1/state` returns the value from before the cycle. Confirm
> `last_update` age < 2 s first.

**USB-CDC write model.** A MicroPython `sys.stdout.write` to USB-CDC is **blocking**:
when no host has the port open / is draining it, the small (~64–256 B) CDC TX FIFO
fills and the write blocks forever, deadlocking the firmware. The firmware therefore
guards every `tx()` with a non-blocking `select.poll()` **POLLOUT** readiness probe
(`poll(0)`) and **drops** the line when the link can't accept it. Telemetry is
periodic, so a dropped sample is harmless and the firmware never wedges, regardless
of whether a host is draining. The board survives a backend-stopped gap with no reset
loop and auto-resumes the instant the backend reconnects.

Two host-side measures keep the link clean:
* **Port-based discovery** — the backend resolves roles from the physical hub port
  (`{1: powerbox, 2: gateway}`) and never opens/probes the device.
* **ModemManager-ignore udev rule** (`99-prius-rp2040-nomm.rules`, VID `2e8a`) stops
  MM's AT probes from opening/closing the CDC port; `MODE="0666"` lets the backend
  open the tty without a `chmod` race.

The board runs `main.py` only (autostarts; has the WDT + a 3 s Ctrl-C safe-boot
window).

### Silent link stall — host-side watchdog

**Observed (2026-06-19):** with the board alive and OUT1 latched (it had survived
an ACC-off test), the backend's powerbox telemetry **froze for ~56 min** — the
heartbeat counter, voltage, `acc_on`, OUT rails and timestamp all stuck at their
last values, yet `/api/v1/state` still reported `connected: true`. The MCU was
fine; the **USB-CDC link had gone silent** (host stopped receiving frames). A
later attempt to *open the port to inspect it* made things worse — that DTR pulse
reset the MCU, and with ACC disconnected OUT1 dropped and the board suicided
(see the power model above).

**Root cause (host side).** The serial reader (`io/serial_io.py`) only flags a
disconnect on an `OSError`/`SerialException`. A silent CDC stall raises neither:
`readline()` just returns empty bytes on every 0.1 s timeout, forever. So the
reader never reconnects, and the powerbox reducers — which set
`connected=True` + `last_update_time` on every frame — are simply never called
again, leaving the **last frame frozen on screen as if live**. (The firmware's
non-blocking `tx()` guard prevents the *firmware* from blocking; it does not
prevent the *link* from going silent.)

**Fix (backend staleness watchdog).** `BackendService._powerbox_watchdog_tick()`
runs every engine tick. The powerbox streams at ~1 Hz, so if **no frame arrives
for `powerbox_stale_s` (default 6 s)** it dispatches
`SetPowerboxConnectionAction(connected=False)` — the dashboard then shows the link
DOWN instead of stale-but-"connected". It is edge-triggered (logs once) and
**auto-clears**: when frames resume, the telemetry/STATUS reducers restore
`connected=True`. Tunable via `BackendConfig.powerbox_stale_s` (0 disables).

**Optional auto-recovery (software link reset, default OFF).** Beyond *reporting*
the stall, the watchdog can recover it by forcing the powerbox serial port to
close+reopen (`SerialPort.force_reconnect()`). That reopen re-asserts **DTR,
which resets the RP2040** — the MCU reboots, re-enumerates USB-CDC and resumes
streaming, clearing the wedge. It is gated by three `BackendConfig` fields:

| Field | Default | Meaning |
|-------|---------|---------|
| `powerbox_auto_recover` | `False` | Master switch (set to `1` via `BACKEND_POWERBOX_AUTO_RECOVER` on prius). Electrically safe (fw 2.x: the box is always powered), but the reset releases every NO relay for a few seconds — MFD Pi hard cut, CAN/camera hubs drop — until the backend re-asserts them. |
| `powerbox_recover_cooldown_s` | `20.0` | Minimum spacing between forced resets, so a permanently dead link is not reset in a tight loop while it re-enumerates. |
| `powerbox_stale_s` | `15.0` | No-frame timeout before the link is marked stale and recovery is triggered. |

With the defaults the watchdog only *reports* the stall (no MCU reset). Enabling
recovery is the same risk as a manual `prius-usb-power cycle` and carries the
same side effect: **every NO relay drops during the reset**. To recover by hand
instead, follow *Recovery procedure & caveat* below.

Recovery / control is per **hub port** via [`uhubctl`](https://github.com/mvp/uhubctl)
(apk package `uhubctl`), wrapped by `prius-usb-power`.

### Port map (devices live on dedicated hub ports)

| Hub port (`1-1`) | Device | by-id / address |
|------------------|--------|-------|
| **port 1** | built-in HID LED controller (ActionStar 2101:8501, internal) | — |
| **port 2** | **powerbox** (12 V telemetry + ACC/ignition) | `usb-MicroPython_Board_in_FS_mode_503359277a7c699f-if00` |
| **port 3** | **gateway** (CAN / AVC-LAN / RS485) | `usb-MicroPython_Board_in_FS_mode_50443405b862d21c-if00` |
| **port 4** | Garmin GPS (mass storage) | — |
| **port 5** | **MFD video board** (Pi Zero v1.3 since 2026-10-14, was Zero 2W; VGA666, USB-eth gadget) | `usb0` → 192.168.100.2 |

Port 5's socket is the **only one with genuinely switchable VBUS (PPPS)** on
this hub (ActionStar 2101:8500 "DUB-H4 rev D1"), so the MFD video board — an
SD-card Linux system that must be power-managed cleanly with ACC — lives
there. The gateway sits on a non-switching socket until the fully-PPPS
replacement hub arrives: `prius-usb-power` commands against it are a harmless
data-only noop, and all the proven gateway power-control code stays in place —
**changing ports back is only a `BACKEND_USB_PORT_ROLES` /
`BACKEND_MFD_PORT` edit in `/etc/prius/backend.env` + service restart.**

The backend's USB discovery uses this topology as the **primary** strategy
(`discover_roles_combined` → `discover_roles_by_port`, `DEFAULT_PORT_ROLES =
{2: powerbox, 3: gateway}`): role is resolved purely from the physical port, so it
binds the right board **even while it is silent/wedged** and is immune to ACM
renumbering / replugging. `whoami` probing is only the fallback for unmapped ports.

### MFD video board power management (`backend/mfd_power.py`)

The Pi Zero 2W enumerates as a `cdc_ether` USB gadget (`usb0`); the host-side
IP (192.168.100.1/24) must be re-applied after **every** enumeration. The
backend's `MfdPowerManager` is an ACC follower:

    OFF ──ACC on──▶ BOOTING ──iface+ping──▶ ON ──ACC off──▶ GRACE (10 min)
     ▲                                       ▲                │
     │                                       └────ACC on──────┘
     └── VBUS cut ◀── POWERING_OFF ◀── SHUTTING_DOWN (ssh `sudo poweroff`) ◀──┘

Configured entirely via `BACKEND_MFD_*` env vars in `/etc/prius/backend.env`
(`ENABLED`, `HUB`, `PORT`, `IFACE`, `HOST_CIDR`, `IP`, `SSH_USER`, `GRACE_S`,
`BOOT_TIMEOUT_S`, `SHUTDOWN_WAIT_S`). Clean shutdown uses root's SSH key
(`/root/.ssh/id_ed25519`, authorized on the Pi as `piotr`). Status is mirrored
into `connection.mfd_state` / `mfd_usb_power` / `mfd_reachable`.

### Hardware quirk: the hub reports "ganged"

The car hub is a Terminus/D-Link-class chip (`idVendor:idProduct = 1a40:0101`)
whose EEPROM **wrongly** reports power switching as *ganged*. `uhubctl` therefore
needs the **force flag `-f`** to drive an individual port (same quirk as the bench
`nokia1` D-Link DUB-H4). Without `-f`, per-port switching is refused.

### The script

```
/usr/local/sbin/prius-usb-power      # POSIX sh, chmod 755 (wraps uhubctl -f)
```

```bash
sudo prius-usb-power status              # list hubs + ports + attached devices
sudo prius-usb-power locate [TARGET]     # print resolved "HUB PORT" (e.g. "1-1 1")
sudo prius-usb-power cycle  [TARGET]     # power-cycle a port (off DELAY s -> on)
sudo prius-usb-power off|on [TARGET]
# DELAY override: PRIUS_USB_CYCLE_DELAY=15 sudo -E prius-usb-power cycle
```

`TARGET` may be a tty path (`/dev/ttyACM0`), a `/dev/serial/by-id/...` symlink
(preferred — stable across renumbering), an explicit `"LOCATION PORT"` pair
(`"1-1 1"`), or omitted → defaults to the **powerbox** by-id. The script resolves
a tty → `HUB PORT` through sysfs (`/sys/class/tty/<n>/device` → `…/1-1/1-1.1/…`;
the USB device dir `1-1.1` → hub `1-1`, port `1`).

### Recovery procedure & caveat

```bash
sudo systemctl stop prius-backend                  # release the port
sudo PRIUS_USB_CYCLE_DELAY=4 prius-usb-power cycle  # cold-reset powerbox (≥4s off)
sudo systemctl start prius-backend                  # backend re-binds + drains
# confirm a FRESH reading (energy_mah resets to ~0 on a true cold boot):
python3 -c 'import urllib.request,json,time; pb=json.load(urllib.request.urlopen("http://127.0.0.1:8080/api/v1/state"))["state"]["powerbox"]; print("mah",pb["energy_mah"],"age",round(time.time()-pb["last_update_time"],1))'
```

> After a power-cycle the backend needs a few seconds to re-bind the re-enumerated
> board; until then `/api/v1/state` returns the value from before the cycle. Check
> `last_update` age < 2 s before reading `energy_mah`. On a cold boot it resets to ~0.

A USB power-cycle (≥ 4 s off) is a hands-off MCU reset for the powerbox (it is
USB-powered). The BOOTSEL reflash below is only needed for a corrupt filesystem /
bad `main.py`.

### Flashing firmware over USB (no BOOTSEL) — `mpremote resume`

The powerbox firmware auto-runs `main.py` on every boot, and on `Ctrl-C` it does
`sys.exit(0)` which MicroPython follows with an automatic **soft reboot** back into
`main.py`. There is therefore **no stable REPL** to land on, and a plain
`mpremote fs cp` fails with `could not enter raw repl`: mpremote's default initial
**soft-reset re-runs `main.py`** and the streaming output hides the raw-REPL prompt.

Reliable hands-off recipe (used by `/usr/local/sbin/prius-flash-powerbox`):

1. Stop whatever holds the port: `sudo systemctl stop prius-backend`.
2. In a short retry loop, for each attempt: send `\r\x03\x03` (Ctrl-C) over pyserial
   to drop the board into the momentary post-`sys.exit` REPL window, then run
   `mpremote resume connect <by-id> fs cp main.py :main.py`.
   - `resume` is the key: it **skips mpremote's own soft-reset**, so it enters
     raw-REPL in the brief REPL window. Once in raw-REPL the board is **held**
     (raw-REPL does not auto-reboot), so the copy completes.
   - It typically succeeds on the first or second attempt.
3. Verify: `mpremote resume connect <by-id> exec "..."` reading the `VERSION` line.
4. `mpremote resume connect <by-id> reset` to boot the new firmware (fw 2.x: the
   box is always powered; a reset only releases every NO relay for a few seconds).
5. `sudo systemctl restart prius-backend` immediately — the new firmware's POCO
   boot-grace is 20 s, and the backend must resume 2 s heartbeats within it or the
   firmware will pulse the POCO power button (GP27). Restarting at once is well inside
   the window.

Do **not** `close()` + reopen the pyserial port between landing and copying: dropping
DTR on close resets the board, and reaching the REPL re-enumerates the CDC (the old fd
dies). The `mpremote resume` loop sidesteps both issues. Use the BOOTSEL reflash below
only if the on-device `main.py` is so broken the board never streams at all.

### ModemManager-ignore udev rule

`/etc/udev/rules.d/99-prius-rp2040-nomm.rules` (shipped in the rootfs overlay) tells
ModemManager to **ignore** VID `2e8a` and sets `MODE="0666"` so the backend/recovery
tooling can open the tty without racing a `chmod`. MM's AT probes would otherwise
open→write→close the CDC port and corrupt the REPL/telemetry. `provision.sh` reloads
udev so it applies without a reboot. Verify: `cat /etc/udev/rules.d/99-prius-rp2040-nomm.rules`
and `ls -l /dev/ttyACM0` (should be `crw-rw-rw-`).

### Failsafe — BOOTSEL + flash-nuke reflash (for a corrupt filesystem)

Needed only when the on-device filesystem itself is bad (a `main.py` that
crashes/wedges on every cold boot, so the board never streams even after a
power-cycle). Requires physically holding **BOOTSEL** while plugging/resetting the
board. A plain UF2 flash does **NOT** erase the littlefs filesystem — only
`flash_nuke.uf2` does — so use the nuke first to clear a bad `main.py`.

```bash
# Board in BOOTSEL → enumerates as 2e8a:0003, mass-storage labelled RPI-RP2.
RP=$(sudo blkid | grep RPI-RP2 | cut -d: -f1)      # e.g. /dev/sdg1 (it IS partitioned)
sudo mount -t vfat "$RP" /mnt/rp2
sudo cp flash_nuke.uf2 /mnt/rp2/ && sync           # erases littlefs + firmware
sleep 8                                             # board re-enters BOOTSEL
RP=$(sudo blkid | grep RPI-RP2 | cut -d: -f1)      # re-detect (node changes)
sudo mount -t vfat "$RP" /mnt/rp2
sudo cp RPI_PICO-vX.Y.Z.uf2 /mnt/rp2/ && sync      # clean MicroPython
sleep 8                                             # boots to 2e8a:0005, empty FS
# then upload firmware via mpremote (main.py only):
mpremote connect /dev/ttyACM0 fs cp ina219.py ahtx0.py bmp280.py pcf8574.py main.py :
```

Notes: re-detect the block node after **each** UF2 (it changes, e.g. `sdg1`→`sdh1`);
use generous `sleep`s and don't `umount` before the board self-consumes the UF2;
`flash_nuke.uf2` is from datasheets.raspberrypi.com, MicroPython UF2 from
micropython.org (currently `RPI_PICO-20250911-v1.26.1.uf2`). After a clean MicroPython
boot, `mpremote` works normally (the udev `MODE=0666` makes the tty immediately
openable).


  Savings are qualitative: 4 cores offline + clock cap vs 8 cores at full clock.
- Modem is deliberately kept `power state: on`; a modem low-power state would drop
  SSH-over-cellular.
- When you need to **work on the phone** (e.g. compiling), set `full` — `low` limits it to
  4× 1.2 GHz.

## Thermal testing rules (bench, 2026-11-04)

Facts and permissions for the heat-management work (fan policy for `low` / `full`,
report in `docs/THERMAL_MODEL.md` once the lane delivers it):

- The car POCO has **no screen and no battery** (battery amputated, 4.1 V fed to the
  terminals, see `ARCHITECTURE.md`). The `qcom-battery` thermal zone is meaningless and
  **no battery-temperature check is required**; the only thermal limit is the SoC die.
- On the bench the stack may be **overridden completely** for a measurement: stop the
  backend, stop roadsense, set the power mode by hand, pin the fan (`set_fan` /
  `fan_auto` API). No risk; just return everything to automatic afterwards.
- Thermal load generation must drive **CPU and GPU together**, so it reproduces the
  real roadsense load (camera + road model on the Adreno + the CPU side). A CPU-only
  burner is not representative. Record the powerbox `v`/`ma` draw with every run so the
  loads are comparable.
