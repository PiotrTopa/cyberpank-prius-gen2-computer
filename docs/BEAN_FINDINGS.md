# BEAN findings on the owner's car (NHW20, EU LHD, smart entry)

Captures `docs/data/bean/2026-11-13_session30_doors_locks.ndjson.gz` (16:29–16:37 CET, 4,934 frames,
0 errors; handle/smart-touch operations) and `docs/data/bean/2026-11-13_remote_test.ndjson.gz`
(key-fob unlock/lock), matched against the owner's timed action list. Confidence: each item was seen at every
occurrence of the action unless noted. BEAN frames carry no source address, so senders are inferred.

| Message | Field | Meaning | Evidence |
|---|---|---|---|
| `52:3F` | 3 bytes | odometer, km (0x0493DF = 299,999) | owner confirmed exact |
| `FE:B6` byte 0 | 0x01 | driver door unlocked | set first at every unlock, cleared at lock |
| `FE:B6` byte 0 | 0x40, 0x80 | other doors unlocked (two groups) | C1 after unlock; C0 → 40 → 00 while locking |
| `FE:B6` byte 1 | 0x80 | driver door open | 31:21–31:26, 32:01–32:11 |
| `FE:B6` byte 1 | 0x40 | passenger door open | 34:01–34:11 |
| `FE:B6` byte 1 | 0x04 | rear door or back door open (one bit for all three) | 35:40–35:52, 36:01–36:16, 36:31–36:41 |
| `FE:7F` byte 2 | 0x80 | any door open (meter indicator) | every door event |
| `FE:E1` byte 2 | **0x04 / 0x08** | **remote (fob) unlock / lock**, one or two frames at priority 10, then back to 000000 | `remote_test` capture: 000004 at key-fob unlock, 000008 at key-fob lock |
| `FE:E1` byte 2 | **0x10 / 0x20** | **handle / smart-touch unlock / lock**, one or two frames at priority 10, then back to 000000 | session30: 000010 at handle-touch unlock, 000020 at handle-touch lock; NOT at the interior lock switch |
| `FE:E1` byte 2 | 0x20 | **30 s auto re-lock** (after a handle unlock with no door opened) uses the lock code | session30: unlock 000010 @ t_ms 4482887 → lock 000020 @ 4512862, Δ≈30.0 s |
| `FE:83` byte 3 | 0x08 | interior (dome) lamp on timer, ~15–25 s after unlock/door | every wake with unlock or door |
| `FE:83` byte 3 | 0x04 | back door open / luggage lamp | 36:01 only (single occurrence) |
| `FE:D8` byte 1 | 0x01 | driver-door-opened timer (~35 s) | driver door only, not other doors |
| `B5:D9`, `C1:E2` | 2–3 bytes | key authentication exchange (values differ every time) | only when the driver door opens |
| `FE:C2` | node + status | wake announcements; the first one after sleep is not ACKed (rsp 0) | every wake |

Not on the bus: the map lamps switched by hand (bus stayed asleep).

`FE:E1` is the lock/unlock candidate for transmission: it appears whenever the lock action does not
come from the body ECU's own interior switch, which fits a request sent to the body ECU (or an
answer-back/hazard-flash request tied to it). Which of the two it is can only be settled by sending it.
The method **is** distinguishable on the bus: remote/fob uses 0x04 (unlock) / 0x08 (lock); handle or
smart-touch uses 0x10 / 0x20; the 30 s auto re-lock reuses the lock code 0x20. (Corrected 2026-11-13
against `docs/data/bean/2026-11-13_remote_test.ndjson.gz`; an earlier note here wrongly called the
methods indistinguishable, having seen only the handle-touch capture.)

## TX replay result — FE:E1 is a report, not a command (2026-11-13, ~18:20 CET, car parked)

