# BEAN on this car: signal map, gateway mirror, actuation, open gaps

Car: Prius NHW20, EU, left-hand drive, smart entry and smart start. Bus protocol, transceiver and
capture format: [`BEAN_PROTOCOL.md`](BEAN_PROTOCOL.md). Powerbox receiver/transmitter firmware and
commands: [`powerbox/README.md`](../powerbox/README.md) ("BEAN receiver", "BEAN transmitter").
Decoder: `cyberpunk_computer/comm/bean.py`. Analysis scripts: `tools/bean_analysis/` (§11).

Notation: `DST:MES`, data bytes in hex, `b0` = first data byte, bits as hex masks. BEAN has no source
field, so every sender below is inferred (start-up order, power domain, timing).

Confidence: **C** = confirmed on this car by at least two independent pieces of evidence (or at every
occurrence of a logged action); **P** = probable (one strong correlation, or a public decode plus a
consistent observation); **G** = guess; **?** = unknown. Evidence is named in one clause.

---

## 1. Cruise control

The owner's top priority. A later phase plans an adaptive cruise by man-in-the-middling the cruise
stalk, so the **set speed** matters most, then engaged/standby state and the stalk switches.

Decoded on the cruise drive of 2026-11-16 21:14–21:28 (experiment E5, partial: owner timeline below,
BEAN, broadcast CAN and the `21D3` poll; recording `2026-11-16_cruise_drive.ndjson.gz`, §10).
Decoder: `comm/solicited_can.py` (`21D3`), `comm/bean.py` (`62:C8`), `comm/can_decoder.py` (0x5C8,
0x120); fusion and freshness: `io/cruise_fusion.py`.

