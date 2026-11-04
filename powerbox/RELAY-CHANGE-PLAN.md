# Relay board + I2C — current state

Status: **IMPLEMENTED and flashed (firmware 2.2.0 on the board since
2026-11-04, bench-verified on the P52).** Decisions of 2026-11-03 are final; this file is the
current-state reference for the hardware and the code that drives it.

## Hardware as it is

**I2C0:** GP0 = SDA, GP1 = SCL (normal assignment).

| Address | Device | Firmware field |
|---|---|---|
| `0x20` | PCF8574 relay expander | `rly`, `rly_fix`, `rly_err` |
| `0x40` | INA219 (sees the 12 V supply) | `v`, `ma`, `mah` |
| `0x76` | BMP280, outside/cabin | `bmp2_t`, `bmp2_p` |

The inside-box BMP280 (`0x77`) and the AHT20 (`0x38`) are not fitted. The
firmware binds only what answers the bus scan (no error for absent chips) and
omits their fields; the backend leaves `bmp_t`/`aht_*` as `None`.

**Relay board:** 8-channel opto board with 12 V coils (HW-281 type), active-low inputs.
- IN1..IN8 go straight to PCF8574 P0..P7, so **board relay N = PCF pin P(N−1) = firmware channel N** (`RELAY_PIN_MAP = {n: n-1}` in `pcf8574.py`).
- JD-VCC = 12 V, always on; JD jumper removed.
- VCC (opto/input side) comes from the **RP2040-Zero 5 V pin**. When the RP2040 is unpowered or resetting, all NO coils are released.
- PCF8574 power-up state is 0xFF, so all coils are released after any restart.

**Loads** (relays 1–4 tested 2026-11-03; 5–8 not yet):

| Ch | Contact | Load | Backend model |
|---|---|---|---|
| 1 | **NC** | the powerbox's own supply, nothing else. Energised = hard restart (USB drops 0.4 s, re-enumerates 1.0 s, STATUS 4.9 s incl. 3 s safe-boot); de-energised (incl. RP2040 off/reset) = powered. | `powerbox_hard_restart` API → `{"a":"hard_restart"}`; never a port, refused by `set_relay`/`relay`. |
| 2 | NO | MFD Raspberry Pi Zero v1.3 (≈100–115 mA @ 12 V). Pi draws within ~1 s; USB gadget (`enx021e4d000001`) + ping at 49–50 s; three hard cycles → clean enumeration each. Every hard cut risks the SD card. | `port_power.mfd` / `backend/mfd_power.py`: `ssh poweroff` + wait, then cut. |
| 3 | NO | USB 2.0 hub `1a40:0101` with the CAN gateway (`2e8a:000a`, fw 3.2.0, powered from this hub) and the comma.ai panda (`bbaa:ddcc`, cuatro). On: 50 → 155 mA; hub enumerates ~6 s, gateway ~7 s (`GW_HB` 1 Hz, CAN LISTEN), panda ~9 s. | `port_power.gateway` (= the CAN hub; switching the gateway switches the panda). |
| 4 | NO | USB 2.0 hub VIA `2109:2817`, first device the fvision dashcam (`1902:3241`, USB storage; 1280×720 JPEG via `VSFILE`, sector 896, JPEG at byte 8704; camera clock wrong). On: +84 mA, enumerates ~6 s; no VBUS back-feed. | `port_power.camera`; `set_relay` ch4. |
| 5 | NO | RS485 satellite power — semantics kept from fw 2.0, **untested on this board**. | satellite wake-lock rule (`rs485_power`). |
| 6–8 | NO | unassigned (single-computer layout since 2026-11-04: no second POCO, so nothing is reserved). | generic `set_relay`. |

**Restart scope (Q3, accepted):** relay VCC comes from the RP2040, so during a
hard restart (and any other reset) every NO relay drops: MFD Pi (hard cut),
CAN hub (gateway + panda), camera hub. They come back **OFF**. The backend
re-asserts its recorded desired states ~6 s after the `POWERBOX_READY` banner
(`BackendService.PP_REASSERT_DELAY_S`, `HubPortPower.reassert_all`) and the
15 s `rly` read-back enforce sweep plus the satellite rule's mirror-retry
converge the rest. The MFD manager recovers through its ON-state watchdog.

**Chassis fan** (ADDA AB07505HX110B00, Dell 00G700, 5 V 0.40 A, 4-wire blower):
- PWM on **GP29** at 25 kHz, **not inverted** (GPIO duty = fan duty; no 2N7000). `FAN_PWM_INVERT = False`, `fan_duty_u16()` in `main.py`.
- Tach on **GP28**, internal pull-up, falling edges, 2 pulses/rev.
- 0 % stops the fan completely; 5 % ≈ 255 RPM; 100 % ≈ 4680 RPM. Full duty→RPM map in `powerbox/README.md`.
- GP29 undriven (bootloader, 3 s safe-boot) = fan at full speed; the firmware sets the PWM first thing in `main()`.

**Main POCO power button:** **GP27**. Press = drive LOW; release = input, no pulls; never driven high (phone line ~1.8 V). 6 s hold boots from off (verified 2026-11-03). Force press = 12 s hold = one clean hard restart (fw 2.1.1; the former 35 s hold rebooted the phone ~4 times, verified 2026-11-04). Since fw 2.2.0: `boot.py` releases GP27 on every reset (RP2040 pads reset with pull-down = button held), presses release in a `finally`, Ctrl-C is disabled after the safe-boot window, max hold 15 s, firmware updates go over NDJSON (`pb_flash.py`, no REPL); see README "Flashing firmware".

