# BEAN actuation research: which frames make an NHW20 ECU *do* something

Research date: 2026-11-13. Method: web and repository reading only, nothing transmitted. Inputs: the
existing `docs/BEAN_PROTOCOL.md`, `docs/BEAN_FINDINGS.md` and `docs/BEAN_ANALYSIS.md` (not repeated here);
every public BEAN code base I could find (cloned and read, list in §3); the complete RM1076E section 05
(all 777 published PDFs downloaded and text-searched); the NCF256E body-electrical chapter (BE-1…BE-160);
the SAE 970297 scan; a newly found **EU NHW20 IG-ON capture** (KostyaSha/bean-cracker).

Confidence labels:

| Label | Meaning |
|---|---|
| **[T-same]** | Someone transmitted it on an NHW20 and saw the car act |
| **[T-other]** | Transmitted with a documented effect, but on another Toyota/Lexus |
| **[P]** | Primary Toyota/Denso document says this signal exists on this path (no ID given) |
| **[C]** | Seen in captures (ours, the JP bean_tools dumps or the EU bean-cracker dump), meaning inferred |
| **[I]** | My inference |
| **[S]** | Speculation |

---

## 1. Verdict

**Nobody has published a BEAN frame that was transmitted on an NHW20 and made it actuate anything.**
Nothing in the table below is [T-same]. The public NHW20 work (bean_tools JP, bean-cracker EU, this
project) is all receive-side, apart from `FE:7F` meter-indicator toggles whose effect was never written
down. On other models, the documented transmits with an effect all target the **combination meter**
(gauges, warning lamps, the buzzer bit) from a replacement engine ECU.

The NHW20 architecture explains why. The repair manual shows that on this car **windows, mirrors, wipers,
horn and the door-lock request are not on BEAN**. The power-window master switch drives the motors over
wires (RM 05-1723, 05-1729: no MPX terminal). The mirror switch is wired. The steering-pad A/C switches are
a resistor ladder into the A/C amplifier (05-1037 ST1/ST2). Lock requests use the dedicated serial line
(already settled in `BEAN_FINDINGS.md`). What BEAN does carry *into* an actuating ECU on this car is:

1. **MFD climate commands, gateway → A/C amplifier (0x98).** The NHW20 A/C amplifier has no AVC-LAN pins;
   its only bus terminals are MPX+/MPX2+ (RM 05-1037). The NCF (BE-84, BE-153) and RM 05-1019 put MFD A/C
   operation on the path multi-display → AVC-LAN → gateway → **BEAN** → A/C ECU [P]. The project's AVC-LAN
   climate remote (`110->1C6 … E0 84 …`, `docs/AVC_BACKEND.md`) therefore already causes a BEAN command
   frame from the gateway to 0x98 on every press. Rear defogger plus the EU mirror heaters (A/C-ECU relays,
   NCF BE-151) and every AUTO/A/C/recirc/mode/fan/temperature function go the same way. **The frame ID is
   unknown.** It is event-only, and no published capture includes an MFD button press.
2. **Meter buzzer requests.** The transponder key ECU, the smart ECU and the transmission control ECU each
   send a "buzzer sounding demand" to the meter over BEAN (RM 05-1678, 05-2030) [P]. The meter's buzzer is
   the one actuator that several ECUs command over BEAN by design. The best candidate is `62:6E` (§2, row 2).
3. **Meter indications** that follow BEAN state (door, tail/head/high beam, A/C indicators, master warning).
   The meter acts on what it receives, but these are periodic status frames. A spoofed value is overwritten
   by the real sender within about 1 s.
4. **Body-ECU actions that exist on BEAN only as diagnostic or theft-ECU requests.** Techstream active tests
   on the body ECU are Door Lock, Hazard, Trunk/Bdor Open and Security Indic (05-1787, 05-1959, 05-2214).
   They travel DLC3 SIL (K-line) → gateway → **BEAN** as diagnostic messages (SAE 970297; SAE 1999-01-1278;
   `docs/TOYOTA_DIAGNOSTICS.md` §0) [P]. The theft-warning ECU (European RHD only) sends the body ECU a
   "security signal: door lock and unlock, head lamp, hazard lamp, horn, dome lamp and tail lamp" over BEAN
   (05-2203) [P]. Neither frame format is public. Both can be captured: the first with a K-line tester on
   this car, the second only on an EU RHD car with the theft deterrent.

