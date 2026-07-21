# Prius Powerbox

This directory contains the MicroPython code to manage and monitor power via an INA219 sensor connected to an RP2040 (Raspberry Pi Pico). The RP2040 is connected via USB to the `prius` computer (a POCO F1 running postmarketOS) and communicates via an NDJSON protocol.

## Architecture & Protocol

The powerbox speaks NDJSON over USB-CDC directly to the POCO. It uses the same envelope structure as the `cyberpunk-prius-gateway`:
`{"id": <device_id>, "d": <payload>}`

Device IDs (locally):
* **0 (System)**: ready banner, IDENT, command acks, errors, and the periodic
  `STATUS` heartbeat (OUT rail states + POCO liveness + rolling counter)
* **1 (Telemetry)**: INA219 voltage, current, and accumulated mAh
* **2 (Events)**: Ignition (ACC) and battery connection status

*Note: On the POCO side, the `MultiInputPort` offsets these IDs by `DEVICE_POWERBOX_BASE` (200), so the computer sees them as IDs 200, 201, and 202 respectively.*

### Ignition (ACC)

The accessory line is sensed on **GP11** with **inverted (active-low) logic**:
`ACC ON => GP11 reads LOW (0)`, `ACC OFF => GP11 reads HIGH (1)`. The firmware
enables the internal pull-up and reports `acc_on = (GP11 == 0)` on the Events
channel (`{"acc": true|false, "batt": true}`) at startup, on every change, and
as a periodic heartbeat (~5 s).

To check the current pin level over the REPL on `prius`:
```bash
~/.local/bin/mpremote connect /dev/ttyACM0 exec \
  "from machine import Pin; print(Pin(11, Pin.IN, Pin.PULL_UP).value())"
# prints 0 when ACC is ON (inverted logic), 1 when ACC is OFF
```

## Power Management (OUT1/OUT2/OUT3 + POCO control + heartbeat)

The powerbox switches the whole computer's power and coordinates a clean
shutdown with the POCO. Three high-side MOSFET drivers are controlled, **all
downstream of the INA219 shunt** (so the coulomb count is the whole-computer
draw). All are **active-high** (HIGH = rail enabled):

| Rail | Pin  | Drives | Boot default | Notes |
|------|------|--------|--------------|-------|
| OUT1 | GP29 | Master rail: POCO + RP2040 + USB-hub 5 V | **HIGH (latched)** | `computer_power = OUT1 OR ACC`. Drive LOW = **suicide**. |
| OUT2 | GP28 | RS485 satellite power | **ON** | Controllable while the Prius is off. |
| OUT3 | GP27 | Spare | OFF | Unused. |

### OUT1 master latch & "suicide"

The hardware OR-s OUT1 with the ACC (ignition) line: `computer_power = OUT1 OR
ACC`. So the RP2040 **powers up automatically when the ignition is ON**, even
before firmware runs. The firmware then drives **OUT1 HIGH immediately at boot**
to *latch* the rail, so the computer keeps running after ACC drops (the latch
holds power for a graceful shutdown).

Driving **OUT1 LOW is the "suicide"**: it removes the latch. Once ACC is also
gone, the whole computer (POCO + RP2040 + hub) loses power — protecting the 12 V
battery from deep discharge. If ACC is still energising the rail, the board stays
powered but holds the latch LOW so power drops the instant ACC does.

OUT1 is **never** host-controllable directly; it is only dropped by the
shutdown→suicide path below.

### Shutdown → suicide sequence

1. **Trigger** — either the backend sends `{"a":"off","grace_s":N}` (its
   `UndervoltageProtectionRule` fires at < 11.0 V for 5 s), **or** the firmware's
   own last-resort backstop trips (rail < `SUICIDE_VOLTAGE` 10.0 V for
   `SUICIDE_CONFIRM_MS` 20 s — only when the backend is dead).
2. **Graceful window** — the powerbox enters `shutdown` state and stops trying to
   wake the POCO. The backend (or operator) does the clean OS poweroff.
3. **Suicide** — when the POCO is confirmed down (its heartbeat stops) **or** the
   grace timeout elapses, OUT1 goes LOW and the board enters `dead` state.

### POCO power button (GP15)

GP15 is soldered directly across the POCO's power button (which triggers by being
shorted to ground). The firmware "presses" it by driving GP15 **LOW**; at all
other times it is **high-impedance** (`Pin.IN`) and is **never driven HIGH**.

* `~3000 ms` press → power the POCO **ON** from off.
* `~10000 ms` press → force a hard reboot.

If the POCO's heartbeat is lost while it *should* be running (past the
`POCO_BOOT_GRACE_MS` 60 s boot grace, and a `POCO_WAKE_COOLDOWN_MS` 60 s cooldown
since the last press), the firmware pulses GP15 ~3 s to wake it. Waking is
disabled during/after a shutdown so we never fight our own poweroff.

### Bidirectional heartbeat

A rolling "automotive" counter lets each side detect if the other died:

