# AVC-LAN in the backend (gateway firmware 3.x)

**Source of truth:** `cyberpunk-prius-gateway/captures/2026-10-07/AVC_MAP.md` (signal map,
touch calibration, remote climate procedure) and
`cyberpunk-prius-gateway/captures/2026-10-07/NAV_EMULATION_SPEC.md` (sections *Resolved on-car
2026-10-14* and *Real nav observed again*). The nav emulation is a port of
`gateway-rp2350/tools/nav_emu.py` as accepted by the real display on 2026-10-14 (`--fw-auto --fw-ack`).
Gateway JSON: `cyberpunk-prius-gateway/PROTOCOL.md` + `gateway-rp2350/README.md` ("Protocol: what changed").

History: the gateway fw 2.x era decoder, analysis scripts and notes (a bit-misaligned
re-reading of the bus: `040->200` buttons, `10C->310` climate, `A00->258` / `110->490` energy) were
removed on 2026-10-15 and live only in the repository history before the commit "remove legacy
fw-2.x AVC-LAN code" and the docs commit that followed it. The fw 2.x recordings themselves remain
in `logs/` and `assets/data/`. The verified capture history is the gateway repo's `captures/`
(AVC_MAP.md and the per-day notes).

Units: **110** display (EMV), **1C6** climate / info ECU (logicals E0 climate, E4/E5 info),
**178** nav (logical 58). Frames below are `master->slave data…` (hex).

## Code map

| Module | Role |
|---|---|
| `comm/avc/frames.py` | `AvcFrame` from gateway ID 2 payloads (`b`, `nak`, `echo`); TX payload builder |
| `comm/avc/decoder.py` | table `RULES` → typed updates (below) |
| `comm/avc/touch.py` | touch calibration |
| `comm/avc/nav_emulator.py` | nav ECU (178) emulation |
| `comm/avc/climate_remote.py` | climate button presses through the gateway |
| `comm/avc/scheduler.py` | timer thread (and a manual one for tests) |
| `io/avc_ingest.py` | updates → store actions, AVC priority, outside-temp expiry, `touch`/`button` events |
| `ui/screens/avc_status_screen.py` | ENGINE menu → AVC-LAN STATUS: read-only view of `AppState.avc` + signal sources |
| `io/file_io.py` | `--replay` of gateway-v3 captures |

## What is decoded

