# Power Management — POCO F1 Prius Board Computer

## Principle: never suspend

The device is **always-on**. We do **not** use system suspend (`freeze`/`mem`), because it
would tear down the cellular modem, WireGuard, the SSH session, and the OTG serial listener.
Instead we keep the system awake and scale the **CPU** between two profiles:

| Profile | Meaning | What it does | `cpus online` |
|---------|---------|--------------|----------------|
| `low`  | ignition OFF / resting | offline Gold cluster (`cpu4-7`); Silver cluster `schedutil`, capped to 1.2 GHz; DSI display kept blanked | `0-3` |
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
- **holds** master and idles, so fbcon can't re-enable the pipe.
- on `SIGTERM`/`SIGINT` it restores the saved CRTC mode and exits (and fbcon's
  lastclose restore is a backstop), bringing the display back exactly as it was.

It's a self-contained Python script using raw DRM ioctls via `ctypes` — no
`libdrm` and no compiler required.

**Measured saving (INA219, clean idle A/B):** blanking drops the total draw by
**~0.19 W (−8.1%)** — from ~2.29 W to ~2.11 W resting — corroborated by
`poco_power` (−14%) with bus voltage flat. This is the single largest idle lever
found (WiFi-off was only ~37 mW).

Lifecycle is a systemd service driven by the power profile, **not** enabled
standalone:

| Unit | Type | Purpose |
|------|------|---------|
| `prius-blank.service` | simple | runs `prius-blankd` (blank + hold master; restore on stop) |

`prius-power` starts it in **both** profiles (`apply_low` and `apply_full` both
run `systemctl start prius-blank`) — the DSI display/graphics output is never
used on this box, so it stays blanked at all times for the ~0.19 W saving. This
does **not** touch the Adreno GPU: blanking is a DRM modeset-off of the display
controller only, so GPU compute (point clouds, radar, etc. via `/dev/dri/renderD128`)
remains fully available. `display_on()` is retained as a manual override if the
physical panel is ever needed for on-site debugging (`systemctl stop prius-blank`).
`prius-power status` prints `display : blanked|on`.

## Status / decisions

- **Current scope is final for now: CPU-only scaling.** Going further on `low` (I/O throttle,
  disabling subsystems, modem low-power, etc.) is **deferred** — we keep `low` as it is rather
  than risk destabilising a working system. Revisit only if real current draw turns out too high.
- An **intermediate** profile could be added later if needed, but is not implemented yet.
- Invariants that must hold in **every** profile: system **loggable over SSH**, **modem ON**,
  **USB ON**, OTG serial + WireGuard alive. Only "everything else" gets trimmed when we optimise.

## Powerbox power management (OUT rails, POCO control, heartbeat)

The powerbox RP2040 switches the whole computer's power and coordinates a clean
shutdown with the POCO. Three active-high MOSFET rails (all downstream of the
INA219 shunt) plus the POCO power button are controlled. Full pinout and wire
protocol live in [`powerbox/README.md`](../powerbox/README.md); the behaviour the
POCO side depends on:

| Rail | Pin  | Drives | Default | Behaviour |
|------|------|--------|---------|-----------|
| OUT1 | GP29 | POCO + RP2040 + USB-hub 5 V (master) | **latched HIGH** | `computer_power = OUT1 OR ACC`. LOW = **suicide**. |
| OUT2 | GP28 | RS485 satellite power | ON | Controllable while the Prius is off. |
| OUT3 | GP27 | Spare | OFF | Unused. |
| GP15 | —    | POCO power button (short-to-GND) | high-Z | LOW pulse = press (~3 s on, ~10 s force-reboot). |

**Auto-start + latch.** The rail is OR-ed with ACC, so the RP2040 boots
automatically when the ignition is ON. The firmware drives OUT1 HIGH immediately
to *latch* the rail, holding power after ACC drops for a graceful shutdown.

**Suicide.** Driving OUT1 LOW removes the latch; once ACC is also gone the whole
computer powers off, protecting the 12 V battery. This happens after a
shutdown is requested — either the backend's `UndervoltageProtectionRule`
(< 11.0 V for 5 s → sends `{"a":"off"}`) or the firmware's last-resort backstop
(< 10.0 V for 20 s, only if the backend is dead) — and the POCO is confirmed off
(heartbeat lost) **or** the grace timeout elapses.

**Graceful POCO shutdown.** "Switching off the POCO" is an OS poweroff (the
backend runs `systemctl poweroff` when `local_poweroff_on_undervoltage` is set);
the rail stays up until the firmware suicides. The firmware never cuts OUT1 to
power the POCO down — only the OS does the clean shutdown, then OUT1 drops.