* **powerbox → POCO**: a `STATUS` message every `HEARTBEAT_TX_MS` (1 s) on the
  System channel: `{"msg":"STATUS","hb":n,"out1":1,"out2":1,"out3":0,"poco":1,"pm":"normal"}`
  where `hb` is the rolling counter and `pm` is the state (`normal`/`shutdown`/`dead`).
* **POCO → powerbox**: the backend sends `{"a":"hb","n":n}` every
  `powerbox_heartbeat_s` (2 s). The powerbox treats the POCO as **dead** if this
  counter stops *advancing* for `POCO_HB_TIMEOUT_MS` (15 s) — a stuck/repeating
  counter also reads as dead.

### Inbound commands (POCO → powerbox, on System id 0)

| Command | Payload | Effect |
|---------|---------|--------|
| Shutdown | `{"a":"off","grace_s":30}` | Begin shutdown → suicide. |
| Heartbeat | `{"a":"hb","n":0-255}` | Keep "POCO alive" fresh. |
| Set rail | `{"a":"out","ch":2|3,"on":true}` | Toggle OUT2/OUT3 (OUT1 rejected). |
| Power button | `{"a":"button","ms":3000}` | Pulse GP15 (wake / force reboot). |
| Interval | `{"a":"set_interval","ms":1000}` | Telemetry cadence. |
| Identify | `{"a":"whoami"}` | IDENT reply. |
| Ping | `{"a":"ping"}` | PONG. |
| Reset mAh | `{"a":"reset_mah"}` | Zero the coulomb counter. |

## Reliability — USB-CDC write model

A MicroPython `sys.stdout.write` to USB-CDC is **blocking**: when no host has the
port open / is actively draining it, the small (~64–256 B) CDC TX FIFO fills and the
next write blocks forever, deadlocking the firmware. A no-drainer gap can occur when
the backend restarts or when something opens → writes → closes the CDC port.

The firmware guards every `tx()` with a **non-blocking** write-readiness probe —
`select.poll()` registered on `sys.stdout` for `POLLOUT`, checked with `poll(0)`. If
the link can't accept the write, the line is **dropped** instead of blocking.
Telemetry is periodic, so a dropped sample is harmless and the firmware never wedges,
regardless of whether a host is draining. See the `tx()` / `_host_writable()` helpers
in `main.py`. The board survives a backend-stopped gap (no reset loop) and auto-resumes
when the backend reconnects. A `WDT_TIMEOUT_MS = 8000` watchdog is a last-resort backstop.

Host-side, two measures keep the link clean:
* **Port-based discovery** — the backend resolves roles from the physical hub port
  (`{1: powerbox, 2: gateway}`) and never opens/probes the device.
* **ModemManager-ignore udev rule** (`99-prius-rp2040-nomm.rules`, VID `2e8a`) stops
  MM's AT probes from opening/closing the port; `MODE="0666"` lets the backend open
  the tty without a `chmod` race.

The board runs **`main.py` only** (autostarts; has the WDT + a 3 s Ctrl-C safe-boot
window). The powerbox MCU is **bus-powered** (runs off USB VBUS; no direct 12 V
line), **but the USB bus is fed from the OUT1 12 V rail**:
`OUT1 (12 V) → USB hub 5 V → VBUS → MCU`. So cutting hub-port VBUS is a true cold
reset (`prius-usb-power cycle`, ≥ 4 s off → `energy_mah` resets to ~0), *and* the
MCU latches its own power by driving OUT1 HIGH (survives ACC removal; dropping
OUT1 = suicide). **A brief MCU reset (reflash, DTR pulse, WDT, soft reboot) is
safe even with ACC off:** the OUT1 rail has a **hardware >30 s hold-up** that
sustains power across the reset, and the rebooting firmware re-drives OUT1 HIGH
(3 s safe-boot then latch) well within that window. Only a *sustained* power loss
beyond the hold-up drops the rail — e.g. a VBUS cut held longer than ~30 s, or a
commanded suicide, with ACC off. (Before the hold-up hardware was added, any
MCU reset with ACC off was fatal — see commit `783e80a`, which also removed the
`powerbox_recover_requires_acc` guard.) See `POCO/POWER.md` for the full power
model + the host-side staleness watchdog that detects a silent USB-CDC link stall.

> **Firmware vs link:** the non-blocking `tx()` guard above stops the *firmware*
> from blocking on a full TX FIFO; it does not stop the *link* from going silent at
> the USB/host layer. A silent stall (board alive, host receiving nothing) is
> handled on the backend by `BackendService._powerbox_watchdog_tick()`, which flips
> `powerbox.connected → False` after ~6 s of no frames and auto-clears on resume.

