# Toyota BEAN (MPX) on the Gen 2 Prius (NHW20): protocol notes for an RP2040 listener and transmitter

Research date: 2026-11-12. This document collects what could be found about the Toyota BEAN bus
(Body Electronics Area Network, called "MPX" in the wiring diagrams). It is written for one setup: a discrete single-wire
transceiver on an RP2040 that first listens and decodes, and later transmits a few selected frames.

Pin-level tap points (connector and pin per ECU, wire colours, ring order) are already in
`docs/prius_buses.md` (section "BEAN (MPX) tap points") and are not repeated here.

## Confidence labels used throughout

| Label | Meaning |
|---|---|
| **[P]** | Confirmed by a primary source: the Toyota/Denso SAE papers, the Toyota repair manual RM1076E, or the Toyota New Car Features / training manuals |
| **[M]** | Several independent hobbyist implementations or captures agree, and/or I checked it against real captured frames (for example, CRCs recomputed) |
| **[S]** | Reported by a single hobbyist source and not cross-checked |
| **[I]** | My own inference from the sources. Treat it as a hypothesis to test on the car |

"NHW20 capture" means the five real Prius NHW20 (Japanese-market) BEAN dumps in the `bean_tools` repository
(see Sources). I decoded them myself (6,723 + 1,505 + 180 + 133 + 86 frames). All frames passed the CRC check.

---

## 1. Summary: what is certain

* **Bus [P].** Single wire, NRZ, maximum 10 kbit/s (100 µs bit), CSMA/CD with bitwise arbitration, 8-bit CRC,
  ACK/NAK response, 1–11 data bytes. The two-way type is used on the '04 Prius. Idle/recessive is about 0 V and
  dominant is about +B. The ECUs form a daisy-chained ring, with the MPX1/MPX2 pins bridged inside each ECU.
  The source is SAE 970297, Toyota and Denso.
* **Frame [P].** `SOF(1) PRI(4) ML(4) DST-ID(8) MES-ID(8) DATA(8×1..11) CRC(8) EOM(8) RSP(2) EOF(6)`, MSB first.
  ML counts DST-ID + MES-ID + DATA, so it ranges from 3 to 13. Bit stuffing inserts an inverted bit after 5 equal
  bits, applied from SOF to CRC. A failed frame is resent up to three times. DST-ID 0xFF means broadcast
  (SAE 970297; on the cars studied, 0xFE behaves as the broadcast address, see §3.3).
* **CRC [M].** CRC-8, polynomial x⁸+x⁴+x+1 (0x13), initial value 0, no reflection, no final XOR. It covers the
  bytes from the PRI/ML byte through the last DATA byte (SOF excluded). Three independent implementations use it,
  and I checked it against 6 toymods frames, the drive2 frame and 8,627 NHW20 frames.
* **EOM [M].** EOM is the fixed pattern `01111110` (0x7E) and is not stuffed. Six consecutive 1s therefore occur
  only in the EOM, which makes the EOM easy to detect.
* **Prius topology and nodes [P].** The BEAN connects the body ECU (instrument panel J/B), meter ECU, A/C ECU,
  transponder key ECU, power source control ECU, transmission control ECU (P-position ECU), theft deterrent ECU
  (European RHD only), smart ECU (with smart entry only) and gateway ECU. The gateway bridges BEAN, CAN and
  AVC-LAN. There is no BEAN pin on the DLC3 (RM1076E 05-2244, NCF256E BE-48).
* **Fault behaviour [P].** A "communication stop" DTC (B1200 to B1294) is set when a node is silent towards the
  gateway for more than 10 s. B1214 and B1215 are set for a +B or GND short on the BEAN, and in that case
  "the related system(s) do not operate".
* **What the Prius sends over BEAN [P], at signal level.** The repair manual lists sender, receiver and signal
  name for the meter, push-button start, smart entry, theft deterrent, key reminder and A/C (§4.1). It does
  **not** give any message IDs.
* **Message IDs on the NHW20 [M]/[I].** These come from the real NHW20 captures only (§4.2). Only a few IDs have
  known meanings, and those are shared with other Toyota/Lexus models: 0x2C coolant (value/2 °C), 0x40 shift
  position, 0x24 vehicle speed, 0xCD ambient temperature, 0x7F meter lamp flags, and 0x3F odometer (24-bit).
* **Not found anywhere.** No Prius-specific BEAN message dictionary, no table that maps BEAN node addresses to
  ECUs, no published sleep timing, no electrical spec for the termination current or receiver threshold, and no
  description of the BEAN diagnostic messages.

---

## 2. Physical layer

| Item | Value | Confidence / source |
|---|---|---|
| Medium | 1 unshielded AV wire, "single wire voltage drive" | [P] RM1076E 05-2248/2249; Toyota MPX training (BE-9/10) |
| Topology | Ring ("daisy chain"); MPX1 and MPX2 of each ECU bridged internally, so the bus survives one open circuit | [P] RM1076E 05-2247; SAE 970297 ("bus reliability") |
| Speed | max 10 kbit/s → nominal bit time 100 µs | [P] RM1076E 05-2248, SAE 970297 |
| Levels | idle/recessive ≈ 0 V (logic 0); dominant ≈ +B (logic 1); SOF is a high bit | [P] SAE 970297 fig. 6 and 9; SC430 NCF frame drawing "Hi (12 V) / Lo (0 V)" |
| Driver | Current-controlled driver: an integrator plus a voltage-to-current converter. Edges are trapezoidal to limit radiated noise | [P] SAE 970297 fig. 5–8 |
| Termination | Constant-current sink ("terminating circuit … works as current limiting circuit"). The bus is high when the driver sources more than the sink limit. Several terminations sit across the network | [P] SAE 970297 |
| DC resistance check | Each MPX terminal to body ground (harness side, ECU unplugged) "10 kΩ or higher"; ring segments < 1 Ω | [P] RM1076E 05-2251…2258, 05-2265 |
| Termination current, thresholds, slew numbers | **not found** | gap |
| 5 V variant | One owner reports 5 V BEAN signalling on a 2000 Crown (JZS173). Not relevant to the NHW20, whose dominant level is +B | [S] Beebird-Labs/toyota-bean |

Consequences for your transceiver [I]:

* **RX.** Use a high-impedance comparator (≥ 100 kΩ load, so you do not fight the current-sink terminations), with a
  threshold around 4–6 V and some hysteresis. A dominant level is +B (11–14.5 V).