**Bidirectional heartbeat.** The powerbox emits a `STATUS` message (~1 s) with a
rolling counter + OUT states + POCO liveness; the backend sends `{"a":"hb","n":N}`
(~2 s, `powerbox_heartbeat_s`). If the POCO's counter stops advancing for 15 s the
powerbox considers it dead and — past the 60 s boot grace and a 60 s cooldown —
pulses GP15 ~3 s to **wake** it. Waking is disabled during a shutdown so we never
fight our own poweroff.

These mirror into `state.powerbox` (`out1`/`out2`/`out3`, `poco_alive`,
`pm_state`, `powerbox_hb`) and onto the dashboard **Power Control** panel.

## USB per-port power (powerbox / gateway recovery)

The RP2040 **powerbox** and **gateway** talk to the backend over USB-CDC behind
the car's USB hub.

**Power model.** The powerbox MCU is **bus-powered** — it runs off USB VBUS, with
no direct 12 V line to the MCU — **but that USB bus is itself fed from the OUT1
12 V rail**. The full chain is:

```
OUT1 (12 V rail)  →  USB hub 5 V  →  VBUS  →  powerbox MCU
```

Two consequences follow from this single chain:

* **Cold reset via VBUS.** Cutting the powerbox's hub-port VBUS removes the MCU's
  only power, so `prius-usb-power cycle "1-1 1"` with ≥ 4 s off (to drain bulk
  capacitance) is a full cold reset: RAM cleared, `energy_mah` → ~0, `main.py`
  restarts from scratch.
* **Self-latch / suicide.** OUT1 is driven by the powerbox MCU *and* gates the hub
  that powers it. On ACC the rail comes up via the `OUT1 OR ACC` term, the MCU
  boots and immediately drives OUT1 HIGH to **latch its own (and POCO's) power**,
  so it keeps running after ACC is removed. A commanded shutdown / "suicide" drops
  OUT1 → the hub loses 5 V → the MCU loses VBUS and dies. **A brief MCU reset with
  ACC off is NOT fatal:** the OUT1 rail has a **hardware >30 s hold-up** that keeps
  it energised across the reset, and the rebooting firmware re-drives OUT1 (3 s
  safe-boot then latch) well inside that window. So reflashing, a DTR-pulse serial
  reset, the WDT, and a soft reboot are all safe with ACC off. What still drops the
  rail with ACC off is a *sustained* power loss beyond the hold-up — a VBUS cut held
  longer than ~30 s (`prius-usb-power cycle` uses ≥ 4 s, which is fine), or a
  commanded suicide. (Historically, before the hold-up hardware, any MCU reset with
  ACC off was fatal — see commit `783e80a`, which removed the now-obsolete
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
| `powerbox_auto_recover` | `False` | Master switch (set to `1` via `BACKEND_POWERBOX_AUTO_RECOVER` on prius). Safe now that OUT1 has the >30 s hold-up hardware: recovery re-enumerates the board via a parent-hub unbind/bind and the rail survives the transient. |
| `powerbox_recover_cooldown_s` | `20.0` | Minimum spacing between forced resets, so a permanently dead link is not reset in a tight loop while it re-enumerates. |
| `powerbox_stale_s` | `15.0` | No-frame timeout before the link is marked stale and recovery is triggered. |

With the defaults the watchdog only *reports* the stall (no MCU reset). Enabling
recovery is the same risk as a manual `prius-usb-power cycle` and carries the
same precondition: **the rail must survive the reset** (ACC on, or the OUT1
self-latch hardware in place). To recover by hand instead, follow *Recovery
procedure & caveat* below.

Recovery / control is per **hub port** via [`uhubctl`](https://github.com/mvp/uhubctl)
(apk package `uhubctl`), wrapped by `prius-usb-power`.

### Port map (devices live on dedicated hub ports)

| Hub port (`1-1`) | Device | by-id |
|------------------|--------|-------|
| **port 1** | **powerbox** (12 V telemetry + ACC/ignition) | `usb-MicroPython_Board_in_FS_mode_503359277a7c699f-if00` |
| **port 2** | **gateway** (CAN / AVC-LAN / RS485) | (attached on its own dedicated port) |

The backend's USB discovery uses this topology as the **primary** strategy
(`discover_roles_combined` → `discover_roles_by_port`, `DEFAULT_PORT_ROLES =
{1: powerbox, 2: gateway}`): role is resolved purely from the physical port, so it
binds the right board **even while it is silent/wedged** and is immune to ACM
renumbering / replugging. `whoami` probing is only the fallback for unmapped ports.

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
4. `mpremote resume connect <by-id> reset` to boot the new firmware. `OUT1` latches
   HIGH in the first lines of `main()`, so the computer keeps power through the reset.
5. `sudo systemctl restart prius-backend` immediately — the new firmware's POCO
   boot-grace is 60 s, and the backend must resume 2 s heartbeats within it or the
   firmware will pulse the POCO power button (GP15). Restarting at once is well inside
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
mpremote connect /dev/ttyACM0 fs cp ina219.py ahtx0.py bmp280.py main.py :
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
