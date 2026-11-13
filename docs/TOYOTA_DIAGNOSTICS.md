# Toyota diagnostics on the Prius NHW20 (Gen 2, 2004–2009)

Research and design notes for the all-ECU diagnostics engine
(`cyberpunk_computer/diag/`). Written 2026-11-13, without access to the car:
nothing below was measured on this vehicle. Every fact carries one of these
labels:

| Label | Meaning |
|---|---|
| **PRIMARY** | Toyota document, a real captured log, or code that is used on cars |
| **MULTI** | several independent sources agree |
| **SINGLE** | one report |
| **INFERRED** | reasoning from the cited facts |
| **UNVERIFIED** | plausible, no source found for this car |

`RM` = Toyota repair manual RM1076E (Prius NHW20, Europe), section 05, online
page group by page group at
`https://toyotamanuals.gitlab.io/PZ471-Z01S0-CA/htmlweb/rm/rm1076e/m_05_NNNN.pdf`
(NNNN = first page of the group; `RM 05-2308` is in `m_05_2308.pdf`).

## 0. Findings in short

1. **Only three ECUs do DTC communication over the DLC3 CAN on this car: the
   ECM, the HV control ECU and the battery ECU.** Every other ECU is diagnosed
   over SIL — DLC3 pin 7, a K-line. The body-side ECUs (body, power source
   control, smart key, transponder key, A/C, meter, transmission control) sit
   on BEAN behind the gateway ECU, and the gateway's diagnostic port is SIL too.
   The car computer has CAN only, so **a complete all-ECU reader needs a K-line
   interface on DLC3 pin 7** (section 8).
2. The CAN ids later Toyotas use (0x7B0 ABS, 0x7A1 EPS, 0x780 airbag, 0x7C0
   meter, 0x7C4 A/C, 0x727 transmission, 0x750 + sub-address for body ECUs) are
   **not established for the NHW20**; one scan database lists 0x7B0, 0x7C0 and
   0x750 as not answering on 2005/2007/2008 cars. The engine keeps them as
   opt-in one-frame probes.
3. **Why the existing scan finds nothing while the red triangle is on:** OBD
   modes 03/07 report emission-related codes — the ones that light CHK ENG. The
   HV control ECU's chart has a separate "master warning" column, and the codes
   that match the unplugged P switch (P0852, INF 580: *open in P position
   switch circuit*) light the master warning but **not** CHK ENG (RM 05-269,
   05-306). Such codes need Toyota's own read service.
4. The request bytes of Toyota's enhanced DTC read **over CAN on this
   generation were not found** in any public source. The engine tries the
   ISO 14230 / Toyota K-line form (`13`, then `13 FF 00`), read-only, and
   reports exactly what comes back.
5. The codes that belong to the unplugged power switch (B2278) and the parking
   lock side (C2315) live in BEAN/K-line ECUs and cannot be read with the
   present hardware.

## 1. Buses and ECUs

### 1.1 What is on which bus

| Fact | Source | Label |
|---|---|---|
| CAN members: ECM, HV control ECU, battery ECU, skid control ECU, power steering ECU, steering sensor, yaw rate sensor, gateway ECU, DLC3 | RM 05-2305…2309 (location, system diagram) | PRIMARY |
| "The ECM, hybrid vehicle control ECU, and battery ECU use the CAN communication system to perform DTC communication instead of the conventional communication line (SIL)." | RM 05-2310 | PRIMARY |
| Skid control ECU and power steering ECU: "DTC communication uses the SIL line"; gateway ECU: "connected to the CAN communication system but CAN communication system DTCs are not output" | later NHW20 manual, CAN section, [jaygroh CA] | PRIMARY |
| BEAN members: body ECU, meter ECU, A/C ECU, transponder key ECU, power source control ECU, transmission control ECU, theft deterrent ECU, smart ECU, gateway ECU | RM 05-2242…2244; meter system diagram RM 05-1677 | PRIMARY |
| "The gateway ECU connects the CAN, BEAN, AVC-LAN and the DLC3" | 2004 New Features manual BE-9 (via the web lane) | PRIMARY |
| Gateway ECU pin `SIL (G1-7)`: "Bus + line, pulse generation during transmission" | RM 05-2251 | PRIMARY |
| DLC3 tables of the body-side systems (push button start, multiplex, door lock, theft deterrent, key reminder) list **SIL (7)** as the only communication pin | RM 05-2139, 05-2259, 05-1785, 05-2213, 05-2037 | PRIMARY |
| Transmission control ECU pin `SIL (T4-20)`: "Diagnosis tester"; power steering ECU `SIL (P8-2)`: "Diagnosis communication"; airbag sensor `A18-16 SIL DLC3` | later NHW20 manual, HX / PS / RS sections, [jaygroh] | PRIMARY |
| A tester reaches the BEAN-only ECUs through the gateway ECU's SIL port | not stated by Toyota in words | INFERRED |