Settled by transmitting. We injected the exact recorded remote-unlock frame `FE:E1 pri=10 data=000004`
on the car's bus, both as a cold frame (our SOF woke the bus) and as a two-stage test (wake first, 2 s
settle so the body ECU 0x40 was fully awake, then inject). Both times the frame was **ACKed** (a node
received it with valid CRC) but:

- `FE:B6` stayed `0000` — **the doors did not unlock**;
- the body ECU's own `FE:E1` stayed `000000` (it never echoed or acted);
- **no 30 s auto re-lock followed** (the behaviour the owner's replay test was designed to detect).

So `FE:E1` is a **status broadcast the body ECU emits *after* it acts**, not a command it listens to.
The `FE:E1`→`FE:B6` 8 ms ordering seen in recordings is report-then-report.

**There is no door lock/unlock command on this BEAN bus at all.** Across both captures the only directed
(non-`FE`) frames are `40:59 00C0` (body-ECU heartbeat, *never varies*), `52:3F` (odometer), `62:6E`
(meter), and `B5:D9`/`C1:E2` (rolling smart-key crypto challenge/response = key authentication). The
body ECU (0x40) owns the lock motors and decides to actuate from its *direct* inputs — key RF/LF and the
interior lock switch (which is wired to the ECU, not on the bus) — then only reports the result. A fob
press wakes the bus with the `FE:C2` announcement cascade; the actual unlock command never travels on
BEAN. **Conclusion: door lock/unlock cannot be achieved by BEAN injection on this car.** Reading lock
state and door-open state from the bus works perfectly; actuation would need either body-ECU message
fuzzing (`40:xx`, risky on a safety ECU) or a physical relay tap on the lock-actuator / lock-switch
wiring (the standard aftermarket approach).

### Corroborated by the factory manual (research lane, 2026-11-13)

The official 2004 NHW20 repair manual (RM1075U) states the architecture explicitly and settles it:

- Smart-entry "Function of Main Components", Multiplex Network Body ECU: it *transmits* body signals
  (courtesy switch, door key lock/unlock switch, door lock position switch, …) to the smart ECU **via
  BEAN**, but *receives* the **door lock/unlock request signal from the smart ECU via a DEDICATED SERIAL
  COMMUNICATION LINE with a local protocol** (body ECU ↔ smart ECU ↔ wireless door-lock receiver). So
  BEAN carries body-ECU→smart-ECU **status only**; the lock/unlock **command** rides a separate
  proprietary serial line, not BEAN.
- The body ECU then **drives the lock motors over hardwired outputs** ("outputs a door lock/unlock
  control signal to each door lock assy"); the 30 s auto re-lock and repeat-lock are body-ECU-internal
  logic — matching our `FE:E1 0x20` auto re-lock being a report, not a command.
- Body-ECU terminals page: the dedicated link is pin **RDA (B7-23) "Door control receiver input"**
  (~6–7 V idle); the lock-motor drive outputs live in the separate "Power Door Lock Control System"
  section, not the multiplex section.

Independent hobbyist corroboration: the only public Toyota BEAN code table (`specadmin/toyota-mpx`,
`MPX-codes.md`) documents door frames `FE:DB/DC/DD/DE/96` etc. — **all status/event reports, no command
packet**. Toyota diagnostic lock control *does* exist but over the **diagnostic channel** (K-line/CAN,
Techstream actuator test), reachable at CAN id 0x750 on *later* (2010+, CAN-body) Prii — not on the
NHW20's BEAN body bus.

**Routes to actual lock control, ranked:** (1) relay/driver on the body ECU's **hardwired lock-motor
outputs** — "Power Door Lock Control System" section of the EWD — bypasses the bus and crypto, highest
confidence; (2) reverse-engineer and inject on the **dedicated serial line** at body-ECU pin RDA (B7-23)
— carries the real request but is an undocumented proprietary protocol, high effort; (3) body-ECU
**diagnostic actuator test** over K-line (NHW20 is pre-CAN-body), not over BEAN.

## First IG-ON drive capture (2026-11-13, 21:47–22:07 CET)

Capture: `docs/data/bean/2026-11-13_drive_ig_on.ndjson.gz` (59 648 frames, 45
IDs, marks for the scripted climate run). The owner logged events by clock;
frames are matched to them within a few seconds. "Confirmed" below means seen
in this capture at the logged moment; nothing here was transmitted by us.

### Climate (scripted through the AVC-LAN remote, 21:58:00–21:59:20)

Every AVC climate command makes the gateway put a momentary button frame on
BEAN (`FE:FB`, one bit, back to zero within ~0.3 s); the status frame `FE:FC`
follows.

`FE:FB` (6 bytes), button bits:

| Action | Byte | Bit |
|---|---|---|
| temperature up | 0 | 80 |
| AUTO | 0 | 08 |
| recirculation toggle | 0 | 02 |
| A/C toggle | 1 | 20 |
| direction face / face+foot / foot / windscreen+foot | 1 | 08 / 04 / 02 / 01 |
| fan 1 / 2 / 3 / 4 / 7 | 2 | 80 / 40 / 20 / 10 / 08 |
| fan 6 (single observation, odd position) | 4 | 04 |

Not seen: temperature down (the 20 -> 18 step produced no `FE:FB`), fan 5, and
"auto off" (no frame at all).

`FE:FC` status: byte 0 `80` AUTO, `40` fresh air (clear = recirculation), `02`
A/C on, `01` face; byte 1 `80` face+foot, `40` foot, `20` windscreen+foot;
byte 2 `04` pulses on each change; byte 5 top three bits = fan level 1..7.
`52:FD` byte 0 = target temperature, +1 per 0.5 °C (`05` = 18.0).

`FE:FB` is the candidate for direct climate control over BEAN (untested).

### Lamps (`FE:7F`), 22:00:10–22:01:01 and 22:07:00

Byte 0: `10` tail/parking lamps, `20` low beam (both confirmed by the switch
sequence off -> parking -> low beam), `01` fog lamp (on 22:00:41, off
22:01:01; only one fog bit seen for the two fog steps logged). Byte 1 `08`
follows the tail lamps (illumination). Byte 2 `80` = a door open (as before).

### Shift, parking, power

* `FE:40` byte 0: `80` P, `10` D, `40` R, `00` in between. Confirmed P -> D
  (22:02:24), D <-> R repeatedly while reversing (22:05–22:06), -> P (22:06:04).
* `FE:50` `80` = parking lock engaged; `FE:62` byte 0 `20` = in P, `10` =
  power on, `08` = power switch pressed (22:06:41). All three confirmed.
* `FE:29` byte 2 bit `08` toggles just before every shift change: brake pedal
  (inferred). Bit `80` clears at power-off; bit `01` unknown.
* `FE:83` byte 0 went `60` -> `40` -> `00` at power-off: the two bits are
  IG and ACC, which is which is not settled by this capture.

### Key-out warning (`62:6E`), 22:01:30–22:02:02

Driver left the READY car with the key: door open 22:01:30.9, closed
22:01:34.0, then `62:6E` `0000` -> `8080` (22:01:34.5) -> `8000` (22:01:35.7)
-> `0000` (22:02:01.8, key back inside). Byte 0 `80` = "key not in vehicle"
warning active, byte 1 `80` = the ~1.2 s beep. This confirms `62:6E` as a
warning/buzzer demand to the meter (see BEAN_ACTUATION_RESEARCH.md).

### Not found on BEAN

* Wipers (single wipe, low, high, off at 21:59:30–53): no frame changed.
* Seat belt (unbuckled 22:01:10, buckled 22:02:15): no frame tied to it.
* Light-reminder chime (door opened with the lamps on, 22:06:50): no separate
  frame; only door-open and the lamp bits themselves.

### Lock on leaving

22:07:18 `FE:E1 000020` (smart lock), `FE:B6` `C1` -> `C0` -> `00`. The doors
stayed unlocked (`C1`) for the whole drive.