## Hardware Setup
* **Host System:** POCO F1 (`prius`) running postmarketOS.
* **USB Hub:** labeled D-Link DUB-H4 **rev D1**, but internally an **ActionStar `2101:8500`** (same guts as the unsupported rev C1 — uhubctl issue #88; a truly per-port rev D enumerates as Genesys `05E3:0608`). Bench-tested 2026-07-21: **only the socket on internal port 5 has switchable VBUS** — put the **gateway** there; all other sockets are hardwired 5 V (off/cycle = data-only), and even an all-ports gang off doesn't cut them. The built-in HID `2101:8501` is the LED controller, not a power interface. (Previous hub: DUB-H4 rev F1, Terminus FE1.1s `1a40:0101`, fully ganged — there `hubcycle` really cut all VBUS.)
* **Microcontroller:** Raspberry Pi Pico (RP2040) connected to **Port 1** of the USB Hub.
* **Power:** The RP2040 is **bus-powered** (runs off USB VBUS — no direct 12 V line to the MCU), **but the USB bus is fed from the OUT1 12 V rail** (`OUT1 → hub 5 V → VBUS → MCU`). The INA219 *senses* the 12 V system. Because the MCU drives OUT1 and OUT1 gates the hub that powers it, the board latches its own power (survives ACC removal) and a USB VBUS cut cold-resets it.

### Pinout (RP2040 / Raspberry Pi Pico)

| GP | Function | Direction | Notes |
|----|----------|-----------|-------|
| GP0 | I2C0 SDA | — | INA219 (+ optional BMP280/AHT20). Falls back to `SDA=GP1/SCL=GP0` SoftI2C if swapped. |
| GP1 | I2C0 SCL | — | |
| GP11 | ACC / ignition sense | IN (PULL_UP) | Inverted: ACC ON = LOW (0), ACC OFF = HIGH (1). |
| GP15 | POCO power button | IN (high-Z) / OUT-LOW pulse | Soldered across the phone button; drive LOW = press, never HIGH. |
| GP27 | OUT3 spare MOSFET | OUT | Active-high. Default OFF. |
| GP28 | OUT2 RS485 satellite power MOSFET | OUT | Active-high. Default ON. |
| GP29 | OUT1 master rail MOSFET | OUT | Active-high. Latched HIGH at boot; LOW = suicide. `computer_power = OUT1 OR ACC`. |

All three OUTn rails are wired **downstream of the INA219 shunt**, so the
measured current/`mah` is the total computer draw. If your MOSFET drivers are
active-low, invert the `value=` in `setup_power_pins()` (the firmware assumes
active-high).

## Infrastructure Configuration (`prius`)

The `prius` host has been configured to remotely manage and program the RP2040 without needing physical access:

1. **`mpremote` Installation**: The official MicroPython tool `mpremote` is installed on `prius` (`~/.local/bin/mpremote`). This allows accessing the REPL and file system of the RP2040 over SSH.
2. **`uhubctl` Installation**: The `uhubctl` package is installed on `prius` to allow programmatic power cycling of the USB hub ports.
3. **Power Cycling the RP2040**:
   On this hub only the internal-port-5 socket really switches VBUS (see Hardware Setup above); keep the force flag (`-f`) — the descriptor claims "ganged" and blocks port commands without it.

   To power cycle the RP2040 on Port 1 (use **≥ 4 s off** for a true cold reset —
   the MCU is bus-powered, so cutting VBUS fully resets it; do this with ACC on so
   the OUT1 rail comes back):
   ```bash
   sudo uhubctl -f -l 1-1 -p 1 -a off
   sleep 4
   sudo uhubctl -f -l 1-1 -p 1 -a on
   ```

## Flashing firmware (OTA, no BOOTSEL)

`main.py` auto-runs at boot and the firmware soft-reboots on `Ctrl-C` (`sys.exit` →
`MPY: soft reboot` → re-run), so there is **no stable REPL** and a plain
`mpremote fs cp` fails with `could not enter raw repl` (mpremote's default soft-reset
re-runs `main.py` and the streaming hides the raw-REPL prompt).

Use the packaged wrapper on `prius` (proven reliable, succeeds on attempt 1–2):

```bash
sudo systemctl stop prius-backend            # release the USB-CDC port
prius-flash-powerbox ~/cyberpunk_computer/powerbox/main.py
sudo systemctl restart prius-backend         # within 60 s (POCO boot-grace) so the
                                             # firmware doesn't pulse the POCO button
```

How it works: a pyserial `Ctrl-C` nudge drops the board into the *momentary*
post-`sys.exit` REPL window, then `mpremote resume` (which **skips** the soft-reset)
enters raw-REPL there and copies the file; once in raw-REPL the board is held (no
auto-reboot), so the copy completes. The wrapper then verifies the on-device `VERSION`
and `reset`s into the new firmware (`OUT1` latches HIGH first, so the computer keeps
power). Full write-up and pitfalls in [`../POCO/POWER.md`](../POCO/POWER.md). Do **not**
close+reopen the port (DTR drop resets the board; reaching the REPL re-enumerates the
CDC and kills the fd); 1200-baud touch does **not** trigger BOOTSEL on MicroPython
RP2040. Only fall back to the BOOTSEL + `flash_nuke` reflash if the board never streams
at all.

## Rescue Scripts
If the microcontroller hits a fast bootloop (e.g., due to an I2C initialization exception), use the provided rescue scripts to drop to REPL and delete the offending `main.py` before it crashes.
* `rescue.py`: Python-based fast-interrupt via USB cycling and Ctrl-C spam.
* `rescue.sh`: Shell script wrapper around `mpremote`.
