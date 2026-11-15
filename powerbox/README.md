# Powerbox (RP2040-Zero power controller)

MicroPython firmware for the Waveshare RP2040-Zero in the power box. It starts and
wakes the POCO F1 through the phone's power button, switches the loads on an
8-channel relay board, measures the 12 V system (INA219), drives the chassis fan,
senses ACC, and hosts the BEAN body-bus receiver and the guarded BEAN
transmitter. It talks NDJSON over USB-CDC to the backend on the POCO.

Firmware in this directory: **2.4.1** (`VERSION` in `main.py`). On the car: **2.4.0**
(flashed 2026-11-13). Confirm with `ver` from `POWERBOX_READY` / IDENT.

| File | Role |
|---|---|
| `main.py` | firmware (autostarts; WDT, 3 s Ctrl-C safe-boot window) |
| `boot.py` | runs first on every reset: releases GP27 (POCO button), holds GP7 (BEAN TX) LOW, restores `main.py.bak` after an interrupted swap |
| `pcf8574.py` | PCF8574 driver + relay-board logic (pin map, NC channel, read-back/re-assert) |
| `ina219.py`, `bmp280.py`, `ahtx0.py` | sensor drivers |
| `bean_rx.py` | BEAN receiver (fw ≥ 2.3.0) |
| `bean_tx.py` | BEAN transmitter (fw ≥ 2.4.0) |
| `pb_flash.py` | firmware update over the NDJSON link (the only supported update path) |
| `rescue.py`, `rescue.sh` | break a boot loop and delete a bad `main.py` (see *Rescue*) |
| `relay_bringup_test.py` | bench tool; sends host heartbeats itself |
| `fast_flash.sh`, `watch_and_flash.sh` | bench helpers |

## Safety rules

- **Flash only with `pb_flash.py` or `prius-flash-powerbox`, never through the
  REPL.** Every RP2040 pad resets with its pull-down on; on GP27 (POCO button,
  active LOW) that is a held button whenever `main.py` is not running, and the
  phone boot-loops. The NDJSON update keeps the firmware running, so the button
  stays released and the WDT fed. Details: *Flashing firmware*.
- **Disconnect the POCO button wire before entering the REPL** (Ctrl-C inside the
  3 s safe-boot window) **and before any BOOTSEL / UF2 reflash**: no code of ours
  runs then, so GP27 sits LOW.
- **BEAN bench ops (`bean` `tx`/`pulse`/`loop`/`square`) never on the car**: they
  hold GP7 high for up to 500 ms, which is a +B short on the body bus. They need the
  TX guard and the per-boot bench override.
- **GP7 (BEAN TX) is never held high for long**: R5 220 Ω is a short-circuit limit
  only.
- **GP14 and GP15 are dead pads** (blown inside the RP2040: diode mode to GND
  0.045 V instead of ~0.5 V, 11 Ω between the two; carrier wiring verified clean).
  Never assign them.
- **A button pin must never share a fan pin**: a fan PWM on a button line presses
  the phone.
