# Toyota BEAN (MPX) on the Gen 2 Prius (NHW20): protocol, transceiver, capture format

BEAN (Body Electronics Area Network, "MPX" in the wiring diagrams) as it applies to the project's
discrete single-wire transceiver on the powerbox RP2040. The signal map of this car (IDs, nodes,
gateway mirror, actuation, do-not-send list, open experiments) is in
[`BEAN_SIGNALS.md`](BEAN_SIGNALS.md). Firmware commands: [`powerbox/README.md`](../powerbox/README.md)
("BEAN receiver", "BEAN transmitter").

## Confidence labels used throughout

| Label | Meaning |
|---|---|
| **[P]** | Primary source: the Toyota/Denso SAE papers, Toyota repair manual RM1076E, Toyota New Car Features / training manuals |
| **[M]** | Several independent hobbyist implementations or captures agree, and/or checked against real frames (CRCs recomputed) |
| **[C]** | Measured on this car |
| **[S]** | Single hobbyist source, not cross-checked |
| **[I]** | Inference from the sources; a hypothesis to test |

"JP dumps" = the five Japanese-market NHW20 BEAN dumps in the `bean_tools` repository (8,627 frames,
all CRCs valid), kept as `tests/fixtures/bean_nhw20_frames.txt.gz`.

---

## 1. Summary

* **Bus [P].** Single wire, NRZ, maximum 10 kbit/s (100 µs bit), CSMA/CD with bitwise arbitration,
  8-bit CRC, ACK/NAK response, 1–11 data bytes. Two-way type on the '04 Prius. Idle/recessive about
  0 V, dominant about +B. Daisy-chained ring, MPX1/MPX2 bridged inside each ECU (SAE 970297).
* **Frame [P].** `SOF(1) PRI(4) ML(4) DST-ID(8) MES-ID(8) DATA(8×1..11) CRC(8) EOM(8) RSP(2) EOF(6)`,
  MSB first. ML counts DST-ID + MES-ID + DATA (3..13). Bit stuffing after 5 equal bits, SOF through
  CRC. A failed frame is resent up to three times. SAE names DST 0xFF as broadcast; the cars studied
  use 0xFE (§3.3).
* **CRC [M].** CRC-8, polynomial 0x13, init 0, no reflection, no final XOR, over PRI/ML through the
  last DATA byte. Checked against 6 toymods frames, the drive2 frame, 8,627 JP frames and every frame
  captured on this car.
* **EOM [M].** Fixed `01111110` (0x7E), not stuffed; six 1s occur only there.
* **Nodes [P].** Body ECU, meter, A/C ECU, transponder key ECU, power source control ECU,
  transmission control (P position) ECU, theft deterrent ECU (European RHD only), smart ECU (smart
  entry only), gateway. The gateway bridges BEAN, CAN and AVC-LAN. No BEAN pin on the DLC3 (RM1076E
  05-2244, NCF256E BE-48). Address → ECU map for this car: [`BEAN_SIGNALS.md`](BEAN_SIGNALS.md) §2.
* **Faults [P].** "Communication stop" DTCs (B1200…B1294) when a node is silent towards the gateway
  for more than 10 s. B1214 / B1215 for a +B / GND short on the bus; "the related system(s) do not
  operate".
* **Message IDs [P]/[C].** The repair manual lists signals per sender and receiver (§4.1) but no IDs.
  IDs and scalings for this car were established from captures ([`BEAN_SIGNALS.md`](BEAN_SIGNALS.md)
  §4).

---

## 2. Physical layer

| Item | Value | Confidence / source |
|---|---|---|
| Medium | 1 unshielded AV wire, "single wire voltage drive" | [P] RM1076E 05-2248/2249; Toyota MPX training BE-9/10 |
| Topology | ring; MPX1 and MPX2 of each ECU bridged internally, survives one open circuit | [P] RM1076E 05-2247; SAE 970297 |
| Speed | max 10 kbit/s, nominal bit 100 µs | [P] |
| Bit time on this car | 103.3 µs (receiver estimate on the live bus) | [C] |
| Levels | idle/recessive ≈ 0 V (logic 0); dominant ≈ +B (logic 1); SOF is a high bit | [P] SAE 970297 fig. 6, 9; SC430 NCF |
| Driver | current-controlled (integrator + voltage-to-current converter), trapezoidal edges | [P] SAE 970297 fig. 5–8 |
| Termination | constant-current sinks across the network; the bus is high when a driver sources more than the sink limit | [P] SAE 970297 |
| DC check | each MPX terminal to body ground (harness side, ECU unplugged) ≥ 10 kΩ; ring segments < 1 Ω | [P] RM1076E 05-2251…2258, 05-2265 |
| Termination current, thresholds, slew numbers | not published | gap |

Transceiver consequences [I]: RX through a high-impedance comparator (≥ 100 kΩ, threshold about
4–6 V, hysteresis); TX through a current-limited high-side switch to +B with a slope of a few µs.
**Power-up and reset state must be recessive**: a stuck-dominant node looks like a +B short (B1214;
key, meter and power source stop working). The as-built circuit is in `powerbox/README.md` (GP6 RX,
GP7 TX).