### 1.2 ECU table as implemented (`diag/ecus.py`)

| Key | ECU | Bus for diagnostics | CAN request / response | Sub-address | In a default scan | Confidence |
|---|---|---|---|---|---|---|
| `engine` | ECM | CAN | 0x7E0 / 0x7E8 | – | yes | PRIMARY + MULTI (captures: `7E8 04 61 21 00 00`; Torque sheet; OBDb) |
| `hv` | HV control ECU | CAN | 0x7E2 / 0x7EA | – | yes | PRIMARY + MULTI (2009 capture: `02 21 C3` → first frame `10 27 61 …`) |
| `battery` | battery ECU | CAN | 0x7E3 / 0x7EB | – | yes | PRIMARY + MULTI |
| `brake` | skid control ECU | SIL (K-line) | probe 0x7B0 / 0x7B8 | – | probe only | address UNVERIFIED (later Toyotas); OBDb: unsupported on 2005/07/08 Prius (SINGLE) |
| `eps` | power steering ECU | SIL | probe 0x7A1 / 0x7A9 | – | probe only | UNVERIFIED |
| `airbag` | airbag sensor assembly | SIL | probe 0x780 / 0x788 | – | probe only | UNVERIFIED |
| `transmission` | transmission control ECU (P lock) | SIL (own pin) + BEAN | probe 0x727 / 0x72F | – | probe only | UNVERIFIED |
| `meter` | combination meter | BEAN via gateway SIL | probe 0x7C0 / 0x7C8 | – | probe only | UNVERIFIED; OBDb: unsupported (SINGLE) |
| `ac` | A/C amplifier | BEAN via gateway SIL | probe 0x7C4 / 0x7CC | – | probe only | UNVERIFIED |
| `body` | body ECU | BEAN via gateway SIL | probe 0x750 / 0x758 | 0x40 | probe only | sub-address PRIMARY for 2010+ cars, UNVERIFIED here; OBDb: 0x750 unsupported (SINGLE) |
| `gateway` | gateway ECU (B12xx multiplex codes) | SIL | probe 0x750 / 0x758 | 0x5F | probe only | 0x5F = "central gateway" of 2016+ Toyotas; UNVERIFIED here |
| `power_source` | power source control ECU | BEAN via gateway SIL | none known | – | never | – |
| `smart_key` | smart key ECU | BEAN via gateway SIL | none known | – | never | – |
| `immobiliser` | transponder key ECU | BEAN via gateway SIL | none known | – | never | – |

The ids already in `comm/solicited_can.py` (`ECUAddress`): ENGINE, HYBRID,
HV_BATTERY and their response ids are **verified**; TRANSMISSION 0x727,
MAIN_BODY 0x750, AIRBAG 0x780, STEERING_ASSIST 0x7A1, ABS_BRAKE 0x7B0,
INSTRUMENT 0x7C0, AIR_CONDITIONER 0x7C4 and the others come from a generic
table (the web lane traced it to a non-NHW20 paper) and are **not verified for
this car**. The legacy `dtc` scan's 0x780 request is expected to stay
unanswered.

Sub-addresses found for later Toyotas (for reference, none NHW20-verified):
0x40 body control module, 0x5F central gateway, 0x2C electronic parking brake,
0xC7 telematics, 0x6D camera, 0x0F radar ([opendbc values]), 0x2A tyre
pressure ([litedash], [tpms-relearn]). They coincide with ISO 14230 K-line
node address ranges, which suggests the 0x750 scheme is Toyota's K-line
addressing carried over CAN on later platforms (INFERRED).

## 2. Diagnostic services

### 2.1 OBD (ISO 15031-5 / SAE J1979 on ISO 15765-4) — ECM, HV ECU, battery ECU