- **Any MCU reset releases every NO relay** (relay opto VCC is the RP2040's 5 V):
  MFD Pi hard cut, CAN hub and camera hub drop. Prefer a clean MFD shutdown before a
  deliberate reset.
- Identify a pin by blinking it before soldering: every earlier "the pin holds the
  button" failure was a wire on a GND header position, not a pad property.

## Link and protocol

NDJSON over USB-CDC, envelope `{"id": <device_id>, "d": <payload>}` (same as the
gateway). The backend's `MultiInputPort` adds `DEVICE_POWERBOX_BASE` (200).

| id | backend id | Content |
|---|---|---|
| 0 System | 200 | ready banner, IDENT, acks, errors, `STATUS` heartbeat, `PMEVENT`/`PMLOG` |
| 1 Telemetry | 201 | INA219 `v`, `ma`, `mah`; fan RPM; environment sensors |
| 2 Events | 202 | `{"acc": true\|false, "batt": true}` at startup, on change, and every 5 s |
| 3 BEAN | 203 | BEAN frames and receiver stats (fw ≥ 2.3.0) |

**Heartbeats.**

- powerbox → POCO: `STATUS` on id 0 every 2 s (`HEARTBEAT_TX_MS`):
  `{"msg":"STATUS","hb":n,"acc":1,"poco":1,"p2":0,"pm":"normal","rc":1,"up":123,"fan":40,"rpm":2100,"rly":[null,1,1,0,1,0,0,0],"rly_fix":0,"rly_err":0}`.
  `hb` rolling counter; `pm` = `normal`/`shutdown`/`sleep`; `rc` reset cause, `up`
  uptime s; `rly` relay read-back `[ch1..ch8]` (1 = load powered, ch1 always `null`,
  whole key omitted when the read-back failed); `acc_sim:1` while the ACC override
  is active.
- POCO → powerbox: `{"a":"hb","n":N}` every 2 s (`powerbox_heartbeat_s`). The POCO
  counts as **dead** when `n` stops advancing for 15 s (`POCO_HB_TIMEOUT_MS`); a
  repeating counter also reads as dead.

**TX gate.** `tx()` writes only while the host is present (recent POCO heartbeat).
A MicroPython write to USB-CDC blocks when nobody drains the port; ungated, the
firmware stalled until the 8 s WDT reset it and then re-pressed the button on a
booting phone. With the gate, frames are dropped while the host is absent, a TX
failure only increments a counter and never resets the MCU, and the first host
heartbeat reopens the gate. Bench tools must send heartbeats or the port stays
silent. The `POWERBOX_READY` banner is parked until the gate first opens, so the
backend sees exactly one banner per boot. `WDT_TIMEOUT_MS = 8000` remains a
backstop for a real hang.

Backend state: `state.powerbox` (`relays`, `rs485_power`, `poco_alive`, `pm_state`,
`powerbox_hb`, `reset_cause`, `uptime_s`, `boot_count`, `relay_fix_count`,
`relay_err_count`, plus telemetry), shown on the dashboard.

**Host side.** The backend resolves the powerbox from its hub port and never probes
it. `/etc/udev/rules.d/99-prius-rp2040-nomm.rules` makes ModemManager ignore VID
`2e8a` (its AT probes would open/close the port) and sets `MODE="0666"`. A silent
link (board alive, nothing received) is detected by the backend staleness watchdog:
[POCO/POWER.md](../POCO/POWER.md#usb-link-to-the-powerbox).

## Hardware

- RP2040-Zero, **bus-powered** from the USB hub (no direct 12 V line). The hub's
  5 V buck and the POCO buck sit on the permanent 12 V feed through relay 1 (NC):
  `12 V → relay 1 (released = closed) → hub 5 V buck → VBUS → powerbox`. There are
  no switched power rails (since fw 2.0; the `out` command returns `OUT_REMOVED`).
- The box cannot cut the POCO's power: the POCO is only started through its
  button, and shut down by itself.
- USB: hub `1-1`, port 1 (`/dev/serial/by-id/usb-MicroPython_Board_in_FS_mode_503359277a7c699f-if00`),
  per the backend.env seed in `POCO/provision.sh`. See *Open gaps* on the hub model.

### Pinout

| GP | Function | Direction | Notes |
|---|---|---|---|
| GP0 / GP1 | I2C0 SDA / SCL, 100 kHz (`I2C_FREQ`) | — | PCF8574 is standard-mode only. Falls back to SoftI2C with SDA/SCL swapped if the hardware bus finds nothing. |
| GP6 | BEAN RX | IN, no internal pull | Q3 collector, R8 10k pull-up to 3V3. **Inverted**: bus recessive (0 V) → 1, dominant (+B) → 0. |
| GP7 | BEAN TX | OUT, LOW at boot | R2 5.1k → Q1 BC547 → Q2 BC327 high side → D1 → R5 220R → bus. HIGH = dominant. Held LOW in `boot.py` and first thing in `main()`; every op releases it in a `finally`; the main loop and every `wdt_sleep_ms` slice check it. Owned by PIO0 only during a `bean_tx` op. |
| GP11 | ACC sense | IN, pull-up | **Inverted**: ACC ON = 0, OFF = 1. Gated through a 2N7000; carries no load. |
| GP14, GP15 | dead | — | never assign |
| GP16 | onboard WS2812 status LED | OUT | see *Status LED* |
| GP27 | POCO power button | IN pulls off / OUT LOW pulse | see *POCO power button* |
| GP28 | fan tach | IN pull-up, falling-edge IRQ | 2 pulses/rev |
| GP29 | fan PWM 25 kHz, not inverted | PWM | GPIO duty = fan duty, no transistor. Undriven (bootloader, safe-boot) = full speed; set first thing in `main()`. |
| — | "poco 2" button | not connected | `POCO2_BTN_PIN = None`; there is one POCO. |

The ACC level is in STATUS `acc` and the Events messages; with fw ≥ 2.2.0 there is
no REPL to read the pin directly (see *Safety rules*).

### I2C devices

| Addr | Device | Fields |
|---|---|---|
| `0x20` | PCF8574 relay expander | `rly`, `rly_fix`, `rly_err` |
| `0x40` | INA219 on the 12 V supply | `v`, `ma`, `mah` |
| `0x76` | BMP280 #2, outside the box (cabin ambient) | `bmp2_t`, `bmp2_p` |
| `0x77` | BMP280 #1, inside the box — **not fitted** | (`bmp_t`, `bmp_p` omitted) |
| `0x38` | AHT20, inside the box — **not fitted** | (`aht_t`, `aht_h` omitted) |

Only chips that answer the boot scan are bound (no error for absent ones). A bound
sensor that stops answering is dropped after one `*_READ` error. The backend's fan
controller uses `bmp2_t` as ambient; its box-purge loop on `bmp_t − bmp2_t` is inert
while `bmp_t` is missing.

### Relay board

8-channel opto board, 12 V coils (HW-281 type), active-low inputs. IN1..IN8 on PCF
P0..P7, so **board relay N = firmware channel N** (`RELAY_PIN_MAP = {n: n-1}` in
`pcf8574.py`). JD-VCC = 12 V always on (JD jumper removed); opto VCC = RP2040-Zero
5 V pin, so **every NO relay releases whenever the RP2040 is unpowered or
resetting**. The PCF8574 powers up at 0xFF (all coils released).

| Ch | Contact | Load | Measured | Backend owner |
|---|---|---|---|---|
| 1 | **NC** | the powerbox's own supply, nothing else | energised: USB gone 0.4 s, re-enumerated 1.0 s, first STATUS 4.9 s (incl. 3 s safe-boot) | `powerbox_hard_restart` → `{"a":"hard_restart"}` only; refused by `relay` / `set_relay`; `rly[0]` = `null` |
| 2 | NO | MFD Raspberry Pi Zero v1.3, ~100–115 mA @ 12 V | USB gadget + ping at 49–50 s | `backend/mfd_power.py`: `ssh poweroff` + wait, then cut (hard cuts risk the SD card) |
| 3 | NO | USB 2.0 hub `1a40:0101`: CAN gateway (`2e8a:000a`) + comma.ai panda (`bbaa:ddcc`) | 50 → 155 mA; hub ~6 s, gateway ~7 s, panda ~9 s | gateway power path (`SetGatewayUsbPowerAction`, satellite wake-locks); one switch for both |
| 4 | NO | USB 2.0 hub VIA `2109:2817`: fvision dashcam (`1902:3241`, USB storage, frames via `VSFILE`) / aux | +84 mA, enumerates ~6 s, no VBUS back-feed | `backend/roadsense_power.py` (roadsense stack or camera preview); `set_relay` ch4 otherwise |
| 5 | NO | RS485 satellite rail | **untested on this board** | `state/rules/satellite_power.py` wake-locks ([docs/SATELLITE_POWER.md](../docs/SATELLITE_POWER.md)) |
| 6–8 | NO | unassigned | untested | generic `set_relay` |

Relays 1–4 were verified on the bench (three hard MFD cycles, clean enumeration
each).

**Channel semantics.** `on` always means **load powered** (`rly` 1): NO channel pin
LOW (coil energised), NC channel pin HIGH (coil released); the NC flag is generic
(`RELAY_NC = (1,)`). Constants: `RELAY_CH_MFD = 2`, `RELAY_CH_CAN_HUB = 3`,
`RELAY_CH_CAM_HUB = 4`, `RELAY_CH_RS485 = 5` (`main.py`, `state/app_state.py`).

**Read-back and re-assert** (`pcf8574.RelayBoard`):

- the wanted byte changes only after a successful I2C write;
- `rly` comes from a chip read-back, every STATUS and every 5 ms in between
  (`RELAY_VERIFY_MS`, fw ≥ 2.2.3), run from `wdt_sleep_ms()` (main-loop wait and
  every button hold), deadline-paced. The 5 ms period targets the relays' ~5 ms
  release time, so a re-assert should land before a load drops. Cost: one 1-byte
  read, ~0.2 ms with interpreter overhead (~4 %). The bound does not hold while the
  loop body runs (sensor reads, STATUS, commands, a bench `bean` op: a few ms each,
  up to ~1 s for `square`);
- a mismatch (lost write, PCF reset to 0xFF) is re-written and counted in `rly_fix`;
  the first mismatch of a streak emits a `rly_fix` PMEVENT, with `pcf_reset=1` when
  the chip read 0xFF;
- I2C errors → `ERROR RELAY_IO`, counted one by one in `rly_err`, reported at most
  once a second (`IO_REPORT_MS`).

**Restart scope.** After `hard_restart` or any other reset, the MFD Pi, CAN hub and
camera hub come back **OFF**. The backend re-asserts its wanted states ~6 s after
the `POWERBOX_READY` banner (`BackendService.PP_REASSERT_DELAY_S`,
`HubPortPower.reassert_all`); the ~15 s `rly` enforce sweep and the satellite rule's
mirror retry converge the rest. `hard_restart`: ack first, coil energised on the
next tick (`RESTART_ACK_DELAY_MS` 300); if the box is still alive 3 s later the coil
is released and `ERROR RESTART_FAILED` sent.

## POCO power button and wake ladder

GP27 is wired straight across the phone's power button (line idles ~1.8 V from the
PMIC pull-up). Press = drive LOW; otherwise input with pulls off; **never driven
HIGH**. GP27 is an ADC pad (resets with input disabled, no pulls), but the RP2040
reset-default pull-down still reads as a press until `boot.py` releases it, which is
why the REPL and BOOTSEL rules above exist.

| Hold | Effect (POCO F1, bench-verified with the phone's input-event log) |
|---|---|
| 300 ms | exactly one press/release |
| 3 s | does not boot an off phone |
| 6 s (`POCO_BTN_PRESS_MS`) | boots an off phone (kernel → Wi-Fi + ssh ~35 s later) |
| 12 s (`POCO_BTN_FORCE_MS`) | one clean forced restart of a frozen SoC; longer holds reboot it every ~8–10 s (35 s rebooted it ~4 times) |
| 15 s | maximum accepted hold (`POCO_BTN_MAX_MS`) |

`Button.press` releases in a `finally`; Ctrl-C handlers release before exiting.

**Wake ladder** (`PowerManager`, `main.py`):

1. Cold start (no heartbeat seen since the RP2040 booted): wait 20 s
   (`POCO_BOOT_GRACE_MS`), then press.
2. A POCO that was heartbeating and went quiet is treated as rebooting: wait 180 s
   after its last heartbeat (`POCO_LOST_GRACE_MS`; `systemctl reboot` brings the
   backend back after ~75 s).
3. Press 6 s; at most once per 5 min (`POCO_WAKE_COOLDOWN_MS`, must exceed boot →
   first heartbeat, ~3–5 min).
4. After 2 unanswered presses (`POCO_WAKE_SHORT_TRIES`) one 12 s force press, then
   the ladder starts over. A heartbeat resets the count.

**Shutdown and sleep.** `{"a":"off","grace_s":30}` means the host is halting itself.
The firmware enters `shutdown`, waits for the heartbeat to stop (≥ 25 s,
`SHUTDOWN_MIN_HOLD_MS`) or the grace, then `sleep`: no wake presses until **ACC
rises** or `{"a":"wake"}`. Last-resort under-voltage backstop (for a dead backend):
below 10.0 V for 20 s (`SUICIDE_VOLTAGE`, `SUICIDE_CONFIRM_MS`) the firmware sends
`SHUTDOWN` to the host (which must `systemctl poweroff` itself) and enters
`shutdown` → `sleep`. The backend's own rule acts first (10.5 V / 5 s).

## Status LED, event log, reset cause

**LED** (GP16 WS2812, dim) — the only firmware output observable during a cold boot,
when the POCO is off and nothing is logged. Point a camera at it.

| LED | Meaning |
|---|---|
| white → blue flash | power-on / reset. **Repeating every few seconds = reset loop.** |
| green blink | `normal`, POCO alive |
| amber solid | `normal`, POCO not alive, armed to press |
| red solid | button press in progress (GP27 LOW) |
| magenta | shutdown in progress |
| bright magenta | `hard_restart`: relay 1 being energised |
| cyan solid | `sleep` |
| dark | RP2040 unpowered |

**Event log** (fw ≥ 1.15.0). An in-RAM ring of power events (boot, every press with
reason + voltage + wake tries + boot age, shutdown, sleep); the RP2040 stays powered
through a POCO cold boot, so the timeline survives. `{"a":"getlog"}` dumps it as
`PMLOG`; the backend fetches it when the link comes up, so a cold boot's wake
sequence lands in the journal. Events are also echoed live as `PMEVENT` (as are
`rly_fix`, `acc_sim`, `bean`, `bean_tx`, `bean_rx`).

**Reset cause.** `rc` = `machine.reset_cause()` in the banner, every STATUS and the
`boot` event: `1` = power-on (also brown-out and `hard_restart`), `3` = watchdog
(WDT timeout, `machine.reset()`, `mpremote reset`). STATUS also carries `up`. The
backend mirrors `powerbox.reset_cause`, `powerbox.uptime_s` and counts banners in
`powerbox.boot_count`.

## Chassis fan

4-wire BLDC blower, ADDA AB07505HX110B00 / Dell 00G700, 5 V 0.40 A (red 5 V, black
GND, blue PWM, yellow tach) on a 5 V buck. Command `{"a":"fan","pct":0-100}` (old
`duty` 0–65535 form accepted). Boot default 0 %, which **stops the fan completely**
(no minimum-speed floor). Policy: `backend/fan_policy.py`,
[docs/THERMAL_MODEL.md](../docs/THERMAL_MODEL.md).

Duty → RPM (bench sweep, 5 % steps, 4 s settle + 3 s count, no hysteresis, ±20 RPM):

| duty % | 0 | 5 | 10 | 15 | 20 | 25 | 30 | 35 | 40 | 45 | 50 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| RPM | 0 | 255 | 650 | 1025 | 1475 | 1675 | 1965 | 2240 | 2505 | 2735 | 2965 |

| duty % | 55 | 60 | 65 | 70 | 75 | 80 | 85 | 90 | 95 | 100 |
|---|---|---|---|---|---|---|---|---|---|---|
| RPM | 3170 | 3380 | 3565 | 3745 | 3920 | 4085 | 4245 | 4400 | 4540 | 4680 |

Near-linear up to ~50 %, flattening above.

## Inbound commands (POCO → powerbox, id 0)

| Command | Payload | Effect |
|---|---|---|
| Heartbeat | `{"a":"hb","n":0-255}` | keeps "POCO alive" fresh; opens the TX gate |
| Shutdown | `{"a":"off","grace_s":30}` | host is halting: `shutdown` → `sleep` |
| Wake | `{"a":"wake","poco":1}` | leave `sleep`, re-arm the ladder |
| Power button | `{"a":"button","poco":1,"ms":6000}` | hold GP27 LOW (max 15000) |
| Relay | `{"a":"relay","ch":2-8,"on":true}` | ack `relay_<ch>_on`; `ch:1` → `ERROR RELAY` |
| Hard restart | `{"a":"hard_restart"}` | `{"ack":"hard_restart"}`, `{"msg":"HARD_RESTART","up":s}`, then relay 1 cuts the box's supply; back in ~5 s |
| Fan | `{"a":"fan","pct":0-100}` | fan duty |
| ACC override (**bench**) | `{"a":"acc_sim","v":0\|1\|null}` | fw ≥ 2.2.5; see below |
| Get event log | `{"a":"getlog"}` | `PMLOG` |
| I2C scan | `{"a":"i2c_scan"}` | `I2C_SCAN` with the ACKing addresses |
| Interval | `{"a":"set_interval","ms":1000}` | telemetry cadence |
| Reset mAh | `{"a":"reset_mah"}` | zero the coulomb counter |
| Identify / ping | `{"a":"whoami"}` / `{"a":"ping"}` | IDENT / PONG |
| p2 alive (legacy) | `{"a":"p2","alive":true}` | echoed as `p2` in STATUS; unused |
| Firmware update | `put_begin` / `put` / `put_end` / `reboot` | fw ≥ 2.2.0; used by `pb_flash.py` (protocol block in `main.py`) |
| BEAN bench (**bench**) | `{"a":"bean","op":...}` | fw ≥ 2.2.3; see *BEAN transceiver bench* |
| BEAN TX guard | `{"a":"bean_tx_enable","v":1\|0}` | fw ≥ 2.3.0; arms GP7-driving ops for 60 s (`BEAN_TX_ENABLE_MS`); ack `{"ack":"bean_tx_enable","v":..,"s":60}`, `bean_tx` PMEVENT |
| BEAN receiver | `{"a":"bean_rx",...}` | fw ≥ 2.3.0; see *BEAN receiver* |
| BEAN transmitter | `{"a":"bean_tx","op":...}` | fw ≥ 2.4.0; see *BEAN transmitter* |

`poco:2` on `button` / `wake` → `ERROR POCO2_NC`, nothing pressed.

Error codes besides the above: `RELAY_IO`, `RESTART_FAILED`, `POCO1_NC`,
`BMP280_READ` / `AHT20_READ` (sensor dropped), `OUT_REMOVED`, `ACC_SIM`, `BEAN_ARGS`,
`BEAN`, `BEAN_TX_DISABLED`, `BEAN_RX <detail>`. `SENSOR_OK` for the PCF8574 includes
the adopted `port` byte.

**ACC override** (`acc_sim`). Simulates key-on (1) / key-off (0) without the ACC
wire; `null` or no `v` clears it. Applied in `read_acc()`, the single entry point of
the pin, so it reaches the Events messages, STATUS `acc` and the wake/sleep logic.
Ack `{"ack":"acc_sim","v":..,"pin":<real ACC, 1 = on>}`; other `v` → `ERROR ACC_SIM`.
RAM only (cleared on boot); auto-expires 30 min after the last set
(`ACC_SIM_MAX_MS`, `acc_sim` PMEVENT `why: expired`). Backend: REST
`powerbox_acc_sim {"on": true|false|null}`, state `powerbox.acc_sim`.

## Flashing firmware

Requires fw ≥ 2.2.0 on the board. Files go over the normal link
(`put_begin` / `put` / `put_end`, then `reboot`) while the firmware keeps running, so
GP27 stays released and the WDT fed. Each file is checked on the board (size +
sha256) before it replaces the old one (kept as `<name>.bak`; `boot.py` restores
`main.py.bak` if a swap was interrupted). `pb_flash.py` sends the host heartbeat
itself, so the wake ladder never presses during an update.

On the POCO (files synced by `POCO/sync.sh --app` to `/home/user/cyberpunk/powerbox`):

```bash
sudo prius-flash-powerbox                         # main.py + boot.py + bean_rx.py + bean_tx.py; stops/starts prius-backend
sudo prius-flash-powerbox /home/user/cyberpunk/powerbox/pcf8574.py   # any other file explicitly
```

From a bench host with the board on USB (nothing else may hold the port):

```bash
python3 powerbox/pb_flash.py powerbox/main.py powerbox/boot.py powerbox/bean_rx.py powerbox/bean_tx.py
```

- `pb_flash.py` always sends `main.py` first. A running firmware refuses file names
  not in its `OTA_FILES` (2.3.0 refuses `bean_tx.py`, older also `bean_rx.py`); then
  `pb_flash.py` reboots into the new `main.py` (which runs without the missing
  module, reporting why) and retries the file once. The first update to 2.4.0 from
  below 2.3.0 therefore reboots twice.
- Keep `bean_rx.py` before `bean_tx.py`: 2.4.0's transmitter needs 2.4.0's receiver
  (with an older `bean_rx.py` every `bean_tx` op answers `err:"exc"`).
- `pcf8574.py` and the sensor drivers are not in the default set; send them when they
  changed (a board below 2.2.3 needs the new `pcf8574.py` too).
- A full `main.py` + `boot.py` update takes ~40 s including the reboot.

**Why there is no REPL path.** With the pad pull-down holding GP27 LOW whenever
`main.py` is not running:

- `boot.py` releases GP27 first thing on every reset; the line is LOW only for the
  MicroPython start-up time;
- after the 3 s safe-boot window Ctrl-C is disabled (`micropython.kbd_intr(-1)`), so
  a stray 0x03 cannot drop the board into a REPL with the WDT armed;
- the REPL is reachable only by Ctrl-C inside the safe-boot window (no WDT yet):
  disconnect the POCO button wire first.
- `POCO_BTN_ACTIVE_HIGH` (`main.py`) / `ACTIVE_HIGH` (`boot.py`) switch to driving
  the button through an N-MOSFET (GP27 → ~100 Ω → gate, ~100 kΩ gate → GND, drain →
  button line, source → GND), with which reset and bootloader leave the button
  released by hardware. Set both to `True` only once the MOSFET is fitted (it is
  not).

### Rescue (boot loop)

If the board boot-loops (e.g. an exception during I2C init), disconnect the POCO
button wire, then use `rescue.py` (USB cycling + Ctrl-C spam into the safe-boot
window) or `rescue.sh` (`mpremote` wrapper) to reach the REPL and delete the
offending `main.py`.

A hub-port power cycle (`prius-usb-power cycle`, ≥ 4 s off) is a cold reset of the
bus-powered MCU only on a socket with real switchable VBUS (see *Open gaps*);
`{"a":"hard_restart"}` is the in-band cold reset.

### Failsafe: BOOTSEL + flash_nuke (corrupt filesystem)

Only when the on-board filesystem is bad (the board never streams, even after a
power cycle). **Disconnect the POCO button wire first.** Hold BOOTSEL while plugging
or resetting. A plain UF2 flash does not erase the littlefs filesystem; only
`flash_nuke.uf2` does.

```bash
# BOOTSEL → 2e8a:0003, mass storage labelled RPI-RP2 (partitioned)
RP=$(sudo blkid | grep RPI-RP2 | cut -d: -f1)      # e.g. /dev/sdg1
sudo mount -t vfat "$RP" /mnt/rp2
sudo cp flash_nuke.uf2 /mnt/rp2/ && sync           # erases littlefs + firmware
sleep 8                                             # re-enters BOOTSEL
RP=$(sudo blkid | grep RPI-RP2 | cut -d: -f1)      # node changes after each UF2
sudo mount -t vfat "$RP" /mnt/rp2
sudo cp RPI_PICO-vX.Y.Z.uf2 /mnt/rp2/ && sync      # clean MicroPython
sleep 8                                             # boots as 2e8a:0005, empty FS
mpremote connect /dev/ttyACM0 fs cp boot.py ina219.py ahtx0.py bmp280.py pcf8574.py bean_rx.py bean_tx.py main.py :
```

Re-detect the block node after each UF2; use generous sleeps and do not unmount
before the board consumes the UF2. `flash_nuke.uf2` is from
datasheets.raspberrypi.com, MicroPython from micropython.org (last used:
`RPI_PICO-20250911-v1.26.1.uf2`; DMA capture needs ≥ 1.21). Power cycling the RP2040
by hub port (keep `-f`, the hub descriptor claims "ganged"):

```bash
sudo uhubctl -f -l 1-1 -p 1 -a off; sleep 4; sudo uhubctl -f -l 1-1 -p 1 -a on
```

## BEAN transceiver bench

fw ≥ 2.2.3.

**Bench only** (since fw 2.4.0 enforced): needs the TX guard and the per-boot bench
override `{"a":"bean_tx","op":"bench","v":1}`, else `ERROR BEAN_TX_DISABLED`. For the
car there is `bean_tx` `op:"edge"`.

Logic check of the discrete BEAN transceiver on GP7 (TX) / GP6 (RX, inverted). BEAN
is 10 kbit/s (100 µs bits), dominant = +B, recessive = 0 V. Not a protocol stack.
Every op is synchronous, capped, and leaves TX LOW (also on errors). Reply
`{"ack":"bean","op":<op>,...}` on id 0 plus a live `{"msg":"PMEVENT","ev":"bean",...}`
(not kept in the `getlog` ring). Bad op / out-of-range or non-integer field →
`ERROR BEAN_ARGS` (nothing driven); an exception → `ERROR BEAN`.

| op | fields (default, range) | reply fields |
|---|---|---|
| `status` | — | `tx` (0/1), `rx` (GP6 level), `bus` (1 = dominant, i.e. `rx` low) |
| `tx` | `v` (1, 0–1), `ms` (1, 1–500) | `v`, `ms`, `rx_on` (end of hold), `rx_off` (50 µs after release) |
| `pulse` | `us` (100, 1–2000) | `us`, `rx_mid`, `rx_after` (50 µs after release), `us_real` |
| `loop` | `n` (20, 1–200) | n × {TX high → µs to RX low (timeout 2000), hold 300 µs, TX low → µs to RX high, 300 µs gap}: `dom_min/max/avg`, `rec_min/max/avg` (µs; `null` if no edge), `to_dom`, `to_rec`, `timeouts`, `rx_idle` |
| `square` | `hz` (5000, 100–20000), `ms` (200, 1–1000) | `edges` (RX level changes), `expected` (= 2 × `cycles`), `cycles`, `hz_real` |

`square` toggles TX and polls RX in one Python loop; each half period is at least
500000/`hz` µs from the actual toggle, so a slow loop lowers `hz_real` instead of
starving the transceiver; `edges == expected` means RX followed every edge.
Interrupts stay enabled; the longest op (~1 s) is far inside the 8 s WDT (fed per
round). All µs figures come from `time.ticks_us()` in interpreted loops (resolution a
few µs at 96 MHz plus USB/IRQ jitter): good for "does RX follow TX and roughly how
fast", not for bit timing.

Backend: REST `powerbox_bean` (same fields and caps, HTTP 400 outside them); reply
mirrored to `/api/v1/state` → `powerbox.bean` (ack fields + `ts`; errors as
`{"error": "BEAN_ARGS", "detail": ...}`).

## BEAN receiver

fw ≥ 2.3.0, `bean_rx.py`.

Receive-only decoder for the BEAN body bus (protocol: `docs/BEAN_PROTOCOL.md`). Reads
GP6 only; never drives GP7, never ACKs. Starts after the safe-boot window, ON at
boot. If `bean_rx.py` is missing or fails, the firmware runs without it: the
`bean_rx` PMEVENT carries the error and `bean_rx` commands answer
`ERROR BEAN_RX <detail>`.

**Capture.** PIO1 SM0 at 2 MHz measures every GP6 level run (2-cycle count loop,
1 µs resolution), one word per edge: bit 16 = level of the run that ended, bits
15..0 = count left, run = `10000 − x + 3` µs. A run longer than 10 ms pushes a
timeout word (`x = 0xFFFF`) and keeps counting (~100 words/s while the bus sleeps).
27 instructions. Words go to a 1024-word RAM ring through `rp2.DMA` (MicroPython
≥ 1.21; write address wraps on the 4 KB-aligned ring, producer index from the DMA
transfer count). Fallbacks: hard IRQ draining the 4-word FIFO (mode `irq`), then FIFO
polling (mode `poll`, loses data whenever the loop blocks > ~1 ms). Mode is in the
ack, the stats and the PMEVENT. PIO RX stalls are counted (`stall`).

Ring capacity: busy traffic (45 frames/s × ~40 runs ≈ 1800 words/s) → 1024 words
cover ~550 ms of blocked main loop. Beyond that the backlog is dropped: `ovf` +1,
`lost` += words, the frame in progress reported as `ovf`, resync on the next idle gap.

**Decode** runs in `poll()` from every `wdt_sleep_ms` slice (~5 ms), ≤ 256 words per
call: runs rounded to bit times with an adaptive bit time (fast for ~250 runs after
start, slow afterwards) and a dominant/recessive skew estimate; pulses < 12 µs merged
(glitch filter); per-run destuffing, frame state machine, CRC-8; EOM `0111111`
searched after the CRC tolerating a stuff bit before it or its absence; the two RSP
bits reported raw. Tested tolerance (CPython): ±20 µs jitter per edge without loss,
±30 µs skew, bit clock 80–120 µs (from cold, ±8 % loses at most the first frame).

**Frames** on id 3 (backend 203), batched at most every 200 ms, ≤ 16 frames per line,
64-frame queue:

```json
{"t":123456,"bf":[[123290,6,"FE","2C","64",1,0],[123301,2,"FE","7F","001000000000",1,0]],
 "st":{...every 2 s...}}
```

- Record `[t_ms, pri, "DST", "MES", "DATAHEX", rsp, code]`; an 8th element `1` (fw
  2.4.0) marks what our own transmitter put on the bus (frame, aborted attempt, or the
  edge-test pulse as a `trunc` record without bytes). The backend keeps own frames in
  the frame ring and capture log (`"own":1`) but out of the message table and the
  error count.
- `t_ms` = firmware `ticks_ms` of the SOF (±~1 ms); `t` = `ticks_ms` at send time
  (frame age = `t − t_ms`). Batches leave every `BATCH_MS` (200 ms) or, fw ≥ 2.4.1, at
  once when 16 frames are queued: on fw 2.4.0 at ~52 frames/s (IG on) lines went out
  every ~254 ms and frames were 347/531/647 ms old at send (p50/p90/p99, 2026-11-13
  drive); the host test of that load gives 95/170 ms (p50/p90) on 2.4.1. Frame times come from
  the edge stamps of the word-time cursor (fw ≥ 2.4.1); fw ≤ 2.4.0 summed run lengths
  instead and its `t_ms` jumped by an arbitrary amount (mod 2^30 µs) at every wake
  after a bus sleep > 537 s, and ran slow by 0.5 µs per dominant run beyond what the
  anchor leak follows (2026-11-13 drive: `t_ms` 236.4 s ahead of `t`). The backend
  times such frames with its estimator (`comm/bean.py` `BeanClock`).
- `rsp`: 2 raw RSP bits (`1` = `01` = ACK, `0` = none, `2` = NAK, `3` = both), `null`
  if the frame broke before RSP.
- `code`: 0 ok, 1 crc, 2 stuff, 3 len (ML outside 3..13), 4 frm (EOM/RSP/EOF shape,
  or bus stuck dominant), 5 trunc (idle inside the frame), 6 ovf. Failed frames carry
  every byte after MES that arrived (may include the CRC).
- At most 5 failed frames reported per second (rest counted in `esup`); `qdrop` =
  queue full; `txdrop` = host absent (TX gate closed).

`st` counters: results `ok crc stuff len frm trunc ovf`; capture `edges glitch lost
stall lag`; reporting `qdrop txdrop esup`; timing `tb` (bit time, 0.1 µs), `skew`
(µs, dominant longer = +), `idle_ms` (since last edge; fw ≥ 2.4.1 keeps counting
past the 2^30 µs run-length saturation on the ms clock, fw ≤ 2.4.0 sticks at
1,073,741), `age_ms` (since last good frame, −1 = none; fw ≤ 2.4.0 wraps negative after
6.2 days); load `cpu` (decoder time, ‰ of wall time); state `on`, `mode`, `err` /
`exc` (last internal exception) / `exn` (count, fw ≥ 2.4.1).

| command | reply |
|---|---|
| `{"a":"bean_rx","on":0\|1}` | `{"ack":"bean_rx","on":..,"mode":"dma"\|"irq"\|"poll"\|null}`; start failure → `ERROR BEAN_RX` |
| `{"a":"bean_rx","op":"stats"}` | ack on id 0 + an immediate `{"t":..,"st":{..}}` on id 3 |
| `{"a":"bean_rx","raw":N}` (1–512) | ack; then the next N raw runs on id 3: `{"raw":[-2000,100,-300,..],"ri":0,"rn":N}` in chunks of 64 (µs, + dominant, − recessive) |

**Robustness.** `poll()` never raises; after 10 consecutive internal errors it stops
(`err` in the stats). Preallocated buffers: allocation only per decoded frame and per
line.

**Tick arithmetic.** MicroPython ticks wrap at 2^30 (`ticks_us` 17.9 min, `ticks_ms`
12.4 days); `ticks_diff` is meaningful below 2^29 and `ticks_add` raises
`OverflowError('ticks interval overflow')` for |delta| ≥ 2^29 (537 s in µs, 6.2 days
in ms). fw ≤ 2.4.0 added a run length to the frame cursor, so the first edge after a
bus sleep > 537 s raised: one exception per such wake-up (`exc` in the stats on the
car since 2026-11-13), the wake-up's sleep time lost from the frame clock, and the
receiver marked unhealthy for 5 s. Ingest itself survived (the exception aborts that
poll, `_abort()` zeroes the run, the next poll re-decodes the same words; ERR_LIMIT is
never reached). A `bean_wake` in that window has its `send` refused with
`err:"rx"` (`health()` = "rx errors"): the edge-test pulse is the first edge after the
sleep. fw 2.4.1 never adds an unbounded delta to a tick value (run end = edge stamp,
anchor bounded to ±2^28 µs), carries long ages (`idle_ms`, `age_ms`, `up`), and caps
or expires every stored ms stamp (power manager, TX arm, TX rate stamps, `own`,
`bad_ms`) before `ticks_diff` can wrap. Host tests on MicroPython tick semantics:
`tests/test_bean_rx_wrap.py`, `TestTickWrap` in `tests/test_powerbox_firmware.py`.

**Cost** (estimate; verify with `cpu`): 40–80 µs per run plus ~0.1 ms per poll →
~2–4 % CPU at 15 frames/s, ~8–14 % during the 45 frames/s IG-ON burst. Idle (fw
2.4.0): with the decoder idle and nothing queued, a native fast path (`_quick`) only
adds timeout words to the idle time; a full pass runs every 40 polls (200 ms) and,
after 1 s recessive, the capture is looked at every 4th poll. Wake latency ≤ 20 ms
(ring then holds ≤ ~40 words; `TestReceiverIdle`). fw 2.3.0 measured 87–94 ‰ asleep;
2.4.0 reads 40–41 ‰ asleep on the car (2026-11-13 and 2026-11-15). The run-length counter saturates at
2^30 µs. RAM: ~8 KB DMA ring (allocated at 2× for alignment), ~10 KB compiled module,
up to 64 queued records.

**TX guard.** Every TX-driving bench op answers `ERROR BEAN_TX_DISABLED` without
touching GP7 unless `{"a":"bean_tx_enable","v":1}` was sent < 60 s ago (`status`
always works; `BEAN_TX_ENABLED = False` at boot, RAM only). The same guard arms the
transmitter; bench ops additionally need its bench override.

Backend: `powerbox.bean_rx` (counters, `fps` over 5 s, `last_frame_ts`, `mode`,
`error`, `raw`); `bean.messages` (table keyed `DST:MES`: count, last data, changes,
period, pri); `bean.decoded` (coolant 0x2C, shift 0x40, speed 0x24, ambient 0xCD,
lamps 0x7F, odometer 0x3F — all **unverified** on this Prius);
`GET /api/v1/bean/frames?since=<seq>&limit=<n>` (last 200 frames); REST
`powerbox_bean_rx`, `bean_capture {"on":true,"name":".."}` (log in `logs/bean/`,
rotated at 20 MB; env `BACKEND_BEAN_CAPTURE_DIR`), `bean_mark {"text":".."}`,
`powerbox_bean_tx_enable {"on":bool}`.

## BEAN transmitter

fw ≥ 2.4.0, `bean_tx.py`.

Sends single frames on command only: no periodic or automatic transmission, never
ACKs other nodes. Protocol background and assumptions: `docs/BEAN_PROTOCOL.md` §3,
§5, §7.2. If `bean_tx.py` is missing or fails, `bean_txm` PMEVENT carries the error,
`bean_tx` answers `{"ok":0,"err":"off"}` and the bench ops are refused. **Status:
every number below comes from the CPython simulation of the PIO programs; nothing has
run on hardware yet** (see *Open gaps*).

### Hardware side: PIO0

PIO1 is full with the receiver (27 of 32), so the transmitter uses **PIO0**: SM0 runs
the frame program *or* the edge-test program (25 instructions each, one loaded at a
time), SM1 a 6-instruction watchdog. GP7 is handed to PIO0 only for the duration of
an operation; otherwise it is a plain output held LOW.

**Frame program (SM0).** 32 cycles per bit, from the bit time the receiver measured
(3.2 µs per cycle at 103.3 µs). The firmware builds the whole wire bit stream (SOF,
stuffed PRI/ML … CRC, EOM `01111110`, MSB first) and loads it into the joined 8-word
TX FIFO before the SM starts (2 count words + up to 5 data words, padded to 8), so
the program cannot stall mid-frame.

1. *Idle check.* 11 bit times (`IFS_BITS`) of continuously recessive bus, GP6 sampled
   every 2 cycles (6.5 µs). Any dominant sample ends the attempt before anything was
   driven ("busy"; the firmware retries). Last idle sample → our SOF edge: 8 cycles
   (26 µs). Other nodes start 7 bit times after the last edge, so after a frame they
   go first; after an un-ACKed frame our SOF comes 2 bit times after its EOF.
2. *Per bit:* drive GP7 (`set pins`), wait to the sample point, read GP6.

   ```
   one bit = 32 PIO cycles (Tb)        d1 = sample point, default 16 cycles = 50 %
   cycle   0  1  2  3  4  5 | 6 ......................... 6+d1 ........ 31 | 0
   SM0     fetch bit, run   |set|      wait d1 cycles      |jmp pin|  wait  |
           counter          |pins|                          (reads GP6)
   GP7     previous bit ____/‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾
   bus     ______________________/‾‾‾ (rise delay)       ^
   GP6     ‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾\___ (inverted)         sample: d1 cycles after the edge
   ```

   - sent **recessive**, read **dominant** → stop on the spot (flag 4); GP7 is already
     released for this bit. Within SOF…MES = `lost_arb`; later = `bit_err`.
   - sent **dominant**, read **recessive** → line fault (flag 5): GP7 released 4 cycles
     (13 µs) after the sample. Never retried; TX latches off.
3. *Dominant-run limit.* X counts dominant bits in a row, reloaded with 6 by every
   recessive bit; a 7th is sent recessive instead. GP7 is never high for more than 6
   bit times (+ 2 cycles in that forced case), whatever is in the FIFO.
4. *RSP.* After the EOM the program samples GP6 in the next two bit slots and stops
   with GP7 low (flag 7). Samples sit 1.5 and 2.5 bit times after the EOM's bus edge
   (our release + measured fall time; at most 1.8 / 2.8).
5. The firmware reads flags, consumed FIFO words and — by executing `push` /
   `mov isr, y` / `push` on the stopped SM — the two RSP samples and the bit counter
   (`pos`, the wire bit an attempt was lost in). If that readout does not work on the
   board, RSP comes from the receiver's decode of our frame (`rx_rsp`) and the
   position from the FIFO level.

**Watchdog (SM1).** 64 cycles of 12.5 µs continuously high (0.80–0.83 ms; longest
legal run 0.65 ms at 108 µs bits) turn GP7 into an **input** (`set pindirs, 0`); SM0
cannot take it back (it only writes pin values). Independent of SM0, its FIFO and the
firmware.

**Timing budget.**

| quantity | value (Tb = 103.3 µs) | depends on |
|---|---|---|
| bit time | measured by the receiver, must be 95–108 µs | else refused |
| sample point `S` | 25–75 % (`op:"sp"`), default 50 % = 51.6 µs | — |
| rise (GP7↑ → GP6↓) | ≤ 40 µs and ≤ `S` − 10 µs | edge test; bench: < 2 µs |
| fall (GP7↓ → GP6↑) | ≤ 40 µs and ≤ `S` − 10 µs | edge test, **not measured on the car**; open bench bus: ~340 µs |
| loss → off the bus | 0 (the lost bit is a released bit) | — |
| fault → released | 13 µs after the sample | — |
| RSP samples | 1.5 / 2.5 bit times after the EOM falling edge on the bus | measured fall, bit time |
| idle before SOF | 11 bit times = 1.14 ms | — |
| longest dominant | 6 bit times = 0.62 ms (EOM) | — |
| watchdog SM | 0.80–0.83 ms | — |
| firmware timer | 0.8–1.8 ms | — |

The 40 µs fall limit protects the other nodes: a slow release shortens every
recessive bit after a dominant one (bit time − fall), and a receiver resyncing on the
edge and sampling half a bit later must still land inside it. Above 40 µs no frame is
sent at any sample point; the fix is hardware (pull-down on the transceiver side).
The sample point only matters downwards (below 50 % the limit is `S` − 10 µs).

**Edge-test program (SM0).** One dominant pulse of exactly one bit time (206 cycles of
Tb/206 ≈ 0.5 µs; counts built inside the program). Requires 7.8 bit times idle, drives
GP7, counts until GP6 goes low (rise, 1 µs steps), holds the rest of the bit,
releases and counts until GP6 is high (fall, gives up after 800 µs). The receiver's
raw runs around the pulse come back (`runs`, `rx_dom_us` ≈ width − rise + fall); the
pulse shows as an own `trunc` record. To the ECUs a 100 µs pulse is a noise glitch,
**but it is an edge: it wakes a sleeping bus** (hence `wake`).

### Firmware guards (the host cannot bypass them)

| # | layer | behaviour |
|---|---|---|
| a1 | arming | every op that can drive GP7 needs `bean_tx_enable` (60 s); rechecked before every retry |
| a2 | frames flag | frames also need the per-boot flag `op:"allow","v":1` (RAM, off at boot, cleared by any fault); `allow` needs arming and a measured edge test |
| a3 | edge test first | no frame until an edge test this boot measured rise and fall ≤ 40 µs that fit the sample point |
| b1 | program run limit | ≤ 6 dominant bits in a row, in the PIO program |
| b2 | watchdog SM | GP7 high 0.8 ms → pin becomes an input |
| b3 | firmware timer | 1 kHz `machine.Timer` during every op: GP7 still high 0.8 ms after a tick → plain LOW output, both SMs off, fault latched (`gp7 high > 1 ms`) |
| b4 | outside ops | `guard()` every `wdt_sleep_ms` slice and `bean_release()` every main-loop pass: GP7 high with no op → released, fault latched; every op ends in a releasing `finally`; `boot.py` holds GP7 LOW on any reset |
| c | budget | 5 wire events/s and 50 per 10 min (`RATE_*`), per attempt on the wire (retries and edge pulses count); beyond → `err:"rate"` |
| d | block list | without the bench override only **`FE:48`** (test frame) is accepted. Refused: every real node destination (`19 40 52 62 98 B5 C1 E9`, other-model `45 46 D1 D2 E0`, `FF`), every broadcast ID seen in the NHW20 captures, on this car or known from other models (`AB`, `C2`, `7F`, `3F`, `24`, `40`, `91`, `62`, `E1`, …; `DENY_FE_MES`), everything not on the allow list, and any DST:MES the receiver has seen from another node since boot. `op:"bench","v":1` (armed, per boot, logged) lifts this |
| e1 | receiver health | no TX while the receiver is off, failed, in `poll` mode, or reported an overflow, PIO stall, exception or bad frame (crc/stuff/len/frm) in the last 5 s, or the bus is stuck dominant |
| e2 | bit time | measured bit time outside 95–108 µs, or none measured since boot → refused (bench override allows nominal 100 µs) |
| e3 | no ACK | there is no code path that drives the RSP slot of other nodes' frames |
| f | sleeping bus | recessive ≥ 1 s = asleep: `edge` / `send` refused unless `"wake":1`. On this car the first frame after a wake is not ACKed and is repeated (19 of 19 wakes: the waker's `FE C2` again at +42 ms, nodes answer from +7 ms; `docs/BEAN_SIGNALS.md` §3). Our retries come ~35 and ~85 ms after the first SOF |
| g | unresolved stuffing | a frame whose CRC ends in 5 equal bits is refused (unknown whether a stuff bit belongs before the EOM); the bench override sends it without one |
| h | latched faults | line fault, watchdog trip, timeout, GP7 high outside an op: TX off (`fault` in `status`) until an edge test passes; `allow` must be given again |

**Retries.** On no ACK (`RSP` ≠ `01`), lost arbitration or bit error: default 2
retries (`retries` 0–3). Before a retry the firmware waits for the receiver to report
our frame (≈ 10 ms after an ACKed frame, ≈ 20 ms after an un-ACKed one, ≤ 40 ms) plus
10 ms per attempt made; the PIO idle check then waits for the bus. One `send` takes
≤ 400 ms (`err:"busy"` if the bus never frees). Never retried: line fault, watchdog,
budget exhausted, disarmed.

### Commands

Replies `{"ack":"bean_tx","op":<op>,"ok":0|1,...}` on id 0; a refusal has `err` and
`detail`.

| command | reply |
|---|---|
| `{"a":"bean_tx","op":"status"}` | `armed`, `allow`, `bench`, `sp`, `fault`, `edge` (last test), `budget` (events left in 10 min), `frames`, `edges`, `test` (`"FE:48"`), `bus` (0 active, 1 asleep, 2 stuck), `rx` (null = healthy), `tb`, `tb_meas` |
| `{"a":"bean_tx","op":"edge"[,"wake":1]}` | `ok` (rise and fall ≤ 40 µs), `rise_us`, `fall_us` (null = not seen / > 800 µs), `w_us`, `rx_dom_us`, `runs`, `ok_meas`. On a busy bus waits ≤ 150 ms for 7.8 idle bit times (`err:"busy"`, nothing sent) |
| `{"a":"bean_tx","op":"allow","v":1\|0}` | `allow` |
| `{"a":"bean_tx","op":"bench","v":1\|0}` | `bench` (also `bean_tx_bench` PMEVENT) |
| `{"a":"bean_tx","op":"sp","pct":25-75}` | `sp` |
| `{"a":"bean_tx","op":"send","pri":0-15,"dst":"FE","mes":"48","data":"00"[,"retries":0-3][,"wake":1]}` | `ok` (ACKed), `acked` (raw RSP: 1 ACK, 0 none, 2 NAK, 3 both; null if no attempt completed), `rx_rsp`, `attempts`, `lost_arb`, `bit_err`, `fault`, `busy`, `pos`, `t_ms`, the frame, `err` when not ok |

`err` codes: `disarmed`, `frames_off`, `edge`, `rx`, `tb`, `asleep`, `blocked`,
`crc_stuff`, `rate`, `fault`, `busy`, `args`, `exc`, `off`; after attempts: `no_ack`,
`nak`, `rsp`, `lost_arb`, `bit_err`.

Backend: REST `powerbox_bean_tx` (same checks, HTTP 400 outside them), state
`powerbox.bean_tx`. Every `edge` / `send` / `allow` / `bench` request and reply goes to
`logs/bean/bean_tx_audit.ndjson` (always; 1 MB, one `.1` generation) and to a running
capture (`{"type":"tx_req",...}` / `{"type":"tx",...}`). No dashboard control.

### First transmission on the car (procedure)

Preconditions: fw 2.4.0 flashed (`status` answers), receiver healthy
(`powerbox.bean_rx`: `mode` dma, no errors), a capture running
(`bean_capture {"on":true,"name":"tx1"}`), diagnostics tool at hand. Arm with
`powerbox_bean_tx_enable {"on":true}` before each step (60 s).

1. **Edge test, bus awake** (door open, so the bit time is measured):
   `powerbox_bean_tx {"op":"edge"}`.
   - Expect `ok:1`, `rise_us` 0–3, `fall_us` a few µs, `w_us` ≈ 103,
     `rx_dom_us` ≈ `w_us − rise + fall`, no change in receiver error counters besides
     one own `trunc` record.
   - `fall_us` ≤ 40 (ideally ≤ 15): go on. Above 40 or `null`: stop (frames are refused
     anyway); fix the hardware (pull-down on the transceiver side) and measure again.
   - `rise_us` `null`: the driver did not move the bus (wiring, +B feed).
   - Repeat 3–5 times a few seconds apart; values should agree within 2 µs; periodic
     frames must keep coming without `crc`/`frm`.
2. **First frame, bus awake:** `{"op":"allow","v":true}`, then
   `{"op":"send","pri":0,"dst":"FE","mes":"48","data":"00","retries":0}`.
   - Expect `ok:1`, `acked:1`, `rx_rsp:1`, `attempts:1`, `t_ms` ≈ 20–30, and one own
     frame `FE 48 00` with rsp 1 in the capture. Broadcasts were ACKed in every
     capture, so `acked:0` means nobody accepted it (check `rx_rsp`, edge numbers,
     bit time before retrying).
   - `lost_arb` with a retry is normal on a busy bus; `bit_err` is not: stop, look at
     `pos` and the raw runs.
   - Then with default retries, a few times, ≤ 5 per second.
3. **Wake-up, bus asleep** (car locked, `status` shows `bus:1`):
   `{"op":"send",...,"wake":true}`. Expect the first attempt un-ACKed and the second
   ACKed (`attempts:2`, possibly a `lost_arb` against the nodes' `FE C2` at PRI 8),
   and the nodes' wake burst (`FE C2`, `52:3F`, `FE:B6`, …). The bus should sleep
   again ~4.6 s later (`docs/BEAN_SIGNALS.md` §3).
4. **Afterwards.**
   - Read the DTCs of all ECUs: no new B12xx (B1214 = +B short on BEAN, B1215 = GND
     short, B1200…B1294 = communication stop).
   - The bus must go back to sleep by itself (`status` `bus:1`, receiver `idle_ms`
     growing); an edge or frame every few minutes would keep it awake.
   - 12 V current (`ma`) back at the parked level once the bus sleeps.
   - `status`: `fault` null, `frames`/`edges` equal to what was sent; the audit log
     lists every request and reply.

## Firmware versions that matter

| fw | Behaviour that still matters |
|---|---|
| 1.15.0 | `getlog` event ring |
| 2.0 | no power rails (`out` → `OUT_REMOVED`) |
| 2.1 | relay board, `hard_restart`, `rly` read-back, `rc`/`up`, `POCO2_NC` |
| 2.1.1 | force press 12 s (was 35 s) |
| 2.2.0 | NDJSON firmware update, `boot.py` releases GP27, Ctrl-C off after safe-boot, max hold 15 s. **Below 2.2.0 there is no `pb_flash.py` path.** |
| 2.2.3 | 5 ms relay read-back, `RELAY_IO` rate limit (needs new `pcf8574.py`), BEAN bench GP6/GP7 |
| 2.2.4 | I2C 100 kHz |
| 2.2.5 | `acc_sim` |
| 2.3.0 | `bean_rx.py`, id 3, TX guard; new OTA file name (one extra reboot) |
| 2.4.0 | `bean_tx.py`; bench ops need the bench override; receiver idle fast path |
| 2.4.1 | full BEAN batches sent at once (no standing backlog); wrap-safe ticks (no OverflowError after a > 537 s bus sleep, frame `t_ms` on the `ticks_ms` base, `idle_ms`/`age_ms`/`up` do not stick or wrap, dead-phone/boot-grace/TX-arm stamps cannot wrap after 6.2 days); `exn` counter |

Tests: `tests/test_powerbox_firmware.py` (firmware on CPython with stubs),
`tests/test_powerbox_io.py`, `tests/test_port_power.py`, `tests/test_bean_rx.py`
(PIO cycle simulation, all 8,627 NHW20 frames of bean_tools), `tests/test_bean_tx.py`
(PIO programs assembled with MicroPython's assembler, `tests/fixtures/micropython_rp2_v1_21.py`,
run against `tests/bean_pio_sim.py`), `tests/test_bean_backend.py`.

## Open gaps

| Gap | What would resolve it |
|---|---|
| **PCF8574 spontaneous reset to 0xFF** (MCU keeps running, every coil releases). Seen at a key-off (MFD Pi browned out, all hubs re-enumerated) and later in the field; cause unknown. Not the ACC line (gate-only input, shares nothing with the expander). | Scope expander VCC (RP2040 5 V) and SDA/SCL around an event; correlate PMLOG `rly_fix` time stamps with 12 V and load switching. Whether the 5 ms re-assert keeps loads up on the car is not yet confirmed. |
| Relays 5–8 never tested on this board (ch5 = RS485 rail). | Bench: switch each, confirm load and `rly`. |
| Cause of spontaneous bench resets (during rewiring) unknown. | Watch `rc` / `boot_count` over a long run: rc 1 → power/wiring, rc 3 → firmware stall. |
| BEAN TX on the car (fw 2.4.0): the bus wakes, but the `bean_wake` send reports `ok:0 err:"rx"`. `err:"rx"` is a pre-transmit refusal by `health()`; on 2.4.0 the edge-test pulse after a > 537 s sleep raises the tick overflow and marks the receiver unhealthy for 5 s (reproduced in `tests/test_bean_rx_wrap.py`). Whether 2.4.1 sends cleanly is not confirmed on the car; PIO load, `fifo_join=JOIN_TX`, 1 kHz `machine.Timer` and real rise/fall are not measured. | Flash 2.4.1, `bean_wake` on the parked car after > 10 min of bus sleep; read the reply `detail` and `exn`. |
| Receiver `cpu` asleep with fw 2.4.0 is 40–41 ‰, not the few ‰ the fast path was meant to give; where the time goes is not measured. | Profile `poll()` paths on the bench with the bus idle (`_quick` vs full pass vs `produced()`). |
| Main hub model and which sockets switch VBUS. `POCO/provision.sh` (backend.env seed) says main hub `1-1` = Terminus `1a40:0101`, 4 ports, powerbox on port 1; `backend/port_power.py`, `io/discovery.py` (`DEFAULT_PORT_ROLES = {5: powerbox, 2: gateway}`) and `prius-usb-power` describe an ActionStar `2101:8500` with real VBUS switching on internal port 5 only. Until settled, a hub-port cycle of the powerbox may be data-only. | `lsusb -t` + `uhubctl` on the car; cut port power and check whether `energy_mah` resets (true cold boot). |
| POCO button MOSFET not fitted. | Fit it, then set `POCO_BTN_ACTIVE_HIGH` / `ACTIVE_HIGH`. |
| No box-air sensor (BMP280 #1, AHT20 not fitted): box purge untested. | Fit one. |
| Second POCO slot is a placeholder (`poco 2`, `p2`). | — (single-computer layout) |
