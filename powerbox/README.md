# Prius Powerbox

This directory contains the MicroPython code to manage and monitor power via an INA219 sensor connected to an RP2040 (Raspberry Pi Pico). The RP2040 is connected via USB to the `prius` computer (a POCO F1 running postmarketOS) and communicates via an NDJSON protocol.

## Architecture & Protocol

The powerbox speaks NDJSON over USB-CDC directly to the POCO. It uses the same envelope structure as the `cyberpunk-prius-gateway`:
`{"id": <device_id>, "d": <payload>}`

Device IDs (locally):
* **0 (System)**: ready banner, IDENT, command acks, errors, and the periodic
  `STATUS` heartbeat (relay read-back + POCO liveness + rolling counter +
  reset cause / uptime)
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

Current firmware: **2.3.0** (2.2.3: BEAN transceiver bench on GP6/GP7 and
5 ms relay read-back; 2.2.4: I2C at 100 kHz; 2.2.5: bench ACC override
`acc_sim`; 2.3.0: receive-only BEAN decoder `bean_rx.py` + TX guard).

## Power Management (POCO button + relay board + heartbeat + sleep) — firmware 2.1

There are **no power rails** since 2.0. The powerbox, its hub and both POCO
bucks sit on the permanent 12 V feed; everything switchable is on the
8-channel relay board (next section). The box's job is to **start and wake the
main POCO through its power button**, switch the relays on command, and tell
the host what it sees.

| Pin | Function | Idle | Notes |
|---|---|---|---|
| GP27 | main POCO button | input, pulls off | LOW = press. 6 s boots an off phone (3 s does not; verified 2026-11-03), 35 s force-resets a frozen SoC. Never driven high. |
| — | "poco 2" slot | **not connected** (`POCO2_BTN_PIN = None`) | No second POCO exists (single-computer layout, 2026-11-04). `{"a":"button","poco":2}` / `{"a":"wake","poco":2}` reply `ERROR POCO2_NC` and press nothing. |

### Main POCO wake ladder

The host (POCO backend) heartbeats `{"a":"hb","n":N}`; if the counter stops
changing for 15 s the POCO is dead. After the boot grace (20 s) the firmware
presses GP27 for 6 s, waits 5 min (cooldown ≥ boot-to-first-heartbeat), presses
again, and after two unanswered presses holds 35 s (frozen-SoC reset), then
restarts the ladder.

### Shutdown and sleep

`{"a":"off","reason":..,"grace_s":30}` means **the host is halting itself**
(the box cannot cut power). The firmware enters `shutdown`, waits for the
heartbeat to stop (≥ 25 s) or the grace, then `sleep`: no wake presses until
**ACC rises** or `{"a":"wake"}`. The under-voltage backstop (< 10 V for 20 s)
does the same from the firmware side and sends `SHUTDOWN` to the host, which
must `systemctl poweroff` itself (backend default since 2.0).

### roadsense on the same POCO

Since 2026-11-04 there is **one** POCO: it runs the backend and the roadsense
perception stack (camerad-dvr / modeld / envd / api + the signd provider) as
systemd units under `roadsense.target`. The backend's `RoadsenseManager`
(`backend/roadsense_power.py`, `BACKEND_ROADSENSE_*`) follows ACC: key-on →
relay ch4 (camera hub) on, 8 s settle, `systemctl start roadsense.target`;
key-off → 30 s grace → `systemctl stop`, wait for the units, relay ch4 off. The
powerbox is not involved beyond relay ch4; the legacy `p2`/`poco 2` fields stay
in the protocol but mean nothing.

### Main POCO power button (GP27)

The pin is wired straight across the phone's power button (the line idles at
~1.8 V from the PMIC pull-up and triggers when pulled to GND). The firmware
"presses" by driving the pin **LOW**; at all other times it is an **input with
pulls off** and is **never driven HIGH**. GP27 is an ADC pad (GP26-29), which
resets with input disabled and no pulls, so the button is untouched through
the bootloader and WDT resets. Bench 2026-11-03: a 6 s LOW from the REPL
booted the phone from off (kernel start → Wi-Fi + ssh ~35 s later);
`{"a":"button","poco":1,"ms":N}` presses on command.

### Relay board (PCF8574 @0x20, 8 × 12 V coils, active-low inputs)

IN1..IN8 sit on PCF P0..P7, so **board relay N = firmware channel N**
(`RELAY_PIN_MAP = {n: n-1}` in `pcf8574.py`). JD-VCC = 12 V always on (JD
jumper removed); the opto/input VCC comes from the **RP2040-Zero 5 V pin**, so
whenever the RP2040 is unpowered or resetting **every NO relay releases**. The
PCF powers up at 0xFF (all coils released).