### 2.1 Tap points (RM1076E section 05, "Multiplex communication system")

All BEAN nodes sit in the instrument panel. The DLC3 carries only SIL 7, CG 4, SG 5, CANH 6, CANL 14,
BAT 16 (Techstream reaches the body side through the K-line and the gateway). Nothing BEAN runs under
the seats. The key slot K3 carries only VC5/CODE/TXCT/GND to the transponder key ECU.

Terminals (manual drawings are harness-side views, lock on top, numbered row by row left → right;
GR grey, B black, P pink, W white; * LHD):

| Node | Connector / pin | Signal | Wire | Ring neighbour |
|---|---|---|---|---|
| Combination meter C10 | IG1-2 (= C10-24) | MPX+ | B (W via centre connector) | S11-32 (with smart entry) or T5-19 (without) |
| Combination meter C10 | IG2-3* (= C10-25) | MPX− | GR (P via centre connector) | P8-7 |
| Power source control ECU P8 (40-pin) | 7 | MPX1 | GR | C10-25 |
| Power source control ECU P8 | 24 | MPX2 | B | G1-3 |
| Gateway ECU G1 (24-pin) | 3 | MPD1 | GR* | P8-24 |
| Smart ECU S11 (40-pin) | 32 / 31 | MPX2 / MPX1 | B / GR | C10-24 / T5-19 |
| Transmission control ECU T5 (26-pin, console) | 19 / 18 | MPX1 / MPX2 | GR / B | S11-31 (or C10-24) / T6-16 |
| Transponder key ECU T6 (24-pin) | 16 | MPX2 | GR | T5-18 |

Easiest taps: transmission control ECU T5 pins 18/19 (centre console, by the shift lever) or the meter
IG1-2 / IG2-3 (top centre of the dash). Identification: unpowered, the wire reads < 1 Ω to the
neighbouring node's MPX terminal; powered, it idles at 0 V with a flickering few-hundred-mV average and
shows ≈ 12 V, 100 µs pulses on a scope. **Never drive TX onto a wire before it is identified.**

---

## 3. Frame format

### 3.1 Field layout [P] (SAE 970297 fig. 9 and text)

```
 bus level
  H   ┌─┬────┬────┬────────┬────────┬───────────────┬────────┬─┬──────┬─┬──┬──────┐
      │S│PRI │ ML │ DST-ID │ MES-ID │ DATA 1..11    │  CRC8  │0│111111│0│RS│000000│ idle (low)
  L ──┘O│4 b │4 b │  8 b   │  8 b   │ 8..88 b       │  8 b   │ EOM 0x7E │P │ EOF  │
       F└────┴────┴────────┴────────┴───────────────┴────────┴──────────┴──┴──────┘
       |<------------- bit stuffing applies (SOF..CRC) -------->| no stuffing from here on
```

| Field | Bits | Meaning |
|---|---|---|
| SOF | 1 | start of frame, always dominant (1) [P] |
| PRI | 4 | priority, 16 levels [P]; the higher value wins arbitration because dominant = 1 [M]. Bit 3 = immediate copy on this car [C] ([`BEAN_SIGNALS.md`](BEAN_SIGNALS.md) §3) |
| ML | 4 | byte count of DST-ID + MES-ID + DATA, 3..13 [P]/[M] |
| DST-ID | 8 | destination; 0xFF broadcast per SAE [P]; 0xFE used for broadcast data (§3.3) |
| MES-ID | 8 | message ID [P] |
| DATA | 8..88 | 1–11 bytes [P] |
| CRC | 8 | CRC-8, poly 0x13, init 0, over PRI/ML … last DATA byte [M] |
| EOM | 8 | `0 1 1 1 1 1 1 0`, "controls the time to prepare the response" [P]; not stuffed [M] |
| RSP | 2 | ACK or NAK [P]; bit meaning §3.5 |
| EOF | 6 | six recessive (low) bits [P]; SAE fig. 9 shows a short IFS after EOF |

On error the data is resent up to three times; frame format is checked in addition to CRC [P].

### 3.2 Bit stuffing [P] + [M]

* After **5 consecutive equal bits** the transmitter inserts one inverted bit, SOF through CRC [P].
  The stuffed bit counts as the first bit of the next run [M] (drive2; specadmin, BeanMPX and mck1117
  reset the run counter to 1).
* SOF takes part in the run count [M].
* **Unresolved edge case:** if the CRC ends in 5 equal bits, both TX implementations (specadmin,
  BeanMPX) insert **no** stuff bit before the EOM, while the mck1117 decoder expects one. The decoder
  therefore searches for the `0111111` EOM pattern instead of assuming its position; the transmitter
  refuses such frames outside bench use (§7).
* Inside SOF..CRC no run exceeds 5 bits; the 6 ones of the EOM are the longest dominant run (600 µs);
  ≥ 6 low bits after EOF = idle.