| Source | Content | Confidence |
|---|---|---|
| BEAN `62:C8` (gateway → meter, PRI 6, 2 bytes, 1 s + on change, IG only) | **b1 `10` = cruise main switch on** (indicator lit). Set 21:16:59.4, cleared/set at the 1 s off/on 21:19:59.9/21:20:01.2, cleared 21:23:54.7, all at the owner's times; the fastest main-switch source. Everything else `00` | C |
| CAN `0x5C8` (the gateway's copy of `62:C8`: `24 00 xx`) | b2 `10` = main switch, as above. In the trip log its content changed ~3 s after `62:C8` and missed the 1 s off/on (but see the timing caveat below). b0 bit 7 (`A4` instead of `24`) for 0.85 s at the refused SET at 37 km/h (21:20:02.3); also twice earlier that day (17:50:58, 17:52:29): meaning ? | main: C; b0 bit 7: ? |
| CAN `0x120` b4 bit 7 (and b7 bit 7) | **cruise engaged**: set at every engage, cleared at every cancel (4 engages incl. the refused one) | C |
| Solicited `0x7E2` mode 21 PID `D3`, reply `0x7EA` `07 61 D3 A B C D E`, polled 1 Hz | **A** = vehicle speed km/h (not a memory speed); **B** = set speed km/h (below); **C** = analog 0..107, rises while cruise pulls the car, 0 when it coasts or is off: probably the cruise drive (torque) request; **D** bit 7 = shift in D, bits 0–3 all set together while the main switch is on, bit 6 = cruise controlling the car; **E** not decoded (0, 1, 2, 5, 7, 8, 16 seen) | A, B, D: C; C: P (meaning) |
| Repair manual RM1076E 05-1678 | HV ECU → meter "cruise indicator" via the gateway on BEAN | = `62:C8` (C) |

`21D3` B in detail: set at SET to the current speed; follows RES/ACC and SET/COAST while engaged; kept
after a cancel (the resume speed: 63 after the 21:17:16 cancel, RES at 21:17:19 resumed to 63); cleared
to 0 once the car is slower than about 35 km/h (36 km/h kept, 32 km/h cleared). The refused SET at
37 km/h showed B = 37 and D bit 6 for exactly one poll, then both cleared (minimum set speed is 40 km/h
per the owner's manual). The third-party bit list in [`prius_can.md`](prius_can.md) (main, ready,
indicator, RES/ACC, SET/COAST, CANCEL, stop-lamp bits) does not match this car for C and D bits 0–5.

Not found anywhere: the stalk switches (RES/ACC, SET/COAST, CANCEL) are on neither BEAN nor broadcast
CAN (bit search over the whole session); they can only be inferred from B changes. No set speed on BEAN
or broadcast CAN: `21D3` is the only set-speed source.

**Timing caveat (trip log).** The gateway's passive CAN stream reaches the phone 0–5 s after the
solicited replies (passive copies of the same `0x7EA` replies: median 0.6 s, p90 2.1 s, max 4.8 s
later). So "0x120 ~2 s behind `21D3`" and "0x5C8 ~3 s behind `62:C8`" are at least partly delivery
latency, not the car. The backend therefore takes `21D3` only from the reply itself, never from a
passive copy of it, and prefers `21D3` over 0x120 for "engaged".

**Backend state** (`vehicle.*`): `cruise_main_switch` (`62:C8` → `21D3` → 0x5C8, best fresh source),
`cruise_active` (`21D3` → 0x120), `cruise_set_speed` (B while engaged), `cruise_memory_speed` (B, the
resume speed), `cruise_drive_request` (C raw). Main off clears engaged/set/resume; all sources silent
for 5 s → everything unknown. The MFD cruise slot (`OFF` / `READY` / `ON`, set speed, resume speed dim)
shows it.

Owner timeline (CET) and what the bus showed:

| Time | Owner | `21D3` B (A = speed) |
|---|---|---|
| 21:16:59 | main ON | D `80 → 8F`; `62:C8` `0010` at 59.4 |
| 21:17:02 | SET 52 | 52 (A 52), D `CF`, C 4 → 66 in 3 s |
| 21:17:06–15 | hold up | 53 55 55 57 58 60 61 63 65 66 (A 51 → 62) |
| 21:17:16 | cancel | 63 kept, D `8F`, C 0 |
| 21:17:19 | resume | 63 |
| 21:17:21–29 | hold down | 58 55 55 55 55 55 53 53 52 (A 61 → 56) |
| 21:17:37–50 | up | 53 (37) … 53, 55 (45), 55, 55, 55, 60 (49), 61 (50) |
| 21:17:52 | cancel | 58 kept, cleared at 21:17:58 (A 32) |
| 21:19:59 / 21:20:00 | main off / on | D `80`, `8F`; `62:C8` `0000` / `0010` |
| 21:20:02.6 | SET at 37 (refused) | 37 for one poll, then 0; 0x5C8 b0 bit 7 |
| 21:20:28 | SET 45 | 45 |
| 21:21:15 | down to 40 | 40 in one poll (45 → 40) |
| 21:21:21 | up to 42 | 42 in one poll (40 → 42) |
| 21:23:07 | cancel | 42 kept, cleared at 21:23:09 |
| 21:23:54 | main OFF | D `80`; `62:C8` `0000` |

**Set-speed steps** (`21D3` is polled once a second, so every step below is the change between two
polls, and taps closer together than 1 s merge):

* resolution: 1 km/h (integer). Smallest step seen +1 and −1 (21:17:06, 21:17:37, 21:17:29), which is
  consistent with one tap = 1 km/h, but no isolated tap was logged as such.
* hold up: +13 km/h in 9 s (≈ 1.4–1.6 km/h per second, steps of +1/+2 per poll), set speed 2–4 km/h
  ahead of the car, which follows at ≈ 1.2 km/h/s.
* hold down: the first poll −5, then −3, then flat for 4 s while the car slowed, then −2, −1: the set
  speed drops ahead of the car and then waits for it; no steady rate.
* single −5 and +5 steps per poll also appear (45 → 40, 55 → 60); whether these are 5 fast taps, a
  hold, or a 5 km/h step mode cannot be told at 1 Hz.
* C (drive request): integer 0..107 seen, smooth (1-count resolution), 70–80 in steady cruise at 42
  km/h, 85–107 while accelerating, falling to 0 while the set speed is below the car's speed.

**What would settle the step size:** the same procedure with `21D3` polled at 5–10 Hz (or the owner
marking each single tap with `bean_mark`), single taps 3 s apart at a steady speed, then holds of 2, 4
and 6 s, up and down. Rest of E5: stalk wiring (connector, which ECU reads it, signal levels per
switch) from the wiring diagram plus a multimeter, before any interposer design.

---|---|---|
| BEAN `62:C8` (gateway → meter, PRI 6, 2 bytes, 1 s, IG only) = CAN `0x5C8` (CAN→BEAN copy, §5) | `00 00` throughout the only IG-ON capture; cruise was not used in it | present: C; meaning: ? |
| Repair manual RM1076E 05-1678 | HV ECU → meter "cruise indicator" travels via the gateway on BEAN (no ID given) | P that a cruise indicator frame exists; G that it is `62:C8` |
| Third-party CAN list ([`prius_can.md`](prius_can.md)) | `0x5C8` byte C bit 4 (= `62:C8` b1 `10`) "cruise control on and active" | G (never seen set on this car) |
| Comment in `cyberpunk_computer/comm/can_decoder.py` | the same bit read as a "cancel" event, `0x5C8` sparse (about 18 frames in 37 min) | contradicts the line above; unresolved |
| Solicited PID `0x7E2` mode 21 PID `D3` ([`prius_can.md`](prius_can.md)) | b0 memory speed, **b1 set/resume speed km/h (0 below about 40 km/h)**, b2/b3 main switch, ready, indicator, RES/ACC, SET/COAST, CANCEL, active | G: third-party list, polled by the backend, never checked against an engaged cruise |

No recording with cruise engaged exists (BEAN, CAN or solicited). Not known: whether the set speed
appears on BEAN or broadcast CAN at all, which ECU reads the stalk, and the stalk's electrical
interface.

**Capture needed (experiment E5, §9):** a drive with the BEAN logger, CAN broadcast logging and the
`21D3` poll running, every step marked with `bean_mark` and 10 s apart:

1. Main switch on (indicator lit), wait. Main switch off, on again.
2. SET at a steady 60 km/h (note the speedometer and, if available, GPS speed).
3. RES/ACC tap ×3 (+1 km/h steps), SET/COAST tap ×3, hold RES/ACC for about 5 km/h, hold SET/COAST.
4. CANCEL with the stalk; RES (resume); cancel with the brake pedal; RES; main switch off.
5. Repeat SET at 80 km/h. Drop below 40 km/h with cruise in standby, then accelerate again.

This settles `62:C8`/`0x5C8` bits, whether `21D3` b1 tracks the set speed, and whether any other
BEAN or CAN ID carries it. A separate bench task: the stalk wiring (connector, which ECU reads it,
signal levels per switch) from the wiring diagram plus a multimeter, before any interposer design.

---

## 2. Nodes

Eight nodes, matching the eight BEAN ECUs RM1076E lists for this car. Addresses come from the first
byte of the `FE:AB` status and `FE:C2` wake frames.

| Node | ECU | Power | Evidence | Conf. |
|---|---|---|---|---|
| `40` | body ECU (instrument panel J/B) | +B | wakes the bus for every door/lock/remote event (19/19); holds the bus 5.5 s after each lock/unlock (answer-back); RM routes door switches and the door-control receiver line to it | P |
| `52` | gateway ECU | +B | receives the odometer `52:3F` (RM: "meter → gateway: ODO"); sends the PRI-6 CAN copies; 11-byte status frame; releases the bus last | P |
| `62` | combination meter | +B | destination of `62:6E`, `62:20/21/C8`; `62:6E` matches the key-not-in-vehicle warning | P |
| `B5` | smart ECU | +B | one side of the key-authentication exchange in the 30 s driver-door window (RM 05-1919) | G (B5/C1 may be swapped) |
| `C1` | transponder key ECU | +B | the other side of that exchange | G |
| `E9` | power source control ECU | +B | wakes the bus at a power-switch press; status follows power mode | P |
| `98` | A/C amplifier | IG | climate frames start 20–170 ms after it appears; receives compressor feedback `98:EC` | P |
| `19` | transmission control (P position) ECU | IG | by elimination; `FE:50` P-lock state appears with it | G |

With the car off only the six +B nodes answer a wake. The ECM is not on BEAN; engine values reach
BEAN through the gateway.

---

## 3. Network management and timing

* **`FE:C2 <node> <code>` = wake announcement (C).** The waking node's first `FE:C2` is never ACKed
  (17/17, 2/2), because the others are still waking. The rest answer 7 ms apart in a fixed order
  (52 at PRI 12, then E9, C1, B5, 62, 40); the waker repeats at +42 ms, ACKed. Then `52:3F` +54 ms,
  `40:59` +66 ms, event frames (`FE:E1`, `FE:B6`, `FE:83`, `FE:7F`) +130 ms, first periodic cycle
  +0.5 s (19/19 wakes within ±2 ms). An awake node can send `FE:C2` again to restart the network
  timers (E9 `87` before a handle-touch unlock). Second byte: `80` normal (90 for E9/B5), `A1` E9 at a
  power-switch press, `87` E9 at a handle event (G).
* **`FE:AB <node> <status> […]` = per-node status / keep-awake, 1.00 s (C).** Status bit 7 = "I need
  the bus"; set at wake, cleared 1.4–1.6 s later if idle. Lower bits, node-specific (G):
  * `40`, `62`, `C1`, `19`, `98`: `80`/`00` only.
  * `B5`: `90` at wake, `9F` driver door open in the key window, `93` door closed, window open; `8F`
    at the power press, `82` while ON, `8F`/`9F`/`93` in the post-off key window.
  * `E9`: `90` at wake, `8F` at the press, `83` while ON, `8F` → `00` after power-off.
  * `52` (11 bytes, `52 s 20 20 00 00 C0 04 8E 03 80` with the car off): b2 `20` → `30` with b7
    `04` → `06` when the IG nodes 19/98 appear; `→ B0` at the READY candidate moment (§4.3, `98:27`);
    at power-off `B0 → 30` (+1 s), `30 → 20` (+11 s), b7 `06 → 04` (+12 s). b9 is `03` on this car,
    `07` on the JP reference car (configuration?).
* **PRI bit 3 = immediate copy (C).** Each message has a base priority for the periodic copy and
  base + 8 for an immediate copy (first after wake, or on a value change): `FE:B6` 2/10 (31 of 31
  PRI-10 copies carry a change), `52:3F` 1/9, `FE:29` 6/14, `FE:C2` 8/12.
* **Base priority by class (G):** 0 network status (4 for the gateway), 1 configuration, odometer,
  display data, 2 body/meter state, 6 gateway CAN copies.
* **Frame rate:** about 19 frames/s awake with the car off, 21/s in the key window; all periodic
  messages 1.00 s ±10 ms (p10–p90), except `FE:D8` (1.010 s).

Sleep and wake (car off, C):

| Quantity | Value |
|---|---|
| Wake with no state change | awake 4.58 s (13 wakes, 4.55–4.62 s) |
| Node keep-awake bit when idle | cleared 1.42–1.62 s after wake (62 first, then E9, C1, B5, 40) |
| Body ECU hold after a lock/unlock event | 5.4–5.6 s (n = 5); 1.0 s after any other door/lamp change |
| Smart/transponder hold | end of the 30 s driver-door window + 1–2 s |
| Gateway `52` keep-awake | drops 0.87–0.93 s after the last other node |
| Last frame after the gateway drops | 2.07 s median (1.97–2.23, n = 24 incl. JP dumps) |
| Last payload change → silence | 4.3–8.0 s (median 4.6 s) |
| Longest awake (car off) | 85 s (two driver-door windows back to back) |
| Locked, no action | silent for 11.3 min (longest observed); no periodic wake seen |

There is no sleep frame: status bits drop node by node, the gateway last, about two more cycles
follow, then silence. Dome-lamp and door-open bits may still be set when the bus sleeps. Wakes with no
state change are started by the body ECU, probably by smart-key radio replies near the car (G).

---

## 4. ID map

Columns: sender (inferred), PRI base/immediate, data length, period, **when** = OFF (also present with
the car off and the bus awake) or IG (only with IG on), CAN copy (§5), short meaning. Details for the
IDs with several fields follow in §4.1–§4.5. Counts and periods are from the IG-ON drive capture.

| ID | Sender | PRI | Len | Period | When | CAN | Meaning | Conf. |
|---|---|---|---|---|---|---|---|---|
| `40:59` | ? (to body) | 2/10 | 2 | 1 s | OFF | — | constant `00 C0` in every state (JP the same) | ? |
| `52:3F` | meter | 1/9 | 4 | 10 s + wake | OFF | 0x53F | odometer, b0–b2 km (24-bit); b3 `80`. Frozen at `04 93 DF` = 299,999 km on this car (the NHW20 meter stops there; `52:76` kept counting): distance must come from 0x0B4 / `52:76` | C (owner read the meter) |
| `52:6D` | A/C amp | 2/10 | 3 | 1 s | IG | 0x56D | b0 cabin (room) temperature, °C = 0.25·b0 − 6.5; b1 `20` blower running; b2 = `FF` − b1 (§4.2) | P |
| `52:76` | meter | 9 | 3 | on change + ~15 s | IG | — | trip distance, b1 0.1 km/count, TRUE distance (= integrated 0x0B4, not the speedometer's); not reset at IG; b2 `80` = km | C |
| `52:90` | meter | 1/9 | 3 | 0.5 s | IG | — | instantaneous consumption b0–b1 0.1 L/100 km over TRUE speed (§4.6); `00 00` no fuel (ICE off / EV, or fuel cut); `FF FE` stopped with ICE on; b2 `40` unit | C |
| `52:99` | A/C amp | 2 | 3 | 5 s | IG | — | constant `00 00 00` | ? |
| `52:A2` | meter | 1/9 | 3 | 10 s | IG | — | average consumption b0–b1 0.1 L/100 km since the meter's own trip reset; frozen while parked; b2 `40` unit (§4.6) | C |
| `52:A4` | meter | 2 | 1 | 3.2 s | IG | 0x5A4 | fuel gauge level (`00` in the first frame) | C |
| `52:CA` | A/C amp | 2 | 2 | 5 s | IG | — | constant `00 71` (configuration?) | ? |
| `52:D0` | meter | 1/9 | 5 | 1 s | IG | — | constant zeros | ? |
| `52:ED` | A/C amp | 2/10 | 5 | 1 s | IG | 0x5ED | compressor request to the HV ECU (§4.2) | C/P |
| `52:FD` | A/C amp | 2/10 | 2 | 5 s | IG | — | b0 target temperature, +1 per 0.5 °C, `05` = 18.0 °C; b1 `38` unused second zone | C |
| `62:20` | gateway | 14 | 2 | 0.92 s, ICE running only | IG | 0x520 | ECM fuel quantity PER INJECTION for the trip computer, 16-bit; flow = K × word × rpm (§4.3) | C |
| `62:21` | gateway | 6 | 1 | 0.3 s | IG | 0x521 | constant `00`; warning demands to the meter? | G |
| `62:6E` | ? | 2/10 | 2 | 1 s | OFF | — | key-not-in-vehicle warning: b0 `80` warning active, b1 `80` the ~1.2 s beep | C |
| `62:C8` | gateway | 6 | 2 | 1 s | IG | 0x5C8 | cruise control (§1): b1 `10` main switch on | C |
| `98:27` | gateway | 6/14 | 2 | 1 s | IG | 0x527 | b0 `08` ICE stopped; b0 `01` + b1 `FF` HV ready (§4.3) | C / P |
| `98:B2` | gateway | 6/14 | 3 | 5 s | IG | 0x5B2 | `00 04 00` from 0.4 s after READY; power permission to the A/C? | G |
| `98:CC` | gateway | 6 | 2 | 0.26 s | IG | 0x5CC | raw outside-temperature sensor count (NTC), ≈ 535 − 5.6·T °C over 4–10 °C (§4.3) | P |
| `98:EC` | gateway | 6 | 6 | 0.5 s | IG | 0x5EC | compressor feedback to the A/C amp (§4.2) | C/P |
| `98:F8` | gateway | 6 | 1 | 1 s | IG | 0x5F8 | constant `00` | ? |
| `FE:24` | meter | 1 | 1 | 0.5 s | IG | — | **displayed speed**, km/h (§4.4) | C |
| `FE:26` | gateway | 6 | 2 | 0.5 s | IG | 0x526 | engine rpm = (b0·256 + b1) / 5.12 | C |
| `FE:29` | gateway | 6/14 | 5 | 1 s | IG | 0x529 | HV status: b2 `80` READY, `08` brake, bits 0–2 MFD SOC bars (§4.3) | C |
| `FE:2C` | gateway | 6 | 1 | 1 s | IG | 0x52C | coolant °C = x/2, floor `20` (16 °C) while colder | C |
| `FE:40` | gateway | 6/14 | 3 | 1 s | IG | 0x540 | shift: b0 `80` P, `40` R, `20` N, `10` D, `00` in between | C |
| `FE:50` | P ECU `19`? | 2/10 | 1 | 1 s | IG | — | `80` P locked, `08` out of P, `04` ECU on, `02` power-off handshake | P |
| `FE:53` | ? | 1 | 6 | 1 s | OFF | 0x553 | constant `49 00 00 00 00 00`; vehicle specification bitmap | G |
| `FE:54` | ? | 1 | 6 | 1 s | OFF | 0x554 | constant `57 42 4C 41 45 4E`; specification/destination bitmap | G |
| `FE:62` | power source `E9` | 2/10 | 2 | 1 s | OFF | — | power state (§4.5) | P |
| `FE:7F` | body | 2/10 | 6 | 1 s | OFF | 0x57F | lamps and door-open indicator (§4.1) | C |
| `FE:83` | body | 2/10 | 4 | 1 s | OFF | — | b0 `20` ACC, `40` IG; b3 `08` dome lamp, `04` luggage lamp (§4.1) | C |
| `FE:91` | gateway | 6/14 | 3 | 0.3 s | IG | 0x591 | `A0 1F 06` before READY, `00 00 00` from READY on | G |
| `FE:AB` | each node | 0 (52: 4) | 2 / 11 | 1 s per node | OFF | — | network status (§3) | C |
| `FE:B6` | body | 2/10 | 2 | 1 s | OFF | 0x5B6 | lock state and door switches (§4.1) | C |
| `FE:C2` | each node | 8 (52: 12) | 2 | at wake | OFF | — | wake announcement (§3) | C |
| `FE:CD` | A/C amp | 2 | 1 | 1 s | IG | — | outside temperature °C = x − 48 | C (offset and slope, 4–10 °C) |
| `FE:D4` | gateway | 6 | 1 | 1 s | IG | 0x5D4 | constant `00`; warning flags (RX300: starter/charge/oil) | G |
| `FE:D8` | ? | 2/10 | 2 | 1 s | OFF | — | b1 `01` driver-door key window (~30 s); b0 power-mode flags (§4.5) | C (b1) / G (b0) |
| `FE:DB` | ? | 2 | 1 | 1 s | OFF | — | constant `00` | ? |
| `FE:E1` | body | 2/10 | 3 | 1 s | OFF | — | lock/unlock **event report** (§4.1) | C |
| `FE:FB` | gateway | 14 | 6 | event | IG | — | climate button frame, one bit per press (§4.2) | C |
| `FE:FC` | A/C amp | 2/10 | 6 | 5 s + event | IG | — | climate status (§4.2) | C / P |
| `B5:D9`, `C1:E2` | smart / transponder | 10 | 3 | 0.5 s in the key window | OFF | — | key authentication exchange (§8) | P |

The 14 IDs present with the car off: `40:59`, `52:3F`, `62:6E`, `FE:53`, `FE:54`, `FE:62`, `FE:7F`,
`FE:83`, `FE:AB`, `FE:B6`, `FE:C2`, `FE:D8`, `FE:DB`, `FE:E1` (plus `B5:D9`/`C1:E2` in the key
window). Every changing bit in them is explained. Constant in every state: `40:59 00 C0`, `FE:DB 00`,
`FE:53`, `FE:54`.

### 4.1 Body: doors, locks, lamps, power flags

`FE:B6` (C, every occurrence in the door/lock captures):

| Byte | Bit | Meaning |
|---|---|---|
| b0 | `01` | driver door unlocked; set first at every unlock, cleared at lock |
| b0 | `40`, `80` | other doors unlocked, two groups (unlock `01 → C1` in 17–25 ms; lock `C0 → 00`, or `C0 → 40 → 00`); which doors: open gap |
| b1 | `80` | driver door open |
| b1 | `40` | front passenger door open |
| b1 | `04` | a rear door or the back door open (one bit for all three) |

`FE:B6` b0 is the body ECU's lock state, not a switch read: it follows `FE:E1` within 8–9 ms, faster
than a lock motor moves.

`FE:E1` b2 = lock/unlock event, one or two PRI-10 frames, then `00 00 00` (C):

| Value | Event |
|---|---|
| `04` / `08` | remote (fob button) unlock / lock |
| `10` / `20` | smart (handle touch) unlock / lock; also on leaving the car |
| `20` | 30 s auto re-lock after an unlock with no door opened (Δ = 30.0 s) |
| none | interior lock switch (`FE:B6` changes alone) |

`FE:E1` is a status report the body ECU sends after it acts, not a command (§7).

`FE:7F` (6 bytes):

| Byte | Bit | Meaning | Conf. |
|---|---|---|---|
| b0 | `10` | tail/parking lamps | C (switch sequence) |
| b0 | `20` | low beam | C |
| b0 | `01` | front fog lamp | C (one occurrence) |
| b0 | `08` | high beam | C (2026-11-16 17:44:36–41, 17:44:45–50, 17:46:23–29, about 5 s each, moving with low beam on; CAN 0x57F b1 `30 → 38` in the same three episodes) |
| b1 | `08` | follows the tail lamps (illumination) | C |
| b1 | `10` | always set except in the first frame of a door-open wake | ? |
| b2 | `80` | any door open (meter indicator) | C |

`FE:83` (4 bytes): b0 `20` = ACC, `40` = IG (C: `00 → 20 → 60` within 34 ms at power-on, the IG
nodes appear 0.3–0.6 s after `40`; `60 → 40 → 00` at power-off; same order on the JP car); `02` = key
in slot on the JP car, never set on this car. b3 `08` = interior (dome) lamp on, set at unlock or door
open, cleared 15.2 s after the driver door closes or at lock (C); b3 `04` = luggage lamp / back door
open (one occurrence). The powerbox ACC input follows these moments 0.6–0.9 s later (debounce) and
cannot separate ACC from IG.

`FE:D8` b1 `01` = driver-door key window: set when the driver door opens, cleared 29.9–30.0 s after it
closes (n = 4); the key exchange runs exactly inside it (C timing, RM 05-1919 for the meaning).

### 4.2 Climate

Every AVC-LAN climate command makes the gateway send a momentary `FE:FB` frame (one bit, back to zero
within about 0.3 s); `FE:FC` status follows (C, scripted run through the project's AVC climate
remote).

`FE:FB` (6 bytes, gateway, PRI 14) button bits:

| Action | Byte | Bit |
|---|---|---|
| temperature up | b0 | `80` |
| AUTO | b0 | `08` |
| recirculation toggle | b0 | `02` |
| A/C toggle | b1 | `20` |
| direction face / face+foot / foot / windscreen+foot | b1 | `08` / `04` / `02` / `01` |
| fan 1 / 2 / 3 / 4 / 7 | b2 | `80` / `40` / `20` / `10` / `08` |
| fan 6 | b4 | `04` (one observation) |

Not yet seen: temperature down, fan 5, AUTO off. For "temperature 18.0" and "AUTO off" the AVC
remote sent no command at all, so these say nothing about BEAN. The hard climate keys and the
steering pad are wired to the A/C amp and produce no `FE:FB` (a 20.0 → 18.0 °C pad change and a
front-defrost press showed none).

`FE:FC` (6 bytes, A/C amp) status:

| Byte | Bit | Meaning | Conf. |
|---|---|---|---|
| b0 | `80` | AUTO | C |
| b0 | `40` | fresh air (clear = recirculation) | C |
| b0 | `10` | front defroster (forces A/C on, fresh air, fan 5, AUTO off) | P (one occurrence) |
| b0 | `08` | rear defogger | P |
| b0 | `02` | A/C on | C |
| b0 | `01` | face | C |
| b1 | `80` / `40` / `20` | face+foot / foot / windscreen+foot | C |
| b2 | `04` | pulses on each change | C |
| b5 | top three bits | fan level 1..7 | C |

`52:FD` b0 = target temperature (+1 per 0.5 °C, `05` = 18.0 °C, `09` = 20.0 °C).

`52:6D` (A/C amp → gateway → CAN 0x56D):
* b0 = cabin (room) temperature, raw (P): rises `35` → `65` during warm-up, drops 2–3 counts within
  about 10 s of each door opening (3/3), independent of outside, coolant and intake temperature.
  °C = 0.25·b0 − 6.5: scale P (two soak points against the powerbox sensor plus the warm-up slope,
  2026-11-16 data crunch), offset G (Toyota A/C data-list range −6.5…57.25 °C; the box suggests −8.5,
  so it may read 1–2 °C high). At the end of each 15-min AUTO drive it read 17.0–20.0 °C at setpoints
  18/20. The backend feeds `climate.inside_temp` from it (None after 10 s without the frame).
* b1 `20` = blower running (= `FE:FC` fan > 0, 3982/3992 s; P → C); b1 `80` clears in warm-up windows
  (?). b2 = `FF` − b1.

`52:ED` (A/C amp → gateway → CAN 0x5ED, the HV ECU's input from the A/C):

| Byte | Bit | Meaning | Conf. |
|---|---|---|---|
| b3–b4 | | compressor target rpm (`06A4` = 1700, `0BB8` = 3000) | C (`98:EC` settles to it) |
| b0 | `40` | compressor running (366/379 set frames with rpm > 0; 652/656 clear with rpm = 0) | C |
| b0 | `20` | A/C switch on (matches `FE:FC` b0 `02` in 1213/1220 frames) | C |
| b0 | `08` | on from IG/READY | P |
| b0 | `04` | ICE stopped (echo of `98:27` b0 `08`, same 16 edges) | C |
| b0 | `02` | target ≠ 0 | C |
| b0 | `01` | only with a 3000 rpm start-up request | G |
| b1 | `80` (b2 = complement) | A/C requests ICE run for cabin heat: clears 1.2–1.5 s before 5 of 6 standstill ICE stops, sets 0.5–1.3 s before 4 restarts, at coolant 53–59 °C | P |
| b1 | `20` | front-defroster mode (exactly the `FE:FC` DEF interval, once) | P |

`98:EC` (gateway → A/C amp, CAN 0x5EC): b0–b1 actual compressor rpm (C, tracks the `52:ED` target);
b2 ≈ rpm/170 and b3 peaks at compressor start, both non-zero only while running: power/current (G);
b4 slow, `06` → `12` over the drive, r = 0.99 with `52:6D` b0 and 0.97 with the 0x3CB battery
temperatures: a slow temperature, maybe the compressor inverter (G); b5 tracks HV bus voltage, V ≈
1.9·x + 102 (r = 0.977 against 0x03B) (P).

`FE:CD` = outside temperature, x − 48 °C (C: `34` = 4 °C against OBD 0146 4–5 °C and the powerbox
sensor 4.9 °C; x − 40 and x/2 ruled out; slope confirmed over 4–10 °C on 2026-11-16, identical or ±1
against OBD 0146 for 4,000 s). Path
(RM 05-1054, 05-1019): sensor → ECM → CAN → gateway → BEAN → A/C amp, which then sends `FE:CD` to the
meter and the MFD (AVC `E0 5D F7`). It exists only with IG on: the ECM and the A/C amp do not take part
in car-off wakes and no BEAN request produces it. With the car off the options are the last IG-ON value
or the project's own sensor.

### 4.3 Powertrain data relayed by the gateway

* `FE:29` (copy of CAN 0x529), b2: `08` brake pedal (C: 74 of 79 edges against 0x030 b4, +2 ms);
  bits 0–2 MFD SOC bars (3/4/5 seen, with hysteresis against 0x3CB SOC: 3 bars at 50–54 %, 4 at
  54–57.5 %, 5 at 55–58 %); `80` HV active/READY (set in the start sequence `0B → 2B → 8B → CB → 8B`,
  cleared 0.2 s after the power-off press). Bytes 0, 1, 3, 4 = warning bits per 0x529 (red triangle,
  P-lock problem, EV-mode messages), always 0 (C).
* `98:27` (0x527): b0 `08` = ICE stopped while READY (C: all 16 edges at ICE spin-down/start, exact
  complement of when `62:20` is sent). b0 `01` + b1 `FF` set at the same moment `FE:91` goes `A0 1F 06
  → 00 00 00` and `FE:AB[52]` b2 gains `80`: the best READY candidate (P; CAN not yet logged then).
* `62:20` (0x520 b1–b2, gateway → meter): sent only while the ICE runs (no frame with rpm < 400 on
  the drive; presence agrees with OBD rpm > 400 98.8 % of the time); 16-bit value 196–2196, r = 0.84
  against OBD 0104 load. It is the fuel quantity PER INJECTION (RM: "ECM → meter: injection volume"),
  so the fuel flow scales with engine speed: flow L/h = K × word × rpm, K = 2.69 × 10⁻⁶ (fitted to the
  meter's own total; 2.82 × 10⁻⁶ fitted to the instantaneous `52:90`; ±5 %). A free fit of the meter's
  L/h gives word^1.0 × rpm^1.0 (C; scale per [`prius_can.md`](prius_can.md) §8).
* `FE:26` rpm = word/5.12 (C: r = 0.991 against OBD 010C, maximum `4230` = 3309 rpm = the OBD maximum).
* `FE:2C` coolant = x/2 °C (C: r = 0.995 against 0x039, within 1 °C; holds `20` = 16 °C when colder).
* `FE:91` (0x591): `A0 1F 06` from IG to the READY moment, then `00 00 00`; CAN 0x591 `25 00 00 00`
  throughout. The JP car shows `80 20 00` in READY; that reading does not transfer (G: HV ECU start-up
  handshake).
* `98:CC` (0x5CC): 10-bit raw count of the outside-temperature sensor (NTC, falls when warmer),
  ≈ 535 − 5.6·T °C over 4–10 °C, r = −0.99 against OBD 0146 and CAN 0x3CF b1 (n = 3,965 s, 2026-11-16)
  (P). The earlier "not outside temperature" came from a single drive at a constant 4 °C.

### 4.4 Speed: real and displayed

`FE:24` is the **displayed (speedometer) speed**, integer km/h, sent by the meter. It reads about
12 % above the true speed: 0x0B4 = 0.891 × `FE:24` (r = 0.998), and the meter's own trip distance
`52:76` agrees with 0x0B4 (25 increments of 95–106 m of integrated 0x0B4 speed). This matches the
owner's observation that the speedometer over-reads by about 12 %. C that it is speed; P for the
over-read explanation (E6 confirms it).

The vehicle model keeps both: `real_speed` = CAN 0x0B4 (true speed, used for distance and any
control) and `display_speed` = BEAN `FE:24` (what the driver sees on the speedometer).

### 4.5 Power state and P position

* `FE:62` (power source ECU): b0 `10` power on, `20` in P, `08` power switch pressed (pulse), `80`
  wake transient (P, confirmed on this car for `10`/`20`/`08`); `40` key present (JP-derived, G); b1
  `20` for 10 ms at the power-off press (G: power-off request).
* `FE:50`: leaving P `84 → 04 → 0C`, entering P `0C → 04 → 84`, power-off `84 → 86 → 82 → 80`:
  `80` P locked, `08` out of P, `04` ECU on (IG), `02` pulses in the power-off/auto-P handshake (P:
  3 exits, 2 entries, 1 power-off, plus the JP car).
* Step-by-step start, 2026-11-16 20:40 (trip log; owner pressed without the brake, then with it), C
  for the order:

  | Time | Step | Bus |
  |---|---|---|
  | 20:40:09.9 | power off (previous IG) | `FE:62` `3820 → 3800 → 3000 → 2000` (b1 `20` = the off press, now seen twice), `FE:50` `86 → 82 → 80`, `FE:83` `40 → 00` |
  | 20:40:15.5 | press, no brake → **ACC** | `FE:83` `20`, `FE:62` `3000` |
  | ~20:40:21 | press again, no brake | `FE:62` b0 `08` (word `0x0800`) for the press only |
  | 20:40:21.5 | **IG** (not READY) | `FE:83` `60`, `FE:50` `04`, `FE:29` starts (brake bit clear) |
  | 20:40:23.9 | brake pedal | `FE:29` b2 `08` |
  | 20:40:26.4–27.6 | press with brake → **READY** | `FE:62` b0 `08` pulse again; `FE:29` b2 `80`, `98:27` `01FF` (READY candidates of E1 confirmed in this order) |
  | 20:40:31.6 / 20:41:06.6 | R / D | `FE:40` |

  The starts at 20:38 and 21:14 (brake held, one press) reached READY within 1.3 s. E1 is done.
* `FE:D8` b0: `00 → 0C` at the power press with `FE:83`; `0C → 04 → 00` within 0.1 s at power-off; on
  the JP car `0C` already at ACC and `04` while a key is in the slot: `08` power not off, `04` key
  detected/authenticated (G).
* `FE:40` (from CAN 0x120 b5): the gateway relays it 45–110 ms after CAN, on all 20 shifts (C). B
  position not captured.

### 4.6 Meter trip computer

`52:76`, `52:90`, `52:A2` go meter → gateway → MFD, PRI 9 on change. b2 `40` in `52:90`/`52:A2` =
unit flag (RX300 uses `80` for km/L; this EU car shows L/100 km) (G).

* The meter works on TRUE speed and TRUE distance: `52:90` × 0x0B4 speed integrates to the
  K × word × rpm fuel within 0–4 % in every speed band (× the speedometer speed `FE:24` it would be
  12–17 % high), and `52:76` steps with integrated 0x0B4 distance (§4.4) (C).
* `52:90` = the meter's instantaneous L/100 km from the `62:20` flow and true speed, lagging the
  flow by 0–1.5 s; median ratio K × word × rpm / speed ÷ `52:90` = 1.00. `FF FE` = stopped with the
  ICE running (987 frames, 96 % at speed 0, 3 with rpm < 400): the meter has no L/100 km then.
  `00 00` = no fuel: ICE off / EV (450 frames, 420 with rpm < 400) or fuel cut while the ICE turns.
  Maximum seen 196.1 at walking pace (C).
* `52:A2` = the meter's average L/100 km since its own trip reset (not reset at IG; frozen while
  parked). `52:A2` × `52:76` / 100 follows the integrated K × word × rpm fuel within 0.03 L over the
  drive (C).

The backend shows `52:90` as the instantaneous figure while it is fresh (FFFE → our L/h, 0000 → 0)
and keeps both averages: the meter's (`52:A2`) and its own per-drive litres / true distance
(`cyberpunk_computer/state/rules/fuel_consumption.py`, `trip_fuel.py`).

---

## 5. Gateway BEAN ↔ CAN copies (CAN 0x500 | MES)

The gateway copies a set of BEAN messages to and from CAN at ID `0x500 | MES`. CAN byte 0 is a
header, bytes 1… are the BEAN data verbatim (C, all pairs below byte-compared in the drive capture).

* Header low nibble = BEAN ML; bit 7 marks an immediate copy (`A4`, `E7`).
* Header high nibble `2` = **CAN → BEAN**: data the gateway builds from CAN and sends on BEAN at PRI
  6/14: `62:20`↔0x520, `62:21`↔0x521, `FE:26`↔0x526, `98:27`↔0x527, `FE:29`↔0x529, `FE:2C`↔0x52C,
  `FE:40`↔0x540, `FE:91`↔0x591, `98:B2`↔0x5B2, `62:C8`↔0x5C8, `98:CC`↔0x5CC, `FE:D4`↔0x5D4,
  `98:EC`↔0x5EC, `98:F8`↔0x5F8.
* Header high nibble `5`/`6` = **BEAN → CAN**: `FE:53`, `FE:54` (`58`), `52:6D` (`65`), `FE:7F` (`68`),
  `52:A4` (`63`), `FE:B6` (`64`), `52:ED` (`67`). `52:3F` appears on CAN as 0x53F.
* `0x528` (`25 xx yy 00`) has no BEAN counterpart in any capture. Its bytes follow the energy-monitor
  arrows: `00 00` idle, `0C 0C` engine charging the battery, `00 74` battery to wheels, `00 AC`
  regeneration, `6C 74`/`6C 6C` engine plus battery. Probably for the display path (P).

Consequences for reading CAN 0x5xx: byte indices are shifted by one against BEAN (0x529 byte D = `FE:29`
b2). `0x5CC` byte 0 is the header `24`, so "ambient = byte 0 − 40" always gives −4 °C: 0x5CC is
`98:CC`, not a temperature. `0x57F` byte 3 bit 7 is `FE:7F` b2 bit 7 = door open, not DRL.
`0x5B6` byte 2 = `FE:B6` b1 (bit `40` = front passenger door, not "other doors"; rear/back = `04`).

On AVC-LAN the gateway also copies three BEAN payloads verbatim (`1C6→110`): `E0 5D F3 …` = `FE:FC`,
`E0 5D F5 …` = `52:FD`, `E0 5D F7 TT` = `FE:CD`.

---

## 6. Not on BEAN (this car)

Checked against logged actions; nothing changed except signals explained by other causes.

| Function | Evidence |
|---|---|
| Wipers (single, low, high, off) | no frame changed (only the climate script and one SOC-bar step in the window) |
| Seat belt (unbuckle/buckle) | nothing within ±5 s except a fan step and an ICE stop |
| Rear fog lamp | only one fog bit (`FE:7F` b0 `01`) exists for two fog steps |
| Light-reminder chime | no chime frame; low beam drops at power-off, tail bit stays until switched off |
| Map lamps | the bus stayed asleep |
| Door lock/unlock command | see §7 |
| Power windows, mirror adjust, horn | wired (RM 05-1722…1729: master switch drives the motors; no MPX terminal) |

---

## 7. Actuation: what BEAN can command on this car

**No door lock/unlock command exists on this BEAN bus (C).**
* Replay: the exact recorded remote-unlock frame `FE:E1 PRI 10 00 00 04` was injected twice on the
  parked car (cold, and 2 s after a wake). Both ACKed; `FE:B6` stayed `00 00`, the body ECU's `FE:E1`
  stayed `00 00 00`, no 30 s auto re-lock followed.
* RM1075U / NCF256E BE-26: the body ECU receives the lock/unlock request from the smart ECU over a
  **dedicated serial line with a local protocol** (body ECU pin RDA, B7-23, "door control receiver
  input", about 6–7 V idle), drives the lock motors over hard-wired outputs, and only reports status on
  BEAN. Auto re-lock is body-ECU internal.
* The only directed (non-`FE`) frames with the car off are `40:59` (constant), `52:3F`, `62:6E` and the
  key exchange.

Routes to lock control, ranked: (1) relay/driver on the body ECU's hard-wired lock-motor outputs
("Power Door Lock Control System" in the EWD); (2) reverse-engineer the RDA serial line (undocumented,
high effort); (3) body-ECU diagnostic active test over K-line.

What BEAN does carry into an actuating ECU:

| Rank | Function | Path | Frame | Status | Route |
|---|---|---|---|---|---|
| 1 | Climate: AUTO, temp, fan, mode, A/C, recirc (and rear defogger, mirror heaters by design) | gateway → A/C amp | `FE:FB` button bits (§4.2) | produced by the AVC remote (C); BEAN transmit untested | keep AVC-LAN (`climate_remote.py`); `FE:FB` only as a fallback |
| 2 | Meter warning/beep | → meter `62` | `62:6E` b0/b1 `80` (key-not-in-vehicle) | meaning C; transmit untested | single PRI-10 frame test (T-2) |
| 3 | Body ECU: Door Lock, Hazard, Back-door open, Security indicator | tester K-line (DLC3 SIL pin 7) → gateway → BEAN diagnostic message → body | unknown | exists per RM 05-1787, 05-2214 (P); honoured only in diagnosis mode (SAE 970297) | send the K-line request ourselves; capture BEAN meanwhile (E9) |
| 4 | Body ECU "security signal" (lock, head, hazard, horn, dome, tail) | theft-warning ECU → body | unknown | EU RHD cars only; LHD acceptance unknown | only if an RHD capture appears |
| 5 | Meter indicators (door, lamps, A/C lamps) | periodic status | `FE:7F`, `FE:FC` | the real sender overwrites within ~1 s | TX self-test only (T-1) |

Transmit candidates (car parked, IG on unless noted, one frame per step, DTCs read afterwards; only
after the self-test frame in [`BEAN_PROTOCOL.md`](BEAN_PROTOCOL.md) §7.2 has worked):

| # | Frame | Expected | Notes |
|---|---|---|---|
| T-1 | `FE:7F 00 10 80 00 00 00` at PRI 10 (CRC 0x68), all doors closed | meter door lamp flashes until the next periodic copy | the PRI-2 version has CRC 0xFF (ends in 5 equal bits) and is refused by the firmware |
| T-2 | `62:6E 80 80` at PRI 10 (CRC 0xB9), bus awake | key-out warning and beep | owner frame resends `00 00` within 1 s |
| T-3 | `FE:FB` button frame (§4.2) replayed once | the A/C changes as for that button | fallback study only |
| T-4 | BEAN diagnostic frames captured in E9 (Hazard on/off, Security Indic) | hazards / indicator | owner's go per test; never Door Lock with anyone inside or while driving |

T-1, T-2 and T-4 need the firmware's bench override (their DST or MES is on the deny lists below).
`FE:FB` is not on `DENY_FE_MES`: the list predates the climate capture.

### 7.1 Do-not-send list

Enforced in firmware by `DENY_DST` / `DENY_FE_MES` in `powerbox/bean_tx.py` (bench override needed).

| Traffic | Why |
|---|---|
| `B5:D9`, `C1:E2`, anything to `B5`/`C1`, any immobiliser/key exchange | encrypted; interference can block READY (B2289) |
| `FE:62` power-source state, anything to `E9` or `19` | power mode, auto-P logic |
| `FE:91` READY, `FE:40` shift, `FE:50` P-lock, `FE:24` speed, brake (`FE:29` b2 `08`) | cross-checked against hard-wired lines: B2286, B2281, B2282, B2284 |
| `FE:29` | HV warnings and buzzer; the power source ECU listens to HV data |
| `FE:C2`, `FE:AB` look-alikes | network management; false alive/dead nodes, B12xx "communication stop" |
| Power-source active-test equivalents (Key Lock, indicators) | power source ECU |
| IDs from other models never seen on this car (`62:D2/FA/D4/DF/A4`, `40:DA`, `D1:E0`, `D2:15`) | receivers on the NHW20 unknown |
| Any periodic or high-rate transmit (the "repeat at 10 Hz so it sticks" approach) | fights the real sender, loads the bus |
| `FE:E1` | not a command (above); sending it is a bench-override step only |

No SRS ECU is on BEAN. Protocol-level TX rules (stuck dominant, never ACK, wake drain, rate limit):
[`BEAN_PROTOCOL.md`](BEAN_PROTOCOL.md) §5.

---

## 8. Key authentication exchange (do not touch)

`B5:D9` (3 bytes) and `C1:E2` (3 bytes) run every 0.5 s (request → response 12–14 ms) inside the
driver-door key window (`FE:D8` b1). Values change per window and are not published. This is the
RM 05-1919 encrypted code exchange between the transponder key ECU and the smart ECU (P). The
published recordings in `docs/data/bean/` have every frame to `B5`/`C1` removed.

---

## 9. Open gaps and owner experiments

Ranked by what they settle. By owner priority **E5 (cruise control) comes first** (§1). Log with
`bean_mark` at every step, 5 s gaps, note the clock times.

| # | Experiment | Settles |
|---|---|---|
| E1 | DONE 2026-11-16 (§4.5). **IG-ON without READY.** Power without brake (ACC), wait 10 s; press again (IG, not READY), wait 20 s; brake + press (READY), wait 10 s; power off | `FE:83` `20` = ACC on this car; the READY bit among `FE:29` b2 `80`, `98:27` b0 `01`, `FE:91`, `98:B2`, `FE:AB[52]` b2 `80`; `FE:D8` b0; `FE:50` `04` |
| E2 | **Hard climate keys, parked READY.** Front defrost on/off, rear defogger on/off, two steering-pad temperature/mode steps | `FE:FC` b0 `10`/`08`, `52:ED` b1 `20`, a possible mirror-heater bit, `52:6D` b1 flags |
| E3 | **Cabin thermometer during a cold start**, parked READY after a night soak, a reference thermometer, one reading per minute for 10 min | `52:6D` b0 offset only (scale P, 2026-11-16); whether `98:EC` b4 is cabin-related |
| E4 | **Trip meter.** Note Trip A/B and average consumption at start and end of a drive; reset Trip A with IG on | which trip `52:76` is; whether `52:A2` resets with it |
| E5 | **Cruise control** — PARTLY DONE 2026-11-16 (§1: main switch, engaged, set speed, `21D3` layout). Left: single taps 3 s apart and timed holds with `21D3` polled at 5–10 Hz; brake cancel; 0x5C8 b0 bit 7; stalk wiring | tap/hold step size, 0x5C8 b0 bit 7 |
| E6 | **GPS speed at steady 50 and 80 km/h** | `FE:24` = displayed (over-reading) speed |
| E7 | **Flash, rear fog**, 10 s gaps (high beam seen 2026-11-16: `FE:7F` b0 `08`) | flash = the same bit?; whether rear fog has any bit |
| E8 | `98:CC`, `40:59`, `52:CA`, `52:D0`, `FE:DB`: no cheap experiment. A K-line data list of the A/C amp (room, evaporator, inverter temperatures) next to a capture | `98:CC`, `98:EC` b4 |
| E9 | **K-line tester on DLC3** (needs K-line hardware): body active tests Hazard on/off, Security Indic, Trunk/Bdor Open, Door Lock (all doors closed); A/C Defogger Rly-R, Blower | BEAN diagnostic frames gateway → `40`/`98`, diagnosis-mode entry, body node address |
| E10 | **Key reminder:** power OFF/ACC, key in the slot, driver door open | other meter-buzzer requests besides `62:6E` |
| E11 | **Shift through B** (READY, brake held): P → R → N → D → B → P | `FE:40` / CAN 0x120 B value |
| E12 | **Remote vs smart per door:** handle touch at each front door, back-door opener, mechanical key | `FE:B6` b0 `40`/`80` door groups; whether anything directed to `40` appears (expected none) |
| E13 | **Long locked idle**, 30 min, key away from the car | confirms no periodic wake (11.3 min so far) |

Other open items:

* Climate remote (`cyberpunk_computer/comm/avc/`): no AVC command was sent for "temperature 18.0" and
  "AUTO off", and "restore" set 20.0 °C instead of the previous 18.0 °C. Until fixed, `FE:FB` bits for
  temperature down and AUTO off cannot be observed through the remote.
* Constant IG frames (`52:99`, `52:CA`, `52:D0`, `62:21`, `98:F8`, `FE:D4`, `98:B2`): warning, option
  or configuration fields that stayed off; decodable only by provoking the condition.
* `FE:53`/`FE:54` bit fields (differ from the JP car in b2/b3 of `FE:53` and b0/b2 of `FE:54`:
  destination/LHD/options?).
* B5 ↔ C1 role assignment; node `19` identity.
* Does the gateway copy any BEAN ID to CAN besides §5, in other states (e.g. during cruise)?
* Powerbox fw 2.4.1 receiver overflow (`ovf`/`lost`) loses 7–9 % of frames on a drive (2026-11-16
  data crunch: 1 s IDs arrive at 0.91–0.98 /s); the BEAN capture file of that evening is lossier still
  (frames batched with decoded vehicle actions were skipped, fixed in commit 79c4bb8).
* Gateway passive CAN stream latency: in the trip log, passive CAN frames reach the phone 0–5 s after
  the solicited replies (§1, timing caveat). CAN-vs-BEAN timing from trip logs needs this correction.
* Recording time base: in the drive capture (fw 2.4.0) the firmware `t_ms` runs 236.4 s ahead of
  `ticks_ms` (a jump at the bus wake-up, tick overflow, `powerbox/README.md` *Tick arithmetic*) and
  drifts while the bus is busy, so the age correction in `frame_from_wire()` is skipped and
  `BeanClock` times frames (§10). Delivery latency on that drive, BEAN receive time vs CAN: FE:24
  speed 0.6 s behind 0x0B4, FE:26 rpm 0.3 s behind OBD rpm (firmware 200 ms batching + queue).
  fw 2.4.1 puts `t_ms` on the `ticks_ms` base (not yet confirmed on the car).

---

## 10. Recordings (`docs/data/bean/`)

Format: NDJSON, `bean-capture/1` (header line, then `frame`, `stats` and `mark` records; written by the
backend's BEAN capture, see [`BEAN_PROTOCOL.md`](BEAN_PROTOCOL.md) §7.3). Frames to `B5`/`C1` are
removed. All frames have valid CRCs. Times are CET.

| File | When | Frames / IDs | State | Content |
|---|---|---|---|---|
| `2026-11-13_remote_test.ndjson.gz` | 15:24:33–15:24:59 | 350 / 14 | car off | key-fob unlock, then lock |
| `2026-11-13_session30_doors_locks.ndjson.gz` | 16:29:49–16:37:06 (capture started 16:18:29; bus silent before) | 4,372 / 14 | car off | handle-touch unlock/lock, interior switch, every door, back door (timeline below) |
| `2026-11-13_drive_ig_on.ndjson.gz` | 21:47:47–22:07:26 | 59,444 / 43 | IG-ON, READY, drive | power-on, scripted AVC climate run (21 marks, 21:58:00–21:59:25), lamps, wipers, seat belt, reversing, key-out warning, power-off, lock on leaving |
| `2026-11-16_cruise_drive.ndjson.gz` | 21:14:24–21:28:48 | 40,990 / 42 | IG-ON, READY, drive | cruise control session (§1), 16 `mark` records with the owner's timeline (main, SET, holds, cancels, refused SET); rebuilt from the phone trip log (powerbox device 203 batches), not from the BEAN capture file, which lost frames (79c4bb8); 254 CRC/length-bad frames and 14 frames to `B5`/`C1` removed |

session30 timeline (from the bus, owner's action list):

| Wake | Awake | What happens |
|---|---|---|
| 16:29:49.6, 16:30:04.5 | 4.6 s | nothing (locked) |
| 16:30:26.2 | 13.3 s | handle-touch unlock (`E1 10`, `B6 01 → C1`) |
| 16:30:45.9 | 4.6 s | nothing |
| 16:31:00.9 | 8.5 s | handle-touch lock (`E1 20`) |
| 16:31:20.8 | 85.0 s | unlock; driver door 31:21.5–31:26.4 and 32:01.5–32:11.3; key window to 32:41.2 |
| 16:32:51.5 | 4.6 s | interior-switch lock (no `E1`) |
| 16:33:00.1 | 36.5 s | driver door 33:00.2–33:01.7 (`B6` b0 `80` alone); interior-switch unlock 33:30.4 |
| 16:34:00.9 / 16:34:10.9 | 4.6 s each | passenger door open / closed (bus slept in between) |
| 16:35:27.1 | 39.5 s | driver door 35:27.3–35:31.0; rear/back door 35:40.2–35:52.0; back door + luggage lamp from 36:01.0 |
| 16:36:16.1 / 16:36:31.1 / 16:36:41.3 | 4.6 s each | back door closed / a rear door open / closed |
| 16:36:56.5 | 9.5 s | handle-touch lock (`E1 20`, `B6 C0 → 40 → 00`) |

Drive capture notes:
* The owner's clock agrees with the bus within 1–2 s (bus later): headlamp steps 22:00:11.9, :21.2,
  :31.0, :41.1; door 22:01:30.9; power off 22:06:40.8. The climate marks are on the phone clock and
  match the AVC `110→1C6 E0 84` frames; that run overlaps the wiper test.
* Events: power on 21:48:04; READY candidate 21:48:07.2; defrost 21:48:55–21:54:39 (hard key);
  steering-pad 20.0 → 18.0 °C 21:55:03; wipers 21:58:00–21:59:53; fog 22:00:41–22:01:01; seat belt
  22:01:10/22:02:15; key-out warning 22:01:34.5–22:02:01.8; P → D 22:02:24; reversing 22:05–22:06;
  P 22:06:04; power off 22:06:41; lamps off 22:07:00.8; smart lock 22:07:18 (doors stayed unlocked
  during the drive).
* Timing: each frame `ts` is the phone's receive time of its batch (median 0.25 s late, up to
  0.8 s). For correlation, re-time frames as `t_ms/1000 + a + b·ts`, with `a`, `b` from a linear fit
  to the lower envelope of `ts − t_ms/1000` (60 s windows). Check: `FE:29` brake bit against CAN
  0x030 gives +2 ms median after re-timing (+0.27 s on raw `ts`).
* The phone's trip log of the same drive (CAN, OBD replies, AVC-LAN, powerbox) was used for the CAN
  correlations; it is not in the repository.

JP reference dumps (bean_tools, NHW20 Japanese market, 8,627 frames) are in
`tests/fixtures/bean_nhw20_frames.txt.gz`; differences from this car are in
[`BEAN_PROTOCOL.md`](BEAN_PROTOCOL.md) §4.2.

---

## 11. Reproduce

```
cd tools/bean_analysis
python3 bean_stats.py    ../../docs/data/bean/*.ndjson.gz          # per-message / per-byte statistics
python3 bean_timeline.py ../../docs/data/bean/2026-11-13_session30_doors_locks.ndjson.gz --skip-ab
python3 bean_sleep.py    ../../docs/data/bean/*.ndjson.gz ../../tests/fixtures/bean_nhw20_frames.txt.gz
python3 bean_correlate.py ../../docs/data/bean/2026-11-13_session30_doors_locks.ndjson.gz --builtin session30
python3 bean_correlate.py CAPTURE --marks marks.csv --toggles     # marks: "mm:ss[-mm:ss],label"
python3 bean_compare.py  ../../docs/data/bean/*.ndjson.gz --fixture ../../tests/fixtures/bean_nhw20_frames.txt.gz
```

`beanlib.py` holds the shared loaders; frame time comes from the firmware `t_ms` plus one offset.

---

## Sources

* Toyota Prius repair manual RM1076E (2004, EU), section 05: A/C 05-1019/1020/1037/1044/1054; meter
  05-1678…1680, 05-1695; power door lock 05-1787; power window 05-1722…1733; smart entry
  05-1863/1883/1889/1919; key reminder 05-2029/2030; push-button start 05-2141; theft deterrent
  05-2202/2203/2214. https://toyotamanuals.gitlab.io/PZ471-Z01S0-CA/htmlweb/rm/rm1076e/m_05_NNNN.pdf
* RM1075U (2004 NHW20, US): smart entry "Function of Main Components"; body ECU terminals (RDA B7-23).
* Toyota NCF256E body electrical (BE-26 dedicated lock line, BE-34, BE-68 DRL from READY over BEAN,
  BE-84/153 A/C path, BE-125, BE-151 mirror heaters):
  https://toyotamanuals.gitlab.io/PZ471-Z01S0-CA/htmlweb/ncf/ncf256e/m_be_NNNN.pdf
* SAE 970297 and SAE 1999-01-1278 (diagnostic messages only in diagnosis mode; gateway conversion).
* haageduard/bean_tools (JP NHW20 dumps; `data/prius.framedb` transmit list with no documented effect):
  https://github.com/haageduard/bean_tools
* KostyaSha/bean-cracker, `dumps/prius/20/eu/ig_on_from_off.txt` (533-frame EU NHW20 IG-ON dump; same
  `FE:54` as this car; no commands): https://github.com/KostyaSha/bean-cracker
* specadmin/toyota-mpx `MPX-codes.md` (RX300 decodes, door frames are status reports):
  https://github.com/specadmin/toyota-mpx
* fiztech-code/BeanMPX, daniel-dmitrzak/BeanMPX_IS300, threepotMR2/BeanBangerMR2: meter frames
  transmitted with effect on other models (`62:2C`, `62:D4`, …); those IDs are not used by the NHW20
  meter.
* drive2.ru/b/494654889247376137: `25 FE 7F 01 00 00` lit the clearance indicator on another Toyota.

No published BEAN frame has been shown to make an NHW20 ECU actuate anything.