| Ch | Contact | Load | Notes |
|---|---|---|---|
| 1 | **NC** | the powerbox's **own** supply | Energised = hard restart of the box (USB gone 0.4 s, back 1.0 s, STATUS 4.9 s). Only via `{"a":"hard_restart"}`; `relay ch=1` is refused; reported as `null` in `rly`. |
| 2 | NO | MFD Raspberry Pi Zero v1.3 (~100–115 mA @ 12 V) | USB gadget link up ~50 s after power. Hard cuts risk the SD card: the backend does `ssh poweroff` + wait before cutting. |
| 3 | NO | USB 2.0 hub (`1a40:0101`): **CAN gateway + comma.ai panda** | One switch for both. Hub enumerates ~6 s, gateway ~7 s, panda ~9 s after power; 50 → 155 mA @ 12 V. |
| 4 | NO | USB 2.0 hub (VIA `2109:2817`): fvision dashcam (`1902:3241`, USB storage, frames via `VSFILE`) / aux | +84 mA @ 12 V, enumerates ~6 s. |
| 5 | NO | RS485 satellite power | Semantics kept from fw 2.0; **not yet tested** on this board. |
| 6–8 | NO | unassigned | Generic `relay` channels. |

`on` always means **load powered** (`rly` 1): for NO channels the pin goes LOW
(coil energised), for the NC channel HIGH (coil released) — the NC flag is
generic (`RELAY_NC` in `pcf8574.py`). The firmware updates its wanted byte
**only after a successful I2C write**, reports `rly` from a **chip read-back**
every STATUS and **every 5 ms in between** (fw 2.2.3, `RELAY_VERIFY_MS`; see
below), re-writes the wanted byte whenever the read-back disagrees
(lost write, PCF reset to 0xFF) and counts those repairs in `rly_fix`
(`rly_err` = I2C errors; `rly` is omitted when the read-back itself failed).
The first mismatch of a streak is logged as a `rly_fix` PMEVENT.

**5 ms read-back (fw 2.2.3, 2026-11-11).** The PCF8574 keeps power-up
resetting to 0xFF in the field. The read-back now runs every 5 ms so the
re-assert lands inside the relays' ~5 ms release time and the loads should not
actually drop. It runs inside `wdt_sleep_ms()` — the main loop's
inter-telemetry wait and every button hold — deadline-paced (the sleep length
is unchanged). Before 2.2.3 the check ran only once per main-loop pass, i.e.
~1 s, despite the old 100 ms constant. Cost: one 1-byte I2C read per check
(~100 µs on the wire at 100 kHz, ~0.2 ms with interpreter overhead, ~4 %). The bus runs at 100 kHz since fw 2.2.4: the PCF8574 is a standard-mode-only part.
The 5 ms bound does not hold while the loop body itself runs (sensor reads,
STATUS, inbound commands, a running `bean` `loop`/`square`/`pulse` op); those
gaps are a few ms each, up to ~1 s for a bench `square`. `RELAY_IO` errors are
still counted one by one in `rly_err` but reported at most once a second.

**Restart scope.** After `hard_restart` (or any other reset) the MFD Pi, CAN
hub and camera hub drop and come back **OFF**; the backend re-asserts its
wanted states ~6 s after the `POWERBOX_READY` banner and the periodic
read-back/enforce sweep converges the rest. If the box is still alive 3 s
after energising relay 1 (relay not wired?), the coil is released and
`ERROR RESTART_FAILED` is sent.

#### Cold-boot event log (`getlog`, fw ≥ 1.15.0)

The prius-backend that logs powerbox messages runs **on the POCO**, which is off
during a cold boot — so nothing records what the firmware did until the phone is
already up. To close that blind spot the firmware keeps an **in-RAM ring buffer**
of power events (boot, every button press with reason + voltage + wake-tries +
boot-age, shutdown, sleep). The RP2040 stays powered across the whole cold
boot, so the timeline survives. Send `{"a":"getlog"}` (System id 0) to dump it as
a `PMLOG` message; the backend auto-fetches it once the link comes up, so a cold
boot's wake sequence lands in the journal automatically. Each event is also echoed
live as a `PMEVENT` message.

#### Status LED (RP2040-Zero onboard WS2812, GP16)

The board's onboard WS2812 RGB LED (GP16) is driven as a live diagnostic — the
**only** observable firmware output during a cold boot, when the POCO (USB host)
is off and nothing can be logged. Point a camera at it. Colour key (all dim):

| LED | Meaning |
|-----|---------|
| white→blue flash | power-on / reset signature. **Repeating every few seconds = reset loop** (e.g. WDT). |
| green blink | `normal`, POCO **alive** (healthy heartbeat, loop running). |
| amber solid | `normal`, POCO **not** alive — armed to press the wake button. |
| **red solid** | a power-button press is happening **right now** (GP27 driven low). |
| magenta | shutdown in progress. |
| **bright magenta** | `hard_restart`: relay 1 being energised, power about to drop. |
| cyan solid | `sleep` — host shut itself down; no wake presses until ACC rises or `wake`. |