### Ranked list (usefulness × confidence)

| Rank | What | Path on NHW20 | Frame known? | Confidence it can be driven from BEAN | Recommended route |
|---|---|---|---|---|---|
| 1 | **Climate: AUTO/OFF, temp ±, fan, mode, A/C, recirc, front defrost, rear defogger + mirror heaters** | gateway 0x52 → A/C amp 0x98, event frame | **No.** Capture it while pressing MFD climate keys (§5, O-3) | High that a BEAN command exists [P]. Replay unknown | **Keep using AVC-LAN** (already proven in `climate_remote.py`). Add BEAN only as a fallback with the MFD unplugged |
| 2 | **Meter buzzer / chime** (key reminder pattern) | transponder/smart/P-ECU → meter 0x62 | **Candidate `62:6E` b0 bit 7** [C]/[I] | Medium | Capture the key-reminder buzzer (O-2), then a single PRI-10 transmit (T-2) |
| 3 | **Body ECU: Hazard, Door Lock, Back-door open, Security indicator** | diag tester → K-line → gateway → BEAN → body 0x40 | No. Needs a K-line tester capture (O-6) | High that the BEAN frames exist [P]. Replay may need diagnosis mode | Prefer sending the K-line request from our own interface; BEAN replay is the second option |
| 4 | **Body ECU: head/tail/hazard/horn/dome/lock "security signal"** | theft-warning ECU → body ECU | No. Only an EU RHD car has the sender | Low-medium (an LHD body ECU may ignore it) | Only if an RHD capture turns up |
| 5 | **Meter indicators** (door, tail/head/high beam, A/C AUTO/REC/DEF lamps) | body/A/C → meter, periodic | Yes: `FE:7F`, `FE:FC` bits [C]/[T-other] | High that it lights, but it flickers back within ~1 s | TX self-test only (`FE:7F` door bit at IG, T-1) |
| 6 | **Meter light-reminder chime** | meter computes it from `FE:7F` TAIL/HEAD + driver-door bits with power OFF (05-1678) | Bits partly known | [S] | Receive-only check first |
| — | Windows, mirrors (adjust), wipers, horn (direct), door locks, power-window lock | **not on BEAN on NHW20** | — | None | Hard-wired taps |
| — | Immobiliser, READY, shift/P, power mode, HV warnings, NM frames | — | — | — | **Do not send** (§5.3) |

---

## 2. Candidate command frames

Notation: `DST:MES data`. "Owner resends" means a node already sends this ID periodically, so a spoof
competes with it.

