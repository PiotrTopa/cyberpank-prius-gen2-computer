# Prius NHW20 CAN: broadcast IDs and diagnostic PIDs

CAN 500 kbit/s on the DLC3 (pins 6/14). Decoders: `cyberpunk_computer/comm/can_decoder.py`
(broadcast) and `cyberpunk_computer/comm/solicited_can.py` (PIDs). Diagnostic services, ISO-TP and
safety rules: [`TOYOTA_DIAGNOSTICS.md`](TOYOTA_DIAGNOSTICS.md). BEAN messages the gateway copies to
CAN 0x5xx: [`BEAN_SIGNALS.md`](BEAN_SIGNALS.md) §5.

## 1. Sources and conventions

* Base list: a third-party compilation for a 2009 Gen 2 Prius (work of Attila Vass, usbseawolf2000
  and others; EAA-PHEV wiki "Prius PHEV TechInfo" for the battery ECU), hardware an Arduino CAN shield
  on the OBD port. Its decodes are **third-party (G)** unless a row says otherwise.
* **Car** in the confidence column = checked on this car (correlation in a recording, or a value the
  owner read); the evidence is named.
* Byte letters: A = data byte 0, B = byte 1, …; for solicited replies A is the first byte after the
  mode/PID echo. `{C:4}` = byte C bit 4; `{D:0-2}` = bits 0–2.
* Period = the list's approximate update interval in ms.

## 2. Broadcast IDs