A cold-boot recording therefore shows exactly what the firmware does: a single
boot flash then amber→red(press)→green means it woke the phone; a **dark** LED
means the RP2040 has no power; a **repeating** boot flash means it is reset-looping.
The boot event in `getlog`, the `POWERBOX_READY` banner and every `STATUS`
carry `rc` (`machine.reset_cause()`): `1` = power-on (also brown-out and
`hard_restart`), `3` = watchdog (WDT timeout, `machine.reset()`, `mpremote
reset`); STATUS also carries `up` (uptime, s). The backend mirrors them
(`powerbox.reset_cause`, `powerbox.uptime_s`) and counts banners in
`powerbox.boot_count` — the way to attribute the spontaneous resets seen on
the bench 2026-11-03 (rc 1 → power/wiring, rc 3 → firmware stall). The banner
is parked until the first host heartbeat opens the TX gate, so the backend
sees exactly one banner per firmware boot.

### Bidirectional heartbeat

A rolling "automotive" counter lets each side detect if the other died:

* **powerbox → POCO**: a `STATUS` message every `HEARTBEAT_TX_MS` (2 s) on the
  System channel:
  `{"msg":"STATUS","hb":n,"acc":1,"poco":1,"p2":0,"pm":"normal","rc":1,"up":123,"fan":40,"rpm":2100,"rly":[null,1,1,0,1,0,0,0],"rly_fix":0,"rly_err":0}`
  where `hb` is the rolling counter, `pm` the state (`normal`/`shutdown`/`sleep`),
  `rc`/`up` the reset cause and uptime, and `rly` the relay read-back
  (`[ch1..ch8]`, 1 = load powered, ch1 always `null`).
* **POCO → powerbox**: the backend sends `{"a":"hb","n":n}` every
  `powerbox_heartbeat_s` (2 s). The powerbox treats the POCO as **dead** if this
  counter stops *advancing* for `POCO_HB_TIMEOUT_MS` (15 s) — a stuck/repeating
  counter also reads as dead.

### Inbound commands (POCO → powerbox, on System id 0)

| Command | Payload | Effect |
|---------|---------|--------|
| Shutdown | `{"a":"off","grace_s":30}` | Host is halting itself: stop waking it (`shutdown` → `sleep`). |
| Heartbeat | `{"a":"hb","n":0-255}` | Keep "POCO alive" fresh. |
| Wake | `{"a":"wake","poco":1}` | Leave `sleep` and re-arm the ladder. `poco:2` → `ERROR POCO2_NC` (not connected). |
| p2 alive | `{"a":"p2","alive":true}` | Legacy: host-reported liveness of a second POCO (echoed as `p2` in STATUS); unused since the single-computer layout. |
| Fan | `{"a":"fan","pct":0-100}` | 4-wire fan speed (GP29, not inverted); `duty` 0-65535 accepted for the old form. |
| Power button | `{"a":"button","poco":1,"ms":6000}` | Hold GP27 low (6000 = power on, 35000 = force reset). `poco:2` → `ERROR POCO2_NC`. |
| Relay | `{"a":"relay","ch":2-8,"on":true}` | Power a load channel (ack `relay_<ch>_on`). `ch:1` → `ERROR RELAY` (restart relay). |
| Hard restart | `{"a":"hard_restart"}` | Ack, then energise relay 1 on the next tick: the box cuts its own supply and is back in ~5 s; every NO relay drops. |
| I2C scan | `{"a":"i2c_scan"}` | `I2C_SCAN` reply with the ACKing addresses. |
| Interval | `{"a":"set_interval","ms":1000}` | Telemetry cadence. |
| Identify | `{"a":"whoami"}` | IDENT reply. |
| Ping | `{"a":"ping"}` | PONG. |
| Get event log | `{"a":"getlog"}` | Dump the power-event ring buffer as `PMLOG` (cold-boot timeline). |
| Reset mAh | `{"a":"reset_mah"}` | Zero the coulomb counter. |
| BEAN bench | `{"a":"bean","op":...}` | fw ≥ 2.2.3, see *BEAN transceiver bench* below. TX-driving ops need the TX guard (fw 2.3.0). |
| BEAN TX guard (**bench only**) | `{"a":"bean_tx_enable","v":1\|0}` | fw ≥ 2.3.0. Arms the `bean` TX ops for 60 s (`BEAN_TX_ENABLE_MS`), `v:0` disarms. Ack `{"ack":"bean_tx_enable","v":..,"s":60}`, logged as a `bean_tx` PMEVENT. |
| BEAN receiver | `{"a":"bean_rx","on":0\|1}` / `{"a":"bean_rx","op":"stats"}` / `{"a":"bean_rx","raw":N}` | fw ≥ 2.3.0, see *BEAN receiver* below. |
| ACC override (**bench only**) | `{"a":"acc_sim","v":0\|1\|null}` | fw ≥ 2.2.5. Simulates key-on (1) or key-off (0) without touching the ACC wire; `null` or no `v` clears it. The override is applied in `read_acc()`, the single place the pin enters the logic, so it reaches the Events `{"acc":..}` messages (sent on the next main-loop pass when the effective state changes), STATUS `acc` and the wake/sleep logic. STATUS carries `"acc_sim":1` while it is active. Ack: `{"ack":"acc_sim","v":0\|1\|null,"pin":<real ACC, 1 = on>}`; any other `v` → `ERROR ACC_SIM`. **RAM only:** a firmware boot clears it. It also **auto-expires 30 min** (`ACC_SIM_MAX_MS`) after the last set, then the real pin takes over again (`acc_sim` PMEVENT with `why: expired`). Re-sending restarts the 30 min. Set and clear are logged as `acc_sim` PMEVENTs. Backend: REST `powerbox_acc_sim {"on": true\|false\|null}`, mirrored in `powerbox.acc_sim`. |