* **TX.** Use a high-side switch to +B with current limiting (the ECUs' termination sinks must be overpowered)
  and a moderate slope of a few µs, not a hard edge. **The power-up and reset state must be OFF (recessive).**
  A stuck-dominant node looks like a +B short. That sets B1214, and Toyota states that the related systems then
  do not operate, which affects key, meter and power source.

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
| SOF | 1 | Start of frame. Always dominant (1) [P] |
| PRI | 4 | Priority, 16 levels [P]. In arbitration the higher value wins, because dominant = 1 [M] (specadmin driver comments; follows from the physics) |
| ML | 4 | Byte count of DST-ID + MES-ID + DATA, so 3..13 [P]. Confirmed on 8,627 NHW20 frames [M] |
| DST-ID | 8 | Destination address. 0xFF = broadcast per SAE [P]. The cars studied use 0xFE heavily for broadcast data (§3.3) |
| MES-ID | 8 | Message ID, "the contents of the message" [P] |
| DATA | 8..88 | 1–11 bytes [P] |
| CRC | 8 | CRC-8, poly 0x13, init 0, over PRI/ML … last DATA byte [M] |
| EOM | 8 | `0 1 1 1 1 1 1 0`, "end of message; controls the time to prepare the response" [P]. Not stuffed [M] |
| RSP | 2 | Response: ACK or NAK [P]. Bit meaning: see §3.5 |
| EOF | 6 | End of frame, 6 recessive (low) bits [P]. SAE fig. 9 also shows a short IFS after EOF, then idle |

The SAE paper also says that "several types of error check methods, such as checking of the frame format in
addition to the error check by CRC, are defined. If an error occurs, the data is automatically resent up to three
times." [P]

### 3.2 Bit stuffing [P] + [M]

* After **5 consecutive equal bits** the transmitter inserts one inverted bit. This applies from SOF through
  CRC [P]. The stuffed bit counts as the first bit of the next run [M]: drive2 states this explicitly, and the
  specadmin, BeanMPX and mck1117 code all reset the run counter to 1 on the stuff bit.
* SOF takes part in the run count. A frame starting `1 0001 …` counts SOF as a 1 [M].
* **Edge case (unresolved).** If the CRC itself ends with 5 equal bits, both TX implementations (specadmin,
  BeanMPX) do **not** insert a stuff bit before the EOM, but the mck1117 decoder would expect one. Make the
  decoder tolerant: after the 8 CRC bits, search for the `0111111` EOM pattern rather than assuming a fixed
  position (§7). [I]
* Consequence: inside SOF..CRC no run is longer than 5 bits. The 6 ones of the EOM are the longest dominant run
  in a valid frame (600 µs). A ≥ 6-bit low period after EOF means the bus is idle.

### 3.3 Addressing

* SAE: point-to-point or broadcast, "switched by the DST-ID. When the DST-ID is FFH, the communication is set to
  the broadcasting method." [P]
* **0xFE is the de-facto broadcast or "all body nodes" address** on every car with captures (RX300/Harrier,
  IS300, an unnamed Toyota on drive2, the NHW20). On the NHW20, 22 of the 42 observed DST/MSG pairs use DST 0xFE.
  Broadcast frames to 0xFE **are ACKed** on the NHW20 (ack flag = 1 in the captures). [M]
  Whether 0xFF is ever used on the Prius is unknown.
* There is **no source ID field** [P]. The Beebird-Labs README claims a "SRC ID". That contradicts SAE 970297
  and the captures, so treat it as wrong. The sender can be identified only through the MES-ID, through the
  per-node status frames (§4.2.3), or by electrical isolation.

### 3.4 CRC-8 (worked example) [M]

Algorithm: `crc = 0; for each byte b in [PRI<<4|ML, DST, MES, DATA…]: crc ^= b; 8× { crc = (crc & 0x80) ? (crc<<1) ^ 0x13 : crc<<1 }`.
The table form starts `00 13 26 35 4C 5F 6A 79 98 8B …` (identical in BeanMPX, specadmin and mck1117).

**Example 1.** Frame from the drive2 article, captured on a real bus: PRI = 1, ML = 3, DST = FE, MES = 24 (vehicle
speed), DATA = 00.

| byte in | CRC before | CRC after |
|---|---|---|
| 0x13 (PRI 1, ML 3) | 0x00 | 0x16 |
| 0xFE | 0x16 | 0x59 |
| 0x24 | 0x59 | 0x2E |
| 0x00 | 0x2E | **0xB4** |

The bits on the wire are shown below; `[x]` is a stuffed bit and RR is the response slot. This reproduces the raw
bit string published by the drive2 author exactly:

```
SOF PRI  ML   DST-ID        MES-ID     DATA          CRC             EOM       RSP EOF
1   0001 0011 111[0]11110   0010 0100  000[1]00000   [1]1011 0100    01111110  RR  000000
```

(Read the stuffing left to right. The two 1s at the end of ML plus the first three 1s of DST make five 1s, so a
0 is inserted. The two trailing 0s of MES plus the first three 0s of DATA make five 0s, so a 1 is inserted. That
stuffed 1 starts a new run, so the remaining five 0s of DATA trigger another stuffed 1 before the CRC. The
stuffed bit counts as the first bit of the next run.)

**Example 2.** A real NHW20 frame: `PRI 6, ML 3, DST FE, MES 2C, DATA 64` gives CRC **0xC1**. This is coolant
temperature 0x64/2 = 50 °C, captured right after IG-ON:

```
1 0110 0011 111[0]11110 0010 1100 0110 0100 1100000[1]1  01111110 RR 000000
```

Other verified frames (CRC recomputed): toymods `64 98 CC 02 A8 →35`, `53 D2 C8 00 →E9`,
`63 D2 2C 00 →B5`; drive2 `25 FE 7F 01 00 00 →3A`; NHW20 `84 FE C2 E9 A1 →2B`.

### 3.5 RSP / ACK field

What the primary source says [P]: RSP is 2 bits, "ACK: acknowledgment / NAK: non-acknowledgment". EOM
"controls the time to prepare the response". The sender resends up to 3 times on error.

What implementations do. They disagree on naming, but the two that drive the bus agree on timing:

| Source | Slot after EOM's final 0 | Interpretation |
|---|---|---|
| specadmin/toyota-mpx (AVR driver with TX; ported to STM32 by aselafernando) | samples RSP1, RSP2, then expects a low bit | `RSP = 0,1` → **ACK**; `0,0` → no ACK (NACK); `1,1` → "bad CRC" |
| fiztech BeanMPX (Arduino, used on a real car) | a receiver that accepts drives `0,1,(0)`; on CRC failure it drives `1,0,(0)` | `01` = ACK, `10` = NAK; the TX side retries (2 retries) unless it reads exactly ACK |
| mck1117 bean-analyzer (Saleae decoder) | reads 2 bits after EOM | header comment says "10 = ACK, 01 = NAK", but it never evaluates them |

Working conclusion [M]/[I]: **ACK = the receiver drives the 2nd RSP bit dominant (RSP = `01`); NAK = the 1st
RSP bit dominant (`10`). Both bits high (`11`) is what you would see when one node ACKs and another NAKs.**
This is consistent with the NHW20 captures. The very first frame after wake-up (`FE C2 E9 A1`) was flagged
"no ACK" by the logging adapter and repeated 14 ms later, after which it was ACKed. So retransmission on a
missing ACK happens on the real Prius [M].

Who ACKs [M]/[I]: BeanMPX ACKs only frames whose DST-ID is in a configured list. That implies the addressed node
ACKs, and for 0xFE broadcast at least one node does. The ACK slot is wired-OR, so several receivers ACKing in the
same bit slot produce the same waveform (harmless if aligned). **A pure listener must never ACK.** If the real
addressee is missing or sick, your ACK would suppress its sender's retries and hide the fault. If your ACK is
misaligned by part of a bit, it bleeds into the EOF, and the sender can classify that as an error (specadmin
treats "third bit not low" as an unknown error). Whether a third-party ACK on the Prius actually causes DTCs or
retries is **not known** (gap).

### 3.6 Arbitration, collisions, inter-frame space

* CSMA/CD with bit-wise arbitration [P] (SAE Table 1). A transmitter that sends recessive (0) but reads
  dominant (1) has lost. It stops driving and continues as a receiver (specadmin `arbitration_lost()`) [M].
  Because PRI is sent first, the higher PRI value wins. On a tie the larger ML wins, then the higher DST-ID,
  and so on [M].
* Bus-idle detection before SOF: implementations wait for **> 6 bit times low** (specadmin
  `same_bit_counter > 6`; mck1117 "6 low bits") [M]. Recommended: wait ≥ 7–8 bit times of continuous low
  before sending SOF, and treat any edge during that time as "bus busy". [I]
* Timing: the SAE simulation of an LS400 body network (about 41.5 % bus utilisation) gives a message delay of
  8.2 ms average / 15.5 ms maximum for the highest priority and 10.6 ms / 191 ms for the lowest. Transmission
  intervals are dimensioned for ≤ 40 % average utilisation [P]. Short frames last 6–8 ms (see §6).

### 3.7 Bit timing and tolerance

* Nominal 100 µs [P]. The SAE paper chose NRZ with bit stuffing "to increase the clock tolerance" and lists
  "larger clock tolerance" as a requirement, but gives **no numeric tolerance** (gap).
* Implementations: specadmin 10,000 bit/s with resync on every edge (half-bit timer re-armed). BeanMPX uses
  1661 cycles at 16 MHz = 103.8 µs (9.63 kbit/s), also with resync on every edge, and works on a real car.
  mck1117 resyncs on every edge [M]. Because there is a resync at least every 6 bit times, ±5 % is easily
  tolerated. **Measure the real bit time on your car** with a scope or with the PIO run-length capture in §7.

---

## 4. Address and message tables

### 4.1 Prius NHW20: who sends what over BEAN (signal names, no IDs) [P]

From RM1076E system-description tables. "Steering lock ECU" in the push-button-start table is the transponder
key ECU (immobiliser ECU) on this car. Signals marked *(via GW)* originate on CAN (HV ECU, ECM, skid control)
and reach BEAN nodes through the gateway. The manual lists them as "CAN" or "BEAN" inconsistently, but the meter,
body ECU, power source ECU and others are not CAN nodes (05-2243), so they must arrive on BEAN [I].

| Sender | Receiver | Signals | Page |
|---|---|---|---|
| A/C ECU | Meter | ambient temperature (low-temp warning), A/C AUTO/RECIRC/Fr DEF/Rr DEF indicator ON | 05-1678 |
| Meter | A/C ECU, power source ECU, body ECU, transponder key ECU | vehicle speed signal | 05-1678, 05-2125 |
| Meter | Gateway | ODO meter information | 05-1678 |
| Body ECU | Meter | tail cancel (rx), D-door courtesy (headlamp reminder), ACC switch, warning ON (HEAD/TAIL), high-beam ON, front fog ON, **meter illumination dimmer demand**, all-door courtesy warning | 05-1678 |
| Smart ECU | Meter | in-cabin key detection, **buzzer sounding demand** | 05-1678 |
| Transmission control ECU | Meter | P state, master warning (P control malfunction), shift position (N, P), master warning (shift warning), buzzer demand | 05-1678 |
| Transponder key ECU | Meter | **buzzer request** (key reminder: key in slot + D-door open, power OFF/ACC) | 05-1678, 05-2030, 05-2039 |
| HV ECU *(via GW)* | Meter | shift position P/R/N/D/B (05-1679 says "(BEAN)"), cruise indicator, master warnings, **READY indicator ON/blink + buzzer**, EV indicator | 05-1678/1679 |
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
| Transponder key ECU ↔ smart ECU | | smart control ON, steering-lock release activation; **encrypted code exchange over BEAN** (05-1919) | 05-1863, 05-1919 |
| Smart ECU | Body ECU | **lock signals to all doors**; **back door open signal** | 05-1894, 05-1916, 05-1923 |
| Body ECU | Theft warning ECU | door lock/smart/wireless signals, key-linked switch, diag erasure, door status, vehicle spec, IG/ACC, all-doors courtesy | 05-2202 |
| Theft warning ECU | Body ECU | **security signal: door lock and unlock, head lamp, hazard lamp, horn, dome lamp, tail lamp** | 05-2202 |
| Transponder key ECU | Body ECU | security indicator ON/OFF | 05-2088 |
| A/C ECU ↔ body ECU / GW | | vehicle spec, DTC clear, diag demand; A/C controls themselves go over AVC-LAN from the multi-display | 05-1019 |

Cross-check DTCs that matter for spoofing [P]: the power source control ECU compares hard-wired signals with
BEAN data and sets **B2281** (P signal, cable vs BEAN), **B2282** (vehicle speed), **B2284** (brake), **B2286**
(READY) and **B2289** (immobiliser) when they disagree (05-2143…2173).

### 4.2 Real NHW20 traffic (bean_tools captures, Japanese-market Prius) [M]

The captures were taken with the bean_tools adapter (unknown hardware, reports "ack" 0/1). Timestamps are
host-side at about 1–3 ms resolution. Periods are approximate. Long captures show jitter and some doubled
intervals, so the adapter probably dropped frames. Payloads are hex, DATA bytes only. "Seen in" uses
OFF = power OFF with a key event, IG = IG-ON, RDY = READY.

#### 4.2.1 Frames present with power OFF (bus awake after a key or door event)

| DST | MES | PRI | data len | period | payloads seen | meaning |
|---|---|---|---|---|---|---|
| FE | C2 | 8 (12) | 2 | burst at wake | `E9 A1`, `C1 80`, `B5 90`, `62 80`, `40 80`, `52 80` | wake/startup announcement, one per node, about 7 ms apart [I] |
| FE | AB | 0 / 4 | 2 or 11 | ~5/s total | `<node> <status>`: `40 80`, `62 00/80`, `B5 00/8F/B3`, `C1 00/80`, `E9 00/A0`; node 52 sends 11 bytes `52 80 20 20 00 00 C0 04 8E 07 80` | per-node "alive/status" frame; first byte = node address [I] |
| FE | 3F | 9 (1) | 4 | at wake, then every 10–27 s | `02 87 E9 80` | odometer: 0x0287E9 = 165 865 (km). bean_tools labels a 24-bit 0x3F value "mileage" [S]/[I] |
| FE | 53 | 1 | 6 | ~1 s | `49 00 08 20 00 00` (constant) | unknown (vehicle spec/config?) [I] |
| FE | 54 | 1 | 6 | ~1 s | `00 42 00 41 45 4E` (constant) | unknown |
| FE | 62 | 2 / 10 | 2 | ~1 s + events | `20/60/70/50/78/A0/A8/E0/E8 00` | changes with key and power state; power-mode/key flags? [I] |
| FE | 7F | 2 / 10 | 6 | ~1 s | `00 10 00 00 00 00`; `00 10 80 …` with a door open; `10/30/31 10 …` in RDY | meter lamp/indicator flags: b0.4 = clearance (tail) lights, b0.3 = high beam, b2.7 = door open (bean_tools Prius dashboard [S]; b2.7 matches the power-on capture [I]) |
| FE | 83 | 2 / 10 | 4 | ~1 s | `00/02/22/42/62 00 00 00/08` | unknown, changes with key/IG |
| FE | B6 | 2 | 2 | ~1 s | `C1 00`, `C1 40` | unknown (node C1 related) |
| FE | D8 | 2 / 10 | 2 | ~1 s | `00 00`, `04 00`, `0C 00`, `00 01` | unknown, changes with key/IG |
| FE | DB | 2 | 1 | ~1 s | `00` | unknown (on the RX300 0xDB = driver door state [S]) |
| FE | E1 | 2 | 3 | ~1 s | `00 00 00` | unknown |
| 40 | 59 | 2 / 10 | 2 | ~1 s | `00 C0` | unknown |
| 62 | 6E | 2 / 10 | 2 | ~1 s | `00 00`, `80 00`, `80 80` | unknown |
| B5 | D9 | 10 | 3 | 150 ms, for about 2.3 s, only in the "power on" capture | `8D CA 00` | unknown; linked to the power-switch action [I] |

#### 4.2.2 Additional frames after IG-ON / READY

These appeared about 5.5 s into the `acc_ign_on` capture (IG switched on). PRI 6 dominates.

| DST | MES | PRI | len | period | payloads | meaning |
|---|---|---|---|---|---|---|
| FE | 24 | 1 | 1 | ~1 s | `00` | vehicle speed, km/h (= 0, parked). Same ID on RX300 (`FE 24 23 80` = 35 km/h) and IS300 [M] |
| FE | 26 | 6 | 2 | ~1 s | `00 00` | engine speed. RX300/IS300 decode it as (D0·256 + D1)/5.12 rpm [M for other models; engine not running in these captures] |
| FE | 2C | 6 | 1 | ~2 s | `64`/`63` at IG, `5F`/`60` in RDY | coolant temperature = value/2 °C (50 °C, 47.5 °C). Same scaling on RX300, IS300, BeanMPX car [M] |
| FE | 40 | 6 / 14 | 3 | ~2 s | `80 00 00` P, `40 …` R, `20 …` N, `10 …` D, `00 …` | shift position bits P/R/N/D as on RX300 (bit7 P, bit6 R, bit5 N, bit4 D) [M]. B position not captured |
| FE | 91 | 6 / 14 | 3 | ~0.6–1 s | `A0 3F 06` before READY, `80 20 00` at READY | HV/READY-related status [I] |
| FE | 29 | 6 / 14 | 6 | ~2 s | `00 00 04/0C/84/8C/2C 00 00 00` | unknown |
| FE | 50 | 2 / 10 | 1 | ~1.5 s | `84`, `0C`, `04`, `80`, `86`, `82` | unknown |
| FE | CD | 2 | 1 | ~2 s | `47`, `46` | ambient temperature = value − 48 °C (23 °C, 22 °C) per the IS300/RX300 scaling [M for other models, I for Prius] |
| FE | D4 | 6 | 1 | ~2 s | `00` | warning flags on RX300/IS300 (starter, no-charge, oil pressure) [S]; always 0 here |
| FE | FC | 2 / 10 | 8 | ~7–14 s | `41 00…`, `C3 00 02 00 00 20 00 00`, `49 00…` | on the RX300 0xFC = climate state [S]; Prius meaning unknown |
| 52 | 6D, 76, 90, 99, A2, A4, CA, D0, ED, FD | 1/2/9/10 | 1–5 | 1–16 s | e.g. `52 90 00 00 80`, `52 ED 08 00 FF 00 00`, `52 FD 39 38` | traffic addressed to node 0x52 (gateway? see §4.2.3) |
| 62 | 21, C8 | 6 | 1–2 | 0.6–2 s | `00`, `00 00` | to node 0x62 |
| 98 | 27, B2, CC, EC, F8 | 6 / 14 | 1–6 | 0.5–15 s | `98 CC 01 94/95`, `98 EC 00 00 00 00 24 3F` (varies in RDY) | to node 0x98 (IG-powered node) |
| FE | AB | 0 / 4 | | | adds nodes `19 80`, `98 80`; in RDY `E9 83`, `B5 82`, node 52 `52 80 B0 20 00 00 C0 06 8E 07 80` | node status [I] |

Frame rate: ~17 frames/s with power OFF (bus awake); 35–44 frames/s for the first 10–20 s after IG-ON;
steady ~15 frames/s at READY while parked [M, from the captures].

#### 4.2.3 Node addresses on the NHW20 (hypotheses) [I]

* Addresses that appear as the first byte of the `FE AB` status frames and of the `FE C2` wake burst:
  **0x40, 0x52, 0x62, 0xB5, 0xC1, 0xE9** (present with power OFF) and **0x19, 0x98** (only after IG-ON).
  That is 8 nodes. The Prius BEAN has 7–9 nodes depending on options (§1), so this fits [I].
* 0x52 receives the odometer-like and many other frames. RM says the gateway receives "ODO meter information"
  from the meter. 0x52 = gateway is plausible but **unverified** [I].
* On other models, 0x62 is the destination for meter-bound frames (BeanMPX car, IS300: fuel, coolant, ECT and
  seat-belt frames go to DST 0x62), and on the IS300 the engine ECU announces itself as `FE AB 40 80` [S].
  The Prius has no ECM on BEAN, so do **not** carry these mappings over without testing.
* Toyota diagnostic CAN requests to the "main body" ECU use CAN ID 0x750 with first byte 0x40 (PriusChat
  reports for door lock/unlock [S]). Whether the 0x750 sub-address equals the BEAN node address (body ECU =
  0x40?) is an open question. Test it (§8).

### 4.3 Other Toyota/Lexus models (for orientation only; IDs may differ on the Prius)

| MES-ID (DST seen) | Meaning | Vehicle | Source |
|---|---|---|---|
| 0x15 (D2) | climate panel buttons (AUTO, OFF, REC, FRONT, Rr DEF, A/C, MODE) | Lexus RX300 / Harrier (meter 83800-48010) | specadmin [S] |
| 0x1A (46), 0x1B (45) | climate TEMP/blower encoder steps, blower level | RX300 | specadmin [S] |
| 0x20 (46) | momentary fuel rate | RX300 | specadmin [S] |
| 0x24 (FE) | vehicle speed km/h; b1.7 = meter power ON | RX300, IS300 (and NHW20, §4.2) | specadmin, IS300 repo [M] |
| 0x25, 0x76, 0x90, 0xA3 (45) | average speed, trip distance, momentary and average consumption; units in the last byte or in 0x6A | RX300 | specadmin [S] |
| 0x26 (D2/FE) | tachometer = (D0·256 + D1)/5.12 rpm | RX300, IS300 | specadmin, IS300 [M] |
| 0x2C (FE/62) | coolant = value/2 °C | RX300, IS300, BeanMPX car, NHW20 | [M] |
| 0x40 (FE/62) | gear: P R N D 4/M 3 2 L bits; D1 = manual-mode gear | RX300, BeanMPX car, NHW20 (P/R/N/D) | [M] |
| 0x7F (FE) | lamp indicators (clearance light) | unnamed Toyota (drive2: `25 FE 7F 01 00 00` lit the clearance indicator); NHW20 (bean_tools: b0.4 clearance, b0.3 high beam, b2.7 door) | [S] each, layouts differ |
| 0x96 (E0) | back door unlocked | RX300 | specadmin [S] |
| 0xA4 (62) | fuel level / consumption | BeanMPX car, IS300 | [M] |
| 0xCD (FE) | outside temperature = value − 48 °C | RX300, IS300 | [M] |
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
| 0xAB (FE) | `40 80` "unknown" | IS300 (emulated by the engine-ECU replacement) | IS300 repo [S]; NHW20 node-status frame [I] |

---

## 5. What can be driven from an extra node, and what to avoid

### 5.1 Realistic, low-risk candidates

Ranked by how likely they are to work and how safe they are [I]:

1. **Body-ECU actuations that Toyota itself triggers over BEAN.** The RM shows the body ECU acting on BEAN
   requests from other ECUs: the smart ECU sends "lock signals to all doors" and a "back door open signal" to
   the body ECU, and the theft warning ECU sends a "security signal" for door lock/unlock, head lamp, hazard,
   horn, dome lamp and tail lamp (European RHD only) [P]. These are the best candidates for an extra node,
   but **the message IDs are unknown**. They must be captured on a car where the sending ECU exists. On a
   European LHD car with smart entry, capture smart lock/unlock and back-door open (§8). The theft-warning
   commands will not exist on an LHD car (theft deterrent ECU is "standard equipment only for European RHD").
2. **Diagnostic active tests (the documented path).** Techstream active tests for the multiplex network body ECU
   are **Door Lock (lock/unlock all), Hazard (on/off), Trunk/Bdor Open, Security Indic** [P] (05-1787, 05-2214).
   The power source control ECU offers Key Lock, Lighting Ind and Ind Condition (green/red), and the A/C ECU
   offers blower, dampers, compressor target speed, rear defogger, water pump and electric fan [P]. The tester
   reaches BEAN ECUs through the gateway, which converts diagnostic frames into BEAN frames (SAE 1999-01-1278
   describes the ISO 9141→BEAN conversion; on the Prius the DLC3 is CAN, ISO 15765-4) [P]. Two consequences:
   * You can trigger the same actions from the **CAN side** with Toyota-style diagnostic requests (0x750 +
     sub-address 0x40 "main body" is reported for door lock/unlock [S]) without transmitting on BEAN at all.
   * Capturing BEAN **while** a tester runs these active tests reveals the BEAN-level diagnostic frames (§8). Replaying
     them on BEAN may need the full diagnostic session sequence (SAE 970297: diagnostic messages "sent only when in
     the diagnosis mode") [P]/[I].
3. **Meter indications.** On the NHW20, `FE 7F` bits drive the clearance-light, high-beam and door indicators
   (bean_tools actions) [S]. **The real owner of that frame resends it about every 1 s**, so a spoofed state is
   overwritten within a second and the lamp flickers. This is only useful as a test that your TX works, or on a
   bench meter. The buzzer is requested over BEAN by the transponder key ECU, smart ECU and transmission control
   ECU [P], so a buzzer request frame exists, but its ID is unknown.
4. **A/C.** On the Prius, A/C operation comes from the multi-display over **AVC-LAN** [P] (05-1019). The project
   already speaks AVC-LAN, so do A/C requests there, not on BEAN.

Power windows: the RM power-window section does not show BEAN commands (master switch → motors). Not a
candidate without capture evidence [I].

### 5.2 Dangerous or pointless: do not transmit

| Traffic | Why | Confidence |
|---|---|---|
| Transponder key ECU ↔ smart ECU ↔ power source ECU immobiliser frames | Encrypted code exchange over BEAN (05-1919). Replay is pointless, and interference can block READY (B2289 "cable and BEAN abnormal … immobiliser") | [P] |
| Shift/P-state (transmission control ECU ↔ power source ECU), vehicle speed, brake, READY | Cross-checked against hard-wired lines → B2281/B2282/B2284/B2286. Auto-P and power-off logic depend on them | [P] |
| Anything with a high PRI or flooding | Delays real traffic; a node silent towards the gateway for > 10 s sets B12xx "communication stop" (B1200, B1207, B1210, B1260, B1262, B1269, B1271, B1294) | [P] |
| Holding the line dominant (TX stuck on) | Equivalent to a +B short → B1214, "related system(s) do not operate" (meter, key, power source) | [P] |
| Holding the line low hard (e.g. a low-impedance RX input) | GND short → B1215 | [P] |
| ACKing frames as a passive listener | Hides missing nodes, may produce `11` responses → retries | [I] |
| Waking the bus while parked | Every ECU wakes (SAE: wake-up by "detecting voltage changes on the transmission line"); repeated wake-ups drain the 12 V battery | [P] mechanism, [I] consequence |
| FE C2 / FE AB look-alikes (node announcements) | They look like network management. Spoofing one could make the gateway believe a node is alive when it is not, or the opposite | [I] |

TX safety rules [I]: hardware default recessive; firmware watchdog that releases TX if it is dominant for longer
than 700 µs (no valid frame has more than 6 dominant bit times in a row); arbitration-loss detection on every
recessive bit; at most 3 attempts per frame; a frame rate limit (≥ 50 ms between own frames); never transmit
until a valid idle period (≥ 7 bit times low) has been seen; low PRI (1–2) unless copying an existing frame's
priority.

---

## 6. What an idle listener should expect on a healthy NHW20

* **Frame durations [I] (computed).** Bits = 33 + 8·ML + stuff bits (+ EOF already included). ML = 3 (1 data
  byte): about 58 bits ≈ 5.8 ms. ML = 4: ≈ 6.6 ms (matches the 7 ms spacing of the `FE C2` burst). ML = 13:
  138–160 bits ≈ 14–16 ms.
* **Power OFF, car locked.** Bus asleep (0 V, no edges) is expected, because SAE describes a power-saving mode
  with wake-up on line activity [P]. The NHW20 captures all begin with the wake burst [M], which is consistent
  with a bus that was asleep. **The time until the bus falls asleep again was not found** (gap).
* **Wake-up (key inserted/removed, door, power button).** A burst of `FE C2 <node> <status>` frames about 7 ms apart,
  one per node (E9, C1, B5, 62, 40, and 52 in one capture). The first one is retransmitted if it gets no ACK.
  Then `FE 3F` (odometer), `FE 62`, `40 59`, `FE 7F`, followed by the 1 s periodic set of §4.2.1 and `FE AB`
  status frames at about 5/s. That gives about 17 frames/s [M].
* **Power button pressed (power OFF → ON):** `B5 D9 8D CA 00` every 150 ms for about 2.3 s [M]; meaning unknown.
* **IG-ON:** nodes 0x19 and 0x98 appear. PRI-6 gateway-style data starts: speed `FE 24`, rpm `FE 26`, coolant
  `FE 2C`, shift `FE 40`, `FE 91`, ambient `FE CD`, plus traffic to 0x52/0x62/0x98. Rate 35–44 frames/s for
  10–20 s, then about 15/s [M].
* **READY:** `FE 91` changes from `A0 3F 06` to `80 20 00`; node status `E9 83`, `B5 82`; `98 EC` payload starts
  changing [M].
* **Door open:** `FE 7F` byte2 bit7 set (seen in the power-on capture) [I]. The body ECU also reports courtesy
  switches to the meter, power source ECU, smart ECU and theft ECU [P].
* **Error frames:** none observed. All 8,627 captured NHW20 frames had a valid CRC [M].

---

## 7. RP2040 implementation notes

### 7.1 Receive (recommended architecture) [I], drawing on what the reference implementations do [M]

**Do not sample with a free-running UART-style clock.** Every reference implementation resyncs on every edge
(BeanMPX `setTimerCounter()` on every pin change; specadmin `resync_timer()` arms a half-bit timer on each edge;
mck1117 "if there's a transition in the next bit time, resync on it, then advance half a bit").

Simplest robust PIO design: **edge-to-edge run-length capture.**

* A PIO state machine counts loop iterations while the input is stable and pushes `(level, length)` to the RX
  FIFO on every edge. For example, a 2-instruction loop at 1 MHz gives 1 µs resolution, and a timeout pushes
  "idle" after 1 ms of no edge. Use DMA to a ring buffer.
* The CPU converts each run to a bit count `n = round(length / T_bit)`, where `T_bit` is about 100 µs and can be
  refined by averaging single-bit runs inside frames. Valid runs are 1–5 bits inside SOF..CRC and up to 6 in
  the EOM. Anything longer than 6 low bits is idle. Because each run is measured from edge to edge, clock drift
  cannot accumulate (≤ 6 bits per run gives ±0.5 bit margin up to about ±8 % clock error).
* Frame decode state machine:
  1. Wait for idle (≥ 7 bit times low), then a rising edge = SOF.
  2. Expand runs into bits. **Destuff**: after 5 equal bits, the next bit must be the inverse; drop it.
     A 6th equal bit means: 1s → EOM reached (expected only after CRC); 0s → frame error, so resync on idle.
  3. Collect PRI/ML (8 bits), then 8·ML bits, then 8 CRC bits.
  4. Find the EOM `0111111` + `0` without assuming a fixed position, to tolerate the unresolved CRC-ends-in-5
     stuffing case (§3.2).
  5. Read **RSP1, RSP2** (the next two bit slots) and then check that the following bits are low (EOF).
  6. Check CRC-8/0x13 over PRI/ML..DATA. Report errors, do not drop silently.
* Sampling point if you sample instead of measuring runs: 50 % of the bit after a resync edge (all references).
  Glitch filter: ignore pulses shorter than about 10 µs, because the trapezoidal edges are slow.

**Implementation (powerbox fw 2.3.0, 2026-11-13).** This design is
implemented in `powerbox/bean_rx.py`: PIO run-length capture at 1 µs
resolution, a DMA ring and the decoder steps 1–6 above. It also adds an
adaptive bit time, a dominant/recessive skew estimate and a 12 µs glitch
filter.
- The firmware protocol, counters and commands are documented in
  `powerbox/README.md` under "BEAN receiver".
- The backend keeps the frame table and capture logs (§7.3 equivalent, as
  JSON lines in `logs/bean/`): REST `bean_capture` / `bean_mark`,
  `GET /api/v1/bean/frames`.
- `tests/test_bean_rx.py` re-encodes all 8,627 bean_tools NHW20 frames on
  the wire and decodes them all.
- The stuff bit before the EOM (§3.2) is accepted both present and absent.

### 7.2 Transmit timing (for later) [M]/[I]

* Wait for idle, drive SOF, then send bits MSB first with stuffing, re-checking the bus at each recessive bit
  (arbitration loss → release, switch to receive, keep decoding the winner's frame as specadmin does).
* After the EOM (sent as 8 raw bits, no stuffing), release the line and sample RSP1 and RSP2 at mid-bit:
  `01` = ACK, `00` = no ACK, `10` = NAK, `11` = conflicting responses. Retry up to 2 more times.
* PIO side-set can drive TX while another state machine (or the same program) reads RX for the bit-by-bit
  comparison. Remember your own echo: on a single-wire bus you read your own bits back, so do not decode your
  own frames as foreign ones (tag frames that you sent).

### 7.3 Capture format for reverse engineering [I]

Log decoded frames as one text line each, with enough raw detail to re-analyse later. Keep a parallel optional
raw edge log (`.bin`, `uint32 t_us, uint16 len_us | level<<15`) for frames that fail to decode.

```
# BEAN v1  host=rp2040  tbit_us=100.4  start=2026-11-13T18:02:11+01:00
t_us=12345678 pri=6 ml=3 dst=FE mes=2C data=64 crc=C1 crc_ok=1 rsp=01 stuff_err=0 bits=58
t_us=12352011 pri=2 ml=8 dst=FE mes=7F data=00.10.80.00.00.00 crc=FF crc_ok=1 rsp=01
t_us=12360000 EVENT "driver door opened"
t_us=12391000 ERR kind=crc pri=2 ml=4 dst=FE mes=62 data=60.00 crc=xx
```

* `t_us` = timestamp of the SOF rising edge (RP2040 64-bit µs timer).
* `rsp` = the two raw RSP bits. Never interpret them in the logger.
* `EVENT` lines come from a push-button GPIO or a serial command, so that each action in the capture plan is
  marked in the log.
* Keep the format line-oriented and grep-friendly. A host script can group by `dst/mes`, show per-byte changes
  and compute periods, as bean_tools' logger does.

To reuse the bean_tools dumps: `.dat` = an int64 start time followed by 40-byte records
`[8 B QDateTime d-pointer, junk][pri, ml, dst, mes, data[11], crc, ack][7 B pad][int64 t_ms]`
(little-endian, x86-64 Qt5 struct dump). Parsing them this way gives 0 CRC errors.

---

## 8. Gaps and capture plan

### 8.1 What could not be found

1. Any Prius-specific BEAN message dictionary. Only about 6 IDs have meanings, borrowed from other models (§4.2).
2. The mapping of BEAN node addresses (0x19, 0x40, 0x52, 0x62, 0x98, 0xB5, 0xC1, 0xE9) to ECUs.
3. The bit-level ACK semantics from a primary source (only implementations agree on `01` = ACK), what happens if
   a third node ACKs, and the exact IFS length.
4. The stuffing rule when the CRC ends in 5 equal bits.
5. Bit-time tolerance, termination current, receiver threshold, driver slope (electrical spec).
6. Sleep behaviour: timeout to sleep, which events wake the bus, whether any node sends while the car is locked.
7. The meaning of PRI values that differ only in bit 3 (the same message is seen with PRI 2 and 10, 6 and 14,
   1 and 9, and `FE AB` with 0 and 4). It may be a retransmission or event flag. Unknown [I].
8. The message IDs for: door lock/unlock commands (smart ECU → body ECU), back-door open, buzzer request,
   meter dimmer, power mode (IG/ACC), seat belt, turn signal and hazard state, headlight switch state, key
   in slot, READY.
9. BEAN-level diagnostic frames (gateway ↔ body ECU) used by Techstream active tests.

### 8.2 Capture plan

Listen only, TX hardware disconnected or disabled. Log with `EVENT` marks. Repeat each action 3× with about 5 s of
quiet between repeats, so that changes stand out from the 1 s periodic traffic.

| # | State | Actions to perform | What it answers |
|---|---|---|---|
| 0 | Bench/scope | Scope the bus at the meter (C10-24): measure bit time, levels, edge slope, RSP waveform position | gaps 3–5 |
| 1 | Car locked, sleeping | Log 10 min with no action; then unlock with the remote; wait until the bus sleeps again (note the time) | sleep timeout and wake source (gap 6) |
| 2 | Power OFF, door closed | Open/close each door separately; lock/unlock with the interior switch, the key cylinder and the remote; smart lock/unlock by handle touch (if fitted); back-door opener | door courtesy, lock position and lock command IDs (gap 8) |
| 3 | Power OFF | Insert/remove key (halfway switch); key in + driver door open (key-reminder buzzer) | key-in-slot ID, buzzer request ID |
| 4 | ACC → IG → READY → IG → OFF | Press the power button step by step, with 10 s per state | power-mode frames; node power domains (which node addresses appear when) |
| 5 | IG | Light switch: TAIL → HEAD → high beam → flash; fog; hazard; left/right turn; dash dimmer knob; seat belt buckle/unbuckle | lamp, indicator and dimmer IDs, FE 7F bit map |
| 6 | READY, foot on brake | P → R → N → D → B → P (stationary) | shift ID, B position bit |
| 7 | IG | A/C on/off, AUTO, recirc, front/rear defog (from the MFD) | A/C status → meter indicator IDs |
| 8 | Driving (passenger operates the laptop) | 0 → 50 km/h → stop | speed (FE 24) and rpm (FE 26) scaling on the Prius |
| 9 | IG, tester on DLC3 | Run Techstream active tests: body ECU Door Lock, Hazard, Trunk/Bdor Open, Security Indic; read DATA LIST | BEAN diagnostic frames and the body ECU's node address (gap 9, gap 2) |
| 10 | IG | Correlate node addresses: compare `FE AB`/`FE C2` node lists with which ECUs are powered in each state (§4.1 power supplies: meter IG2/+B, A/C +B, transmission control ECU +B, transponder key ECU IG …). Do not unplug ECUs on the live car; that sets B12xx DTCs | gap 2 |

After each session, diff the per-`dst/mes` payloads between the EVENT marks. Write every confirmed bit into a
Prius table here, with the capture file name as evidence.

---

## 9. Sources

Primary (Toyota / Denso):

1. H. Honda, S. Uehara, K. Sakai, T. Akatsuka (Toyota), S. Akiyama (Denso), **"Body Electronics Area Network
   (BEAN)", SAE 970297** (1997). Frame format (fig. 9), stuffing, CRC-8, ACK/NAK, 3× resend, CSMA/CD,
   physical layer, wake-up. https://frankensteinmotorworks.com/MKIII/2ARSwap/SAE970297.pdf (scan also contains
   the next paper); also in https://github.com/rusefi/rusefi_documentation/blob/master/PDFs/SAE970297.pdf and
   https://github.com/specadmin/toyota-mpx/tree/master/docs
2. A. Tanaka, M. Kamiya, K. Sakai (Toyota), T. Kishigami (Denso), **"Gateway Application for Automotive Network
   System 'BEAN'", SAE 1999-01-1278**. Gateway, diagnostic conversion, option-LAN security check (same PDF as 1).
3. **Toyota Prius Repair Manual RM1076E (2004, EU)**, section 05. Multiplex communication 05-2238…2300
   (system description 05-2242…2249, ECU terminals 05-2251…2258, DTC chart 05-2261, B1200 05-2262, B1214/B1215
   05-2274, the other B12xx pages); combination meter 05-1677…1679; power door lock data list/active test 05-1787;
   smart entry 05-1861…1863, 05-1894…1929; key reminder 05-2029…2040; engine immobiliser 05-2088; push button
   start 05-2124/2125, 05-2141, 05-2143…2173; theft deterrent 05-2202, 05-2214; A/C 05-1019, 05-1044.
   https://toyotamanuals.gitlab.io/PZ471-Z01S0-CA/htmlweb/rm/rm1076e/m_05_NNNN.pdf (NNNN = page; there is no
   index page; I enumerated 1–2330)
4. **Toyota Prius New Car Features NCF256E**, BE-46…49 (multiplex): https://toyotamanuals.gitlab.io/PZ471-Z01S0-CA/htmlweb/ncf/ncf256e/m_be_0046.pdf
5. Toyota training "Body Electrical – Multiplex Communication" ('04 Prius; CAN/BEAN/AVC-LAN table, one-way
   vs two-way type): `MPX_multi2.pdf` in https://github.com/specadmin/toyota-mpx/tree/master/docs
6. Toyota NCF SC430 "Multiplex communication system" (communication frame drawing, bus structure):
   `BEAN_ECUs.pdf` in the same folder
7. Toyota Prius repair manual "MP Multiplex Communication" (later NHW20, includes the TPMS ECU on BEAN; B1214/B1215
   "the related system(s) do not operate"): https://hybrids.ru/wp-content/uploads/2021/04/MP-Multiplex-Communication.pdf

Implementations and captures:

8. **haageduard/bean_tools.** Qt BEAN logger/toggler/player developed on a Prius NHW20 (JP). Real NHW20
   captures (`data/*.dat`), Prius dashboard (`data/prius_20.json`), frame DB. https://github.com/haageduard/bean_tools
9. **specadmin/toyota-mpx.** AVR asynchronous BEAN driver with TX, arbitration and ACK readback; `MPX-codes.md`
   message list (Lexus RX300 / Harrier). https://github.com/specadmin/toyota-mpx ; viewer:
   https://github.com/specadmin/MPX-viewer ; STM32 port: https://github.com/aselafernando/BeanMPX-STM32
10. **fiztech-code/BeanMPX.** Arduino BEAN library (CRC table, stuffing, ACK by DST list, 3× resend), example
    frames. https://github.com/fiztech-code/BeanMPX
11. **mck1117/bean-analyzer.** Saleae logic-analyser decoder (edge resync, stuffing, EOM detection).
    https://github.com/mck1117/bean-analyzer
12. daniel-dmitrzak/BeanMPX_IS300. Lexus IS300 sniffing and MPX↔CAN conversion, IDs and scalings.
    https://github.com/daniel-dmitrzak/BeanMPX_IS300
13. drive2.ru, "BEAN bus" (2018). Raw bit string with stuffing, CRC check with reveng, `FE 7F` clearance-lamp test
    frames. https://www.drive2.ru/b/494654889247376137/
14. Toymods forum, "Toyota BEANs, Multiplex etc", pages 1, 3, 4. Captured frames with CRCs (verified here), ring
    behaviour on IS200/LS400. https://www.toymods.org.au/forums/threads/39361-Toyota-BEANs-Multiplex-etc
15. rusEFI forum, "Just a collection of cool BEAN links": https://www.rusefi.com/forum/viewtopic.php?p=49513
16. Beebird-Labs/toyota-bean (2000 Crown, 5 V; contains errors: "SRC ID", "lower value = higher priority"):
    https://github.com/Beebird-Labs/toyota-bean
17. PriusChat, door lock via CAN 0x750 / 0x40 (reported; the page returned HTTP 522 during this research, so it
    was not read directly): https://priuschat.com/goto/post?id=1756198 , https://www.priuschat.com/posts/2472155
18. Prius Gen 2 CAN PID list (0x750 = "Main Body"):
    https://attachments.priuschat.com/attachment-files/2021/09/211662_Prius22009_CAnCodes.pdf