| Frame | Update | State |
|---|---|---|
| `1C6->110 00 E0 5D F3 b1..b6` (5 s + on change) | `ClimateStatus`: fan `b6>>5` (0 = climate OFF), AUTO `b1&80`, fresh `40`, recirc `20`, front defrost `10`, rear heater `08`, A/C `02`; vent `b2` 80 face+foot / 40 foot / 20 windscreen+foot / 00 with `b1&01` face | `climate.fan_speed, auto_mode, ac_on, recirculation, fresh_air, defrost, rear_defogger, air_direction, system_on, mode` |
| `1C6->110 00 E0 5D F5 NN 38` | `ClimateSetpoint` °C = 15.5 + 0.5·NN, NN 39 = OFF | `climate.target_temp` (unclamped) |
| `1C6->110 00 E0 5D F7 TT` (1 Hz) | `OutsideTemp` TT − 48 °C | `climate.outside_temp` (the only writer; `None` = N/A once stale, see below) |
| `1C6->110 00 E4 5F B9 F1 F2 00` | `EnergyFlow`: F1 40 engine→wheels, F1 08 engine→MG; F2 40 MG→wheels, 80 wheels→MG, 10 battery→MG, 08 MG→battery; engine running = F1 ≠ 0 | `energy.flow_*` (new MFD arrows + legacy flags), `charging/discharging/regen_active`, `flow_source = "avc"`, `vehicle.ice_running` |
| `1C6->110 00 E4 5F B8 00 00 XX YY 00` | `BatteryInfo` bars = (XX&07)+1, EV mode YY&40, EV cancelled YY&80 (provisional rows of AVC_MAP, user-confirmed live) | `dynamics.soc_bars`, `ev_mode_active`, `vehicle.ev_mode`, `avc.battery_bars` |
| `110->FFF 12 01 60 XX …` | `DisplayScreen` 58 nav, 5F energy, 5D climate, 5E audio, 56 info | `avc.screen`, `screen_name`, `nav_screen_active` |
| `110->FFF 01 01 5B 40` / `01 01 5A 80` | `Lights` on (display dims) / off — events only | `avc.lights_on` |
| `110->178 00 25 58 84 KK 00` | `NavButton` menu 01, dest 04, map 02, release 00 | `button` event, `input.last_button_*` |
| `110->178 00 25 58 80 KK 00 00` | `NavButton(soft)` maintenance 01, calendar 08, language 40 | `button` event |
| `110->178 00 21 24 78 X Y X Y` | `Touch`; `00 00 00 00` = release | `touch` event, `input.last_touch_*`, `avc.touch_*` |
| `110->1C6 00 25 E0 84 B1..B6` | `ClimateButtonFrame` (display's own presses, our echoes) | log only |

Not decoded (unknown or provisional in AVC_MAP): `E4 5F 97`, `E5 5F D8`, `E4 5F 96`, `E4 5F B4`
(shift-position candidate), `E5 5F DC`, `01 01 5F XX`, the remaining B8 bits.

Touch calibration (measured 2026-10-14): `px = (X − 4)·480/245`, `py = (255 − Y)·240/246`, clamped
to 0..479 / 0..239. X grows left→right, Y bottom→top.

## AVC priority

`AvcIngest.fresh(signal)` is true for 15 s after the last frame of a signal. While fresh, the CAN /
OBD paths in `io/ingress.py` do not dispatch the competing value:

| Signal | AVC source | Fallback when AVC is stale |
|---|---|---|
| energy arrows, charging / discharging / regen | `E4 5F B9` | CAN 0x3B6 (unverified bits; not on the bus the gateway taps), 0x3CB |
| engine running | `E4 5F B9` F1 ≠ 0 | CAN 0x038, OBD rpm > 0 |
| battery bars, EV mode | `E4 5F B8` | CAN 0x529 |
| climate | `E0 5D F3/F5` | none (CAN has no climate) |

**Outside temperature is AVC-only.** The CAN / OBD values were never right, so nothing else writes
`climate.outside_temp`: OBD PID 0146 only fills `vehicle.ambient_air_temp` (engine diagnostic, shown
on the solicited monitor and as the `ambient_temp` metric) and CAN 0x5CC is decoded but unused.
When the AVC value is stale (15 s, e.g. bus silent or climate ECU asleep), `AvcIngest.expire()` —
run on every `IngressController.update()` — sets `climate.outside_temp = None` and the UI shows N/A.

READY is not on AVC; it still comes from CAN. Lights from AVC go to `avc.lights_on`; CAN 0x57F
(when sniffed) still fills `dynamics.headlight_state`.

## Events (ZMQ PUB, `{"type":"event","name":…,"data":…}`)

* `touch`: `{"x": 0..479, "y": 0..239, "pressed": bool, "raw_x": int, "raw_y": int, "t": epoch}` —
  every touch frame while the finger is down (10–15 Hz); the release is `pressed: false` with the last
  x/y/raw. A release without a preceding press (the display sends one at some screen switches) is
  dropped.
* `button`: `{"key": "menu"|"dest"|"map"|"maintenance"|"calendar"|"language", "pressed": bool,
  "t": epoch}`. menu/dest/map send a release frame; the info soft buttons do not, so their
  `pressed: false` is emitted right after the press.

The display sends touch and buttons to 178 **only while it believes a nav is present**, i.e. with the
nav emulation running, and touch coordinates only while it shows screen 58.

## Nav emulation (live gateway only)

On every serial (re)connect and on `GATEWAY_READY` the backend sends, in nav_emu.py's order:

```
{"id":2,"d":{"a":"auto","slot":0,"on":true,"m":"110","s":"FFF","d":["12","01","00"],
            "tx":{"m":"178","s":"110","c":15,"d":["00","01","12","10","58","24","85"]}}}
{"id":2,"d":{"a":"auto","slot":1,"on":true,"m":"110","s":"FFF","d":["12","01","01"],
            "tx":{"m":"178","s":"110","c":15,"d":["00","01","12","12","58","28","29","32","60","74","A4","56"]}}}
{"id":0,"d":{"a":"avc_cfg","ack":"178"}}
```

`pol` is never sent (the gateway keeps its own setting). At shutdown `ack:"000"` and both slots off.

| Display sends (110) | Backend answers (178) |
|---|---|
| `FFF 12 01 00` / `12 01 01` | gateway auto-reply slots 0 / 1 (≈0.3 ms); a NAKed `00 01 12 10 58 24 85` echo is resent (≤ 3×) |
| `00 01 07` | `00 01 00 17` |
| `00 12 58 8F` (30 s) | `00 58 12 9F 02 F9 D1 13 0A 58 A6 9A 80 80` |
| `00 12 01 02 17 80 …` | `00 01 12 12 27 58 40 64 62 61 55 6E 6D` |
| `00 12 01 02 17 81 …` | `00 01 12 12 34 A2 58 43 63 65 44 45 E0` |
| `00 12 01 02 11 00 17 82 …` | initial burst: `58 32/56/34 E0` to 110, `85 E0 E0/E4/E6` to 1C6, then 3× `58 32/56/34 E0` at 300 ms |
| `00 32 58 E0` / `00 6E 58 E0` | `00 58 32 F0 C9 00 00 01` / `00 58 6E F0 C9 00 00 01` |
| `00 56 58 F0 …`, `00 32 58 F0 00 00 00 02` | `00 58 56 B7 02` (UI language; `--nav-lang`) |
| `00 56 58 8E` | `00 58 56 9E 00 01 01 HH MM SS FF` (local time, BCD) |
| `00 12 58 42 02 01` | `00 58 12 52 02 01` + `1FF 58 31 F1 02 00` (+4 ms); heartbeats on |
| `00 12 58 43 02 00` | `00 58 12 53 02 00` + `1FF 58 31 F1 00 00` |
| `FFF 12 01 20 XX` (60 s) | `00 01 12 30 XX FF` |
| `00 25 58 84 KK 00` (KK ≠ 00) | `00 58 12 50 02 01` (+4 ms) |
| `00 25 58 80 KK 00 00` | `00 58 12 50 02 01` |

Heartbeats `178->1FF 58 31 F1 02 00` and 0.6 s later `178->FFF 58 01 DB 00 00 00 00 00 00` every 5 s,
**only while the display shows screen 58**.

Backend restart while the car is on: if AVC traffic is seen for 3 s without a roll call and without any
display frame to 178, the backend broadcasts `178->1FF 01 12 13 58 24 85` (what the real nav sent once
booted, `realnav_full.ndjson` 15:07:09); the display re-runs the setup (`00 12 01 03`, `12 01 01`, …),
which the handlers above answer. Up to 3 announcements, 5 s apart, per bus session (a bus session ends
after 10 s of silence).

## Climate remote control

Press `110->1C6 00 25 E0 84 B1..B6`, release `… 84 00 00 00 00 00 00` 100 ms later, from the one
gateway serial writer; presses are queued (max 16) and spaced by 0.35 s after each release (the gap is
a choice, not a measurement; a 0.6 s hold auto-repeats).

| Button | B1..B6 | Command alias |
|---|---|---|
| AUTO | `08 00 00 00 00 00` | `climate_auto` |
| OFF | `04 00 00 00 00 00` | `climate_system_off` |
| recirculation | `02 00 00 00 00 00` | `climate_recirc` |
| front defrost | `01 00 00 00 00 00` | `climate_front_defrost` |
| temp up / down | `80 …` / `40 …` | `climate_temp_up` / `climate_temp_down` |
| windscreen+foot / foot / face+foot / face | `00 01 …` / `00 02 …` / `00 04 …` / `00 08 …` | `climate_vent_windscreen_foot` / `_foot` / `_face_foot` / `_face` |
| A/C toggle | `00 20 …` | `climate_ac` |
| rear window heater | `00 80 …` | `climate_rear_defogger` |
| fan 1/2/3/4/7 | `00 00 80/40/20/10/08 00 00 00` | `climate_fan_1` … |
| fan 5 / 6 | `00 00 00 00 08 00` / `00 00 00 00 04 00` | `climate_fan_5`, `climate_fan_6` |

Commands (ZMQ REP `{"command": …, "params": …}` or REST):

* `climate_button` `{"button": <name>, "count": 1..10}` and the `climate_<name>` aliases above;
* `set_climate` keeps working: `target_temp` → closed-loop up/down presses until `E0 5D F5` matches
  (gives up if the setpoint does not move within 1.2 s); `fan` 1..7 → fan button, 0 → OFF;
  `air_direction` 0 face, 1 face+foot, 2 foot, 3 windscreen+foot; `ac` / `recirculation` press the
  toggle only when the last status (≤ 15 s old) differs; `auto: true` → AUTO, `auto: false` is refused
  (no AUTO-off button exists).

In replay the climate frames go to the log output (`[WOULD SEND]`).

## Replay

`python -m cyberpunk_computer.backend --replay FILE` accepts the old backend logs and the gateway-v3
capture format (one `{"hm","raw","obj","ht"}` record per gateway line, plus `{"tx"}` / `{"note"}` /
header lines, which are skipped). `obj` is used, or `raw` when `obj` is missing (2026-10-07
captures), and playback is paced by `ht`. Nav emulation is off in replay.

## Unverified / open

* Gap between remote presses (0.35 s) and the 1.2 s setpoint confirmation timeout are choices.
* The recirculation button is assumed to toggle back to fresh air (only fresh → recirc is captured).
* Live re-announcement after a backend restart follows the real nav's behaviour but has not been
  tried with the emulator in the car.
* Time reply uses the phone's local clock (`00 01 01 HH MM SS FF`, as nav_emu.py does).
* `pol` is never sent: the gateway detects the RX polarity at boot (`avc_pol` in `GATEWAY_READY`).
  The 2026-10-14 car sessions forced `pol hi` from nav_emu.py; if boot detection disagrees on this
  PHY, the polarity has to be fixed on the gateway side.