### 3.3 Addressing

* SAE: point-to-point or broadcast, "switched by the DST-ID. When the DST-ID is FFH, the communication
  is set to the broadcasting method." [P]
* **0xFE is the de-facto broadcast address** on every car with captures (RX300/Harrier, IS300, drive2,
  NHW20) [M]. Broadcast frames are ACKed (JP dumps, and every `FE` frame on this car except the first
  after a wake [C]). Whether 0xFF is ever used on the Prius is unknown.
* **There is no source field** [P]. (Beebird-Labs' "SRC ID" contradicts SAE 970297 and the captures.)
  Senders are identified through MES-ID, the per-node status frames, or timing.

### 3.4 CRC-8 (worked example) [M]

Algorithm: `crc = 0; for each byte b in [PRI<<4|ML, DST, MES, DATA…]: crc ^= b; 8× { crc = (crc & 0x80) ? (crc<<1) ^ 0x13 : crc<<1 }`.
The table form starts `00 13 26 35 4C 5F 6A 79 98 8B …` (identical in BeanMPX, specadmin and mck1117).

**Example 1.** Frame from the drive2 article, captured on a real bus: PRI = 1, ML = 3, DST = FE,
MES = 24 (vehicle speed), DATA = 00.

| byte in | CRC before | CRC after |
|---|---|---|
| 0x13 (PRI 1, ML 3) | 0x00 | 0x16 |
| 0xFE | 0x16 | 0x59 |
| 0x24 | 0x59 | 0x2E |
| 0x00 | 0x2E | **0xB4** |

Bits on the wire (`[x]` = stuffed bit, RR = response slot), identical to the raw bit string the drive2
author published:

```
SOF PRI  ML   DST-ID        MES-ID     DATA          CRC             EOM       RSP EOF
1   0001 0011 111[0]11110   0010 0100  000[1]00000   [1]1011 0100    01111110  RR  000000
```

(Left to right: the two 1s at the end of ML plus the first three 1s of DST make five 1s, so a 0 is
inserted. The two trailing 0s of MES plus the first three 0s of DATA make five 0s, so a 1 is inserted;
that stuffed 1 starts a new run, so the remaining five 0s of DATA trigger another stuffed 1 before the
CRC.)

**Example 2.** A real NHW20 frame: `PRI 6, ML 3, DST FE, MES 2C, DATA 64` gives CRC **0xC1** (coolant
0x64/2 = 50 °C):

```
1 0110 0011 111[0]11110 0010 1100 0110 0100 1100000[1]1  01111110 RR 000000
```

Other verified frames: toymods `64 98 CC 02 A8 →35`, `53 D2 C8 00 →E9`, `63 D2 2C 00 →B5`; drive2
`25 FE 7F 01 00 00 →3A`; NHW20 `84 FE C2 E9 A1 →2B`.

### 3.5 RSP / ACK field

Primary source [P]: RSP is 2 bits, "ACK: acknowledgment / NAK: non-acknowledgment"; EOM "controls the
time to prepare the response"; resend up to 3 times on error.

| Source | Slot after EOM's final 0 | Interpretation |
|---|---|---|
| specadmin/toyota-mpx (AVR driver with TX; STM32 port by aselafernando) | samples RSP1, RSP2, expects a low bit after | `0,1` ACK; `0,0` no ACK; `1,1` "bad CRC" |
| fiztech BeanMPX (Arduino, used on a real car) | an accepting receiver drives `0,1,(0)`; on CRC failure `1,0,(0)` | `01` ACK, `10` NAK; TX retries (2) unless exactly ACK |
| mck1117 bean-analyzer (Saleae decoder) | reads 2 bits after EOM | comment "10 = ACK, 01 = NAK", never evaluated |

Working definition [M]/[C]: **ACK = second RSP bit dominant (`01`); NAK = first bit dominant (`10`);
`11` = one node ACKs while another NAKs.** On this car every frame is ACKed with `01`, except the first
frame after a wake, which is repeated (after 42 ms on this car, 14 ms in a JP dump).

Who ACKs [M]/[I]: BeanMPX ACKs only frames whose DST is in a configured list, so the addressed node
ACKs, and for 0xFE at least one node does. The slot is wired-OR. **A pure listener must never ACK**:
it would suppress the sender's retries when the real addressee is missing, and a misaligned ACK bleeds
into EOF. Whether a third-party ACK on the Prius causes DTCs or retries is not known.

### 3.6 Arbitration, collisions, inter-frame space

* CSMA/CD with bit-wise arbitration [P]. A transmitter that sends recessive but reads dominant has lost,
  stops driving and continues as a receiver [M]. Higher PRI wins; on a tie the larger ML, then the
  higher DST-ID [M].
* Bus-idle before SOF: references wait for > 6 bit times low [M]. The project's transmitter waits 11
  (§7.2).
* SAE simulation of an LS400 body network (41.5 % utilisation): message delay 8.2 ms average / 15.5 ms
  maximum at the highest priority, 10.6 / 191 ms at the lowest; intervals dimensioned for ≤ 40 % average
  utilisation [P].

### 3.7 Bit timing and tolerance

* Nominal 100 µs [P]; no numeric clock tolerance published. 103.3 µs measured on this car [C].
* All reference implementations resync on every edge (specadmin 10,000 bit/s; BeanMPX 103.8 µs at
  16 MHz on a real car; mck1117). With a resync at least every 6 bit times, ±5 % is easily tolerated
  [M].

---

## 4. Address and message tables

### 4.1 Prius NHW20: who sends what over BEAN (signal names, no IDs) [P]

From RM1076E system-description tables. "Steering lock ECU" in the push-button-start table is the
transponder key ECU here. Signals marked *(via GW)* originate on CAN (HV ECU, ECM, skid control) and
reach BEAN nodes through the gateway; the meter, body ECU and power source ECU are not CAN nodes
(05-2243) [I].

| Sender | Receiver | Signals | Page |
|---|---|---|---|
| A/C ECU | Meter | ambient temperature (low-temp warning), A/C AUTO/RECIRC/Fr DEF/Rr DEF indicator ON | 05-1678 |
| Meter | A/C ECU, power source ECU, body ECU, transponder key ECU | vehicle speed | 05-1678, 05-2125 |
| Meter | Gateway | ODO meter information | 05-1678 |
| Body ECU | Meter | tail cancel (rx), D-door courtesy (headlamp reminder), ACC switch, warning ON (HEAD/TAIL), high-beam ON, front fog ON, meter illumination dimmer demand, all-door courtesy warning | 05-1678 |
| Smart ECU | Meter | in-cabin key detection, buzzer sounding demand | 05-1678 |
| Transmission control ECU | Meter | P state, master warning (P control malfunction), shift position (N, P), master warning (shift warning), buzzer demand | 05-1678 |
| Transponder key ECU | Meter | buzzer request (key reminder: key in slot + D-door open, power OFF/ACC) | 05-1678, 05-2030, 05-2039 |
| HV ECU *(via GW)* | Meter | shift position P/R/N/D/B, **cruise indicator**, master warnings, READY indicator ON/blink + buzzer, EV indicator | 05-1678/1679 |
| ECM, skid control, EPS *(via GW)* | Meter | coolant temp warning, engine speed, injection volume (trip info), oil pressure, ABS/VSC/BRAKE/ECB/SLIP, EPS warnings | 05-1678 |
| HV ECU *(via GW)* | Power source ECU | shift position, drive OK, stop switch | 05-2125 |
| Body ECU | Power source ECU | diagnosis erasure, diagnostic tool request, door lock positions, courtesy switches, vehicle model, IG, ACC, key switch, fading dome lamp request, taillamp illumination, all-doors courtesy | 05-2125 |
| Power source ECU | Body ECU | power switch signal (security cancel) | 05-2125 |
| Transponder key ECU ↔ power source ECU | | immobiliser information; ID match request; key insertion status | 05-2125 |
| Transmission control ECU | Power source ECU | P status, P-control malfunction, auto-P cancel, NP status, auto-P OK | 05-2125 |
| Power source ECU | Transmission control ECU | power source OFF ready (auto-P on OFF) | 05-2125 |
| Smart ECU ↔ power source ECU | | immobiliser ID match request, key insertion status, key code confirmation | 05-2125, 05-1863 |
| Theft warning ECU | Power source ECU | power switch signal (security set/cancel) | 05-2125 |
| HV ECU *(via GW)* | Smart ECU | shift position | 05-1863 |
| Body ECU | Smart ECU | D-door key-linked lock/unlock, DTC erasure, door lock positions, back door status, courtesy switches, vehicle spec, IG, ACC, halfway switch, back door switch, all-doors courtesy | 05-1863 |
| Transponder key ECU ↔ smart ECU | | smart control ON, steering-lock release activation; encrypted code exchange over BEAN (05-1919) | 05-1863, 05-1919 |
| Smart ECU | Body ECU | lock signals to all doors; back door open signal | 05-1894, 05-1916, 05-1923 |
| Body ECU | Theft warning ECU | door lock/smart/wireless signals, key-linked switch, diag erasure, door status, vehicle spec, IG/ACC, all-doors courtesy | 05-2202 |
| Theft warning ECU | Body ECU | security signal: door lock and unlock, head lamp, hazard lamp, horn, dome lamp, tail lamp | 05-2202 |
| Transponder key ECU | Body ECU | security indicator ON/OFF | 05-2088 |
| A/C ECU ↔ body ECU / GW | | vehicle spec, DTC clear, diag demand; MFD A/C operation arrives via AVC-LAN → gateway → BEAN | 05-1019 |

The smart ECU's "lock signals to all doors" reach the body ECU over a dedicated serial line, not BEAN
(NCF256E BE-26; [`BEAN_SIGNALS.md`](BEAN_SIGNALS.md) §7).

Cross-check DTCs [P]: the power source control ECU compares hard-wired signals with BEAN data and sets
**B2281** (P signal), **B2282** (vehicle speed), **B2284** (brake), **B2286** (READY) and **B2289**
(immobiliser) when they disagree (05-2143…2173).

### 4.2 JP reference dumps (bean_tools) [M]

Five dumps (3 power-OFF, 1 IG-ON, 1 READY), 42 distinct DST/MES pairs. The bean_tools adapter drops
about 2/3 of the frames (`FE:53` measures 3.3 s there against 1.00 s on this car), so JP periods are
upper bounds. Differences from this car (car off unless noted):

| Item | JP | This car |
|---|---|---|
| Odometer | `FE:3F 02 87 E9 80` (broadcast) | `52:3F 04 93 DF 80` (to the gateway) |
| `FE:53` | `49 00 08 20 00 00` | `49 00 00 00 00 00` |
| `FE:54` | `00 42 00 41 45 4E` | `57 42 4C 41 45 4E` (also in an EU dump, KostyaSha/bean-cracker) |
| `FE:AB[52]` b9 | `07` | `03` |
| `FE:E1` | never non-zero | `04/08/10/20` lock/unlock events |
| `FE:83` b0 `02` | key in slot | never set |
| `FE:91` in READY | `80 20 00` | `00 00 00` |
| `C1:E2` | absent | 0.5 s exchange in every driver-door window |

### 4.3 Other Toyota/Lexus models (decoding aid; IDs may differ on the Prius)

| MES-ID (DST seen) | Meaning | Vehicle | Source |
|---|---|---|---|
| 0x15 (D2) | climate panel buttons (AUTO, OFF, REC, FRONT, Rr DEF, A/C, MODE) | Lexus RX300 / Harrier | specadmin [S] |
| 0x1A (46), 0x1B (45) | climate TEMP/blower encoder steps, blower level | RX300 | specadmin [S] |
| 0x20 (46) | momentary fuel rate | RX300 | specadmin [S] |
| 0x24 (FE) | vehicle speed km/h; b1.7 = meter power ON | RX300, IS300, NHW20 | specadmin, IS300 [M] |
| 0x25, 0x76, 0x90, 0xA3 (45) | average speed, trip distance, momentary and average consumption; units in the last byte or in 0x6A | RX300 | specadmin [S] |
| 0x26 (D2/FE) | tachometer = (D0·256 + D1)/5.12 rpm | RX300, IS300, NHW20 | [M] |
| 0x2C (FE/62) | coolant = value/2 °C | RX300, IS300, BeanMPX car, NHW20 | [M] |
| 0x40 (FE/62) | gear: P R N D 4/M 3 2 L bits; D1 = manual-mode gear | RX300, BeanMPX car, NHW20 (P/R/N/D) | [M] |
| 0x7F (FE) | lamp indicators | unnamed Toyota (drive2: `25 FE 7F 01 00 00` lit the clearance indicator); NHW20 | [S] each, layouts differ |
| 0x96 (E0) | back door unlocked | RX300 | specadmin [S] |
| 0xA4 (62) | fuel level / consumption | BeanMPX car, IS300 | [M] |
| 0xCD (FE) | outside temperature = value − 48 °C | RX300, IS300, NHW20 | [M] |
| 0xD0 (45) | meter indicators: doors, CHECK ENGINE, ABS, VSC, oil, battery, dimmed backlight | RX300 | specadmin [S] |
| 0xD2 (45/62) | ECT PWR/SNOW, CRUISE, "M", A/T oil temp | RX300, IS300, BeanMPX car | [M] |
| 0xD4 (FE/62) | starter running, no charge, oil pressure | RX300, IS300 | [M] |
| 0xD7 (D2/FE) | blower/compressor/rear-defogger relays; A/C on = b0.6 | RX300, IS300 | [M] |
| 0xDA (40) | "make a single ding sound" | RX300 | specadmin [S] |
| 0xDB, 0xDC, 0xDD, 0xDE (FE) | per-door: open, unlocked, key cylinder lock/unlock, lock/unlock buttons, window-lock | RX300 | specadmin [S] |
| 0xDF (FE/62) | parking brake, IG ON, ACC, any door open; b3.7 back door; (IS300: door, seat belt) | RX300, IS300 | [S]/[M] |
| 0xE0 (D1) | power window commands, per window up/down/auto | RX300 | specadmin [S] |
| 0xE4 (FE) | head/tail/rear-fog lights, key inserted, driver seat belt, front fog | RX300 | specadmin [S] |
| 0xFA (62) | door warning flash (requires 0xD4 bit set first) | BeanMPX car | fiztech [S] |
| 0xFC, 0xFD (45) | climate mode / target temperature | RX300 | specadmin [S] |
| 0xAB (FE) | `40 80` | IS300 (emulated by an engine-ECU replacement) | IS300 repo [S]; network status on the NHW20 |

---

## 5. Transmit safety rules (protocol level)

ID-level do-not-send list: [`BEAN_SIGNALS.md`](BEAN_SIGNALS.md) §7.1.

| Hazard | Consequence | Confidence |
|---|---|---|
| Holding the line dominant (TX stuck on) | equivalent to a +B short → B1214, "related system(s) do not operate" (meter, key, power source) | [P] |
| Holding the line low hard (low-impedance RX input) | GND short → B1215 | [P] |
| High PRI or flooding | delays real traffic; a node silent towards the gateway > 10 s sets B12xx (B1200, B1207, B1210, B1260, B1262, B1269, B1271, B1294) | [P] |
| ACKing frames as a passive listener | hides missing nodes, may produce `11` responses → retries | [I] |
| Waking the bus while parked | every ECU wakes (wake-up on line activity, SAE); repeated wakes drain the 12 V battery | [P] mechanism, [I] consequence |

Rules the transmitter enforces (`powerbox/bean_tx.py`; details in `powerbox/README.md`): hardware
default recessive (GP7 held low at boot); a PIO watchdog state machine and a firmware timer release GP7
if it stays dominant about 0.8 ms (no valid frame has more than 6 dominant bit times in a row);
arbitration checked on every recessive bit; 3 attempts per frame by default (4 at most); one send
bounded to 400 ms; SOF only after 11 recessive bit times; deny lists by DST and by broadcast MES; a
frame whose DST/MES was seen from another node since boot is refused; TX ops need an explicit 60 s arm
(`bean_tx_enable`).

---

## 6. Expected bus behaviour

* **Frame durations [I] (computed).** Bits = 33 + 8·ML + stuff bits (EOF included). ML = 3: about
  58 bits ≈ 5.8 ms; ML = 4: ≈ 6.6 ms (the 7 ms spacing of the wake burst); ML = 13: 138–160 bits ≈
  14–16 ms.
* **Sleep [P]:** power-saving mode with wake-up on line activity. Measured wake, keep-awake and sleep
  timing, frame rates and the network-management frames of this car: [`BEAN_SIGNALS.md`](BEAN_SIGNALS.md)
  §3.
* **Errors:** no CRC or stuffing errors in any capture (JP dumps and this car).

---

## 7. RP2040 implementation notes

### 7.1 Receive [I], drawing on the reference implementations [M]

Every reference implementation resyncs on every edge (BeanMPX on every pin change; specadmin re-arms a
half-bit timer per edge; mck1117 resyncs then advances half a bit). A free-running UART-style clock is
not used.

Design: **edge-to-edge run-length capture.**

* A PIO state machine counts while the input is stable and pushes `(level, length)` on every edge
  (1 µs resolution; a timeout pushes "idle" after 1 ms without an edge); DMA to a ring buffer.
* The CPU converts each run to `n = round(length / T_bit)`, `T_bit` refined from single-bit runs. Valid
  runs: 1–5 bits in SOF..CRC, up to 6 in the EOM; longer low = idle. Edge-to-edge measurement cannot
  accumulate drift (±0.5 bit margin up to about ±8 % clock error).
* Decode:
  1. Wait for idle (≥ 7 bit times low), then a rising edge = SOF.
  2. Expand runs into bits and destuff (after 5 equal bits the next must be the inverse; drop it). A
     6th equal bit: 1s → EOM reached; 0s → frame error, resync on idle.
  3. Collect PRI/ML (8 bits), 8·ML bits, 8 CRC bits.
  4. Find the EOM `0111111` + `0` without assuming a fixed position (§3.2).
  5. Read RSP1, RSP2, then check the following bits are low (EOF).
  6. Check CRC-8/0x13 over PRI/ML..DATA. Report errors, do not drop silently.
* Glitch filter: pulses shorter than about 10 µs are ignored (slow trapezoidal edges).

**Implementation:** `powerbox/bean_rx.py` (fw ≥ 2.3.0): PIO run-length capture at 1 µs, DMA ring,
decoder steps 1–6, adaptive bit time, dominant/recessive skew estimate, 12 µs glitch filter; the stuff
bit before the EOM is accepted present or absent. Firmware protocol and counters: `powerbox/README.md`
"BEAN receiver". The backend keeps a frame table and capture logs (§7.3): REST `bean_capture` /
`bean_mark`, `GET /api/v1/bean/frames`. `tests/test_bean_rx.py` re-encodes all 8,627 JP frames on the
wire and decodes them all.

### 7.2 Transmit [M]/[I]

* Wait for idle, drive SOF, send MSB first with stuffing, re-check the bus at each recessive bit
  (arbitration loss → release, keep decoding the winner's frame).
* After the EOM (8 raw bits), release and sample RSP1/RSP2 at mid-bit: `01` ACK, `00` nobody, `10` NAK,
  `11` conflicting.
* On a single wire the transmitter reads its own bits back; own frames are tagged so they are not
  decoded as foreign.

**Implementation:** `powerbox/bean_tx.py` (fw ≥ 2.4.0), described in `powerbox/README.md` "BEAN
transmitter" (design, timing diagram, guards, commands, first-transmission procedure). It has sent
frames on this car (the `FE:E1` replay, ACKed). Protocol assumptions:

- **Bit time.** The bit time the receiver measured on the live bus (103.3 µs); refuses to send
  outside 95–108 µs.
- **Idle / IFS.** SOF only after 11 bit times of continuously recessive bus, checked in the PIO program
  (references wait 7; the longer wait makes this node go last).
- **Arbitration.** Checked on every recessive bit at the sample point (default mid-bit). A loss inside
  SOF…MES counts as arbitration lost, a later one as a bit error; both stop the frame and are retried.
  A dominant bit that reads back recessive is a line fault and is never retried.
- **Stuffing.** After 5 equal bits SOF through CRC; none after the last CRC bit (as specadmin and
  BeanMPX). Frames whose CRC ends in 5 equal bits are refused outside bench use (§3.2).
- **RSP / ACK.** Only `01` counts as ACK; `00`, `10` and `11` lead to a retry. The raw bits (`acked`)
  are reported next to the receiver's decode of the same frame (`rx_rsp`).
- **Retries.** Default 2 (3 attempts), configurable up to 3 (SAE "resent up to three times"). Spacing:
  the wait for the own frame to come back through the receiver (10–20 ms) plus 10 ms per attempt.
- **Wake-up.** No separate wake pattern exists; nodes just send (`FE:C2` first, un-ACKed, then
  repeated). The transmitter wakes the bus only on an explicit `wake`, since every wake costs 12 V
  charge.
- **Never ACKing.** No code path drives the RSP slot of a frame it did not send.

**Self-test frame: `PRI 0, DST FE, MES 48, DATA 00`** (CRC 0x15; 53 wire bits SOF…EOM, 61 with RSP and
EOF: 6.3 ms at 103.3 µs).

- `FE` because broadcast frames are ACKed on this car, so a correct frame gets an answer without
  addressing a node.
- MES `48`: not used with any DST in the 42 JP DST/MES pairs (§4.2), in any capture of this car
  ([`BEAN_SIGNALS.md`](BEAN_SIGNALS.md) §4), or in the other-model tables (§4.3, specadmin
  `MPX-codes.md`, BeanMPX, IS300); not a node address; in the middle of the largest unused gap
  (`41`…`4F`).
- DATA `00` and PRI 0 (loses every arbitration).
- **Residual risk.** The captures do not cover every state (diagnostics, every option), so `48` could
  still be an ID some ECU knows. Mitigations: the firmware refuses any DST/MES pair seen from another
  node since boot; all-zero payload; sent a handful of times, not periodically; DTCs read afterwards. If
  `FE:48` ever shows up from the car, pick another ID from the gap and change `TEST_MES`.

The deny lists (`DENY_DST`, `DENY_FE_MES` in `bean_tx.py`) hold every destination of a real node,
every broadcast ID of the JP dumps and this car's captures, and the IDs with a known meaning on other
models. Sending anything on them needs the bench override.

### 7.3 Capture formats

**Project capture log** (`bean-capture/1`, written by `cyberpunk_computer/backend/bean_monitor.py` on
REST `bean_capture`; files `logs/bean/bean_<YYYYmmdd-HHMMSS>[_<name>][_partN].ndjson`, rotated at
20 MB). One JSON object per line:

```
{"type":"header","format":"bean-capture/1","start":"2026-11-13T18:02:11+01:00","name":"doors","part":1}
{"type":"frame","ts":1763053331.204,"t_ms":123456,"pri":6,"dst":"FE","mes":"2C","data":"64","rsp":1,"ok":1}
{"type":"frame","ts":...,"ok":0,"err":"crc",...}
{"type":"mark","ts":1763053340.0,"text":"driver door opened"}
{"type":"stats","ts":...,"st":{firmware counters}}
```

* `ts` = the phone's receive time of the frame's batch (late by a median 0.25 s, up to 0.8 s);
  `t_ms` = firmware time of the frame. Use `t_ms` for intervals.
* `rsp` = the raw 2-bit RSP field (1 = `01` = ACK), never interpreted in the logger. A frame sent by the
  powerbox's own transmitter carries `"own":1`.
* `mark` lines come from REST `bean_mark`, so each action in a session is marked in the log.
* Transmit requests and results are also written to `logs/bean/bean_tx_audit.ndjson`.

Recordings in this format: `docs/data/bean/` ([`BEAN_SIGNALS.md`](BEAN_SIGNALS.md) §10).

**bean_tools dumps** (JP reference): `.dat` = an int64 start time followed by 40-byte records
`[8 B QDateTime d-pointer, junk][pri, ml, dst, mes, data[11], crc, ack][7 B pad][int64 t_ms]`
(little-endian, x86-64 Qt5 struct dump). Parsed this way, 0 CRC errors.

---

## 8. Open gaps (protocol level)

1. Bit-level ACK semantics from a primary source (only implementations agree on `01` = ACK); what a
   third-party ACK does; the exact IFS length.
2. The stuffing rule when the CRC ends in 5 equal bits.
3. Electrical spec: bit-time tolerance, termination current, receiver threshold, driver slope.
   Resolves with a scope on the meter (C10-24): bit time, levels, edge slope, RSP waveform position.
4. BEAN-level diagnostic frames (gateway ↔ body ECU) used by Techstream active tests; needs a K-line
   tester capture ([`BEAN_SIGNALS.md`](BEAN_SIGNALS.md) E9).

Car-specific gaps (IDs, bits, nodes, cruise control): [`BEAN_SIGNALS.md`](BEAN_SIGNALS.md) §9.

---

## 9. Sources

Primary (Toyota / Denso):

1. H. Honda, S. Uehara, K. Sakai, T. Akatsuka (Toyota), S. Akiyama (Denso), **"Body Electronics Area
   Network (BEAN)", SAE 970297** (1997). Frame format (fig. 9), stuffing, CRC-8, ACK/NAK, 3× resend,
   CSMA/CD, physical layer, wake-up. https://frankensteinmotorworks.com/MKIII/2ARSwap/SAE970297.pdf ;
   https://github.com/rusefi/rusefi_documentation/blob/master/PDFs/SAE970297.pdf ;
   https://github.com/specadmin/toyota-mpx/tree/master/docs
2. A. Tanaka, M. Kamiya, K. Sakai (Toyota), T. Kishigami (Denso), **"Gateway Application for Automotive
   Network System 'BEAN'", SAE 1999-01-1278**. Gateway, diagnostic conversion (same PDF as 1).
3. **Toyota Prius Repair Manual RM1076E (2004, EU)**, section 05. Multiplex communication 05-2238…2300
   (system description 05-2242…2249, ECU terminals 05-2251…2258, DTC chart 05-2261, B1200 05-2262,
   B1214/B1215 05-2274); combination meter 05-1677…1679; power door lock 05-1787; smart entry
   05-1861…1863, 05-1894…1929; key reminder 05-2029…2040; engine immobiliser 05-2088; push button
   start 05-2124/2125, 05-2141, 05-2143…2173; theft deterrent 05-2202, 05-2214; A/C 05-1019, 05-1044.
   https://toyotamanuals.gitlab.io/PZ471-Z01S0-CA/htmlweb/rm/rm1076e/m_05_NNNN.pdf (NNNN = page)
4. **Toyota Prius New Car Features NCF256E**, BE-46…49 (multiplex):
   https://toyotamanuals.gitlab.io/PZ471-Z01S0-CA/htmlweb/ncf/ncf256e/m_be_0046.pdf
5. Toyota training "Body Electrical – Multiplex Communication" ('04 Prius): `MPX_multi2.pdf` in
   https://github.com/specadmin/toyota-mpx/tree/master/docs
6. Toyota NCF SC430 "Multiplex communication system": `BEAN_ECUs.pdf` in the same folder
7. Toyota Prius repair manual "MP Multiplex Communication" (later NHW20):
   https://hybrids.ru/wp-content/uploads/2021/04/MP-Multiplex-Communication.pdf

Implementations and captures:

8. **haageduard/bean_tools**: Qt BEAN logger/toggler/player, NHW20 (JP) captures, Prius dashboard
   (`data/prius_20.json`). https://github.com/haageduard/bean_tools
9. **specadmin/toyota-mpx**: AVR BEAN driver with TX, arbitration and ACK readback; `MPX-codes.md`
   (RX300 / Harrier). https://github.com/specadmin/toyota-mpx ; https://github.com/specadmin/MPX-viewer ;
   STM32 port https://github.com/aselafernando/BeanMPX-STM32
10. **fiztech-code/BeanMPX**: Arduino BEAN library. https://github.com/fiztech-code/BeanMPX
11. **mck1117/bean-analyzer**: Saleae decoder. https://github.com/mck1117/bean-analyzer
12. daniel-dmitrzak/BeanMPX_IS300: Lexus IS300 sniffing and MPX↔CAN conversion.
    https://github.com/daniel-dmitrzak/BeanMPX_IS300
13. drive2.ru, "BEAN bus" (2018): raw bit string with stuffing, `FE 7F` test frames.
    https://www.drive2.ru/b/494654889247376137/
14. Toymods forum, "Toyota BEANs, Multiplex etc": frames with CRCs.
    https://www.toymods.org.au/forums/threads/39361-Toyota-BEANs-Multiplex-etc
15. rusEFI forum, BEAN links: https://www.rusefi.com/forum/viewtopic.php?p=49513
16. Beebird-Labs/toyota-bean (2000 Crown, 5 V; contains errors: "SRC ID", "lower value = higher
    priority"): https://github.com/Beebird-Labs/toyota-bean
17. KostyaSha/bean-cracker (EU NHW20 IG-ON dump): https://github.com/KostyaSha/bean-cracker
18. Prius Gen 2 CAN PID list (0x750 = "Main Body"):
    https://attachments.priuschat.com/attachment-files/2021/09/211662_Prius22009_CAnCodes.pdf