### BEAN transceiver bench (fw 2.2.3, 2026-11-11)

Logic verification of the discrete BEAN transceiver on GP7 (TX) / GP6 (RX,
inverted); BEAN itself is 10 kbit/s (100 µs bits), dominant = +B, recessive =
0 V. Not a protocol stack. Every op is synchronous, capped, and leaves TX LOW
(also on errors). Reply: `{"ack":"bean","op":<op>,...}` on id 0, plus a live
`{"msg":"PMEVENT","ev":"bean","t_ms":..,...}` echo for the journal (not kept in
the `getlog` ring). Bad op / out-of-range or non-integer field →
`ERROR BEAN_ARGS` (nothing driven); an exception during an op → `ERROR BEAN`.

| op | fields (default, range) | reply fields |
|---|---|---|
| `status` | — | `tx` (0/1), `rx` (GP6 level), `bus` (1 = dominant, i.e. `rx` low) |
| `tx` | `v` (1, 0–1), `ms` (1, 1–500) | `v`, `ms`, `rx_on` (sampled at the end of the hold), `rx_off` (50 µs after release) |
| `pulse` | `us` (100, 1–2000) | `us`, `rx_mid`, `rx_after` (50 µs after release), `us_real` (measured width) |
| `loop` | `n` (20, 1–200) | n × {TX high → µs to RX low (timeout 2000), hold 300 µs, TX low → µs to RX high, 300 µs gap}: `dom_min/max/avg`, `rec_min/max/avg` (µs; `null` if no edge seen), `to_dom`, `to_rec`, `timeouts`, `rx_idle` (RX before the first round) |
| `square` | `hz` (5000, 100–20000), `ms` (200, 1–1000) | `edges` (RX level changes counted), `expected` (= 2 × `cycles`), `cycles`, `hz_real` |

`square` toggles TX and polls RX in one Python loop; each half period is at
least 500000/`hz` µs counted from the actual toggle, so a loop that cannot keep
up lowers `hz_real` instead of starving the transceiver — `edges == expected`
means RX followed every TX edge at `hz_real`. Interrupts stay enabled (USB,
tach); the longest op (~1 s) is far inside the 8 s WDT, which is fed per round.
Timing caveat: all µs figures come from `time.ticks_us()` in interpreted loops
(resolution = one loop pass, a few µs at 96 MHz, plus jitter from USB/IRQ
service); good for "does RX follow TX and roughly how fast", not for bit
timing. A real BEAN implementation needs PIO for TX/RX.

Backend: REST `powerbox_bean` (`op` + the same optional fields, same caps,
HTTP 400 outside them); the reply is mirrored into `/api/v1/state` →
`powerbox.bean` (the ack fields + `ts`; a BEAN error appears there as
`{"error": "BEAN_ARGS", "detail": ...}`).

## Reliability — USB-CDC write model

A MicroPython `sys.stdout.write` to USB-CDC is **blocking**: when no host has the
port open / is actively draining it, the small (~64–256 B) CDC TX FIFO fills and the
next write blocks forever, deadlocking the firmware. A no-drainer gap can occur when
the backend restarts or when something opens → writes → closes the CDC port.

The firmware guards every `tx()` with a **host-presence gate** (`_host_present`):
outbound frames are written only once the POCO host has proven it is present and
draining the port — i.e. a recent POCO heartbeat (`poco_alive`). While the host is
absent, frames are **dropped** instead of blocking. This matters most on a **cold
boot**: the POCO is off while the firmware powers it on, and during the phone's own
boot its USB attaches *before* Linux opens the ACM — a connected-but-not-drained
window where a blocking write would stall until the 8 s WDT reset the MCU. That reset
made the firmware re-press the power button ~20 s later, killing the still-booting
phone: a press→reset→re-press loop (observed on-bench 2026-08-10, LED reset loop).
Gating TX on host presence removes that stall entirely. Inbound reads are unaffected,
so the first heartbeat re-opens the gate and telemetry resumes; a TX failure now only
increments a diagnostic counter and **never** resets the MCU. Discovery is
topology/port-based and the backend heartbeats unconditionally, so gating TX can't
deadlock the link. See the `tx()` / `set_host_present()` helpers in `main.py`. A
`WDT_TIMEOUT_MS = 8000` watchdog remains a last-resort backstop for a genuine hang.

Host-side, two measures keep the link clean:
* **Port-based discovery** — the backend resolves roles from the physical hub port
  (`{1: powerbox, 2: gateway}`) and never opens/probes the device.