| # | Frame (source's words → our notation) | Effect / receiving ECU | Command or status? Evidence | Source model; NHW20 transferability; seen in our captures? | Confidence | TX risk | Source |
|---|---|---|---|---|---|---|---|
| 1 | *unknown ID*, gateway → `98:??` | MFD climate key press → A/C amplifier acts | **Command** by design: the only path from MFD/AVC-LAN to the A/C amp is BEAN (05-1037: the amp has MPX pins only; NCF BE-84/153) | NHW20 itself. Never captured: absent from JP and EU dumps and ours (no presses). The gateway→98 IDs seen (`98:27`, `98:B2`, `98:CC`, `98:EC`, `98:F8`) are HV-ECU data mirrored on CAN as 0x527/0x5B2/0x5CC/0x5EC/0x5F8, so they are not the button frame [C]/[I] | [P] exists; ID unknown | Low (comfort function) | RM 05-1019/1037; NCF BE-84, BE-153 |
| 2 | `62:6E 80 80` → `80 00` → `00 00` (JP, at power press with door open); `62:6E 00 00` periodic (ours, EU) → `62:6E` byte 0 bit 7 | **Candidate meter buzzer request** (key-reminder pattern: key in slot, power OFF/ACC, driver door open → transponder ECU → meter, 05-2030) | Unproven. DST 0x62 = meter [I]; the only directed frame to the meter in OFF traffic; non-zero exactly in the one JP capture where a key-reminder buzzer is expected [I]. bean_tools saved `62:6E 00 00` in its transmit DB (no result written) | NHW20 JP, EU, ours: present, periodic 1 s, owner resends | [C]/[I] | Low (buzzer only); a spoof is overwritten in ≤ 1 s | `BEAN_PROTOCOL.md` §4.2.1; bean_tools `data/prius.framedb` |
| 3 | `FE:7F` b2 bit 7 (door), b0 0x10 tail / 0x20 head / 0x08 high beam | Meter indicator lamps | **Status the meter displays.** Cross-check: the gateway mirrors it on CAN as `0x57F` (Parkers 0x10, Low 0x30, High 0x38; `docs/prius_can.md`) | NHW20: bean_tools actions "Индикация Габариты/Дальний/Двери" (set/reset/toggle), effect not documented. drive2 (unnamed Toyota, probably UCF30): `25 fe 7f 01 00 00` lit the clearance indicator, PRI/length irrelevant [T-other] | [T-other]; [C] on NHW20 | Low; owner resends every 1 s | bean_tools `prius_20.json`; drive2.ru/b/494654889247376137 |
| 4 | `FE:FC` b0: 0x80 AUTO, 0x20 recirc, 0x10 front DEF, 0x08 rear DEF, 0x02 A/C | Meter A/C indicator lamps (05-1678: "A/C ECU → meter: indicator ON demand") | Status/indication request from the A/C amp; periodic | NHW20 JP/EU [C]; RX300 layout [X]; same bit layout as AVC `E0 5D F3` b1 | [C]/[I] | Low; owner resends | specadmin MPX-codes 0xFC; `AVC_BACKEND.md` |
| 5 | `FE:29` (6 bytes) ≈ CAN 0x529 bytes B…G | Master warning + "1 beep" + EV/READY buzzer to meter/MFD | Gateway relay of HV ECU data. On CAN 0x529 the bits are "1 beep, red triangle", "EV-Mode cancelled 1 beep", "not in Park and driver door open" (`prius_can.md`) [I] mapping | NHW20 [C]; bean_tools saved `FE:29 00 00 84 40 …` for TX (no result) | [I] | **Do not send on the car** (false HV warnings, the power source ECU listens to HV data) | `prius_can.md`; bean_tools framedb |
| 6 | *unknown*, gateway → body 0x40, BEAN diagnostic message (ISO 9141 request packed in BEAN DATA, "sent only in the diagnosis mode") | Active tests: Door Lock LOCK/UNLOCK, Hazard ON/OFF, Trunk/Bdor Open, Security Indic | **Command** [P]. The format is not public | NHW20 path [P]; not in any capture (no tester was connected) | [P] exists | Medium: diag session to the body ECU; door-lock test needs all doors closed | SAE 970297 "Gateway for diagnosis data"; SAE 1999-01-1278; RM 05-1787, 05-2214 |
| 7 | *unknown*, theft-warning ECU → body ECU | Lock/unlock, head lamp, hazard, horn, dome, tail (alarm outputs) | **Command** [P] | EU RHD NHW20 only (the theft deterrent ECU is not fitted on LHD). Not in our captures (LHD car) | [P] exists; LHD body ECU acceptance [S] | Medium (alarm logic) | RM 05-2202/2203 |
| 8 | `40:DA 80` "make a single ding sound" | Ding (receiver at DST 0x40 on the RX300) | Labelled as an action; **no transmit result published** | Lexus RX300/Harrier. `40:DA` never seen on NHW20; on the NHW20, 0x40 is the body ECU | [S] for NHW20 | Unknown ECU behaviour; not recommended | specadmin MPX-codes.md |
| 9 | `D1 E0 A0 A0 A0 A0` "open all power windows in AUTO mode" | Power windows (RX300 door/window ECUs on BEAN) | Command semantics; **no transmit result published** | RX300 only. **Not applicable**: NHW20 windows are not on BEAN (05-1722/1729) | — | — | specadmin MPX-codes.md |
| 10 | `D2 15 xx` climate panel buttons (b7 AUTO, b6 OFF, b5 recirc, b4 FRONT, b3 Rr DEF, b0 any); `46 1A` temp/blower encoder steps | Climate ECU | Real panel → climate-ECU commands on the RX300 (the panel is a BEAN node); no transmit result | RX300. On the NHW20 the "panel" is the MFD via the gateway; the frame format may resemble this [S] | [S] for NHW20 | Low | specadmin MPX-codes.md |
| 11 | `62 D2 08 10 00`: byte 2 bit 3 = "Beep … toggle on/off"; `62 FA FF` door-warning flash (needs `62 D4 28` first); `62 D4 28` BATT/OIL; `62 DF 10 80` door/seat belt; `62 A4 3C` fuel; `62 2C A5` coolant; `62 40 08 10` gear | Combination meter (gauges, lamps, **buzzer**) | Transmitted to a real meter with visible effect (BeanMPX bridge/demo videos); IS300 and MR2 forks drive clusters in daily use [T-other] | Altezza/IS-family (ECT PWR/SNOW, manual-mode gears), IS300, MR2 W30. **NHW20 meter uses different traffic**: `62:D2`, `62:FA`, `62:D4`, `62:DF`, `62:A4` never appear on NHW20 (the meter-bound IDs here are `62:6E`, `62:21`, `62:C8`) | [T-other]; NHW20 [S] | Low on a meter; but do not send IDs the car itself does not use (unknown receivers) | fiztech-code/BeanMPX examples; daniel-dmitrzak/BeanMPX_IS300; threepotMR2/BeanBangerMR2 |
| 12 | `46 D3 20`, `FE D2 30 00 00`, `FE E6 71 B9 00`, `FE E4 05 FF`, `FE DF 20 00 00 80 00 00`, `FE 2C A0` | Bench meter 83800-48010 / instrument panel 84010-48031 | Transmitted on a bench (sample `main.cpp`), effects **not written down** | RX300 bench | [S] | — | specadmin/toyota-mpx samples/MPX-dev-board |