**"poco 2" command slot:** there is no second POCO (the one POCO runs the backend and roadsense; decision 2026-11-04). The firmware keeps the slot but refuses it (`POCO2_BTN_PIN = None`; `button`/`wake` for poco 2 → `ERROR POCO2_NC`, no GPIO touched). Backend: `io.powerbox.POCO2_BUTTON_CONNECTED = False`, no manager behind it. The roadsense stack is started/stopped on this POCO by `backend/roadsense_power.py` (camera hub = relay ch4).

**GP14/GP15:** dead pads, never assigned.

## What the code does (plan items, all implemented)

1. **Pin map** — `pcf8574.RELAY_PIN_MAP = {n: n-1}`; `RELAY_CH_MFD = 2`, `RELAY_CH_CAN_HUB = 3`, `RELAY_CH_CAM_HUB = 4`, `RELAY_CH_RS485 = 5` in `main.py` and `state/app_state.py`.
2. **NC contacts** — generic per-channel flag `pcf8574.RELAY_NC = (1,)`; `relay_level()`/`relay_is_on()` keep `on`/`rly` meaning "load powered".
3. **Restart relay** — `{"a":"hard_restart"}` only (`RELAY_RESTRICTED`); `relay ch=1` → `ERROR RELAY`; `rly[0]` is `null`. Ack first, coil energised on the next tick (`RESTART_ACK_DELAY_MS`); if still alive 3 s later the coil is released and `ERROR RESTART_FAILED` sent.
4. **Backend channels** — `PortPowerConfig(gateway_relay_ch=3, mfd_relay_ch=2, camera_relay_ch=4)`; `RelayPortPower`/`HubPortPower.build` refuse ch1; `build_relay_command(1)` raises; `set_relay` API rejects ch1; `SetGatewayUsbPowerAction` = CAN hub.
5. **Docs** — `main.py` header, `powerbox/README.md`, `POCO/POWER.md`, `docs/SATELLITE_POWER.md`, this file.
6. **Tests** — `tests/test_powerbox_firmware.py` (firmware on CPython with stubs: pin map, NC, restart lock-out, read-back/re-assert, fan duty, poco2 rejection, reset cause), `tests/test_powerbox_io.py`, `tests/test_port_power.py`.
7. **Read-back** — `pcf8574.RelayBoard`: wanted byte updated only after a successful write; `rly` from a chip read every STATUS (2 s); mismatch → re-write, `rly_fix` count, `rly_fix` PMEVENT on the first of a streak, `pcf_reset` flag when the chip read 0xFF; I2C errors → `ERROR RELAY_IO` + `rly_err`.
8. **Host heartbeats** — unchanged: TX gated on `{"a":"hb","n":N}`. New: the boot banner is parked and sent once on the first host-present edge, so the backend sees exactly one `POWERBOX_READY` per boot.
9. **Fan pins** — GP29 PWM non-inverted, GP28 tach (the former second-POCO button pin).
10. **Reset cause (Q5)** — `machine.reset_cause()` in the banner (`rc`), every STATUS (`rc`, `up`) and the `boot` event; backend mirrors `reset_cause`, `uptime_s`, `boot_count`. Cause of the bench resets (2026-11-03 ~13:03, 15:21, 15:22, 15:30, during rewiring) is still unknown — read `rc` from the car: 1 = power/brown-out, 3 = watchdog.

## Protocol delta (fw 2.0 → 2.1)

- New command `{"a":"hard_restart"}` → `{"ack":"hard_restart"}`, then `{"msg":"HARD_RESTART","up":s}`, then power drops.
- `{"a":"relay","ch":1,...}` → `ERROR RELAY` (was: switched the gateway socket under the old reversed map).
- `{"a":"button"|"wake","poco":2}` → `ERROR POCO2_NC` (was: pressed GP28).
- STATUS: `rly` entries may be `null` (ch1 always), plus `rc`, `up`, `rly_fix`, `rly_err`; `rly` omitted when the read-back failed.
- `POWERBOX_READY` carries `rc` and is delivered once the host heartbeats.
- New errors: `RELAY` (refused channel), `RELAY_IO`, `RESTART_FAILED`, `POCO1_NC`/`POCO2_NC`, `BMP280_READ`/`AHT20_READ` (sensor dropped).
- `SENSOR_OK` for the PCF8574 includes the adopted `port` byte.

## Bench verification checklist (after flashing `pcf8574.py` + `main.py`)

1. Banner: `POWERBOX_READY` with `ver 2.1.0` and `rc` arrives once the heartbeat daemon runs; STATUS shows `rc`, `up`, `rly: [null,0,0,0,0,0,0,0]`, `rly_fix 0`, `rly_err 0`.
2. Fan: boot → fan stops (0 %); `{"a":"fan","pct":50}` → ~2965 RPM in `fan_rpm`; 100 % → ~4680; 0 % → stops.
3. Relays: `relay ch 2/3/4 on` powers the right load (Pi draws, CAN hub + gateway + panda enumerate, camera hub enumerates); `rly` reflects it; `relay ch 1` → `ERROR RELAY`, nothing moves.
4. Read-back: pull the PCF's power or short a line briefly → `rly_fix` increments, `rly_fix` PMEVENT, load restored.
5. `hard_restart`: ack, `HARD_RESTART`, USB gone ~0.4 s, STATUS back ~5 s with `rc = 1`; relays 2–4 come back off; the backend (if running) re-asserts them ~6 s after the banner.
6. Button: `{"a":"button","poco":1,"ms":300}` → one press event on the phone; `poco:2` → `ERROR POCO2_NC`, GP28 tach unaffected.
7. Watch `rc`/`boot_count` over a long bench run for the spontaneous resets.