* **ModemManager-ignore udev rule** (`99-prius-rp2040-nomm.rules`, VID `2e8a`) stops
  MM's AT probes from opening/closing the port; `MODE="0666"` lets the backend open
  the tty without a `chmod` race.

The board runs **`main.py`** (autostarts; has the WDT + a 3 s Ctrl-C safe-boot
window) plus a two-line **`boot.py`** that releases the POCO button (GP27) before
anything else: the RP2040's reset-default pull-down would otherwise hold the
button pressed whenever `main.py` is not running and boot-loop the phone. The powerbox MCU is **bus-powered** (runs off USB VBUS; no direct 12 V
line); since 2026-10-26 the hub's 5 V buck hangs on the **permanent 12 V feed**,
so cutting hub-port VBUS is a true cold reset (`prius-usb-power cycle`, ≥ 4 s off
→ `energy_mah` resets to ~0) and **any MCU reset (reflash, DTR pulse, WDT, soft
reboot) is electrically harmless** — there is no rail to lose. (The old OUT1
latch/hold-up story is gone with firmware 2.0.) See `POCO/POWER.md` for the full
power model + the host-side staleness watchdog that detects a silent USB-CDC link stall.

> **Firmware vs link:** the non-blocking `tx()` guard above stops the *firmware*
> from blocking on a full TX FIFO; it does not stop the *link* from going silent at
> the USB/host layer. A silent stall (board alive, host receiving nothing) is
> handled on the backend by `BackendService._powerbox_watchdog_tick()`, which flips
> `powerbox.connected → False` after ~6 s of no frames and auto-clears on resume.