Items that look like commands but are not, on this car:

* `FE:E1` (lock/unlock event) is a report, proven by our replay (`BEAN_FINDINGS.md`). **No source
  contradicts this.** The RX300 door frames `FE:DB…DE`, `96` are documented as states and events too.
* `40:59 00 C0` (ours; `00 80` in the EU dump) is constant. It is addressed to the body ECU but never
  varies, so it is not usable.

---

## 3. Per-source notes

**Primary documents**

* **RM1076E section 05**, all 777 PDFs text-searched
  (`https://toyotamanuals.gitlab.io/PZ471-Z01S0-CA/htmlweb/rm/rm1076e/m_05_NNNN.pdf`).
  * Active-test tables: A/C 05-1044; door lock 05-1787; smart entry 05-1889; wireless door lock 05-1959
    and 05-2006 (the table header says "Transponder key ECU" for Hazard/Trunk/Door Lock, which looks like a
    misprint for the body ECU: 05-1787/05-2214 list the same items under the body ECU); push-button start
    05-2141; theft deterrent 05-2214.
  * The combination-meter (05-1695) and power-window (05-1733) pages have DATA LIST only, **no active test**.
  * Power window system 05-1722…1729: master switch drives the motors directly; the body ECU is shown only
    for key-off operation (BEAN link body ↔ power source ECU).
  * Key reminder 05-2029/2030: transponder ECU → meter buzzer request over BEAN.
  * Meter signal table 05-1678.
  * Theft deterrent communication table 05-2203.
  * A/C system 05-1019/1020/1037: the amplifier has MPX+/MPX2+ and the steering-pad ladder ST1/ST2, no AVC-LAN.
  * No lighting or wiper diagnostic sections exist in this section of the manual.