| ID | Period | Content | Decode | Confidence |
|---|---|---|---|---|
| 0x022 | 13 | lateral / longitudinal acceleration | (256A + B) − 0x200 and (256C + D) − 0x200, uncalibrated | G |
| 0x023 | 13 | longitudinal acceleration | (256A + B) − 0x200, uncalibrated | G |
| 0x025 | 13 | steering angle | 12-bit signed (low nibble of A, then B) × 1.5°, positive = left; E high nibble = a fraction (list: × 0.1°, unused) | Car (2026-11-13 drive): scale from the range (±650–660° at the stops); correlation 0.95 with the rear wheel-speed yaw, same sign as (right − left). Absolute sign = Toyota convention (G). Decoded in `can_decoder.py` (`decode_steering_deg`, shared by `dynamics.steering_angle` and the position feeder) |
| 0x030 | 6 | brake pedal | E, 0–127; may read 0x7F at start-up with the brake held | Car: E > 8 edges match BEAN `FE:29` brake bit (74/79 within 1 s) |
| 0x038 | 8 | engine status | B > 0 seen only with the ICE turning (not an rpm), but B = 0 for 32 % of the time the ICE turns at low load (e.g. 1300 rpm warm-up): NOT an engine-running flag | Car (2026-11-13 drive against OBD 010C) |
| 0x039 | 8 | ICE coolant | A °C, no offset; 254/255 = sensor not ready | Car: within 1 °C of BEAN `FE:2C` (r = 0.995) |
| 0x03B | 8 | HV battery current, voltage | current = low 12 bits of A,B signed × 0.1 A (> 0 discharge); voltage = 256C + D V | Car: voltage is 16-bit (byte D alone wraps at 256 V; the pack reaches ~260 V under hard regen) |
| 0x0B1 / 0x0B3 | 13 | front / rear wheels (right A,B; left C,D) | (256A + B), (256C + D) × 0.01 km/h; E,F not decoded | Car (2026-11-13 drive): speed, not pulses: wheel mean = 0.978 (rear) / 0.981 (front) × 0x0B4; front and rear (right − left) correlate 0.97. Right/left labels G (consistent with 0x025's sign) |
| 0x0B4 | 14 | vehicle speed | (256F + G) × 0.01 km/h | Car: true speed; the meter's trip distance agrees with it; the speedometer (`FE:24`) reads 12 % higher |
| 0x120 | 17 | gear | byte F low nibble: 0 P, 1 R, 2 N, 3 D, 4 B (observed F = 0x20–0x23) | Car: P/R/N/D on 20 shifts, BEAN `FE:40` follows 45–110 ms later; B not observed. (The list's `{D:0-1}` does not match this car) |
| 0x244 | 25 | accelerator pedal | G, 0–0xC8 | G |
| 0x3B6 | — | energy-monitor flow | bytes F, G bitmask | G (unvalidated; 0x528 carries the energy arrows, [`BEAN_SIGNALS.md`](BEAN_SIGNALS.md) §5) |
| 0x3C8 | 70 | ICE speed target | 256C + D rpm | G |
| 0x3CA | 108 | vehicle speed | C km/h | G |
| 0x3CB | 108 | battery ECU | A discharge limit A; B charge limit A (nominal ~100 / 105 A below 51 °C); SOC = (256C + D)/2 %; E, F battery temperatures, signed °C | Car: SOC tracks the MFD SOC bars (`FE:29`); temperatures correlate with `98:EC` b4 |
| 0x3CD | 108 | battery fault code, voltage | AB fault code; voltage field unresolved | G |
| 0x4CE | — | battery-compartment intake air temperature (not outside air) | — | P (`can_decoder.py` comment) |
| 0x520 | 2231 | ECM fuel quantity per injection (gateway CAN → BEAN `62:20`); measured every 0.92 s, only while the ICE runs | header + 16-bit word 256B + C; fuel flow L/h = K × word × rpm, K = 2.69 × 10⁻⁶ (see below) | Car (BEAN copy; flow fitted to the meter) |
| 0x526 | 530 | engine rpm (gateway CAN → BEAN `FE:26`) | header + word / 5.12 | Car (the list's "ignition timing B,C" is this rpm word) |
| 0x528 | 522 | energy-monitor arrows | `25 xx yy 00`, see [`BEAN_SIGNALS.md`](BEAN_SIGNALS.md) §5 | P |
| 0x529 | 1000, immediate on event | HV status to the meter (gateway CAN → BEAN `FE:29`) | see below | Car for `{D:3}`, `{D:0-2}` |
| 0x52C | 1050 | coolant (→ BEAN `FE:2C`) | B / 2 °C | Car |
| 0x57F | 1050 | lamps (BEAN `FE:7F` → CAN) | `{B:3-5}`: 0x00 off, 0x10 parking, 0x30 low beam, 0x38 high beam; `{D:7}` = any door open | Car (parking, low beam, door) |
| 0x5A4 | 3400 | fuel level (BEAN `52:A4` → CAN) | B; full tank read 0x2C (44), assumed litres | G (scale) |
| 0x5B6 | 1050 | doors (BEAN `FE:B6` → CAN) | `{C:7}` driver door, `{C:6}` front passenger door, `{C:2}` rear or back door | Car (the list's "0x40 = other doors" is the passenger door only) |
| 0x5C8 | 1050 | cruise control (gateway CAN → BEAN `62:C8`) | list: `{C:4}` cruise on and active | ?, see §8 and [`BEAN_SIGNALS.md`](BEAN_SIGNALS.md) §1 |
| 0x5CC | 272 | BEAN `98:CC` copy (not a temperature) | header `24` + 10-bit value | Car |

**Fuel flow (0x520).** The word is the fuel quantity per injection, not a flow; the flow scales with
engine speed:

    flow [L/h] = K × (256B + C) × rpm,   K = 2.69 × 10⁻⁶

K makes the integrated flow equal the meter's own trip computer (BEAN `52:90` × true speed and
`52:A2` × `52:76`) on the 2026-11-13 drive; fitted on the instantaneous `52:90` it is 2.82 × 10⁻⁶
(±5 %). rpm = BEAN `FE:26` or OBD 010C. 0x520 is sent only while the ICE runs (no frame with rpm < 400
on that drive): its presence is an engine-running indicator, and the last word must not be held
after the frames stop. A flow of 0.008 L/h per count without rpm reads 2.1× the meter per sample and
+64 % on a trip. Code: `cyberpunk_computer/state/fuel_model.py`; engine-running gate (rpm > 400 →
BEAN `98:27` → `62:20`/0x520 presence → AVC) in `cyberpunk_computer/comm/engine_gate.py`.

0x529 bits (list; byte A bit 7 = event flag, an event causes an immediate frame):

| Bits | Meaning |
|---|---|
| `{B:2,4,6}`, `{C:1}` | general problem with red triangle, 1 beep |
| `{B:5}`, `{C:0}` | general problem, 1 beep |
| `{B:3}` | not in Park and driver door open |
| `{D:0-2}` | MFD SOC bars (Car: 3–5 seen, hysteresis against SOC) |
| `{D:3}` | brake pedal depressed (Car) |
| `{D:7}` | READY / HV active (Car, from BEAN `FE:29` b2 `80`) |
| `{E:6}` / `{E:7}` | EV mode active / EV mode cancelled (1 beep) |
| `{F:4}` | P-lock problem ("problem with the transmission 'P' lock mechanism") |
| `{F:5-7}` | EV mode denied ("Cannot change to EV mode now") |

## 3. Broadcast IDs without a decode, and gateway copies

All gateway copies 0x5xx have byte 0 = header and bytes 1… = the BEAN data
([`BEAN_SIGNALS.md`](BEAN_SIGNALS.md) §5): 0x520/521/526/527/529/52C/540/591/5B2/5C8/5CC/5D4/5EC/5F8
(CAN → BEAN) and 0x53F/553/554/56D/57F/5A4/5B6/5ED (BEAN → CAN).

No decode (period in ms): 0x020 (12), 0x03A (8; NOT a yaw rate in the `can_decoder.py` reading: correlation 0.01 with the wheel-speed yaw on the 2026-11-13 drive, byte A only 0/1), 0x03E (8), 0x230 (28),
0x262 (21), 0x348 (44), 0x3C9 (106), 0x3CF (139), 0x423 (1075), 0x484 (1120), 0x4C1 (971), 0x4C3
(1042), 0x4C6 (1018), 0x4C7 (1028), 0x4C8 (1058), 0x4D0 (1118), 0x4D1 (1135), 0x602 (73009).

## 4. Diagnostic addresses

| Request ID | ECU |
|---|---|
| 0x727 | transmission |
| 0x750 | main body (sub-address byte, body ECU 0x40 on 2010+ cars; reported not answering on 2005–2008 NHW20, whose body ECUs are on K-line) |
| 0x780 | airbag |
| 0x781 | pre-crash |
| 0x790 | distance |
| 0x791 | pre-crash 2 |
| 0x7A1 | steering assist |
| 0x7A2 | park assist |
| 0x7B0 | ABS / brake |
| 0x7C0 | instrument |
| 0x7C4 | air conditioner |
| 0x7D0 | navigation |
| 0x7E0 | engine (reply 0x7E8) |
| 0x7E2 | hybrid system (reply 0x7EA) |
| 0x7E3 | HV battery (reply 0x7EB) |

Which of these answer on this car: [`TOYOTA_DIAGNOSTICS.md`](TOYOTA_DIAGNOSTICS.md).

## 5. Solicited PIDs, engine ECU 0x7E0

| PID | Content | Formula | Range / unit | Notes |
|---|---|---|---|---|
| 0100 | supported 0101–0120 | bitmap | | 01, 03, 04, 05, 06, 07, 0C, 0D, 0E, 0F, 10, 11, 13, 15, 1C, 1F, 20 |
| 0101 | MIL status / DTC count / engine type | `{A:7}` MIL, `{A:0-6}` count, `{B:3}` compression ignition | | |
| 0104 | calculated engine load | A (as listed and decoded; J1979 scales 100A/255) | % | Car: correlates with `62:20` (r = 0.84) |
| 0105 | ICE coolant | A − 40 | °C | same as 0x039 |
| 0106 / 0107 | short / long-term fuel trim | 0.7812 × (A − 128) | % | |
| 010C | ICE rpm | (256A + B) / 4 | rpm | Car: BEAN `FE:26` r = 0.991 |
| 010D | vehicle speed | A | km/h | |
| 010E | timing advance | A/2 − 64 | ° | |
| 010F | intake air temperature | A − 40 | °C | |
| 0110 | MAF | (256A + B) / 100 | g/s | |
| 0111 | throttle position | A × 100 / 255 | % | |
| 0120 | supported 0121–0140 | bitmap | | 21, 24, 30, 31, 33, 3C, 3E, 40 |
| 0124 | lambda / O2 sensor | 0.0000305 × (256A + B); 0.000122 × (256C + D) V | | |
| 0131 | distance since DTCs cleared | 256A + B | km | |
| 0133 | barometric pressure | A | kPa | |
| 0140 | supported 0141–0160 | bitmap | | 42, 43, 44, 45, 46, 47, 4C, 4D, 4E |
| 0142 | ECU (12 V) supply | (256A + B) / 1000 | V | |
| 0146 | ambient air temperature | A − 40 | °C | Car: 4–5 °C against BEAN `FE:CD` 4 °C |
| 21F3 | injector time | 0.128 × C | ms | |

## 6. Solicited PIDs, hybrid ECU 0x7E2

21C3 (multi-frame, about 30 bytes):

| Bytes | Content | Formula | Range / unit |
|---|---|---|---|
| A,B | MG2 speed | 256A + B − 16383 | rpm |
| C,D | MG2 torque | (256C + D)/8 − 500 | Nm |
| E | regenerative brake torque, actual (= MG2 torque while regen braking, else 0) | 4E | Nm |
| F | regenerative brake torque, request | 4F | Nm |
| G,H | MG1 speed | 256G + H − 16383 | rpm |
| I,J | MG1 torque | (256I + J)/8 − 500 | Nm |
| K,L | ICE power request | (256K + L)/100 | kW |
| M,N | ICE speed target | 256M + N | rpm |
| O,P | ICE speed actual | 256O + P | rpm |
| R | master cylinder torque (regen + friction) | 4R − 512 | Nm |
| S | state of charge | 100S/255 (Vass: 0.5S) | % |
| T | WOUT battery → converter | 320T | W |
| U | WIN battery → converter | U − 40800 | (as listed) |
| V | discharge request to adjust SOC | V − 20480 | W |
| X | drive condition ID | X | 0–6 |
| Y / Z | MG1 / MG2 inverter temperature | − 40 | °C |
| AA / AB | motor temperature No2 (MG2?) / No1 (MG1?) | − 40 | °C |
| AC | HV battery voltage (sum of cells) | 2AC | V |
| AE | HV battery current | 2AE − 256 | A |
| AF–AJ | shift sensors main / sub / select main / select sub / position | 0.019608 × x | V |

21C4:

| Bytes | Content | Formula / bit |
|---|---|---|
| A | driving pattern 1/2/3, loading condition | `{A:0}`, `{A:1}`, `{A:2}`, `{A:7}` |
| B | requests: engine warm-up, A/C, engine-stop inhibit, HVAC OBD, battery charging, idling, engine stop; check mode | `{B:0}` … `{B:7}` in that order |
| C | accelerator pedal angle | 100C/255 % |
| D | VL, voltage before boost | 2D V |
| E | VH, voltage after boost | 2E V |
| F | converter temperature | F − 40 °C |
| G | crank position | 0.706G ° |
| H | system main relays 1/2/3 | `{H:0}`, `{H:1}`, `{H:2}` |
| I | converter carrier frequency, smart key, A/C gate, converter gate, MG2 gate, MG1 gate | `{I:0}`, `{I:2}`, `{I:4}`, `{I:5}`, `{I:6}`, `{I:7}` |
| J / K | MG2 / MG1 torque execute value | 4x − 512 Nm |
| L | short-circuit wave highest value | 0.019608L V |
| O | boost (raising pressure) ratio | 100O/255 % |
| P | A/C consumption power | 0.019608P kW |

21D3 (cruise control; never checked against an engaged cruise on this car):

| Byte | Content |
|---|---|
| A | cruise memory vehicle speed, km/h (0–150) |
| B | **cruise set/resume speed, km/h**; 0 below about 40 km/h |
| C | `{C:0}` main switch, `{C:2}` main switch ready, `{C:5}` indicator, `{C:6}` cruise control, `{C:7}` shift D |
| D | `{D:0}` stop light switch 1 (sub CPU), `{D:1}` switch 2 (sub), `{D:2}` switch 1 (main), `{D:3}` RES/ACC, `{D:4}` SET/COAST, `{D:5}` CANCEL, `{D:6}` cruise active (set; reported to drop to 0 for < 1 s at times), `{D:7}` D selected |

## 7. Solicited PIDs, HV battery ECU 0x7E3

| PID | Bytes | Content | Formula |
|---|---|---|---|
| 21CE | A | SOC | 0.5A % |
| 21CE | B,C | battery current | (256B + C)/100 − 327.68 A |
| 21CE | D…AE | block 01…14 voltages, two bytes each | (256x + y)/100 − 327.68 V |
| 21CF | A,B | battery air intake temperature | (256A + B)/100 − 327.68 °C |
| 21CF | C | VMF fan motor voltage | 0.2C − 25.6 V |
| 21CF | D | auxiliary battery voltage | 0.2D − 25.6 V |
| 21CF | E / F | charge / discharge limit | x − 64 kW |
| 21CF | G | delta SOC | 0.01G % |
| 21CF | I | battery fan speed | 0–6 |
| 21CF | K,L / M,N / O,P | battery temperatures 1 / 2 / 3 | (256x + y)/100 − 327.68 °C |
| 21D0 | A | block count | 0–14 |
| 21D0 | B,C / D,E / F,G / H,I | accumulated time of battery low / DC inhibit / battery too high / hot temperature | 256x + y s |
| 21D0 | J,K / L | lowest block voltage / its block number | 2.56J + 0.01K − 327.68 V |
| 21D0 | M,N / O | highest block voltage / its block number | 2.56M + 0.01N − 327.68 V |
| 21D0 | P…AC | internal resistance R01…R14 | 0.001x Ω |

Battery power = current × sum of block voltages.

## 8. Open gaps

* **0x5C8 / 21D3 (cruise):** the list reads 0x5C8 `{C:4}` as "cruise active"; a comment in
  `can_decoder.py` reads the same bit as a cancel event and calls the message sparse (about 18 frames
  in 37 min), while the list gives a 1050 ms period. Neither has been checked with cruise engaged.
  Resolves with experiment E5 in [`BEAN_SIGNALS.md`](BEAN_SIGNALS.md) §1.
* **0x5xx cadence:** the list gives about 1 s for most gateway copies; own CAN logs contain far fewer
  frames (e.g. 58 × 0x5C8 in 53 min). Whether the gateway sends them on change only or the logs drop
  them: compare a CAN log with a simultaneous BEAN capture (BEAN side is 1 s).
* **0x025 / 0x0B1 / 0x0B3 absolute sign:** steering and wheel left/right agree with each other on the
  2026-11-13 drive, but both rest on third-party labels. Indirect support: tight turns (|curvature| >
  0.08 /m at 2–8 m/s) come out right-handed 131 times against 87 left (right turns are the tighter ones
  in right-hand traffic). Resolves with one deliberate left turn logged, or a GNSS course.
* **0x0B1/0x0B3 scale:** the wheels read 2.2 % below 0x0B4 (tyre size / ECU scaling); not corrected.
* **0x025 fraction nibble** (E high nibble): not decoded; the angle has 1.5° resolution.
* **0x348:** decoded as a pack temperature (byte C − 40) in `can_decoder.py`, while a comment there
  reports byte C × 25 tracking ICE rpm (r = 0.986). Resolves with a log covering ICE start/stop at a
  steady pack temperature.
* **0x120 B position** and **0x3B6 bits**: not observed / unvalidated.
* **0x3CA, 0x3C8, 0x244, 0x022/0x023 scalings**: third-party, unchecked. No yaw-rate sensor frame is
  decoded (0x03A is not one in the current reading).
* **Fuel figures:**
  * Absolute scale unverified: K is fitted to the combination meter, and the meter reads 14 % above
    the fuel derived from MAF and lambda (0.55 L against 0.48 L on the 2026-11-13 drive). Resolves
    with a brim-to-brim petrol fill over ≥ 100 km on petrol only.
  * The fit rests on one drive of 2.4 km / 18 min, mostly near 1300 rpm: K at high rpm and highway
    load is not verified.
  * LPG: no input reports which fuel the engine burns or the LPG tank level, so every litre figure is
    petrol-equivalent and no LPG volume factor is applied (`LPG_VOLUME_FACTOR` = None in
    `state/fuel_model.py`). Detection needs an input from the LPG installation (changeover-switch /
    fuel-select LED line or LPG solenoid-valve supply on a powerbox input, or the LPG ECU's
    diagnostic link) and a level sender reading.