## Hardware Setup
* **Host System:** POCO F1 (`prius`) running postmarketOS.
* **USB Hub:** labeled D-Link DUB-H4 **rev D1**, but internally an **ActionStar `2101:8500`** (same guts as the unsupported rev C1 — uhubctl issue #88; a truly per-port rev D enumerates as Genesys `05E3:0608`). Bench-tested 2026-07-21: **only the socket on internal port 5 has switchable VBUS** — put the **gateway** there; all other sockets are hardwired 5 V (off/cycle = data-only), and even an all-ports gang off doesn't cut them. The built-in HID `2101:8501` is the LED controller, not a power interface. (Previous hub: DUB-H4 rev F1, Terminus FE1.1s `1a40:0101`, fully ganged — there `hubcycle` really cut all VBUS.)
* **Microcontroller:** Waveshare RP2040-Zero connected to **Port 1** of the USB Hub.
* **Power (since 2026-10-26):** the RP2040 is **bus-powered** from the hub, and the hub's 5 V buck and the POCO's buck hang on the **permanent 12 V feed**. There is no master rail and no suicide path any more: the box is always on, the main POCO is only ever started through its power button, and the backend puts the box to "sleep" (no wake presses) when it shuts the phone down, until ACC rises again. The INA219 *senses* the 12 V system.

### Pinout (RP2040 / Raspberry Pi Pico)

| GP | Function | Direction | Notes |
|----|----------|-----------|-------|
| GP0 | I2C0 SDA | — | INA219 @0x40, BMP280 @0x76 (outside box), PCF8574 @0x20 (relays). BMP280 @0x77 and AHT20 @0x38 are not fitted (auto-detected if they ever answer). Falls back to `SDA=GP1/SCL=GP0` SoftI2C if swapped. |
| GP1 | I2C0 SCL | — | |
| GP11 | ACC / ignition sense | IN (PULL_UP) | Inverted: ACC ON = LOW (0), ACC OFF = HIGH (1). |
| GP27 | Main POCO power button | IN (pulls off) / OUT-LOW pulse | Wired straight across the phone's button (line idles ~1.8 V). 6 s hold powers on (bench 2026-11-03); 35 s forces a reset of a frozen SoC. Never driven HIGH. |
| GP28 | Chassis fan tach | IN (PULL_UP), falling-edge IRQ | Fan's open-collector tach, 2 pulses/rev → `fan_rpm` in telemetry. |
| GP29 | Chassis fan PWM | PWM 25 kHz, **not inverted** | Straight to the fan's PWM wire, no transistor: GPIO duty = fan duty. Undriven (bootloader, safe-boot) = fan at full speed; the firmware takes it over first thing in `main()`. |
| — | "poco 2" slot | not connected | `POCO2_BTN_PIN = None`; no second POCO exists. Must never share a fan pin (a fan PWM on a button line presses the phone). |
| GP6 | BEAN transceiver RX (fw 2.2.3) | IN, **no internal pull** | Q3 collector with R8 10k pull-up to 3V3 on the board. **Inverted**: bus idle (0 V, recessive) → 1, bus at +B (dominant) → 0. |
| GP7 | BEAN transceiver TX (fw 2.2.3) | OUT, **LOW at boot** | R2 5.1k → Q1 BC547 → Q2 BC327 high side → D1 → R5 220R → bus. HIGH = bus driven to ~+B (dominant), LOW = released. Set LOW in `boot.py` and again first thing in `main()` (the reset-default pad pull-down keeps Q1 off before that); every `bean` op releases it in a `finally` and the main loop forces it low each pass. R5 is a short-circuit limit only: never hold TX high for long (op cap 500 ms). |
| GP14, GP15 | unused, **dead** | — | Both pads are blown inside the RP2040 (2026-10-27: diode mode to GND 0.045 V instead of ~0.5 V, 11 Ω between the two pins, carrier wiring verified clean). Never assign them. |

**Button wiring, verified on the bench** (2026-10-26 with the phone's
input-event log, 2026-11-03 on GP27 from the REPL): as a plain input with pulls
off the line sits at ~1.8 V and the phone sees nothing; a 300 ms LOW gives
exactly one press/release; a 3 s hold does not boot the phone from off, a 6 s
hold does. Every earlier "the pin holds the button" failure was the wire
sitting on a **GND header position**, not a pad property — identify the pin by
blinking it before soldering.

**No power rails since firmware 2.0.** The powerbox, its hub and both POCO
bucks sit on the permanent 12 V feed; the main POCO is only ever started
through its button, and everything switchable is on the 8-channel relay board
(see *Relay board* above). The `out` command returns `OUT_REMOVED`.

### Environmental sensors (I2C0)

| Sensor | Addr | Placement | Telemetry keys |
|--------|------|-----------|----------------|
| BMP280 #2 | `0x76` (SDO low) | **Outside** the box — cabin ambient reference | `bmp2_t`, `bmp2_p` |
| BMP280 #1 | `0x77` (SDO high) | inside the box — **not fitted** | (`bmp_t`, `bmp_p` omitted) |
| AHT20 | `0x38` | inside the box — **not fitted** | (`aht_t`, `aht_h` omitted) |

Absent sensors are simply not bound at boot (no error); a fitted sensor that
stops answering is dropped after one `*_READ` ERROR instead of stalling the
telemetry loop. The backend's chassis-fan controller uses `bmp2_t` as the
ambient reference for the POCO delta; its box-purge loop on `bmp_t − bmp2_t`
stays inert while `bmp_t` is missing.

The chassis fan is a **4-wire BLDC** (ADDA AB07505HX110B00 / Dell 00G700, 5 V,
0.40 A: red 5 V, black GND, blue PWM, yellow tach) on a 5 V buck. The firmware
drives its PWM input at 25 kHz **directly from GP29, not inverted** (GPIO duty =
fan duty; the former 2N7000 stage is gone) and reports the tach RPM from GP28,
so the backend only sends `{"a":"fan","pct":0-100}` (the old
`duty`/`pin`/`freq` form is accepted, `duty` as 0-65535). Boot default 0 %,
which **stops this fan completely** (no minimum-speed floor). While GP29 is
undriven (bootloader, safe-boot window) the fan runs at full speed.

Duty → RPM, bench sweep 2026-11-03 (5 % steps, 4 s settle + 3 s count, no
hysteresis, ±20 RPM):

| duty % | 0 | 5 | 10 | 15 | 20 | 25 | 30 | 35 | 40 | 45 | 50 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| RPM | 0 | 255 | 650 | 1025 | 1475 | 1675 | 1965 | 2240 | 2505 | 2735 | 2965 |

| duty % | 55 | 60 | 65 | 70 | 75 | 80 | 85 | 90 | 95 | 100 |
|---|---|---|---|---|---|---|---|---|---|---|
| RPM | 3170 | 3380 | 3565 | 3745 | 3920 | 4085 | 4245 | 4400 | 4540 | 4680 |

Near-linear up to ~50 %, flattening above.

## Infrastructure Configuration (`prius`)

The `prius` host has been configured to remotely manage and program the RP2040 without needing physical access:

1. **`mpremote` Installation**: The official MicroPython tool `mpremote` is installed on `prius` (`~/.local/bin/mpremote`). This allows accessing the REPL and file system of the RP2040 over SSH.
2. **`uhubctl` Installation**: The `uhubctl` package is installed on `prius` to allow programmatic power cycling of the USB hub ports.
3. **Power Cycling the RP2040**:
   On this hub only the internal-port-5 socket really switches VBUS (see Hardware Setup above); keep the force flag (`-f`) — the descriptor claims "ganged" and blocks port commands without it.

   To power cycle the RP2040 on Port 1 (use **≥ 4 s off** for a true cold reset —
   the MCU is bus-powered, so cutting VBUS fully resets it):
   ```bash
   sudo uhubctl -f -l 1-1 -p 1 -a off
   sleep 4
   sudo uhubctl -f -l 1-1 -p 1 -a on
   ```

### BEAN receiver (fw 2.3.0, `bean_rx.py`)

Receive-only decoder for the Toyota BEAN body bus (protocol:
`docs/BEAN_PROTOCOL.md`). It only reads GP6; it never drives GP7 and never
ACKs. It starts after the safe-boot window and is ON at boot. If
`bean_rx.py` is missing or fails to import or start, the firmware runs as
before: the `bean_rx` PMEVENT carries the error, and `bean_rx` commands answer
`ERROR BEAN_RX <detail>`.

**Capture.** A PIO program on PIO1 SM0 at 2 MHz measures every GP6 level run:
a 2-cycle count loop gives 1 µs resolution. It pushes one word per edge, with
bit 16 = the GP6 level of the run that ended and bits 15..0 = the count left,
so run = `10000 − x + 3` µs. A run longer than 10 ms pushes a timeout word
(`x = 0xFFFF`) and keeps counting, so idle and stuck-bus periods are seen in
real time (~100 words/s while the bus sleeps). The program is 27
instructions. The words go to a 1024-word RAM ring through **rp2.DMA**
(MicroPython ≥ 1.21; the write address wraps on the 4 KB-aligned ring, and
the producer index comes from the DMA transfer count). Without `rp2.DMA` the
firmware falls back to a hard IRQ handler that drains the 4-word RX FIFO into
the ring (mode `irq`). If that also fails it polls the FIFO (mode `poll`,
which loses data whenever the loop blocks for more than ~1 ms). The mode is
in the ack, the stats and the `bean_rx` PMEVENT. PIO RX stalls are counted
(`stall`).

*Ring capacity:* busy traffic is 45 frames/s × ~40 runs = ~1800 words/s, so
1024 words cover **~550 ms** of a blocked main loop with no loss (more at
normal rates). A 100 ms I2C/USB stall uses ~20 % of the ring. A blocked loop
longer than that drops the backlog: `ovf` +1, `lost` += words, and the frame
in progress is reported as `ovf`. The decoder then resyncs on the next idle
gap.

**Decode** runs in `poll()`, called from every `wdt_sleep_ms` slice (~5 ms)
and never more than 256 words per call. The steps:
- Each run is rounded to whole bit times. The bit time adapts (fast for the
  first ~250 runs after start, slow afterwards), and a dominant/recessive
  skew estimate compensates an asymmetric comparator.
- Pulses shorter than 12 µs are merged into their neighbours (glitch filter).
- Destuffing is done per run, followed by the frame state machine and the
  CRC-8 check.
- The EOM `0111111` is searched for after the CRC, tolerating a stuff bit
  before it or its absence.
- The two RSP bits are reported raw.

Tested tolerance (CPython, see tests): ±20 µs jitter on every edge with no
loss, ±30 µs comparator skew, and a bit clock of 80–120 µs (from cold, ±8 %
loses at most the first frame).

**Frames** go out on **id 3** (backend 203), batched at most every 200 ms,
up to 16 frames per line, with a 64-frame queue:

```json
{"t":123456,"bf":[[123290,6,"FE","2C","64",1,0],[123301,2,"FE","7F","001000000000",1,0]],
 "st":{...every 2 s...}}
```

- Record: `[t_ms, pri, "DST", "MES", "DATAHEX", rsp, code]`.
- `t_ms` is the firmware `ticks_ms` of the SOF (±~1 ms). `t` is `ticks_ms`
  at send time, so the backend gets the frame age from `t − t_ms`.
- `rsp` is the 2 raw RSP bits (`1` = `01` = ACK, `0` = no ACK, `2` = NAK,
  `3` = both). It is `null` when the frame broke before RSP.
- `code`: 0 ok, 1 crc, 2 stuff, 3 len (ML outside 3..13), 4 frm (EOM, RSP or
  EOF shape wrong, or bus stuck dominant), 5 trunc (idle inside the frame),
  6 ovf.
- For failed frames DATAHEX holds every byte after MES that arrived, which
  may include the CRC.
- At most 5 failed frames are reported per second. All of them are counted;
  the ones not reported go to `esup`.
- `qdrop` counts frames dropped because the queue was full. `txdrop` counts
  frames lost because the host was absent (the TX gate was closed).

`st` counters:
- Frame results: `ok`, `crc`, `stuff`, `len`, `frm`, `trunc`, `ovf`.
- Capture: `edges`, `glitch`, `lost`, `stall`, `lag` (largest backlog in
  words since the last stats).
- Reporting: `qdrop`, `txdrop`, `esup`.
- Timing: `tb` (bit time in 0.1 µs), `skew` (µs, dominant longer = +),
  `idle_ms` (time since the last edge), `age_ms` (time since the last good
  frame, −1 = none).
- Load: `cpu` (decoder time in per mille of wall time).
- State: `on`, `mode`, plus `err` / `exc` when something failed.

| command | reply |
|---|---|
| `{"a":"bean_rx","on":0\|1}` | `{"ack":"bean_rx","on":..,"mode":"dma"\|"irq"\|"poll"\|null}`; start failure → `ERROR BEAN_RX` |
| `{"a":"bean_rx","op":"stats"}` | ack on id 0 + an immediate `{"t":..,"st":{..}}` on id 3 |
| `{"a":"bean_rx","raw":N}` (1–512) | ack; then the next N raw runs on id 3: `{"raw":[-2000,100,-300,..],"ri":0,"rn":N}` in chunks of 64 (µs, + = dominant, − = recessive) for checking the analogue side |

**Robustness.** `poll()` never raises. After 10 consecutive internal errors
it stops itself (`err` in the stats). It uses preallocated buffers: no
allocation per edge. Allocation happens only per decoded frame (the record)
and per line.

**Cost (estimate, verify with `cpu`).** About 40–80 µs of interpreted or
native Python per run, plus ~0.1 ms per poll:
- ~2–4 % CPU at 15 frames/s;
- ~8–14 % during the 45 frames/s IG-ON burst.

RAM: ~8 KB for the DMA ring (allocated at twice the size for alignment), a
compiled module of ~10 KB, and up to 64 queued frame records. MicroPython
≥ 1.21 is needed for DMA; any rp2 port works with the `irq`/`poll`
fallbacks.

**TX guard.** The receiver runs on the car's live bus, so every TX-driving
bench op (`bean` `tx` / `pulse` / `loop` / `square`) answers
`ERROR BEAN_TX_DISABLED` without touching GP7 unless
`{"a":"bean_tx_enable","v":1}` was sent less than 60 s ago. `status` always
works. `BEAN_TX_ENABLED = False` at boot (RAM only).

Backend:
- `powerbox.bean_rx`: counters, `fps` over 5 s, `last_frame_ts`, `mode`,
  `error`, `raw`.
- `bean.messages`: the table keyed by "DST:MES" (count, last data, changes,
  period, pri).
- `bean.decoded`: known IDs, all *unverified* on the Prius (coolant 0x2C,
  shift 0x40, speed 0x24, ambient 0xCD, lamps 0x7F, odometer 0x3F).
- `GET /api/v1/bean/frames?since=<seq>&limit=<n>`: the last 200 frames
  (kept out of the 1 Hz state).
- REST commands: `powerbox_bean_rx`, `bean_capture {"on":true,"name":".."}`
  (log in `logs/bean/`, rotated at 20 MB; env `BACKEND_BEAN_CAPTURE_DIR`),
  `bean_mark {"text":".."}`, `powerbox_bean_tx_enable {"on":bool}`.

## Flashing firmware (over the NDJSON link, firmware >= 2.2.0)

Updates never use the REPL. The firmware receives files over its normal link
(`put_begin` / `put` / `put_end`, then `reboot`; see the protocol block in
`main.py`) while it keeps running, so the POCO button stays released and the
WDT fed. Each file is checked on the board (size + sha256) before it replaces
the old one (kept as `<name>.bak`; `boot.py` puts `main.py.bak` back if a swap
was interrupted).

On the POCO (files synced by `POCO/sync.sh --app` to `/home/user/cyberpunk/powerbox`):

```bash
sudo prius-flash-powerbox                    # main.py + boot.py + bean_rx.py; stops/starts prius-backend
```

From a bench host with the board on USB (nothing else may hold the port):

```bash
python3 powerbox/pb_flash.py powerbox/main.py powerbox/boot.py powerbox/bean_rx.py
```

`pb_flash.py` always sends `main.py` first. A firmware older than 2.3.0
refuses the new name `bean_rx.py` (`OTA_FILES`). In that case `pb_flash.py`
reboots into the new `main.py`, which runs without the receiver until the
file arrives, and then retries the file once. The first 2.3.0 update
therefore reboots twice.

`pb_flash.py` sends the host heartbeat itself, so the wake ladder never presses
the POCO button during an update. A full update of `main.py` + `boot.py` takes
~40 s including the reboot.

### Why there is no REPL path any more

Every RP2040 pad resets with its pull-down on. On GP27 (POCO button, active LOW)
that reads as a held button whenever `main.py` is not running, and the phone
boot-loops (2026-11-04). Hence:

- `boot.py` releases GP27 first thing on every reset (and restores an interrupted
  swap); the line is LOW only for the MicroPython start-up time after a reset.
- `Button.press` releases in a `finally`; Ctrl-C handlers release before exiting.
- After the 3 s safe-boot window Ctrl-C is disabled (`micropython.kbd_intr(-1)`):
  a stray 0x03 cannot drop the board into a REPL with the WDT armed.
- The REPL is reachable only by Ctrl-C inside the safe-boot window (no WDT yet).
  **Disconnect the POCO button wire before doing that**, and before any BOOTSEL /
  UF2 reflash of MicroPython itself: no code of ours runs then, so GP27 sits LOW.
- `POCO_BTN_ACTIVE_HIGH` (main.py) / `ACTIVE_HIGH` (boot.py) switch to driving the
  button through an N-MOSFET (GP27 → ~100 Ω → gate, ~100 kΩ gate→GND, drain →
  button line, source → GND). With it, reset and bootloader leave the button
  released by hardware. Set both to `True` only once the MOSFET is fitted.

## Rescue Scripts
If the microcontroller hits a fast bootloop (e.g., due to an I2C initialization exception), use the provided rescue scripts to drop to REPL and delete the offending `main.py` before it crashes. Disconnect the POCO button wire first (see above).
* `rescue.py`: Python-based fast-interrupt via USB cycling and Ctrl-C spam.
* `rescue.sh`: Shell script wrapper around `mpremote`.