* **NCF256E body electrical** (`…/ncf/ncf256e/m_be_NNNN.pdf`):
  * BE-34: smart back-door unlock goes "via the body ECU".
  * BE-26: the body ECU receives lock/unlock requests from the smart ECU "via a dedicated serial
    communication line (with local protocol)". This agrees with `BEAN_FINDINGS.md`.
  * BE-63/64: customize items.
  * BE-68: DRL is enabled by the READY signal from the gateway over BEAN.
  * BE-69/71: illuminated entry is body-ECU internal.
  * BE-73/75: the meter has an enclosed buzzer and the BEAN signal list.
  * BE-84: A/C operation path.
  * BE-125: electrical back door. The body ECU gets vehicle speed from the meter over BEAN; activation
    needs < 5 km/h.
  * BE-151/152: mirror heaters are A/C-ECU relays linked to rear defog (EU).
  * BE-153: the steering-pad / MFD / gateway / BEAN / A/C diagram.
* **SAE 970297** (scan in specadmin/toyota-mpx/docs): diagnostic messages are "set on each node beforehand
  and sent only when in the diagnosis mode". The gateway places the ISO 9141 diagnostic message "into the
  DATA area of BEAN … sent via the broadcast method". On the 1997 reference body network, window up/down
  were the highest-priority BEAN signals; the NHW20 does not have them on BEAN. **SAE 1999-01-1278**
  (same scan) shows an ISO 9141 ↔ BEAN frame-conversion figure, but the scan is too degraded to read its
  bytes. Not usable at message level.

**Code and captures**