| Service | Request | Positive response | Use | Label |
|---|---|---|---|---|
| 01 current data | `01 <pid>` | `41 <pid> <data>` | PID 01 = MIL + DTC count + readiness; 0D speed; 31 km since clear | answered on 7E0/7E2/7E3: SINGLE (OBDb); 0x7E0: existing repo traffic |
| 02 freeze frame | `02 <pid> <frame>` | `42 <pid> <frame> <data>` | PID 02 = the code that stored the frame; PID 00 = supported PIDs | standard |
| 03 stored DTCs | `03` | `43 <n> <hi lo>…` | emission-related confirmed codes | standard; RM 05-25 |
| 07 pending DTCs | `07` | `47 <n> <hi lo>…` | "Continuous Test Results (Mode 7 for SAE J1979)" | RM 05-25, PRIMARY |
| 09 vehicle info | `09 02`, `09 04` | `49 02 <n> <17 ASCII>`, `49 04 <n> <16 ASCII each>` | VIN, calibration ids | support on a 2004 EOBD ECM UNVERIFIED |
| 04 clear | `04` | `44` | clears codes, freeze frame, readiness | standard; RM 05-25 "Erase DTCs and freeze frame data" |

Worked examples used in the tests: `43 01 04 20` → P0420; `41 01 81 07 65 00`
→ MIL on, 1 code; `42 02 00 04 20` → freeze frame stored by P0420.

Negative response: `7F <service> <code>`; an OBD ECU may also simply not answer
an unsupported PID. `7F xx 78` = response pending (the real answer follows).

### 2.2 Toyota enhanced services (ISO 14230-3 / KWP2000 service ids)

| Service | What is known | Label |
|---|---|---|
| `21 <id>` read data by local identifier | NHW20 HV ECU / battery ECU / ECM, response `61 <id> <data>`; multi-frame for 21C3 etc. | PRIMARY (captures, existing repo traffic) |
| `1A 88 01` ECU identification | Toyota KWP-style ECUs answer `5A 88 01 …`, preceded by tester present `3E` → `7E` | PRIMARY for later Toyotas ([opendbc]); UNVERIFIED on NHW20 |
| `13` read DTCs | K-line captures on a Corolla E11: request `13`, `13 00 00` or `13 FF 00`; response `53 <hi lo> <hi lo> …` **without a count byte**, zero-filled (`53 01 30 00 00 00 00` = P0130; `53 52 71 52 72 52 73` = C1271–C1273) | PRIMARY for that car on K-line ([hit-karlsruhe]); **on NHW20 CAN: UNVERIFIED** |
| `13` per ISO 14230-3 | `13 <group hi lo>` → `53 <count> <hi lo>…` | standard |
| `12` freeze frame, `17` status of DTC, `18` DTCs by status, `14` clear | exist in ISO 14230-3; no NHW20 CAN log found; not sent automatically | UNVERIFIED |
| `30 …` active test (IO control) | Toyota's active tests: `07B0: 05 30 21 02 FF 01` (ABS solenoid), `07E0: 06 30 1C 00 0F A5 01` (fuel cut) on a 2010 Prius | PRIMARY for Gen 3 ([Miller-Valasek]); **never sent** |
| UDS `19` / `22` | later Toyotas (hybrid at 0x7D2); not this generation | INFERRED |

Because the `53` response does not say whether it starts with a count byte,
`services.kwp_parse_dtcs` decides by length: an odd number of bytes after
`0x53` = count + pairs, an even number = pairs only; zero pairs are filler in
both layouts.

