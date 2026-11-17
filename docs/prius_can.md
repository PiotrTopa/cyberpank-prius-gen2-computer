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
* **Checksum (Car, 2026-11-17):** the last byte of every powertrain broadcast ID from 0x022 to 0x3CF
  (0x022, 023, 025, 030, 038, 039, 03A, 03B, 03E, 0B1, 0B3, 0B4, 120, 230, 244, 262, 348, 3C8, 3C9,
  3CA, 3CB, 3CD, 3CF) is the Toyota checksum `(id >> 8) + (id & 0xFF) + length + sum(data bytes
  before it)` mod 256, in 100 % of the frames of the 2026-11-17 drive. It carries no signal; a bit of
  it that "follows" a signal only follows the checksum. The 0x4xx/0x5xx/0x6xx frames have none.

## 2. Broadcast IDs

| ID | Period | Content | Decode | Confidence |
|---|---|---|---|---|
| 0x022 | 13 | **yaw rate**, two redundant channels (not an acceleration) | (256A + B) − 0x200 and (256C + D) − 0x200; ≈ 0.0044 rad/s (≈ 0.25 °/s) per count, positive = turning left; standstill offset −2 (AB) and −7 (CD) counts | Car (2026-11-17, 29 min at full rate): the ratio to the rear wheel-speed yaw stays 0.0042–0.0046 rad/s per count from 15 to 80 km/h (r 0.94–0.999), while its ratio to lateral acceleration grows 3× with speed. Scale and sign P (rest on the wheel-speed yaw with a 1.48 m track and the 0x0B3 left/right labels) |
| 0x023 | 13 | **acceleration sensor, two channels at ±45°** | P = (256A + B) − 0x200, Q = (256C + D) − 0x200; longitudinal ≈ 0.020 × (P − Q) m/s², lateral ≈ 0.025 × (P + Q) m/s² (lateral offset about −0.35 m/s²) | P (2026-11-17, > 15 km/h: r 0.93 against d(0x0B4)/dt and 0.95 against yaw × speed; a single channel gives only 0.72; the sensor also sees road grade) |
| 0x025 | 13 | steering angle | 12-bit signed (low nibble of A, then B) × 1.5°, positive = left; E high nibble = a fraction (list: × 0.1°, unused) | Car (2026-11-13 drive): scale from the range (±650–660° at the stops); correlation 0.95 with the rear wheel-speed yaw, same sign as (right − left). Absolute sign = Toyota convention (G). Decoded in `can_decoder.py` (`decode_steering_deg`, shared by `dynamics.steering_angle` and the position feeder) |
| 0x030 | 6 | brake pedal | E, 0–127; may read 0x7F at start-up with the brake held. B (signed) moves with regenerative braking (r −0.70 against 21C3 regen torque, G); A bit 0, B bits 6–7 and F bit 6 switch together with the brake (46–52 edges, 76 % at a `FE:29` brake edge, ?) | Car: E > 8 edges match BEAN `FE:29` brake bit (74/79 within 1 s) |
| 0x038 | 8 | engine status | B > 0 seen only with the ICE turning (not an rpm), but B = 0 for 32 % of the time the ICE turns at low load (e.g. 1300 rpm warm-up): NOT an engine-running flag. B ≈ ICE power request, ≈ 0.23 kW per count (r 0.92 against 21C3 K,L, P). D signed ≈ drive torque / acceleration demand (r 0.78 against 21C3 MG2 torque, 0.80 against acceleration, G) | Car (2026-11-13 drive against OBD 010C; 2026-11-17 for B, D) |
| 0x039 | 8 | ICE coolant | A °C, no offset; 254/255 = sensor not ready | Car: within 1 °C of BEAN `FE:2C` (r = 0.995) |
| 0x03B | 8 | HV battery current, voltage | current = low 12 bits of A,B signed × 0.1 A (> 0 discharge); voltage = 256C + D V | Car: voltage is 16-bit (byte D alone wraps at 256 V; the pack reaches ~260 V under hard regen) |
| 0x0B1 / 0x0B3 | 13 | front / rear wheels (right A,B; left C,D) | (256A + B), (256C + D) × 0.01 km/h; E,F not decoded | Car (2026-11-13 drive): speed, not pulses: wheel mean = 0.978 (rear) / 0.981 (front) × 0x0B4; front and rear (right − left) correlate 0.97. Right/left labels G (consistent with 0x025's sign) |
| 0x0B4 | 14 | vehicle speed | (256F + G) × 0.01 km/h; E bit 0 = moving: set at 7–14 km/h, cleared at ≤ 2 km/h (22 edges, 2026-11-17, P) | Car: true speed; the meter's trip distance agrees with it; the speedometer (`FE:24`) reads 12 % higher |
| 0x120 | 17 | gear | byte F low nibble: 0 P, 1 R, 2 N, 3 D, 4 B (observed F = 0x20–0x23) | Car: P/R/N/D on 20 shifts, BEAN `FE:40` follows 45–110 ms later; B not observed. (The list's `{D:0-1}` does not match this car). **Byte E bit 7 = cruise engaged**: Car, every engage/cancel of 2026-11-16/17 (`10 → 90`); byte H is the checksum and changes with it ([`CRUISE_CONTROL.md`](CRUISE_CONTROL.md)) |
| 0x244 | 25 | accelerator pedal; **MG2 speed** | G, 0–0xC8 (pedal); **E,F = MG2 rpm, signed 16-bit (256E + F), negative in reverse** | pedal G; MG2 rpm Car (2026-11-17: slope 1.00, r 0.999 against 21C3 MG2 speed, −161 in R): a broadcast, ~30 Hz source of MG2 speed and travel direction |
| 0x3B6 | — | energy-monitor flow | bytes F, G bitmask | G (unvalidated; 0x528 carries the energy arrows, [`BEAN_SIGNALS.md`](BEAN_SIGNALS.md) §5) |
| 0x3C8 | 70 | ICE speed | C ≈ rpm / 32 (r 0.99 against BEAN `FE:26`); D ? (the list's 256C + D does not fit) | Car (2026-11-17), redundant with `FE:26` |
| 0x3CA | 108 | vehicle speed | C km/h, integer (true speed: slope 1.01, r 0.994 against 0x0B4) | Car (2026-11-16); not decoded, 0x0B4 is the speed source |
| 0x3CB | 108 | battery ECU | A discharge limit A; B charge limit A (nominal ~100 / 105 A below 51 °C); SOC = (256C + D)/2 %; E, F battery temperatures, signed °C | Car: SOC tracks the MFD SOC bars (`FE:29`); temperatures correlate with `98:EC` b4 |
| 0x3CD | 108 | battery fault code, voltage | AB fault code; 256C + D = pack voltage V (median difference against 0x03B 1 V) | P (2026-11-16); 0x03B stays the voltage source |
| 0x3CF | 139 | ECM air temperatures | B = outside air °C, C = intake air °C, no offset (= OBD 0146 / 010F, median difference 0, r 0.975 / 0.988 over 4–10 °C). Decoded as signed; 0 °C and below not yet recorded | P. Replaces the OBD 0146 and 010F polls (`can_decoder.py` `AIR_TEMPS`); a value ≤ 0 °C leaves the poll running until the sign is verified |
| 0x4CE | — | constant `0F 00 01 01 00 00 …` on every drive: an index/configuration frame like 0x4C1…0x4D1, not a temperature | — | P (2026-11-16) |
| 0x520 | 2231 | ECM fuel quantity per injection (gateway CAN → BEAN `62:20`); measured every 0.92 s, only while the ICE runs | header + 16-bit word 256B + C; fuel flow L/h = K × word × rpm, K = 2.69 × 10⁻⁶ (see below) | Car (BEAN copy; flow fitted to the meter) |
| 0x526 | 530 | engine rpm (gateway CAN → BEAN `FE:26`) | header + word / 5.12 | Car (the list's "ignition timing B,C" is this rpm word) |
| 0x528 | 522 | energy-monitor arrows | `25 xx yy 00`, see [`BEAN_SIGNALS.md`](BEAN_SIGNALS.md) §5 | P |
| 0x529 | 1000, immediate on event | HV status to the meter (gateway CAN → BEAN `FE:29`) | see below | Car for `{D:3}`, `{D:0-2}` |
| 0x52C | 1050 | coolant (→ BEAN `FE:2C`) | B / 2 °C | Car |
| 0x57F | 1050 | lamps (BEAN `FE:7F` → CAN) | `{B:3-5}`: 0x00 off, 0x10 parking, 0x30 low beam, 0x38 high beam; `{D:7}` = any door open | Car (parking, low beam, door) |
| 0x5A4 | 3400 | fuel level (BEAN `52:A4` → CAN) | B; full tank read 0x2C (44), assumed litres | G (scale) |
| 0x5B6 | 1050 | doors (BEAN `FE:B6` → CAN) | `{C:7}` driver door, `{C:6}` front passenger door, `{C:2}` rear or back door | Car (the list's "0x40 = other doors" is the passenger door only) |
| 0x5C8 | 1050 | cruise control (gateway copy of BEAN `62:C8`, `24 00 xx`) | **`{C:4}` = cruise MAIN SWITCH on** (not "active", not a cancel event); A `A4` = immediate-copy header (first frame after a change, [`BEAN_SIGNALS.md`](BEAN_SIGNALS.md) §5) | Car (2026-11-16/17 cruise drives, [`CRUISE_CONTROL.md`](CRUISE_CONTROL.md)); leads `62:C8` by 40–60 ms (2026-11-17); the "~3 s lag" of 2026-11-16 was trip-log delivery delay (§8) |
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

Partly read (2026-11-16 data crunch, P; none of them is decoded by the backend): 0x3CF A ?, D 0, E
varies (B, C in §2). 0x484 B ≈ coolant, 0.626·B − 0.6 °C (r 0.9998 against 0x039, redundant). 0x230 D
`04` = brake pressed. 0x348 B,C follow ICE speed (about 23.4 × C rpm, r 0.89), not a pack
temperature. 0x4C1…0x4D1, 0x020, 0x423 constant.

Partly read (2026-11-17 drive, 29 min; none decoded by the backend). These IDs are unknown to the
decoder and reach the trip log once a second only (§8), so fast behaviour is not visible:

* 0x230: A,B = 16-bit distance counter, wraps, 102.8 counts per metre of 0x0B4 distance over 10.8 km
  (P); D bit 2 = brake pressed (C: 85 edges, 99 % within 1.5 s of a `FE:29` brake edge); F a small
  signed value that follows turning weakly (r 0.39 against lateral acceleration, ?).
* 0x348: C ≈ rpm / 25 (r 0.99); A bit 2 and B bits 0, 5, 6 = ICE running (37 edges, 95–98 % at ICE
  start/stop) (P).
* 0x03A: E bit 4 = accelerator pressed (0x244 G > 0 in all 6,576 samples with the bit set, in 2 %
  of the 10,948 with it clear; P); E bit 0 with F = a 9-bit voltage ≈ 0x03B pack voltage − 11.5 V (r 0.996, P: probably a
  second HV voltage, e.g. the boost converter input); A,B not decoded.
* 0x484: C follows engine load (r 0.90 against OBD 0104, G).
* 0x262 B bit 1: short pulses at 5–20 km/h, 38 edges, meaning ?

No decode (period in ms): 0x020 (12), 0x03A (8; NOT a yaw rate in the `can_decoder.py` reading: correlation 0.01 with the wheel-speed yaw on the 2026-11-13 drive, byte A only 0/1), 0x03E (8), 0x230 (28),
0x262 (21), 0x348 (44), 0x3C9 (106), 0x423 (1075), 0x484 (1120), 0x4C1 (971), 0x4C3
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
| 0105 | ICE coolant | A − 40 | °C | same as 0x039; not polled |
| 0106 / 0107 | short / long-term fuel trim | 0.7812 × (A − 128) | % | |
| 010C | ICE rpm | (256A + B) / 4 | rpm | Car: BEAN `FE:26` r = 0.991 |
| 010D | vehicle speed | A | km/h | |
| 010E | timing advance | A/2 − 64 | ° | |
| 010F | intake air temperature | A − 40 | °C | = CAN 0x3CF C; polled every 30 s only while 0x3CF is stale or ≤ 0 °C |
| 0110 | MAF | (256A + B) / 100 | g/s | |
| 0111 | throttle position | A × 100 / 255 | % | |
| 0120 | supported 0121–0140 | bitmap | | 21, 24, 30, 31, 33, 3C, 3E, 40 |
| 0124 | lambda / O2 sensor | 0.0000305 × (256A + B); 0.000122 × (256C + D) V | | |
| 0131 | distance since DTCs cleared | 256A + B | km | |
| 0133 | barometric pressure | A | kPa | |
| 0140 | supported 0141–0160 | bitmap | | 42, 43, 44, 45, 46, 47, 4C, 4D, 4E |
| 0142 | ECU (12 V) supply | (256A + B) / 1000 | V | |
| 0146 | ambient air temperature | A − 40 | °C | Car: 4–5 °C against BEAN `FE:CD` 4 °C; = CAN 0x3CF B; polled every 60 s only while 0x3CF is stale or ≤ 0 °C |
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

21D3 (cruise control), reply `07 61 D3 A B C D E`. Checked against an engaged cruise on this car
(2026-11-16, [`BEAN_SIGNALS.md`](BEAN_SIGNALS.md) §1). The third-party list (memory speed in A, switch
and stop-lamp bits in C/D) does **not** match this car:

| Byte | Content (this car) | Confidence |
|---|---|---|
| A | vehicle speed, km/h | Car |
| B | **cruise set speed, km/h**: set at SET, follows RES/ACC and SET/COAST, kept after a cancel (resume speed), 0 once slower than ~35 km/h; the internal value moves in 1 mph steps and B is it floored to km/h | Car |
| C | analog 0..107: rises while cruise pulls the car, 0 when coasting or off; probably the cruise drive request | Car (behaviour), G (meaning) |
| D | bit 7 shift in D; bits 0–3 all set while the main switch is on; bit 6 cruise controlling the car; bits 4–5 never set | Car |
| E | switch inputs at the poll: `10` SET/COAST, `08` RES/ACC, bits 0–2 brake pedal (`07` while pressed, `05`/`04`/`01` at press or release); `02` for two polls at power-off; CANCEL not yet sampled | Car (2026-11-17, 22 switch samples and 1,500 brake samples over all trips) |

The stalk switches are visible only here (sampled at the poll rate), not on broadcast CAN or BEAN.
Details: [`CRUISE_CONTROL.md`](CRUISE_CONTROL.md).

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

* **0x5C8 / 21D3 (cruise):** resolved 2026-11-16/17 (§2, §6; 0x5C8 A bit 7 = immediate-copy header;
  tap step 1 mph). Open: CANCEL bit and hold timing (needs `21D3` faster than 1 Hz),
  [`CRUISE_CONTROL.md`](CRUISE_CONTROL.md) §8.
* **Passive-stream latency (cause found and fixed 2026-11-17; drive check passed 10:10–10:39: reader
  ~1690 frames/s at ~20 % busy, 0x5C8 40–60 ms ahead of its BEAN copy, 0x120 ahead of the `21D3`
  reply at every cruise change):** the broadcast
  frames come from the panda, not from the gateway. Its reader thread could not keep up with the bus
  (about 1650 frames/s on the bus, of which about 500 a second passed the filters and each cost a
  full state update: all the reader thread could do on the phone), so for 81 % of the 2026-11-16 21:14 drive the frames arrived in bursts 0.3–1.2 s apart and
  reached the state and the trip log 0–5 s late (median 0.6 s). Now a fast id updates the state at
  most every 0.1 s, every changed frame is still logged, and a backlog batch is reduced to the newest
  frame per id (`io/ingress.py`, `io/panda_can.py`). The journal line `Panda reader: … busy … %` once
  a minute shows the reader load; a drive must show `busy` well under 100 % and `coalesced` near 0.
  Timing comparisons in trip logs recorded before 2026-11-17 must allow for the lag.
* **Trip-log cadence (fixed 2026-11-17):** until then an id the decoder does not know reached the trip
  log 3 times a minute (0.05 /s, 100 ms frames included): the unknown-id cache sat before the log
  tap. Such ids are now logged once a second (0x020, 0x03E, 0x230, 0x262, 0x348, 0x3C9, 0x3CA,
  0x423, 0x484, 0x4C1…0x4D1 on 2026-11-17). Frame counts in trip logs say nothing about the car's
  period for those ids, and a signal faster than 1 Hz in them (e.g. a blinking bit) cannot be seen.
* **0x5xx cadence:** the list gives about 1 s for most gateway copies; own CAN logs contain far fewer
  frames (e.g. 58 × 0x5C8 in 53 min). Whether the gateway sends them on change only or the logs drop
  them: compare a CAN log with a simultaneous BEAN capture (BEAN side is 1 s).
* **0x025 / 0x0B1 / 0x0B3 absolute sign:** steering and wheel left/right agree with each other on the
  2026-11-13 drive, but both rest on third-party labels. Indirect support: tight turns (|curvature| >
  0.08 /m at 2–8 m/s) come out right-handed 131 times against 87 left (right turns are the tighter ones
  in right-hand traffic). Resolves with one deliberate left turn logged, or a GNSS course.
* **0x0B1/0x0B3 scale:** the wheels read 2.2 % below 0x0B4 (tyre size / ECU scaling); not corrected.
* **0x025 fraction nibble** (E high nibble): not decoded; the angle has 1.5° resolution.
* **0x120 B position** and **0x3B6 bits**: not observed / unvalidated.
* **0x3CA, 0x244 pedal scalings**: third-party, unchecked. **0x022 yaw rate and 0x023 accelerations**
  (2026-11-17, §2): scales P; a calibration needs GNSS course and speed, or a turn-table-free check:
  a steady 360° circle (yaw integral = 2π).
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

## 9. What the gateway transmits, and its transmit errors

The gateway (RP2040 + MCP2515) is the only device that transmits on the car's CAN; the panda is in
SAFETY_SILENT (no frames, no acknowledge bits) and delivers every broadcast frame. Polls are therefore
gateway-only, and a value that is broadcast is not polled.

Poll table and rates: [ARCHITECTURE.md](ARCHITECTURE.md) §3. Nominal transmit rate 4.3 frames/s
(3.0 requests + 1.3 flow controls) since 2026-11-17, 8.6 before (measured before: 6.5 requests/s
including retries, about 8.7 frames/s).

Transmit errors, fw 3.0.0 to 3.2.10 (`can_diag` of 2026-11-13 and 2026-11-16 on 3.2.9): 19–35 % of
the transmit attempts were hit by an error, the TEC climbed to bus-off about every 15 s, while the
receive error counter stayed at 0 and the controller answered 0xFF on SPI once per frame it sent.
Each failed attempt was an error frame on the powertrain CAN (about 2 per second).

Cause, found 2026-11-17 on the car with the ignition off (gateway and panda alone on the bus): the
gateway firmware polled the MCP2515 over SPI while the MCP2515 was transmitting. On this module (SPI
through level shifters) the SPI port is dead from about 50 µs into a dominant-heavy frame until about
350 µs after it, and one SPI transaction in that window destroys the frame. Not bit timing, not a
second tester, and not a hardware fault that needs a repair: frames sent without SPI traffic go out
clean (2000 of 2000). Gateway fw 3.2.11 waits for the end of its frame on the INT pin and runs the
controller in one-shot mode (the engine repeats a request that lost arbitration). Bench result with
the panda acknowledging and answering, 12 s per phase: 3.2.10 three bus-offs per phase, 3.2.11
`tec_up` 0 over about 5000 requests including 576 multi-frame answers. Evidence and the test plan:
gateway repository, `gateway-rp2350/README.md` section "CAN transmit path: root cause (2026-11-17)
and firmware 3.2.11", logs in `captures/2026-11-17/`.

What a drive on 3.2.11 must show in `can_diag`: `tec` 0–7, `eflg` 00, `busoff` 0, `tec_up` near 0,
`isotp_err` near 0, `tx_retry` growing slowly (lost arbitration, harmless).

**First drive on 3.2.11 (2026-11-17 10:10–10:39, 29 min): passed.** `can_diag` at the end: `req`
5379, `resp` 5379, `to` 0, `txerr` 0, `isotp_err` 0, `tec` 0, `rec` 0, `eflg` 00, `busoff` 0,
`tec_up` 0, `reinit` 0, `poll_bad` 0; `tx_retry` = `mloa` 1965 (lost arbitration: one retry for
37 % of the requests on the busy bus; each retry is a normal re-send, no error frame). 3.1 requests/s.
The 2026-11-16 data-crunch reading "physical-layer fault, inspect wiring" is withdrawn: it was the
firmware.

`BACKEND_OBD_PAD=0x55` (requests padded with `55` instead of `00`) stays available and off. It was
meant as a test of the supply-sag idea; with 3.2.11 it is not needed.