* **haageduard/bean_tools** (NHW20 JP; https://github.com/haageduard/bean_tools). Dashboard actions toggle
  `FE:7F` bits. `data/prius.framedb` (136 bytes) holds the frames the author saved for transmission:
  `FE:29 00 00 84 40 …`, `FE:24 00`, `FE:83 …`, `62:6E 00 00`, `FE:7F 10 …`, `FE:FC FF 00`, `FE:2C 80`,
  `FE:26 00 00 1D`. **The README documents no effect from any transmit.**
* **KostyaSha/bean-cracker** (https://github.com/KostyaSha/bean-cracker, commit 2025-11-09): a Qt
  monitor/logger/sender/player. **`dumps/prius/20/eu/ig_on_from_off.txt` is a 533-frame EU NHW20 IG-ON
  capture.** Same spec frame as ours (`FE:54 57 42 4C 41 45 4E`), `FE:CD 37` (7 °C), gateway→A/C traffic
  `98:27/B2/CC/EC/F8`, A/C→gateway `52:6D/ED/FD`. No button or command events. Useful as an EU IG-ON
  reference, but contains nothing that actuates.
* **specadmin/toyota-mpx** (RX300/Harrier; https://github.com/specadmin/toyota-mpx). `MPX-codes.md`:
  "recognized experimentally, do not pretend to be complete". It has the only published command-like
  entries (`0x15` panel buttons, `0x1A` encoders, `0xE0` windows, `0xDA` ding), but **no statement that
  any was transmitted with effect**. The dev-board sample sends meter frames on a bench, with no result
  recorded. MPX-viewer is a sniffer only.
* **fiztech-code/BeanMPX** (https://github.com/fiztech-code/BeanMPX). Examples transmit meter frames
  (`62:A4`, `62:2C`, `62:40`, `62:D2` with a "Beep" bit, `62:DF`, `62:D4`, `62:FA`). The bridge example
  splits the meter onto its own bus and forwards between ECU and meter. Videos: youtu.be/FXD5fauDTvY
  ("BeanMPX interface with CAN-BUS"), youtu.be/dopTZ3F5d6s. I could not read the video descriptions or
  identify the car; the ECT PWR/SNOW and manual-mode gear bits suggest an Altezza/IS200 [I].
* **daniel-dmitrzak/BeanMPX_IS300** (Lexus IS300 engine-ECU replacement). Sends `FE:26`, `62:2C`, `FE:40`,
  `40:52 A0 A5` ("dunno"), `FE:CD`, `62:D2`, `FE:D4`, `98:F8 45` ("dunno"), `FE:24`, plus `FE:AB 40 80`,
  `13:D6 36`. These emulate the missing ECM's broadcasts towards the meter and A/C. [T-other] for the meter.
* **threepotMR2/BeanBangerMR2** (MR2 W30, 2026): sends `62:2C` (temp gauge) and `62:D4` (BATT/OIL lamps)
  from an aftermarket ECU; the README reports it working. [T-other].
* **gooxik/BeanMPX-wireless-interface / BeanMPX-analyzer** (2UZ-swap project): a generic Wi-Fi send/receive
  bridge that re-emits frames at 10 Hz "so gauge state sticks". This is independent confirmation that
  periodic status frames must be out-sent against the real owner.
* **aselafernando/BeanMPX-STM32**, **f4ni/BeanMPX_Stm32** (empty), **Beebird-Labs/toyota-bean** (2000 Crown,
  5 V; protocol prose only, with errors), **mck1117/bean-analyzer**: drivers or decoders only, no command
  frames.
* **drive2.ru/b/494654889247376137**: `25 fe 7f 01 00 00` lights the clearance indicator, and priority or
  extra data bytes do not matter. The author planned a full MES-ID scan but published no result.
  rusEFI links it as "bean bus ucf30" (LS430/Celsior) [S].
* **Toymods "Toyota BEANs, Multiplex etc"** p.3 (https://www.toymods.org.au/forums/showthread.php?p=1581242):
  captured frames (`98:CC 02 A8`, `62:D2 00 A0 08`, `D2:26/C8/2C`) for CRC work only; no transmit results.
* **hpacademy "can to mpx"** (Altezza): Link ECU's Altezza plug-in uses a BEAN translator, but the frames
  are withheld as client-confidential. **CANNY forum**: the vendor states CANNY has no BEAN implementation.
* **PriusChat**: no NHW20 BEAN frame dumps found. A 2025 Gen 3 thread ("Body Control CAN bus tap?",
  https://priuschat.com/posts/3562989) finds windows controllable through a CAN gateway at 0x631. That is
  Gen 3 with a CAN body, not applicable here.
* **Japanese searches** (BEAN通信, ボデー多重通信 解析, プリウス BEAN) returned only the SAE abstract and
  Toyota material, no hobbyist frame work. **Russian searches** (BEAN шина, MPX, Prius 20) returned
  the drive2 article above and generic CAN posts. **Polish/German**: nothing BEAN-specific found.

**Cross-check found in the project's own CAN notes.** The gateway mirrors BEAN message IDs onto CAN as
`0x500 | MES`:

| CAN ID | BEAN frame |
|---|---|
| `0x52C` | `FE:2C` coolant |
| `0x53F` | odometer |
| `0x540` | `FE:40` shift |
| `0x553` / `0x554` | `FE:53` / `FE:54` |
| `0x56D` | `52:6D` |
| `0x57F` | `FE:7F` lamps |
| `0x591` | `FE:91` |
| `0x5A4` | `52:A4` fuel |
| `0x5B6` | `FE:B6` doors |
| `0x5C8` | `62:C8` cruise |
| `0x5D4` | `FE:D4` |
| `0x5ED` | `52:ED` |
| `0x527` / `0x5B2` / `0x5CC` / `0x5EC` / `0x5F8` | `98:27/B2/CC/EC/F8` |

(`docs/prius_can.md`). Two consequences:

* Much BEAN state can already be read from CAN.
* The IDs that are *not* mirrored are local to BEAN.

Also, AVC `1C6->110 E0 5D F7 TT` (outside temp, TT − 48) agrees with `FE:CD x − 48`, which supports that
scaling [I].

**Could not open or verify:** the YouTube video contents (BeanMPX, BeanBanger); the ClubLexus SC430
gateway reverse-engineering thread (HTTP 403); the oguchi-rd Gen 2 PID PDF (served HTML, not a PDF);
PriusChat 0x750 posts (already noted as unreachable in `BEAN_PROTOCOL.md`); the readable text of SAE
1999-01-1278's conversion figure.

---

## 4. Factory docs: active tests and the path they use

| ECU (tester name) | Active-test items (RM page) | Path |
|---|---|---|
| Multiplex network body ECU | Door Lock LOCK/UNLOCK (05-1787, 05-2214); Hazard ON/OFF; Trunk/Bdor Open; Security Indic (05-2214, also 05-1959/2006) | Tester on DLC3 **SIL (pin 7, K-line)** → gateway (ISO 9141 ↔ BEAN conversion) → **BEAN** diagnostic message → body ECU drives its hard-wired outputs (lock motors, flasher relay, back-door opener motor) |
| A/C amplifier | Blower 0–31; air-mix damper; outlet-mode damper; inlet damper; compressor target speed; **rear defogger relay**; water pump; electric fan (05-1044) | Same: SIL → gateway → BEAN |
| Smart ECU | LF transmitters D/P/Fr/In-Bdr/Bdoor; select signals (05-1889) | Same. Useless for us |
| Power source control ECU | Key Lock (IG off during test); Lighting Ind; Ind Condition green/red (05-2141) | Same. **Do not touch** (power source) |
| Theft warning ECU (EU RHD) | Security Indic; Security Horn; Security Horn 2 = vehicle horn (05-2214) | Same |
| Combination meter | **none** (DATA LIST only, 05-1695) | — |
| Power window | **none** (05-1733) | — |
| Customize (NCF BE-63) | Lighting time, open-door warning buzzer, wireless answer-back/buzzer, auto-relock time, etc. | Same diagnostic path, writes settings (not an actuation) |

So the BEAN diagnostic path *can* make the body ECU lock/unlock, flash the hazards and open the back door,
and make the A/C amp drive the rear defogger and blower. But:

* The message format is not public, and the SAE paper says these messages are honoured only in
  "diagnosis mode". Expect a session/entry exchange from the gateway first.
* The cleaner route is to send the **K-line request ourselves** (the project lacks K-line hardware,
  `TOYOTA_DIAGNOSTICS.md` §8) and let the gateway do the conversion. Capturing BEAN at the same time
  gives the BEAN form for free.
* Active tests are what the project's diag guard forbids (`30` IO control). Enabling them is the owner's
  decision, test by test.

---

## 5. Proposed test order

### 5.1 Receive-only observations (no TX; bus logger + `bean_mark`; repeat each 3× with 5 s quiet)

| # | State | Action | Looking for |
|---|---|---|---|
| O-1 | IG-ON, MFD on climate screen | Press each MFD climate key once: AUTO, OFF, temp +/−, A/C, recirc, front defrost, **rear defogger**, each mode, each fan step. Log AVC-LAN in parallel | **The gateway→`98:??` event frame** (rank 1). Expect a new DST-0x98 ID that appears only on a press, a few ms after the AVC `110->1C6 E0 84` frame, with a 6-byte bitmap mirroring the AVC one [S] |
| O-2 | IG-ON | Repeat O-1 using the **project's own AVC climate remote** instead of a finger | The same BEAN frame proves our AVC injection already produces it (no BEAN TX needed for climate) |
| O-3 | Power OFF/ACC, mechanical key in slot | Open driver door (key-reminder buzzer); close; remove key | `62:6E` byte 0 bit 7 (rank 2), or whichever directed-to-0x62 frame changes with the buzzer |
| O-4 | READY, then OFF | Leave car with the key outside while READY / door closes (smart "key not in vehicle" buzzer); P-lock buzzer if reproducible safely | Smart-ECU and P-ECU buzzer request frames (more meter-buzzer candidates) |
| O-5 | IG-ON | Tail → head → high → flash; dimmer knob; power OFF with lights on and driver door open (light-reminder chime) | `FE:7F` bit map, dimmer demand, whether a separate chime frame exists |
| O-6 | IG-ON, **K-line tester (Techstream + mini-VCI) on DLC3** | Body ECU active tests: Hazard ON/OFF, Security Indic, Trunk/Bdor Open, Door Lock (all doors closed); A/C: Defogger Rly-R ON/OFF, Blower 5 | **BEAN diagnostic frames** gateway→0x40 / gateway→0x98, and the diag-mode entry sequence |
| O-7 | Car locked, smart key in pocket | Press back-door opener (smart back-door unlock) | Whether anything directed to 0x40 appears (expected: nothing new, because the request uses the dedicated serial line, NCF BE-26) |

### 5.2 Low-risk transmits (after the TX self-test in `BEAN_PROTOCOL.md` §7.2 passed; car parked, IG-ON unless noted, one frame per step, DTC read afterwards)

| # | Frame | Expected | Abort/side effects |
|---|---|---|---|
| T-1 | `FE:7F 00 10 80 00 00 00` at **PRI 10** (CRC 0x68) while all doors are closed | Meter door lamp flashes until the next periodic copy (≤ 1 s) | Use PRI 10: the PRI-2 version has CRC 0xFF, which ends in 5 equal bits and is refused by our firmware |
| T-2 | `62:6E 80 00` at PRI 10 (CRC 0xB2), power OFF/ACC, key out, door closed, bus awake | One buzzer chirp if the hypothesis is right | Only after O-3 confirms the bit. The owner frame resends `00 00` within 1 s |
| T-3 | The gateway→98 climate event captured in O-1, replayed once with the MFD **on** | A/C changes exactly as the MFD key would | Only as a fallback study; AVC-LAN remains the production path |
| T-4 | BEAN diagnostic frames from O-6 (Hazard ON then OFF; Security Indic) | Hazards flash / indicator | Owner's go per test. Never Door Lock while anyone is inside; never while driving |

### 5.3 Do-not-send list

* `B5:D9`, `C1:E2` and every immobiliser/key exchange (B2289; encrypted).
* `FE:62` (power-source state), `FE:91` (READY, B2286), `FE:40` (shift, B2281), `FE:50` (P-lock),
  `FE:24` (speed, B2282), `FE:29` (HV warnings and buzzer), anything brake-related (B2284).
* `FE:C2`, `FE:AB` look-alikes (network management; B12xx).
* Power source ECU active-test equivalents (Key Lock, indicator), anything to 0xE9 or 0x19 (P-position ECU).
* Airbag: no SRS ECU is on BEAN; nothing to send.
* IDs from other models that never appear on this car (`62:D2/FA/D4/DF/A4`, `40:DA`, `D1:E0`, `D2:15`):
  their receivers on the NHW20 are unknown.
* Anything periodic at high rate (the 10 Hz "make it stick" approach of other projects) on a live NHW20:
  it fights the real sender and loads the bus.

---

## 6. Open questions

1. The ID and layout of the gateway→A/C climate command frame (O-1/O-2). Does it carry the AVC-LAN
   6-byte bitmap unchanged?
2. Is `62:6E` b0 bit 7 really the meter buzzer request, and who sends it (transponder 0xC1?)? What do the
   other bits/bytes select (pattern, single chirp)?
3. The BEAN form of the K-line diagnostic messages. Do they need a diagnosis-mode entry, and does the body
   ECU accept them from a node other than the gateway (there is no source field, so probably yes)?
4. Does an EU LHD body ECU act on the theft-ECU "security signal" frames if someone supplies them? An RHD
   capture is needed to know the frame at all.
5. Does the meter generate the light-reminder chime purely from `FE:7F` + door state? (Receive-only test O-5
   first.)
6. Does the gateway mirror *all* BEAN traffic to CAN 0x5xx, or only some IDs? If the climate command (Q1)
   also appears on CAN, the project could read it there.
7. bean_tools `FE:83` is 13 bytes in its framedb (vs 4 on the bus) and `FE:26` carries `00 00 1D`. These
   may be framedb padding artefacts. Not important.