The **INF (information) codes** of the HV control ECU (three-digit detail
codes such as P0852-**580**) are shown by the Toyota tester on the freeze-frame
screen (RM 05-737 says the same of the brake ECU's detail codes). How they are
requested over CAN was not found. The dictionary lists every INF code of a DTC
with its detection condition, so a code read without its INF still narrows to a
handful of candidates.

### 2.3 Data lists (service 0x21)

The NHW20 identifiers with scaling are tabulated in `docs/prius_can.md`
(sections 5–7; source: the community Gen 2 Torque sheet [torque-sheet]). The
diagnostics engine exposes these by name through `diag_read`:

| ECU | Item | Request | Decoder |
|---|---|---|---|
| hv | `hybrid_system` | `21 C3` | MG1/MG2 rpm and torque, inverter and motor temperatures, HV voltage/current, shift sensor voltages |
| hv | `hybrid_additional` | `21 C4` | accelerator, VL/VH, converter temperature |
| hv | `cruise_control` | `21 D3` | cruise speeds and switches |
| battery | `battery_detail` | `21 CE` | SOC, current, 14 block voltages |
| battery | `battery_temps` | `21 CF` | raw (the repo parser returns nothing) |
| battery | `battery_resistance` | `21 D0` | internal resistances, min/max block |
| engine | `monitor_status`, `coolant_temp`, `short_fuel_trim`, `long_fuel_trim`, `rpm`, `speed`, `distance_since_clear`, `warmups_since_clear`, `vin` | `01 xx` / `09 02` | standard OBD scaling |

Any other read-only request can be sent as hex (`diag_read {ecu:"hv",
item:"21C4"}`).

## 3. ISO-TP (ISO 15765-2)

| Frame | Normal addressing | Extended addressing (0x750 / 0x758) |
|---|---|---|
| single | `0N <N bytes>` (N ≤ 7) | `<sub> 0N <N bytes>` (N ≤ 6) |
| first | `1L LL <6 bytes>` | `<sub> 1L LL <5 bytes>` |
| consecutive | `2S <7 bytes>` | `<sub> 2S <6 bytes>` |
| flow control | `30 <BS> <STmin>` | `<sub> 30 <BS> <STmin>` |

* Worked example, NHW20 HV ECU (2009 car, [gen2-2009]): request `02 21 C3` on
  0x7E2; first frame `10 27 61 …` (39 bytes announced) on 0x7EA; tester flow control `30 00 00`
  on 0x7E2; consecutive frames `21 …`, `22 …`. PRIMARY. After `30 00 00` the
  ECU streams all remaining frames.
* On 0x750 the response on 0x758 starts with the same sub-address byte
  ([opendbc uds.py]: `if rx_data[0] != self.rx_sub_addr: raise
  InvalidSubAddressError`); a tool for 2012+ cars sends the raw flow control
  `2A 30 00 00 00 00 00 00` ([tpms-relearn]). PRIMARY for those cars.
* Whether NHW20 ECUs honour BlockSize / STmin: not found.

## 4. DTC numbers

Two bytes (SAE J2012): bits 15–14 system (00 P, 01 C, 10 B, 11 U), bits 13–12
first digit, then three hex digits. `04 20` = P0420, `08 52` = P0852, `63 00` =
C2300, `A2 78` = B2278, `C1 00` = U0100, `0A 80` = P0A80. The same encoding is
used by OBD modes 03/07 and by service `13`.

The brake ECU's codes carry a second, two-digit Toyota code (`C1241/41`); it is
the blink code of the warning-lamp readout and is kept in the dictionary as
`toyota_code`.

## 5. Code dictionary

`cyberpunk_computer/diag/data/dtc_nhw20.json`, loaded by `diag/dictionary.py`,
generated by `tools/diag_dictionary/` from RM1076E section 05 (777 PDFs of the
2330 page numbers exist):

| Step | Extracts | Trust |
|---|---|---|
| `dtc_pages.py` | the `DTC <code> <TITLE>` header of every circuit page → code, title, system, page | high: 355 codes, text is the manual's own |
| `areas.py` | the "Trouble area" column of those pages (word coordinates) | good; 24 pages not parsed (no bullets / unsolved rows) — those entries have no trouble area |
| `hv_chart.py` | the HV control DTC chart RM 05-268…286: 289 rows of DTC, INF code, detection condition, trouble area, CHK ENG and master-warning columns | good; row extents are solved from the layout — verify on the chart page before acting |
| `build_dict.py` | merge + 10 hand entries for chart-only codes (flagged `"hand": true`) | – |

Coverage: 364 codes / 371 entries / 289 INF codes — engine 72, HV 77, battery
28, brake 58, airbag 49, A/C 22, EPS 19, push button start 13, shift control
12, multiplex 11, immobiliser 8, door lock 2. Not covered: codes that appear
only in running text (C127x test-mode codes, U0123/U0124/U0126 details), the
audio/navigation AVC-LAN device codes, systems without a DTC chart (smart
entry, power window, meter). Unknown codes are shown raw.

A code can exist in several systems with different meanings (U0100 in the HV
chart and in the battery chart); lookups take the reporting ECU.

## 6. The red triangle (master warning)

| Fact | Source | Label |
|---|---|---|
| The combination meter lights the master warning on demand signals: transmission control ECU "master warning light (P control malfunction) ON demand" and transponder key ECU "(shift warning)" over **BEAN**; HV ECU "(HV system, main battery, NDB warning, motor inverter, CHARGE)", ECM (oil pressure, coolant temperature) over CAN | RM 05-1677…1679 | PRIMARY |
| HV control ECU: "illuminates the master warning lamp … together with any of indicators on the multi-information display"; the chart marks per INF code whether CHK ENG and/or the master warning light | RM 05-242, 05-268…286 (`*2`) | PRIMARY |
| 272 of the 289 HV chart rows light the master warning; many of them do **not** light CHK ENG | dictionary (`master_warning`, `chk_eng`) | PRIMARY (machine-read) |
| Shift control: "When a DTC is detected, the master caution light comes on and an error message is indicated on the multi-display." | RM 05-929 | PRIMARY |
| Shift control codes C2300, C2301, C2303–C2307 "cannot be erased with the intelligent tester II": remove the P CON MAIN fuse (7.5 A) for 60 s with the power switch off | RM 05-924, 05-929 | PRIMARY |
| C2300/01/04/05/06/07/11/12 also set P3102 in the HV ECU; "the hybrid system DTCs must be cleared after clearing the shift control system DTCs" | RM 05-929 | PRIMARY |
| EPS: "the PS warning is indicated on the multi-display and the master warning light … comes on"; battery ECU: master warning + HV system warning | RM 05-986, 05-653 | PRIMARY |
| CHK ENG goes off by itself after 3 consecutive trips without the malfunction; "the DTCs remain recorded" | RM 05-242 | PRIMARY |
| Whether the master warning stays on with history-only codes, and how it latches per ECU | – | not found |
| CAN broadcast 0x529 carries "general problem, red triangle" bits | [gen2-2009] | SINGLE |

### The unplugged power switch and P switch

| Unplugged part | Where it is wired | Expected code | ECU | Lights | Readable now? | Manual |
|---|---|---|---|---|---|---|
| P position switch | resistor switch on HV control ECU terminal P1 | **P0852, INF 580** "Open or +B short in P position switch circuit" | HV control ECU | master warning yes, CHK ENG no | only through the Toyota read on 0x7E2 (unverified) | RM 05-306, chart 05-269 |
| | HV ECU reports the malfunction to the transmission control ECU | **C2315** "HV system malfunction" | transmission control ECU | master caution (RM 05-929) | no (K-line/BEAN) | RM 05-964 |
| Power switch | SS1/SS2 to the power source control ECU | **B2278** "Main switch (power switch) malfunction" | power source control ECU | not stated | no (BEAN) | RM 05-2159 |
| | STSW line to the HV ECU | B2275 "STSW monitor malfunction" (possible) | power source control ECU | not stated | no | RM 05-2156 |
| follow-ups | | P3102 (INF 524/525/581/582/597–599) in the HV ECU if the transmission ECU reports a fault; B2281 "P signal malfunction" | HV ECU / power source ECU | P3102: master warning yes | P3102 like P0852 | RM 05-590, 05-2161 |

The transmission control ECU's master-warning demand travels on BEAN, so the
BEAN receiver (powerbox lane) can observe it even though the ECU's codes cannot
be read.

## 7. Safety rules (enforced in `diag/services.py` and `diag/engine.py`)

| Class | Services | Rule |
|---|---|---|
| read-only | OBD 01, 02, 03, 07, 09, 0A; Toyota 12, 13, 17, 18, 1A, 21, 3E | allowed at any time, rate-limited |
| erasing | OBD 04 (Toyota 14 is recognised but not used) | only from `diag_clear {confirm:true}` or auto-clear; vehicle speed must be 0 (asked from the ECM, PID 0D; broadcast speed as fallback; unknown speed = refused) unless `allow_moving`; never during a scan; codes and freeze frame are read and logged first |
| forbidden | 08, 10 (session control), 11 (ECU reset), 20, 27 (security access), 28, 2C, 2E, 2F, 30 (Toyota active test), 31–38 (routines, programming), 3B, 3D, 83–87, A0–A9 | refused before the transport, whatever the caller asks |
| anything else | unknown service ids | refused (allow-list, not deny-list) |

* Active tests are the dangerous part of Toyota diagnostics — the Gen 3 ABS
  solenoid and fuel-cut tests need a stationary car in Park, others have "no
  restrictions as to when this test can occur" ([Miller-Valasek]). None is ever
  sent.
* No report of a Toyota ECU misbehaving on an unknown **read** request was
  found; the expected reaction is a `7F` refusal or silence.
* Bus load: one request outstanding at a time; ≥ 50 ms between requests; at
  most 20 transmitted frames per second including flow control (a frame is
  ≈ 0.26 ms at 500 kbit/s → < 1 % bus time including responses). A full default
  scan is about 20 request frames; a probe adds 8. An ECU that does not answer
  its first request gets no second one.
* Requests go to physical ids only (never the 0x7DF functional id), so exactly
  one ECU answers.
* 2026-11-06 incident (gateway firmware forcing a resetting MCP2515 into NORMAL
  mode): the engine adds no new transmit path — every frame is an ordinary
  gateway `req`, serialised by the firmware's single job engine.

## 8. Gateway firmware 3.2.9: what it supports, what is missing

Read from `cyberpunk-prius-gateway/gateway-rp2350/src/can_sol.c` and
`commands.c`; the gateway repo was not changed.

| Need | Firmware 3.2.9 | Used how |
|---|---|---|
| single-frame request, single/multi-frame response, normal addressing | yes: `req` with `"isotp":true` reassembles up to 256 bytes and sends flow control `30 00 05` to the request id | ECM / HV / battery (transport `gw`) |
| one job at a time, 5 ms gap, 8-entry one-shot queue, reply or `TIMEOUT` per job | yes | the engine sends the next request only after the reply |
| raw frame exchange | yes: `"isotp":false` returns the first frame with a matching id; but a timeout ≥ 300 ms silently turns ISO-TP on | 0x750 probes use `t=250` |
| extended addressing (sub-address byte before the PCI) | **no**: the ISO-TP parser reads byte 0 as PCI, flow control has no address byte | host-side reassembly: flow control with BlockSize 1 sent as a raw `req`, which returns exactly one consecutive frame; frames seen by the panda are merged in |
| multi-frame **requests** | no (one 8-byte frame per job) | not needed: every request is 1–3 bytes |
| listening without sending (late answer after `7F xx 78`) | no | only the panda tap can catch it |
| K-line (DLC3 pin 7) | **no hardware** | — |

Firmware changes that would remove the work-arounds (none is needed for the
three CAN ECUs):

1. `req` option `"xa": <byte>`: extended addressing — match and strip the
   address byte on receive, prefix it on flow control.
2. Honour an explicit `"isotp": false` for timeouts ≥ 300 ms.
3. Optional `"n": <count>` / "collect until timeout" for raw mode, returning
   every matching frame of one job.

For the K-line ECUs the missing piece is hardware: a K-line transceiver
(L9637D / MC33290 / a one-transistor level shifter) between DLC3 pin 7 and a
UART, plus a KWP2000 driver. The only protocol evidence is from another Toyota
(Corolla E11, [hit-karlsruhe]): ISO 14230 at 9600 baud with a modified fast
init (35 ms low / 15 ms high) and target addresses 0x29 (ABS) and 0x58
(airbag), 10400 baud for the engine ECU. The NHW20 parameters (init, baud rate,
node addresses of the gateway, transmission, EPS, skid control and airbag ECUs)
were **not found** and would have to be captured from a Toyota tester on this
car.

## 9. Implementation

| File | Role |
|---|---|
| `diag/isotp.py` | framing, reassembler (normal + extended addressing) |
| `diag/dtc.py`, `diag/services.py` | code numbers, request builders, parsers, safety allow-list |
| `diag/ecus.py` | ECU registry (table 1.2) |
| `diag/dictionary.py`, `diag/data/dtc_nhw20.json` | code dictionary |
| `diag/engine.py` | scheduler: jobs as generators, one request outstanding, timeouts, rate cap, result model, trace |
| `diag/autoclear.py` | auto-clear rule and monitor |
| `diag/report.py` | scan reports, clear log |
| `diag/commands.py`, `diag/actions.py`, `diag/wiring.py`, `diag/api.py` | REST commands, store actions, backend wiring, HTTP routes |
| `diag/sim.py` | ECU + gateway simulator |
| `scripts/diag_cli.py` | CLI: simulator dry run or the backend's REST API |
| `dashboard/src/tabs/DiagnosticsTab.tsx` | dashboard tab |

State (`state.diagnostics`): the legacy fields (`stored_dtcs`, `pending_dtcs`,
`mil_on`, `scan_in_progress`, `last_scan_time`) keep working — after a
`diag_scan` they hold the codes of every ECU, so the MFD DTC screen shows them
— plus `ecus` (per ECU: name, address, sub_address, protocol, bus, status,
responded, ident, dtcs[{code, status, sources, description, system,
manual_page, manual_url, trouble_area, master_warning}], mil, dtc_count,
monitors, freeze_frame, last_scan_ts, last_clear, error, log), `scan`
(progress, report file), `vehicle` (VIN), `last_read`, `autoclear`,
`diag_stats`.

REST (`POST /api/v1/commands/<name>`): `diag_scan {ecus?, kinds?, probe?}`,
`diag_identify {ecus?}`, `diag_read {ecu, item}`, `diag_clear {ecu,
confirm:true, allow_moving?}`, `diag_cancel`, `diag_autoclear_set {…}`.
`GET /api/v1/diag/report[?name=]`, `/diag/reports`, `/diag/clears`,
`/diag/ecus`. Reports are written to `<dir of the metrics db>/diag/`
(`PRIUS_DIAG_DIR` overrides): `diag_report_<date>_<time>.json`, `latest.json`,
`clear_log.jsonl`.

### Procedure for the next drive (IG ON or READY, parked)

1. `diag_identify {}` — VIN, calibration ids, `1A 88 01`. Shows which of the
   three ECUs answer and whether the Toyota identification service exists.
2. `diag_scan {"probe": true}` — reads the three CAN ECUs and sends one
   tester-present frame to each unverified address. Then
   `GET /api/v1/diag/report` (or `scripts/diag_cli.py --url … report --trace`).
3. Look at `ecus.hv`: codes with source `toyota 13` are the ones mode 03 does
   not show — P0852 / P3102 are the candidates. `ecus.hv.log` says what the ECU
   answered to `13` if no codes came.
4. If `13` was refused (`7F 13 11` service not supported, `7F 13 12` format):
   try by hand, read-only: `diag_read {"ecu":"hv","item":"13 FF 00"}`,
   `"13 00 00"`, `"18 00 FF 00"`, `"17 08 52"`; the raw answer is in
   `state.diagnostics.last_read`.
5. Clear only after the codes are on file and the switches are plugged back in:
   `diag_clear {"ecu":"hv","confirm":true}` (then `engine` / `battery` if they
   hold codes). Mode 04 on the HV ECU clearing non-emission codes is INFERRED
   from RM 05-243 ("the OBD scan tool also enables you to erase the DTCs"). The
   transmission control ECU's C23xx codes need the P CON MAIN fuse procedure.
6. If an ECU does not answer: keep the report (its `trace` has every request,
   the gateway's reply and the timing), the trip recording of that minute
   (gateway lines + panda frames 0x7E0–0x7EF) and the gateway `can_diag`
   counters (`tec`, `rec`, `txerr`, `to`).

## 10. Auto-clear of recurring codes (LPG / catalyst case)

Facts:

* OBD mode 04 clears **all** emission-related diagnostic information of the
  ECU: confirmed and pending codes, the freeze frame, the MIL request and the
  readiness (I/M) monitor status — never a single code (SAE J1979 /
  ISO 15031-5; RM 05-25 "Erase DTCs and freeze frame data"). After a clear the
  monitors read "not complete" until their drive cycles have run again, which
  an emissions inspection that reads readiness will see.
* ISO 15031-5 requires ECUs to answer service 04 with the ignition on and the
  engine not running; with the engine running an ECU may refuse with `7F 04 22`
  (conditions not correct). Stated from the standard's text, not re-read for
  this document (UNVERIFIED); whether the NHW20 ECM accepts a clear with the
  engine running was not found. The engine reports a refusal as such and backs
  off for the rate-limit interval.
* Whether a clear resets the ECM's learned fuel trims on this engine: not
  found. `long_fuel_trim` before the clear is in the freeze frame of every
  logged clear, and `diag_read {ecu:"engine", item:"long_fuel_trim"}` reads it
  afterwards, so the effect can be measured on the car.
* P0420 "Catalyst system efficiency below threshold (bank 1)" is in the ECM's
  chart (RM 05-139); trouble area: exhaust pipe with front and rear catalyst,
  gas leakage in the exhaust system, A/F sensor, heated oxygen sensor. The
  monitor compares the rear oxygen sensor with the A/F sensor. Which code this
  car's ECM actually stores is not known until a scan; no source was found that
  ties LPG operation to a specific code on the 1NZ-FXE. General P0420
  references name fuel-trim problems and misfires as contributors; P0171
  (lean) is the other code forum reports mention after gas conversions
  (anecdotal).

Design (`diag/autoclear.py`):

| Item | Behaviour |
|---|---|
| Whitelist | `{ecu, code, enabled}` entries + master switch (default **off**), persisted in `user_settings.json` under `diagnostics.autoclear` |
| Detection | only while CAN traffic is live (ignition on), every `poll_s` (30 s) and only for ECUs with enabled entries: `01 01` (MIL + stored count) — one request frame, one response frame. The code lists (03, 07, Toyota 13) are read only when the count is non-zero or the MIL is on, when the trigger is `pending`, while a clear is blocked or waiting, and every 10th poll. Idle cost: 2 frames per 30 s; a stored code is noticed within 30 s of the ECM confirming it |
| Rule | clear only when **every** stored and pending code of the ECU is whitelisted (including codes that only the Toyota read shows); otherwise `blocked`, with "auto-clear blocked by <code>" in `state.diagnostics.autoclear.blocked` |
| Trigger | `stored` (default): act when a whitelisted code is confirmed. `pending`: act already on a pending code — the MIL then never comes on, at the price of a clear on every first-trip detection |
| Speed | `stopped` (default): the ECM's PID 0D must read 0 km/h just before the clear; otherwise the clear is deferred to the next poll. `anytime` exists as a setting; no source supports it, so it is not the default |
| Limits | not during a scan; at most one attempt per `min_interval_s` (600 s) and `max_per_day` (6) per ECU; a refusal by the ECU counts as an attempt; limits survive a restart (read back from the clear log) |
| Log | before the clear: monitor status, km since the previous clear (PID 31), stored + pending codes, freeze frame (code, load, coolant, fuel trims, rpm, speed, …); then the clear's result, the codes read back, and the driving context (speed, READY, engine running, gear, km since previous clear). One JSON line per clear in `clear_log.jsonl`; `GET /api/v1/diag/clears` |

## 11. Not found / open questions

1. Toyota enhanced DTC read / INF code / freeze frame / clear request bytes on
   the NHW20's CAN ECUs — the central unknown; step 3–4 of the procedure
   settles it on the car.
2. Whether any non-powertrain ECU answers on CAN at all (the probe settles it).
3. K-line parameters of the NHW20's SIL ECUs.
4. Whether the master warning stays on for history-only codes.
5. Whether mode 04 on the HV control ECU also erases its non-emission codes.
6. Mode 09 (VIN) support of the 2004 European ECM.
7. Engine-running behaviour of mode 04 and its effect on fuel trims.
8. PriusChat thread texts were unreachable (HTTP 522) throughout the research;
   owner reports of this exact fault were not seen.

## Sources

* RM1076E section 05: `https://toyotamanuals.gitlab.io/PZ471-Z01S0-CA/htmlweb/rm/rm1076e/m_05_NNNN.pdf`
  — pages cited inline (0020, 0025, 0242, 0268, 0306, 0590, 0653, 0737, 0923,
  0924, 0929, 0964, 0986, 1677, 2139, 2143, 2159, 2242, 2251, 2259, 2261, 2305,
  2308)
* [jaygroh] later NHW20 repair manual, per section:
  `http://stuff.jaygroh.com/prius/Prius%20Info/Official%20Toyota%20Info/Repair%20Information/Repair%20Manual/`
  (CA - CAN Communication.pdf, and MP, HX, PS, RS, ME)
* 2004 manual RM1075U, CAN diagnosis system:
  `https://share.qclt.com/%E4%B8%B0%E7%94%B0%E6%99%AE%E7%91%9E%E6%96%AF%E5%8E%9F%E5%8E%82%E8%8B%B1%E6%96%87%E6%89%8B%E5%86%8CPDF%E6%A0%BC%E5%BC%8F/Repair%20Manual/04pruisr/05/20ubq/cisysdia.pdf`
* [gen2-2009] 2009 Gen 2 CAN logger notes:
  `https://attachments.priuschat.com/attachment-files/2021/09/211662_Prius22009_CAnCodes.pdf`
* [torque-sheet] Gen 2 Torque PID sheet:
  `https://docs.google.com/spreadsheets/d/1XfML_Rco29MDkUfYrssnC9WVi1eSZK5ivH6upvF-cpE`
* OBDb Toyota Prius test cases: `https://github.com/OBDb/Toyota-Prius/tree/main/tests/test_cases`
* EAA-PHEV wiki: `https://www.eaa-phev.org/wiki/Prius_PHEV_TechInfo`
* [opendbc values] `https://github.com/commaai/opendbc/blob/master/opendbc/car/toyota/values.py`;
  [opendbc uds.py] `https://github.com/commaai/opendbc/blob/master/opendbc/car/uds.py`
* [Miller-Valasek] `https://ioactive.com/pdfs/IOActive_Adventures_in_Automotive_Networks_and_Control_Units.pdf`
* [litedash] `https://raw.githubusercontent.com/IrvingGuerra/litedash/HEAD/feature/obd/src/main/java/com/irvingguerra/litedash/feature/obd/FactoryTires.kt`;
  [tpms-relearn] `https://raw.githubusercontent.com/roosht3/tpms-relearn/HEAD/tpms_relearn/transport.py`
* [hit-karlsruhe] Toyota Corolla E11 K-line diagnostics:
  `http://www.hit-karlsruhe.de/aol2mime/toyota_corolla_e11_diagnose.htm`
* SAE J1979 (service list): `https://saemobilus.sae.org/content/j1979_199709`
* P0420 general references: `https://www.samarins.com/diagnose/p0420.html`,
  `https://repairpal.com/OBD-II-Code-P0420-Toyota`
